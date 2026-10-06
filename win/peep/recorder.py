"""Process control and the record flow.

`FfmpegProcess` owns one ffmpeg child: starts it (argv logged verbatim
first), drains its stderr into the log and a tail buffer, detects
readiness, stops it by writing `q` to its stdin (interop does not
forward Ctrl-C reliably; a Windows pipe does), and surfaces exit code
and stderr tail on failure.

`Recorder.record()` is the whole recording, start to catalog:

  name    foreground window -> slug (unless given) -> collision-free stem
  claim   sidecar written at once (status "recording", reserves the stem),
          control file claimed so `peep stop` can find us
  audio   (A.1) one WASAPI capture child per audio source (system, mic),
          each serving raw PCM on a localhost port; a child that cannot
          start is reported loudly and the recording goes on without it
  start   ffmpeg launched into <stem>.recording.mkv; if the pipeline dies
          during start-up, the next one in video.fallback is tried, loudly.
          The moment ffmpeg prints `Input #0` (its first ddagrab frame is
          VIDEO_EPOCH before that, measured), every child is sent that
          instant as its anchor, and its stream starts there: audio t=0 and
          video t=0 are the same moment
  flash   start flash once ffmpeg reports its output open (+ lead_ms), with
          the clap tone when system audio is recorded
  wait    until stop_event (q/Enter in the terminal, or the agent's hotkey), a
          stop-request file (`peep stop`), or ffmpeg exiting on its own (a
          failure); meanwhile mark requests (`peep mark`, the agent's mark
          hotkey) flash the mark colour and land in the sidecar's `marks`
  stop    stop flash, settle_ms, then `q`; escalate to terminate on timeout;
          then the audio children (they keep feeding ffmpeg until it exits,
          and ffmpeg's -rw_timeout bounds a hung one: no hang on the stop path)
  final   remux to <stem>.mp4 (streams copied) and drop the capture, or
          keep the Matroska; sidecar completed; catalog event appended

Matroska is the capture container because a capture cut short by a
crash or power loss is still playable; MP4 is only produced from a
cleanly closed capture.

Sections:
  1. FfmpegProcess              (~line 73)
  2. Request / result           (~line 231)
  3. Recorder                   (~line 268)
"""

from __future__ import annotations

import collections
import datetime as _dt
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import catalog, ffmpeg_cmd, naming, paths
from .config import Config, effective_sources, source_set
from .flash import FlashRecord, NullFlasher
from .logsetup import event
from .winapi import ForegroundInfo

log = logging.getLogger("peep.recorder")

# When an audio child gets no anchor (ffmpeg printed no `Input #0` before connecting, which
# the probes never saw), it starts the stream this long before ffmpeg's connect instead:
# the median of connect - first video frame across the A.1 probe runs (424-442 ms).
FALLBACK_LEAD_MS = 430

# ---------------------------------------------------------------------------
# 1. FfmpegProcess
# ---------------------------------------------------------------------------

