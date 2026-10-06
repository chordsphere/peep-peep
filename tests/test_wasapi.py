"""Our WASAPI capture (session A.1), everything but the COM calls themselves:
formats, the continuous timeline (silence gaps, overlaps, drift, the ring),
anchors, the socket sender's no-deadlock contract, the server over real
sockets, and CaptureProcess across a real process boundary to
tests/fake_wasapi.py. No Win32, no audio devices, no ffmpeg.exe."""

import io
import json
import socket
import struct
import sys
import threading
import time
import unittest
import wave

from tests import LOOPBACK_SKIP, REPO, loopback_tcp_available
from tests.fake_wasapi import (FMT, TONE, FakeCaptureProcess, SyntheticCapture, _LineSink, _NoCom, _Pipe, connect,
                               unix_listener)
from peep import wasapi as wa

RATE, BA = 48000, 8


def pkt(frames, value=0.25):
    return struct.pack("<ff", value, value) * frames


class FormatTest(unittest.TestCase):
    def test_laptop_mix_format(self):
        # the probe: both output devices and the mic array report this (WAVE_FORMAT_EXTENSIBLE float)
        f = wa.format_from_waveformat(0xFFFE, 2, 48000, 32, 8, "{00000003-0000-0010-8000-00AA00389B71}", 32, 3)
        self.assertEqual((f.ffmpeg_format, f.block_align, f.bytes_per_second), ("f32le", 8, 384000))
        self.assertEqual(f.describe(), "48000 Hz, 2 ch, 32-bit float (f32le)")
        self.assertEqual(wa.PcmFormat.from_dict(f.to_dict()), f)

    def test_integer_formats(self):
        self.assertEqual(wa.format_from_waveformat(1, 2, 44100, 16, 4).ffmpeg_format, "s16le")
        self.assertEqual(wa.format_from_waveformat(1, 2, 48000, 24, 6).ffmpeg_format, "s24le")
        f = wa.format_from_waveformat(0xFFFE, 8, 48000, 32, 32, wa.SUBTYPE_PCM, 24, 0x63F)
        self.assertEqual((f.ffmpeg_format, f.valid_bits), ("s32le", 24))

    def test_unsupported_formats_say_why(self):
        with self.assertRaisesRegex(wa.UnsupportedFormat, "sub-format"):
            wa.format_from_waveformat(0xFFFE, 2, 48000, 32, 8, "{00000092-0000-0010-8000-00aa00389b71}")
        with self.assertRaisesRegex(wa.UnsupportedFormat, "0x0055"):
            wa.format_from_waveformat(0x55, 2, 48000, 16, 4)
        with self.assertRaisesRegex(wa.UnsupportedFormat, "nBlockAlign"):
            wa.format_from_waveformat(3, 2, 48000, 32, 4)
        with self.assertRaisesRegex(wa.UnsupportedFormat, "8-bit"):
            wa.format_from_waveformat(1, 1, 48000, 8, 1)


class TimelineTest(unittest.TestCase):
    def tl(self, **kw):
        return wa.Timeline(FMT, 100.0, **kw)

    def test_contiguous_packets_append_untouched(self):
        tl = self.tl()
        for n in range(5):
            tl.add_packet(480, pkt(480), 100.0 + n * 0.01)
        self.assertEqual(tl.total_frames, 2400)
        self.assertEqual((tl.stats.gap_fills, tl.stats.overlap_drops), (0, 0))
        self.assertEqual(tl.read(0, 2400), pkt(2400))

    def test_silence_gap_is_filled_to_the_packet_stamp(self):
        # The classic loopback defect: nothing arrives while nothing plays. The packet after the
        # gap must land at its own stamp, with silence before it.
        tl = self.tl()
        tl.add_packet(480, pkt(480), 100.0)
        tl.add_packet(480, pkt(480), 100.5)
        self.assertEqual(tl.stats.gap_fills, 1)
        self.assertEqual(tl.total_frames, int(0.51 * RATE))
        data = tl.read(0, tl.total_frames)
        self.assertEqual(data[480 * BA:24000 * BA], bytes((24000 - 480) * BA))
        self.assertEqual(data[24000 * BA:24480 * BA], pkt(480))

    def test_idle_fill_keeps_the_stream_wall_clock_long(self):
        tl = self.tl(idle_lag_s=0.12)
        self.assertEqual(tl.fill_idle(100.5), int(round(0.38 * RATE)))
        self.assertEqual(tl.fill_idle(100.5), 0)              # nothing new until time moves on
        tl.fill_idle(103.0)
        self.assertEqual(tl.total_frames, int(round(2.88 * RATE)))
        self.assertEqual(tl.stats.idle_fill_frames, tl.total_frames)

    def test_idle_fill_does_nothing_while_audio_flows(self):
        tl = self.tl()
        for n in range(50):
            tl.add_packet(480, pkt(480), 100.0 + n * 0.01)
            self.assertEqual(tl.fill_idle(100.0 + n * 0.01 + 0.03), 0)

    def test_packet_after_idle_padding_is_realigned_not_lost(self):
        tl = self.tl(idle_lag_s=0.12)
        tl.fill_idle(101.0)                                    # padded to 100.88
        tl.add_packet(480, pkt(480), 100.95)                   # sound starts: 70 ms after the padding
        self.assertEqual(tl.total_frames, tl.frame_at(100.96))
        self.assertEqual(tl.read(tl.frame_at(100.95), 480), pkt(480))

    def test_early_stamp_drops_only_the_overlap(self):
        tl = self.tl()
        tl.add_packet(4800, pkt(4800), 100.0)                  # covers to 100.1
        tl.add_packet(4800, pkt(4800, 0.5), 100.05)            # 50 ms overlap
        self.assertEqual(tl.stats.overlap_drops, 1)
        self.assertEqual(tl.stats.dropped_frames, 2400)
        self.assertEqual(tl.total_frames, tl.frame_at(100.15))
        self.assertEqual(tl.read(4800, 1), pkt(1, 0.5))

    def test_jitter_inside_the_tolerance_is_left_alone(self):
        tl = self.tl(tolerance_s=0.02)
        tl.add_packet(480, pkt(480), 100.0)
        tl.add_packet(480, pkt(480), 100.015)                  # 5 ms late: jitter
        tl.add_packet(480, pkt(480), 100.012)                  # 8 ms early: jitter
        self.assertEqual((tl.stats.gap_fills, tl.stats.overlap_drops, tl.total_frames), (0, 0, 1440))
        self.assertAlmostEqual(tl.stats.max_abs_drift_ms, 8.0, places=3)

    def test_slow_card_clock_drift_is_corrected_in_bounded_steps(self):
        # a sound card running 0.1% slow against QPC: 60 s of 10 ms packets stamped late by 0.1%
        tl = self.tl(tolerance_s=0.02)
        for n in range(6000):
            tl.add_packet(480, pkt(480), 100.0 + n * 0.01 * 1.001)
        stamped_end = 100.0 + 6000 * 0.01 * 1.001
        self.assertLess(abs(tl.time_of(tl.total_frames) - stamped_end), 0.021)
        self.assertGreaterEqual(tl.stats.gap_fills, 2)
        self.assertLessEqual(tl.stats.gap_fill_frames / max(1, tl.stats.gap_fills), 0.021 * RATE)

    def test_silent_flag_and_timestamp_error(self):
        tl = self.tl()
        tl.add_packet(480, pkt(480), 100.0, wa.AUDCLNT_BUFFERFLAGS_SILENT)
        self.assertEqual(tl.read(0, 480), bytes(480 * BA))
        tl.add_packet(480, pkt(480), 999.0, wa.AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR)   # stamp ignored
        self.assertEqual((tl.total_frames, tl.stats.silent_packets, tl.stats.timestamp_errors), (960, 1, 1))
        tl.add_packet(480, None, None, wa.AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY)
        self.assertEqual((tl.total_frames, tl.stats.discontinuities), (1440, 1))

    def test_reads_before_the_epoch_and_after_eviction_are_silence(self):
        tl = self.tl(ring_s=1.0)
        for n in range(300):                                    # 3 s of audio, ring keeps ~1 s
            tl.add_packet(480, pkt(480), 100.0 + n * 0.01)
        self.assertEqual(tl.read(0, 480), bytes(480 * BA))      # evicted
        self.assertEqual(tl.read(-960, 480), bytes(480 * BA))   # before the epoch
        self.assertEqual(tl.read(tl.total_frames - 480, 480), pkt(480))
        mixed = tl.read(-240, 480)
        self.assertEqual(len(mixed), 480 * BA)

    def test_read_waits_for_data_then_returns_empty(self):
        tl = self.tl()
        t = time.monotonic()
        self.assertEqual(tl.read(0, 480, timeout_s=0.05), b"")
        self.assertGreaterEqual(time.monotonic() - t, 0.04)
        threading.Timer(0.05, lambda: tl.add_packet(480, pkt(480), 100.0)).start()
        self.assertEqual(tl.read(0, 480, timeout_s=2.0), pkt(480))

    def test_stubbed_loopback_bursts_become_one_continuous_stream(self):
        """End to end with the synthetic capture client: audio only in two bursts,
        nothing between; the timeline is as long as the wall time and each burst
        sits at its own time."""
        cap = SyntheticCapture(bursts=((0.1, 0.3), (0.6, 0.7)))
        cap.start()
        tl = wa.Timeline(cap.format, cap.t0)
        end = cap.t0 + 1.0
        while wa.qpc_now() < end:
            for frames, data, qpc_s, flags in cap.packets():
                tl.add_packet(frames, data, qpc_s, flags)
            tl.fill_idle(wa.qpc_now())
            time.sleep(0.005)
        self.assertAlmostEqual(tl.total_frames / RATE, 1.0 - tl.idle_lag_s, delta=0.05)
        data = tl.read(0, tl.total_frames)

        def frame(t):
            i = int(t * RATE) * BA
            return data[i:i + BA]
        self.assertEqual(frame(0.05), bytes(BA))
        self.assertEqual(frame(0.2), TONE[:BA])
        self.assertEqual(frame(0.45), bytes(BA))
        self.assertEqual(frame(0.65), TONE[:BA])