READY_MARKERS = ("Output #0",)          # printed once the muxer header is written: capture is live
_TIME = re.compile(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_CAPTURE_SIZE = re.compile(r"Stream #0:0.*?Video:.*?(\d{3,5})x(\d{3,5})")
CREATE_NO_WINDOW = 0x08000000


class FfmpegProcess:
    def __init__(self, popen: Callable = subprocess.Popen, tail_lines: int = 200, job=None,
                 on_input0: Callable[[float], None] | None = None):
        self._popen_factory = popen
        self.proc = None
        self.argv: list[str] = []
        self.tail = collections.deque(maxlen=tail_lines)
        self.ready = threading.Event()
        self.started_mono: float | None = None   # time.monotonic() at launch: the t0 of the timeline
        self.started_at: str | None = None        # the same instant as local ISO time
        self._job = job
        # A.1: QPC stamps (time.perf_counter: QueryPerformanceCounter on Windows, the clock the
        # WASAPI children stamp audio with) of the launch and of ffmpeg's "Input #0" line.
        self.started_qpc: float | None = None
        self.input0_qpc: float | None = None
        self.on_input0 = on_input0
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, argv: list[str]) -> None:
        self.argv = list(argv)
        event(log, logging.INFO, "ffmpeg.argv", argv=self.argv, cmdline=subprocess.list2cmdline(self.argv))
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        self.started_mono = time.monotonic()
        self.started_qpc = time.perf_counter()
        self.started_at = _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")
        self.proc = self._popen_factory(self.argv, **kwargs)
        event(log, logging.INFO, "ffmpeg.start", pid=getattr(self.proc, "pid", None))
        if self._job is not None:
            self._job.assign(self.proc)
        self._reader = threading.Thread(target=self._drain, name="ffmpeg-stderr", daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        stream = self.proc.stderr
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                if self.input0_qpc is None and line.startswith("Input #0"):
                    self._input0(time.perf_counter())
                with self._lock:
                    self.tail.append(line)
                log.debug("ffmpeg| %s", line)
                if not self.ready.is_set() and any(m in line for m in READY_MARKERS):
                    self.ready.set()
        except (OSError, ValueError) as exc:
            event(log, logging.WARNING, "ffmpeg.stderr_read_failed", error=repr(exc))

    def _input0(self, qpc: float) -> None:
        """ffmpeg has opened the screen input (it prints `Input #0` right after
        probing it); stamped on arrival, then handed to the recorder, which
        anchors the audio children before ffmpeg opens their inputs."""
        self.input0_qpc = qpc
        event(log, logging.INFO, "ffmpeg.input0", since_launch_ms=round((qpc - self.started_qpc) * 1000, 1))
        if self.on_input0 is not None:
            try:
                self.on_input0(qpc)
            except Exception as exc:     # never let an anchor problem kill the stderr drain
                event(log, logging.ERROR, "ffmpeg.input0_callback_failed", error=repr(exc))

    def wait_ready(self, timeout_s: float, poll_s: float = 0.05,
                   clock: Callable[[], float] = time.monotonic) -> str:
        """'ready' once the output is open, 'exited' if ffmpeg died first,
        'timeout' if neither happened in time (ffmpeg still running)."""
        deadline = clock() + timeout_s
        while clock() < deadline:
            if self.ready.wait(poll_s):
                return "ready"
            if self.poll() is not None:
                self.join_reader()
                return "ready" if self.ready.is_set() and self.poll() == 0 else "exited"
        return "timeout"

    def poll(self):
        return self.proc.poll() if self.proc else None

    def send_quit(self) -> bool:
        """Write 'q' to ffmpeg's stdin. False (and logged) if the pipe is gone —
        on Windows a closed pipe raises EINVAL rather than EPIPE (probed)."""
        try:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
            self.proc.stdin.close()
            event(log, logging.INFO, "ffmpeg.quit_sent", pid=self.proc.pid)
            return True
        except (OSError, ValueError) as exc:   # ValueError: the pipe object is already closed
            event(log, logging.WARNING, "ffmpeg.quit_failed", pid=self.proc.pid, error=repr(exc))
            return False

    def wait(self, timeout_s: float):
        try:
            rc = self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None
        self.join_reader()
        return rc

    def terminate(self, reason: str) -> int | None:
        event(log, logging.ERROR, "ffmpeg.terminate", pid=self.proc.pid, reason=reason)
        self.proc.kill()
        rc = self.wait(10)
        return rc

    def join_reader(self, timeout_s: float = 5) -> None:
        if self._reader is not None:
            self._reader.join(timeout_s)

    def close_pipes(self) -> None:
        """Release our ends of ffmpeg's pipes once it has exited."""
        if self.proc is None:
            return
        self.join_reader()
        for stream in (getattr(self.proc, "stdin", None), getattr(self.proc, "stderr", None)):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def stderr_tail(self, n: int = 15) -> list[str]:
        with self._lock:
            return list(self.tail)[-n:]

    def media_time_s(self) -> float | None:
        """The last `time=` ffmpeg reported (its final stats line on exit)."""
        with self._lock:
            lines = list(self.tail)
        for line in reversed(lines):
            m = _TIME.search(line)
            if m:
                h, mnt, s = m.groups()
                return round(int(h) * 3600 + int(mnt) * 60 + float(s), 3)
        return None

    def capture_size(self) -> tuple[int, int] | None:
        with self._lock:
            lines = list(self.tail)
        for line in lines:
            m = _CAPTURE_SIZE.search(line)
            if m:
                return int(m.group(1)), int(m.group(2))
        return None


# ---------------------------------------------------------------------------
# 2. Request / result
# ---------------------------------------------------------------------------


@dataclass
class RecordRequest:
    slug: str | None = None
    collection: str | None = None
    flash: bool | None = None
    audio: bool | None = None            # A's switch: False = no audio at all (kept for callers that set it)
    audio_sources: str | None = None     # A.1: system | mic | both | none; None = config audio.sources
    pipeline: str | None = None
    scale: str | None = None
    fps: int | None = None
    # Session B: the agent reads the foreground window when the hotkey arrives
    # (A's Proposal 2) and says who started the recording ("terminal" | "agent").
    foreground: ForegroundInfo | None = None
    origin: str = "terminal"


@dataclass
class RecordResult:
    ok: bool
    message: str
    uid: str | None = None
    media_path: Path | None = None
    sidecar_path: Path | None = None
    exit_code: int | None = None
    stderr_tail: list[str] = field(default_factory=list)
    duration_s: float | None = None


class RecordError(RuntimeError):
    """A recording that could not start; the message is user-facing."""


# ---------------------------------------------------------------------------
# 3. Recorder
# ---------------------------------------------------------------------------


def _seconds_between(start_iso: str | None, end_iso: str | None) -> float | None:
    """end - start in seconds, both ISO strings with offsets; None if either is unusable."""
    try:
        return round((_dt.datetime.fromisoformat(end_iso) - _dt.datetime.fromisoformat(start_iso)).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


def audio_summary(block: dict | None) -> str:
    """One status line: what audio this recording has, and from which device."""
    if not block or not block.get("tracks"):
        return "audio: none" if not block else "audio: none (no source could start; see the warnings above)"
    parts = []
    for src in ("system", "mic"):
        b = block.get(src)
        if not b or b.get("error"):
            continue
        name = (b.get("endpoint") or {}).get("name") or b.get("device") or "?"
        parts.append(f"{src} ({name})")
    mix = " mixed" if block.get("mix") == "mix" and len(parts) > 1 else (" as separate tracks" if len(parts) > 1 else "")
    return "audio: " + " + ".join(parts) + mix


def _default_capture_factory(job):
    from .wasapi import CaptureProcess
    return CaptureProcess(job=job)


def _default_player(path: str) -> bool:
    from .wasapi import play_wav_async
    return play_wav_async(path)


def resolve_ffmpeg(path: str, which: Callable[[str], str | None]) -> str | None:
    if os.path.isabs(path):
        return path if os.path.exists(path) else None
    return which(path)


class Recorder:
    def __init__(self, cfg: Config, control, *, popen: Callable = subprocess.Popen,
                 run: Callable = subprocess.run, flasher_factory: Callable | None = None,
                 foreground: Callable | None = None, which: Callable | None = None,
                 job_factory: Callable | None = None, today: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 status: Callable[[str], None] = print,
                 capture_factory: Callable | None = None, player: Callable[[str], bool] | None = None,
                 clap_path: Path | None = None):
        from . import winapi
        import shutil
        self.cfg = cfg
        self.control = control
        self.popen = popen
        self.run = run
        self.flasher_factory = flasher_factory or self._default_flasher
        self.foreground = foreground or winapi.foreground_window
        self.which = which or shutil.which
        self.job_factory = job_factory or winapi.KillOnCloseJob
        self.today = today or _dt.date.today
        self.sleep = sleep
        self.status = status
        self.capture_factory = capture_factory or _default_capture_factory
        self.player = player or _default_player
        self.clap_path = clap_path

    @staticmethod
    def _default_flasher():
        from .flash import TkFlasher
        return TkFlasher()

    # -- the flow ------------------------------------------------------------

    def record(self, req: RecordRequest, stop_event: threading.Event) -> RecordResult:
        cfg = self.cfg
        ffmpeg = resolve_ffmpeg(cfg.ffmpeg.path, self.which)
        if not ffmpeg:
            raise RecordError(f"ffmpeg not found ({cfg.ffmpeg.path!r}); run `peep doctor` "
                              f"(fix: winget install Gyan.FFmpeg)")
        collection = naming.validate_collection(req.collection or cfg.default_collection)
        fg = req.foreground if req.foreground is not None else self.foreground()
        slug = naming.slug_from_user(req.slug) if req.slug else naming.suggest_slug(fg.image, fg.title)
        coll_dir = cfg.root_path() / collection
        coll_dir.mkdir(parents=True, exist_ok=True)
        stem = naming.allocate_stem(str(coll_dir), self.today(), slug)
        capture = coll_dir / f"{stem}.recording.mkv"
        final = coll_dir / f"{stem}.{cfg.output.container}"
        side = coll_dir / f"{stem}.json"
        use_flash = cfg.flash.enabled if req.flash is None else req.flash
        sources = "none" if req.audio is False else effective_sources(cfg.audio, req.audio_sources)
        wanted = source_set(sources)

        uid = catalog.new_uid()
        sc = catalog.new_sidecar(uid=uid, slug=slug, collection=collection, file=final.name,
                                 created=catalog.now_iso())
        sc["foreground"] = {"title": fg.title, "process": fg.process, "image": fg.image}
        sc["audio"] = self._audio_block(sources) if wanted else None
        sc["flash"]["enabled"] = use_flash
        sc["timeline"]["origin"] = req.origin

        # Claim before writing anything: a refused claim (already recording) must leave no trace.
        self.control.claim({"uid": uid, "capture": str(capture), "final": str(final), "sidecar": str(side),
                            "origin": req.origin, "status": "starting"})
        proc: FfmpegProcess | None = None
        flasher = NullFlasher()
        captures: dict = {}
        try:
            catalog.write_sidecar(side, sc)   # reserves the stem while the capture runs
            event(log, logging.INFO, "record.begin", uid=uid, stem=stem, collection=collection,
                  slug_source="user" if req.slug else "foreground", fg_process=fg.process)
            if use_flash:
                flasher = self._prepare_flasher(sc)
            captures = self._start_audio(wanted, sc)
            proc, pipeline, spec = self._start_capture(ffmpeg, capture, req, wanted, captures, sc)
            if proc is None:
                return self._finish_failed_start(sc, side, capture, uid, collection, spec)
            t0 = proc.started_mono
            timeline = sc["timeline"]
            timeline["ffmpeg_started_at"] = proc.started_at
            timeline["ffmpeg_ready_s"] = round(time.monotonic() - t0, 3)
            self.sleep(cfg.flash.lead_ms / 1000)
            if use_flash:
                if "system" in captures and cfg.audio.clap:
                    self._clap(sc, proc)
                rec = flasher.flash(cfg.flash.start_color, cfg.flash.duration_ms)
                sc["flash"]["start"] = rec.to_sidecar(t0)
            self.control.update_active(status="recording", recording_since=catalog.now_iso(),
                                       ffmpeg_started_at=proc.started_at)
            self.status(f"● recording → {final}\n  {audio_summary(sc.get('audio'))}\n"
                        f"  press q or Enter to stop (or run `peep stop` elsewhere)")

            def take_marks():
                self._take_marks(flasher if use_flash else None, sc, side, t0, proc.started_at)

            reason = self._wait_for_stop(proc, stop_event, on_tick=take_marks)
            if reason != "ffmpeg-exited":
                take_marks()              # a mark pressed just before the stop still counts
            self.control.update_active(status="stopping")
            timeline["stop_requested_at"] = catalog.now_iso()
            timeline["stop_requested_s"] = round(time.monotonic() - t0, 3)
            timeline["stop_reason"] = reason
            rc = proc.poll()
            if reason != "ffmpeg-exited":
                if use_flash:
                    rec = flasher.flash(cfg.flash.stop_color, cfg.flash.duration_ms)
                    sc["flash"]["stop"] = rec.to_sidecar(t0)
                self.sleep(cfg.flash.settle_ms / 1000)
                self.status("■ stopping…")
                proc.send_quit()
                rc = proc.wait(cfg.ffmpeg.stop_timeout_s)
                if rc is None:
                    rc = proc.terminate(f"no exit {cfg.ffmpeg.stop_timeout_s}s after q")
            else:
                proc.join_reader()
            timeline["ffmpeg_exited_at"] = catalog.now_iso()
            timeline["ffmpeg_exited_s"] = round(time.monotonic() - t0, 3)
            event(log, logging.INFO if rc == 0 else logging.ERROR, "ffmpeg.exit", pid=proc.proc.pid,
                  exit_code=rc, reason=reason)
            self._stop_audio(captures, sc)
            return self._finalize(sc, side, capture, final, proc, rc, ffmpeg, spec, uid, collection, reason)
        except BaseException as exc:
            # Anything unexpected (including Ctrl-C reaching this process): never leave a
            # sidecar claiming "recording". The capture file, if any, is left where it is.
            event(log, logging.ERROR, "record.aborted", uid=uid, error=repr(exc))
            if sc.get("status") == "recording":
                sc["status"] = "failed"
                sc["error"] = repr(exc)
                try:
                    catalog.write_sidecar(side, sc)
                except OSError as write_exc:
                    event(log, logging.ERROR, "sidecar.write_failed", path=str(side), error=repr(write_exc))
            raise
        finally:
            flasher.close()
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate("recorder exiting with ffmpeg still running")
                proc.close_pipes()
            for cap in captures.values():      # no-op when _stop_audio already ran
                if cap.poll() is None:
                    cap.stop(2.0)
            self.control.release()

    # -- steps ---------------------------------------------------------------

    def _prepare_flasher(self, sc: dict):
        try:
            f = self.flasher_factory()
            f.prepare()
            return f
        except Exception as exc:  # no desktop / no tkinter: record anyway, but say so everywhere
            msg = f"clapper flash unavailable ({exc!r}); recording without it"
            event(log, logging.WARNING, "flash.unavailable", error=repr(exc))
            self.status(f"! {msg}")
            sc["flash"]["enabled"] = False
            sc["flash"]["error"] = repr(exc)
            return NullFlasher()

    # -- audio (A.1) -------------------------------------------------------------

    def _audio_block(self, sources: str) -> dict:
        """The sidecar's audio block. A's four keys keep their meaning (`device`
        is the microphone, None without one); the rest is additive, so
        peep.sidecar/1 readers keep working."""
        a = self.cfg.audio
        wanted = source_set(sources)
        return {"device": (a.device or None) if "mic" in wanted else None, "codec": "aac", "bitrate": a.bitrate,
                "offset_ms": a.offset_ms, "sources": sources, "mix": a.mix if len(wanted) > 1 else None,
                "mic_backend": a.mic_backend if "mic" in wanted else None, "tracks": [], "epoch": None,
                "clap": None, **{src: None for src in wanted}}

    def _start_audio(self, wanted: tuple, sc: dict) -> dict:
        """Start one WASAPI child per source that needs one. A child that fails
        is reported (status line, log, sidecar) and left out; the recording
        goes on, like A's flash-unavailable path."""
        cfg = self.cfg
        captures = {}
        for src in wanted:
            if src == "mic" and cfg.audio.mic_backend == "dshow":
                sc["audio"]["mic"] = {"backend": "dshow", "device": cfg.audio.device or ffmpeg_cmd.DEFAULT_MIC,
                                      "shift_ms": cfg.audio.dshow_align_ms + cfg.audio.offset_ms}
                continue
            selector = cfg.audio.system_device if src == "system" else cfg.audio.device
            cap = self.capture_factory(self.job_factory())
            try:
                ready = cap.start(src, selector, ready_timeout_s=cfg.ffmpeg.startup_timeout_s,
                                  fallback_lead_ms=FALLBACK_LEAD_MS)
            except Exception as exc:
                tail = cap.stderr_tail(6) if hasattr(cap, "stderr_tail") else []
                sc["audio"][src] = {"backend": "wasapi", "error": str(exc), "exit_code": getattr(cap, "exit_code", None),
                                    "stderr_tail": tail}
                event(log, logging.ERROR, "audio.source_failed", source=src, error=str(exc), stderr_tail=tail)
                self.status(f"! {src} audio unavailable ({exc}); recording without it — see `peep doctor`")
                continue
            captures[src] = cap
            sc["audio"][src] = {"backend": "wasapi", "endpoint": ready.endpoint, "format": ready.format.to_dict(),
                                "selector": selector or "default", "error": None}
            if src == "mic":
                sc["audio"]["device"] = ready.endpoint.get("name") or sc["audio"]["device"]
            event(log, logging.INFO, "audio.source_ready", source=src, port=ready.port,
                  endpoint=ready.endpoint.get("name"), format=ready.format.describe())
        return captures

    def _anchor(self, captures: dict, sc: dict, proc: "FfmpegProcess", input0_qpc: float) -> None:
        """Called from ffmpeg's stderr drain the moment `Input #0` arrives."""
        a = self.cfg.audio
        anchor = input0_qpc - a.epoch_lead_ms / 1000 - a.offset_ms / 1000
        for src, cap in captures.items():
            cap.send_anchor(anchor)
        if sc.get("audio") is not None:
            sc["audio"]["epoch"] = {"ffmpeg_started_qpc": proc.started_qpc, "input0_qpc": input0_qpc,
                                    "epoch_lead_ms": a.epoch_lead_ms, "offset_ms": a.offset_ms,
                                    "anchor_qpc": anchor, "video_epoch_qpc_est": input0_qpc - a.epoch_lead_ms / 1000}

    def _clap(self, sc: dict, proc: "FfmpegProcess") -> None:
        """The audible fiducial: a short tone through the default output at the
        start flash. The system track carries it exactly (loopback); C finds
        it near flash.start. The probe measured 45-110 ms from this call to the
        tone reaching the FxSound loopback."""
        path = Path(self.clap_path) if self.clap_path else paths.data_dir() / "clap.wav"
        try:
            if not path.exists():
                from .wasapi import tone_wav
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tone_wav(1000.0, 120, amplitude=0.4))
            qpc = time.perf_counter()
            ok = self.player(str(path))
        except OSError as exc:
            ok, qpc = False, None
            event(log, logging.WARNING, "audio.clap_failed", error=repr(exc))
        sc["audio"]["clap"] = {"played": bool(ok), "requested_qpc": qpc, "freq_hz": 1000, "ms": 120,
                               "since_ffmpeg_start_s": round(qpc - proc.started_qpc, 3) if (ok and qpc) else None}

    def _stop_audio(self, captures: dict, sc: dict) -> None:
        """After ffmpeg has exited: stop each child (bounded wait, then kill)
        and keep what it reported. A child that died during the recording is
        a warning on the status line, never silence."""
        for src, cap in captures.items():
            event(log, logging.INFO, "audio.stop", source=src)
            rc = cap.stop(3.0)
            block = sc["audio"][src]
            block["connections"] = [{k: v for k, v in e.items() if k != "event"} for e in cap.all("connected")]
            stats = cap.last("stats") or {}
            block["stats"] = stats.get("timeline")
            block["first_audio_qpc"] = stats.get("first_audio_qpc")
            block["exit_code"] = rc
            if rc != 0 or stats.get("capture_error"):
                block["error"] = stats.get("capture_error") or f"exited {rc}"
                block["stderr_tail"] = cap.stderr_tail(10)
                self.status(f"! {src} audio capture ended badly ({block['error']}); "
                            f"the {src} track may be short — see the log")
            if src == "system" and block["connections"]:
                sc["audio"]["first_sample_qpc"] = block["connections"][-1].get("first_sample_qpc")

    # -- video ---------------------------------------------------------------------

    def _start_capture(self, ffmpeg: str, capture: Path, req: RecordRequest, wanted: tuple, captures: dict,
                       sc: dict):
        cfg = self.cfg
        primary = req.pipeline or cfg.video.pipeline
        order = [primary] + [p for p in cfg.video.fallback if p != primary]
        attempts = []
        spec = None
        ready = {src: cap.ready_info for src, cap in captures.items()}
        inputs = ffmpeg_cmd.audio_inputs_for(cfg, wanted, ready)
        if sc.get("audio") is not None:
            sc["audio"]["tracks"] = ffmpeg_cmd.audio_track_layout(inputs, cfg.audio.mix)
            if not inputs:
                self.status("! no audio source could start; recording video only")
        for i, pipeline in enumerate(order):
            spec = ffmpeg_cmd.spec_from_config(cfg, ffmpeg=ffmpeg, output=str(capture), pipeline=pipeline,
                                               scale=req.scale, fps=req.fps, audio_inputs=inputs)
            proc = FfmpegProcess(self.popen, job=self.job_factory())
            if captures:
                proc.on_input0 = (lambda p: lambda qpc: self._anchor(captures, sc, p, qpc))(proc)
            proc.start(ffmpeg_cmd.build_capture_argv(spec))
            state = proc.wait_ready(cfg.ffmpeg.startup_timeout_s)
            if state == "timeout":
                event(log, logging.WARNING, "ffmpeg.ready_timeout", pipeline=pipeline,
                      timeout_s=cfg.ffmpeg.startup_timeout_s)
                self.status(f"! ffmpeg has not reported its output after {cfg.ffmpeg.startup_timeout_s:g}s; "
                            f"continuing (check the log if the file is empty)")
                state = "ready"
            if state == "ready":
                sc["video"].update(pipeline=pipeline, encoder=ffmpeg_cmd.ENCODERS[pipeline], fps=spec.fps,
                                   output_idx=spec.output_idx)
                sc["timeline"]["ffmpeg_started_qpc"] = proc.started_qpc
                sc["timeline"]["input0_qpc"] = proc.input0_qpc
                sc["ffmpeg"]["argv"] = proc.argv
                sc["ffmpeg"]["attempts"] = attempts
                return proc, pipeline, spec
            rc, tail = proc.poll(), proc.stderr_tail(12)
            proc.close_pipes()
            attempts.append({"pipeline": pipeline, "exit_code": rc, "stderr_tail": tail})
            event(log, logging.ERROR, "ffmpeg.start_failed", pipeline=pipeline, exit_code=rc, stderr_tail=tail)
            self._discard_empty(capture)
            nxt = order[i + 1] if i + 1 < len(order) else None
            self.status(f"! {pipeline} pipeline failed to start (ffmpeg exit {rc})"
                        + (f"; trying {nxt}" if nxt else ""))
            for line in tail[-4:]:
                self.status(f"    ffmpeg: {line}")
        sc["ffmpeg"]["attempts"] = attempts
        return None, None, spec

    def _wait_for_stop(self, proc: FfmpegProcess, stop_event: threading.Event, poll_s: float = 0.1,
                       on_tick: Callable[[], None] | None = None) -> str:
        while True:
            if stop_event.is_set():
                return getattr(stop_event, "reason", None) or "terminal"
            if self.control.stop_requested():
                return "peep-stop"
            if proc.poll() is not None:
                return "ffmpeg-exited"
            if on_tick is not None:
                on_tick()
            stop_event.wait(poll_s)

    def _take_marks(self, flasher, sc: dict, side: Path, t0: float, ffmpeg_started_at: str) -> None:
        """Turn pending mark requests into sidecar `marks` entries.

        `t` (A's reserved key) is the press, in seconds since the ffmpeg launch:
        the requester's wall-clock `requested_at` minus `ffmpeg_started_at`, so the
        <=100 ms poll delay does not shift it. When flashes are on, the mark-colour
        flash's own stamp (same clock as flash.start/stop) is kept beside it, and
        that flash is what session C finds in the video. The sidecar is rewritten
        straight away, so a crash later in the recording keeps its marks."""
        requests = self.control.take_marks()
        if not requests:
            return
        cfg = self.cfg
        for req in requests:
            consumed = time.monotonic()
            t = _seconds_between(ffmpeg_started_at, req.get("requested_at"))
            if t is None:
                t = round(consumed - t0, 3)
            mark = {"t": t, "label": str(req.get("label") or ""), "since_ffmpeg_start_s": t,
                    "at": req.get("requested_at"), "source": req.get("source"),
                    "consumed_s": round(consumed - t0, 3), "flash": None}
            if flasher is not None:
                mark["flash"] = flasher.flash(cfg.flash.mark_color, cfg.flash.duration_ms).to_sidecar(t0)
            sc["marks"].append(mark)
            n = len(sc["marks"])
            event(log, logging.INFO, "record.mark", n=n, t=t, source=mark["source"], label=mark["label"],
                  flashed=mark["flash"] is not None)
            self.status(f"◆ mark {n} at {t:.1f}s" + (f" ({mark['label']})" if mark["label"] else ""))
        try:
            catalog.write_sidecar(side, sc)
        except OSError as exc:      # the marks are still in memory and land with the final sidecar
            event(log, logging.WARNING, "sidecar.mark_write_failed", path=str(side), error=repr(exc))

    def _discard_empty(self, path: Path) -> None:
        try:
            if path.exists() and path.stat().st_size == 0:
                path.unlink()
                event(log, logging.INFO, "file.discard_empty", path=str(path))
        except OSError as exc:
            event(log, logging.WARNING, "file.discard_failed", path=str(path), error=repr(exc))

    def _finish_failed_start(self, sc, side, capture, uid, collection, spec) -> RecordResult:
        # Nothing was recorded: drop the stem-reserving sidecar rather than leave clutter in
        # the Videos folder. Every attempt's exit code and stderr tail is already in the log.
        sc["status"] = "failed"
        try:
            side.unlink()
        except FileNotFoundError:
            pass
        event(log, logging.ERROR, "record.start_failed", uid=uid, attempts=len(sc["ffmpeg"]["attempts"]))
        last = sc["ffmpeg"]["attempts"][-1] if sc["ffmpeg"]["attempts"] else {}
        return RecordResult(False, "every pipeline failed to start; see the ffmpeg lines above, "
                                   "`peep doctor`, and the log", uid=uid, sidecar_path=None,
                            exit_code=last.get("exit_code"), stderr_tail=last.get("stderr_tail", []))

    def _finalize(self, sc, side, capture, final, proc, rc, ffmpeg, spec, uid, collection, reason) -> RecordResult:
        cfg = self.cfg
        root = cfg.root_path()
        size = proc.capture_size()
        sc["video"]["capture_size"] = f"{size[0]}x{size[1]}" if size else None
        out = ffmpeg_cmd.output_size(spec, size)
        sc["video"]["output_size"] = f"{out[0]}x{out[1]}" if out else None
        media_t = proc.media_time_s()
        sc["duration_s"] = media_t if media_t is not None else sc["timeline"].get("ffmpeg_exited_s")
        sc["ffmpeg"]["exit_code"] = rc
        has_data = capture.exists() and capture.stat().st_size > 0
        if rc != 0 or not has_data:
            tail = proc.stderr_tail(15)
            sc["ffmpeg"]["stderr_tail"] = tail
            sc["status"] = "failed"
            media = None
            if has_data:   # keep what was captured, minus the in-progress marker; Matroska plays as-is
                media = capture.with_name(capture.name.replace(".recording.mkv", ".mkv"))
                os.rename(capture, media)
                sc["video"]["container"] = "mkv"
            sc["file"] = media.name if media else None
            catalog.write_sidecar(side, sc)
            catalog.Catalog(root).append({"event": "failed", "uid": uid, "collection": collection,
                                          "file": catalog.relpath_posix(root, media) if media else "",
                                          "title": sc["title"], "created": sc["created"],
                                          "duration_s": sc["duration_s"], "exit_code": rc, "reason": reason})
            why = "ffmpeg stopped on its own" if reason == "ffmpeg-exited" else f"ffmpeg exited {rc}"
            kept = f"; partial capture kept at {media}" if media else ""
            return RecordResult(False, f"recording failed: {why}{kept}", uid=uid, media_path=media,
                                sidecar_path=side, exit_code=rc, stderr_tail=tail, duration_s=sc["duration_s"])

        media = capture
        if cfg.output.container == "mp4":
            media = self._remux(ffmpeg, capture, final, sc)
        else:
            os.rename(capture, final)
            media = final
        sc["video"]["container"] = media.suffix.lstrip(".")
        sc["file"] = media.name
        sc["status"] = "ok"
        catalog.write_sidecar(side, sc)
        catalog.Catalog(root).append({"event": "recorded", "uid": uid, "collection": collection,
                                      "file": catalog.relpath_posix(root, media), "title": sc["title"],
                                      "created": sc["created"], "duration_s": sc["duration_s"]})
        dur = f"{sc['duration_s']:.1f}s" if isinstance(sc["duration_s"], (int, float)) else "?s"
        return RecordResult(True, f"saved {media} ({dur})", uid=uid, media_path=media, sidecar_path=side,
                            exit_code=rc, duration_s=sc["duration_s"])

    def _remux(self, ffmpeg: str, capture: Path, final: Path, sc: dict) -> Path:
        """Capture -> MP4. On failure the Matroska is kept (renamed to <stem>.mkv)
        and the reason is logged, printed and stored in the sidecar."""
        argv = ffmpeg_cmd.build_remux_argv(ffmpeg, str(capture), str(final))
        sc["ffmpeg"]["remux_argv"] = argv
        event(log, logging.INFO, "ffmpeg.argv", argv=argv, cmdline=subprocess.list2cmdline(argv), purpose="remux")
        try:
            res = self.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=600)
            rc, err = res.returncode, (res.stderr or b"").decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            rc, err = None, repr(exc)
        event(log, logging.INFO if rc == 0 else logging.ERROR, "ffmpeg.remux_exit", exit_code=rc,
              stderr_tail=err.strip().splitlines()[-5:])
        if rc == 0 and final.exists() and final.stat().st_size > 0:
            capture.unlink()
            return final
        keep = capture.with_name(capture.name.replace(".recording.mkv", ".mkv"))
        os.rename(capture, keep)
        sc["remux_error"] = {"exit_code": rc, "stderr_tail": err.strip().splitlines()[-5:]}
        self.status(f"! MP4 remux failed (exit {rc}); kept the Matroska capture: {keep}")
        if final.exists() and final.stat().st_size == 0:
            final.unlink()
        return keep