class AnchorTest(unittest.TestCase):
    def test_choose_start_frame(self):
        tl = wa.Timeline(FMT, 100.0)
        self.assertEqual(wa.choose_start_frame(tl, 100.5, 101.0, 0.43), (24000, "anchor"))
        self.assertEqual(wa.choose_start_frame(tl, None, 101.0, 0.43), (int(round(0.57 * RATE)), "fallback"))

    def test_anchor_box_newest_wins_once(self):
        box = wa.AnchorBox()
        box.put(1.0)
        box.put(2.0)
        self.assertEqual(box.take(0), 2.0)
        self.assertIsNone(box.take(0.01))
        threading.Timer(0.05, box.put, args=(3.0,)).start()
        self.assertEqual(box.take(2.0), 3.0)


class SendStreamTest(unittest.TestCase):
    """The no-deadlock contract: the sender never blocks past a stop, whatever
    the reader does."""

    def setUp(self):
        self.tl = wa.Timeline(FMT, 0.0)
        self.tl.add_packet(48000 * 5, pkt(48000 * 5), 0.0)       # 5 s ready to send

    def test_stop_returns_even_when_the_reader_never_reads(self):
        a, b = socket.socketpair()
        a.settimeout(0.05)
        stop, stall = threading.Event(), threading.Event()
        out = {}
        t = threading.Thread(target=lambda: out.update(r=wa.send_stream(a, self.tl, 0, stop, stall, 960)))
        t.start()
        time.sleep(0.3)                                          # socket buffers fill; send would block
        stop.set()
        t.join(2.0)
        self.assertFalse(t.is_alive())
        sent, why = out["r"]
        self.assertEqual(why, "stopped")
        self.assertGreater(sent, 0)
        a.close()
        b.close()

    def test_peer_closing_ends_the_stream(self):
        a, b = socket.socketpair()
        a.settimeout(0.05)
        b.close()
        sent, why = wa.send_stream(a, self.tl, 0, threading.Event(), threading.Event(), 960)
        self.assertIn("peer closed", why)
        a.close()

    def test_frames_arrive_in_order_and_whole(self):
        a, b = socket.socketpair()
        a.settimeout(0.25)
        stop = threading.Event()
        threading.Thread(target=wa.send_stream, args=(a, self.tl, 1000, stop, threading.Event(), 333),
                         daemon=True).start()
        got = b""
        while len(got) < 48000 * BA:
            got += b.recv(65536)
        stop.set()
        self.assertEqual(got[:48000 * BA], self.tl.read(1000, 48000))
        a.close()
        b.close()

    def test_stall_then_stop(self):
        a, b = socket.socketpair()
        a.settimeout(0.05)
        stop, stall = threading.Event(), threading.Event()
        stall.set()
        t = threading.Thread(target=wa.send_stream, args=(a, self.tl, 0, stop, stall, 960))
        t.start()
        time.sleep(0.1)
        stop.set()
        t.join(1.0)
        self.assertFalse(t.is_alive())
        a.close()
        b.close()


class ServerTest(unittest.TestCase):
    """CaptureServer over real sockets with the synthetic capture client."""

    def start(self, fail=None, bursts=((0.0, 1e9),), fallback_lead_s=0.0, listen=unix_listener):
        self.out, self.err, self.stdin = [], [], _Pipe()
        em = wa.Emitter(_LineSink(lambda l: self.out.append(json.loads(l))), _LineSink(self.err.append))
        self.srv = wa.CaptureServer("system", "", fallback_lead_s=fallback_lead_s, emitter=em, stdin=self.stdin,
                                    capture_factory=lambda so, se: (_NoCom(), SyntheticCapture(so, se, bursts, fail)),
                                    listen=listen)
        self.rc = {}
        self.thread = threading.Thread(target=lambda: self.rc.update(rc=self.srv.run()), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.out:
            time.sleep(0.01)
        return self.out[0]

    def events(self, name):
        return [e for e in self.out if e["event"] == name]

    def stop(self):
        self.stdin.put("stop\n")
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def read(self, url, seconds):
        s = connect(url)
        got, end = b"", time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                got += s.recv(65536)
            except socket.timeout:
                pass
        s.close()
        return got

    def test_ready_line_then_anchor_honoured(self):
        ready = self.start()
        self.assertEqual((ready["event"], ready["format"]["ffmpeg_format"], ready["source"]), ("ready", "f32le",
                                                                                                "system"))
        anchor = wa.qpc_now() - 0.2
        self.stdin.put(f"anchor {anchor:.7f}\n")
        time.sleep(0.05)
        got = self.read(ready["url"], 0.5)
        time.sleep(0.2)
        self.stop()
        conn = self.events("connected")[0]
        self.assertEqual(conn["start_source"], "anchor")
        self.assertAlmostEqual(conn["first_sample_qpc"], anchor, delta=1 / RATE)
        self.assertEqual(len(got) % BA, 0)
        self.assertGreater(len(got) / BA / RATE, 0.5)            # the 0.2 s before connecting came too
        self.assertEqual(self.rc["rc"], 0)
        stats = self.events("stats")[0]
        self.assertEqual(stats["connections"], 1)

    def test_no_anchor_falls_back_to_connect_minus_lead(self):
        ready = self.start(fallback_lead_s=0.43)
        t0 = wa.qpc_now()
        self.read(ready["url"], 1.3)                            # the server waits ~1 s for an anchor first
        self.stop()
        conn = self.events("connected")[0]
        self.assertEqual(conn["start_source"], "fallback")
        self.assertAlmostEqual(conn["first_sample_qpc"], conn["accept_qpc"] - 0.43, delta=0.001)
        self.assertGreater(conn["accept_qpc"], t0 - 0.1)

    def test_a_second_connection_is_served_after_the_first_closes(self):
        # the recorder's pipeline fallback: ffmpeg attempt 1 dies, attempt 2 connects to the same child
        ready = self.start()
        for _ in range(2):
            self.stdin.put(f"anchor {wa.qpc_now():.7f}\n")
            time.sleep(0.02)
            self.assertGreater(len(self.read(ready["url"], 0.3)), 0)
            time.sleep(0.3)
        self.stop()
        self.assertEqual([c["n"] for c in self.events("connected")], [1, 2])
        self.assertTrue(all("peer closed" in d["reason"] for d in self.events("disconnected")))

    def test_stdin_eof_stops_the_server_even_mid_stream(self):
        ready = self.start()
        s = connect(ready["url"])                                  # connected, never reads
        time.sleep(1.3)
        self.stdin.close()                                        # the recorder died: EOF
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        s.close()
        self.assertEqual(self.events("disconnected")[0]["reason"], "stopped")

    @unittest.skipUnless(loopback_tcp_available(), LOOPBACK_SKIP)
    def test_production_tcp_listener(self):
        # what ffmpeg.exe connects to on the laptop: tcp://127.0.0.1:<ephemeral>
        ready = self.start(listen=None)
        self.assertEqual(ready["url"], f"tcp://127.0.0.1:{ready['port']}")
        self.stdin.put(f"anchor {wa.qpc_now():.7f}\n")
        self.assertGreater(len(self.read(ready["url"], 0.4)), 0)
        self.stop()
        self.assertEqual(self.events("connected")[0]["start_source"], "anchor")

    def test_a_listener_that_cannot_open_is_an_error_not_a_dead_thread(self):
        def broken():
            raise OSError(101, "Network is unreachable")
        out, stdin = [], _Pipe()
        srv = wa.CaptureServer("system", "", emitter=wa.Emitter(_LineSink(lambda l: out.append(json.loads(l))),
                                                                 _LineSink(lambda l: None)),
                               stdin=stdin, listen=broken,
                               capture_factory=lambda so, se: (_NoCom(), SyntheticCapture(so, se)))
        self.assertEqual(srv.run(), 4)
        self.assertIn("cannot listen for ffmpeg", out[-1]["error"])

    def test_capture_error_is_an_error_event_and_exit_3(self):
        self.start(fail="open")
        self.thread.join(5)
        self.assertEqual(self.rc["rc"], 3)
        self.assertIn("0x88890004", self.events("error")[0]["error"])
        self.assertTrue(any("wasapi.capture_failed" in l for l in self.err))


class CaptureProcessTest(unittest.TestCase):
    """CaptureProcess across a real process boundary (tests/fake_wasapi.py)."""

    def proc(self, mode="ok"):
        def argv(python, source, selector, lead):
            return [sys.executable, str(REPO / "tests" / "fake_wasapi.py"), "serve", "--source", source,
                    "--fallback-lead-ms", str(lead), "--mode", mode, "--unix"]
        return wa.CaptureProcess(argv_factory=argv)

    def test_start_anchor_stream_stop(self):
        p = self.proc()
        ready = p.start("system", "", ready_timeout_s=10)
        self.assertEqual((ready.format.ffmpeg_format, ready.format.rate), ("f32le", 48000))
        self.assertTrue(p.send_anchor(wa.qpc_now() - 0.1))
        self.assertTrue(ready.stream_url.startswith("unix:@"))
        s = connect(ready.stream_url)
        got, end = b"", time.monotonic() + 0.5
        s.settimeout(0.1)
        while time.monotonic() < end:
            try:
                got += s.recv(65536)
            except socket.timeout:
                pass
        s.close()
        time.sleep(0.3)
        self.assertEqual(p.stop(5), 0)
        self.assertGreater(len(got), 0)
        self.assertEqual(p.last("connected")["start_source"], "anchor")
        self.assertIsNotNone(p.last("stats"))
        self.assertIn("serve", p.argv)

    def test_device_that_cannot_open_is_a_start_error_with_the_reason(self):
        p = self.proc("open-fails")
        with self.assertRaisesRegex(wa.CaptureStartError, "0x88890004"):
            p.start("mic", "", ready_timeout_s=10)
        self.assertEqual(p.exit_code, 3)

    def test_a_child_that_never_answers_is_killed(self):
        p = self.proc("hang")
        t = time.monotonic()
        with self.assertRaisesRegex(wa.CaptureStartError, "no answer"):
            p.start("system", "", ready_timeout_s=0.5)
        self.assertLess(time.monotonic() - t, 8)
        self.assertIsNotNone(p.poll())                            # killed, not orphaned

    def test_a_crashed_child_reports_exit_code_and_stderr(self):
        p = self.proc("crash-after-ready")
        p.start("system", "", ready_timeout_s=10)
        self.assertEqual(p.stop(3), 7)
        self.assertIn("ERROR fake crash", p.stderr_tail())

    def test_server_argv(self):
        self.assertEqual(wa.server_argv("python.exe", "mic", "Headset", 430),
                         ["python.exe", "-X", "utf8", "-m", "peep.wasapi", "serve", "--source", "mic",
                          "--fallback-lead-ms", "430", "--device", "Headset"])


class FakeProcessContractTest(unittest.TestCase):
    """The in-process fake the recorder tests use behaves like the real thing."""

    def test_fake_matches_the_contract(self):
        p = FakeCaptureProcess()
        ready = p.start("system")
        self.assertIsNotNone(p.ready_info)
        self.assertEqual(ready.format, FMT)
        self.assertIsNone(p.poll())
        self.assertEqual(p.stop(), 0)
        self.assertEqual(p.poll(), 0)


class EndpointAndToneTest(unittest.TestCase):
    EPS = [wa.Endpoint("{a}", "FxSound Speakers (FxSound Audio Enhancer)", "render", "active", True),
           wa.Endpoint("{b}", "Speakers (Realtek XU)", "render", "active", False)]

    def test_pick_endpoint(self):
        self.assertEqual(wa.pick_endpoint(self.EPS, "{b}").name, "Speakers (Realtek XU)")
        self.assertEqual(wa.pick_endpoint(self.EPS, "speakers (realtek xu)").id, "{b}")
        self.assertEqual(wa.pick_endpoint(self.EPS, "FxSound").id, "{a}")
        with self.assertRaisesRegex(LookupError, "several"):
            wa.pick_endpoint(self.EPS, "Speakers")
        with self.assertRaisesRegex(LookupError, "active: 'FxSound"):
            wa.pick_endpoint(self.EPS, "HDMI")

    def test_tone_wav_is_a_real_wav(self):
        data = wa.tone_wav(1000, 120, amplitude=0.4)
        with wave.open(io.BytesIO(data)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()),
                             (1, 2, 48000, 5760))
            frames = w.readframes(w.getnframes())
        peak = wa.peak_level(frames, wa.PcmFormat(48000, 1, 16, False))
        self.assertAlmostEqual(peak, 0.4, delta=0.01)
        self.assertEqual(struct.unpack("<h", frames[:2])[0], 0)          # ramped: no click

    def test_peak_level_formats(self):
        self.assertAlmostEqual(wa.peak_level(pkt(10, -0.75), FMT), 0.75)
        self.assertEqual(wa.peak_level(b"", FMT), 0.0)
        s24 = (-4194304).to_bytes(3, "little", signed=True) * 2
        self.assertAlmostEqual(wa.peak_level(s24, wa.PcmFormat(48000, 2, 24, False)), 0.5)

    def test_qpc_now_is_monotonic_here(self):
        a = wa.qpc_now()
        self.assertGreaterEqual(wa.qpc_now(), a)
        self.assertIn(wa.qpc_source(), ("QueryPerformanceCounter", "time.perf_counter"))

    def test_wasapi_refuses_off_windows(self):
        if sys.platform == "win32":
            self.skipTest("Windows")
        with self.assertRaisesRegex(OSError, "needs Windows"):
            wa.Wasapi()


if __name__ == "__main__":
    unittest.main()
