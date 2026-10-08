"""A stand-in for ffmpeg.exe, used by the recorder tests across a real
process boundary. It understands only what peep sends:

  capture   (argv contains ddagrab): prints the stream lines and
            "Output #0" to stderr, writes some bytes to the output file
            (last argv element), then waits for 'q' on stdin and prints a
            final stats line with time=..., exit 0. EOF on stdin is treated
            like ffmpeg does on a closed pipe: it keeps running until killed.
  remux     (argv contains -movflags): copies the input to the output.
  sockets   (session A.1) every `-i tcp://host:port` or `-i unix:@name`
            (abstract AF_UNIX, what the tests use) input is connected to
            right after "Input #0" is printed, as real ffmpeg opens its
            inputs in order, and drained on a thread; on exit the bytes read
            per input are printed as `fake_ffmpeg: tcp <url> <bytes>`.

Behaviour switches, via FAKE_FFMPEG_MODE (comma-separated):
  fail-qsv       exit 1 at start-up when the argv uses h264_qsv
  fail-all       exit 1 at start-up for every capture
  die-midway     exit 3 one second after becoming ready (ffmpeg crashed)
  ignore-q       never exit on 'q' (forces the recorder to kill it)
  fail-remux     remux exits 1 without writing

Session C1b adds the render's two kinds of invocation:
  analysis  (output "-" with -f rawvideo / -f s16le, or -af volumedetect): the
            window is synthesised from a scene, FAKE_FFMPEG_SCENE (a JSON file
            written by tests/render_fixtures.py): per media file, its duration
            and the coloured spans (full-screen flashes, corner patches) and
            tones in it. Frames come out as rgb24 with showinfo-style
            `pts_time:` lines on stderr, audio as s16le, levels as volumedetect's
            `max_volume:` line. A file the scene does not know yields nothing.
            C1d: a decode scaled to anything but the 16x10 flash grid, without a crop,
            of a file whose scene has "screens", is a learned-screen thumbnail: the
            screen's pattern (plus the encoder's noise) or the frame's content.
  render    (-filter_complex with -progress): `-progress` lines on stdout, then
            a small file at the output path.
  modes     fail-analysis (every analysis exits 1), fail-render (every render
            exits 1, leaving a partial file), fail-render-qsv (only an h264_qsv
            render fails: the x264 retry succeeds), slow-render (0.3 s per
            progress step)
  FAKE_FFMPEG_ARGV_LOG, when set, gets each analysis/render argv as a JSON line.
"""

import json
import math
import os
import shutil
import socket
import struct
import sys
import threading
import time

argv = sys.argv[1:]
mode = set(filter(None, os.environ.get("FAKE_FFMPEG_MODE", "").split(",")))
err = sys.stderr


def say(line):
    err.write(line + "\n")
    err.flush()


def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def log_argv():
    path = os.environ.get("FAKE_FFMPEG_ARGV_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")


def load_scene(path_arg):
    spec = os.environ.get("FAKE_FFMPEG_SCENE")
    if not spec:
        return None, 30.0
    with open(spec, encoding="utf-8") as fh:
        scene = json.load(fh)
    name = os.path.basename(path_arg.replace("\\", "/"))
    return scene.get("files", {}).get(name), float(scene.get("fps", 30))


def hex_rgb(c):
    c = c.lstrip("#")
    return bytes((int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)))


def color_at(f, t, patch_area):
    for span in f.get("video", []):
        if span["t0"] <= t < span["t1"] and (span["where"] == "full" or patch_area):
            return hex_rgb(span["color"])
    return bytes(f.get("background", [60, 60, 60]))


def screen_pixel(seed, i):
    """A copy of render_fixtures.pattern_pixel (C1d; test_auto_render checks they agree)."""
    x = (seed * 2654435761 + i * 40503 + 12345) & 0xFFFFFFFF
    x ^= x >> 13
    x = (x * 1103515245 + 12345) & 0xFFFFFFFF
    return (x >> 16) & 0xFF


def thumb_rgb(f, t, fps, w, h):
    """C1d: a learned-screen thumbnail decode (scale to WxH, no crop, anything but the
    16x10 flash grid): rgb24 with r = g = b, the grey render_fixtures.thumb_at draws."""
    for span in f.get("video", []):
        if span["where"] == "full" and span["t0"] <= t < span["t1"]:
            r, g, b = hex_rgb(span["color"])
            return bytes([(77 * r + 150 * g + 29 * b + 128) >> 8] * 3) * (w * h)
    frame = int(t * fps + 1e-6)
    seed, noise = f.get("content_seed", 100000) + frame, 0
    for sp in f.get("screens", []):
        if sp["t0"] <= t < sp["t1"]:
            seed, noise = sp["pattern"], f.get("screen_noise", 3)
            break
    out = bytearray()
    for i in range(w * h):
        v = screen_pixel(seed, i)
        if noise:
            v += screen_pixel(frame + 7919, i) % (2 * noise + 1) - noise
        v = min(255, max(0, v))
        out += bytes((v, v, v))
    return bytes(out)


def sample_at(f, t, n):
    v = 0.0
    for tone in f.get("audio", []):
        if tone["t0"] <= t < tone["t1"]:
            v += tone["amp"] * math.sin(2 * math.pi * tone["freq"] * t)
    noise = f.get("noise", 0.0005)
    v += noise * (((n * 1103515245 + 12345) >> 8) % 2001 / 1000.0 - 1.0)
    return max(-32768, min(32767, int(v * 32767)))


joined = " ".join(argv)
analysis = ("ddagrab" not in joined and argv and argv[-1] == "-"
            and ("rawvideo" in argv or "s16le" in argv or "volumedetect" in joined))
if analysis:
    log_argv()
    if "fail-analysis" in mode:
        say("[in#0 @ 0x0] Error opening input: Invalid data found when processing input (fake)")
        sys.exit(1)
    f, fps = load_scene(opt("-i", ""))
    if f is None:
        sys.exit(0)
    ss, dur = float(opt("-ss", "0")), float(opt("-t", "1e9"))
    end = min(ss + dur, float(f["duration"]))
    out = sys.stdout.buffer
    if "rawvideo" in argv:
        vf = opt("-vf", "")
        w, h = 16, 10
        for part in vf.split(","):
            if part.startswith("scale="):
                w, h = (int(x) for x in part[len("scale="):].split(":")[:2])
        ph = float(f.get("phase", 0.0))
        i = math.ceil((ss - ph) * fps - 1e-6)
        n = 0
        say("[Parsed_showinfo_3 @ 0x0] config in time_base: 1/15360, frame_rate: 30/1")
        thumbs = "crop=" not in vf and (w, h) != (16, 10) and "screens" in f
        while ph + i / fps < end - 1e-9:
            t = ph + i / fps
            out.write(thumb_rgb(f, t, fps, w, h) if thumbs else color_at(f, t, "crop=" in vf) * (w * h))
            say(f"[Parsed_showinfo_3 @ 0x0] n:{n:4d} pts:{round(t * 15360):8d} pts_time:{t:.6g} duration:512")
            i += 1
            n += 1
        out.flush()
        sys.exit(0)
    if "s16le" in argv:
        rate = int(opt("-ar", "16000"))
        first = math.ceil(ss * rate - 1e-9)
        last = int(end * rate)
        out.write(b"".join(struct.pack("<h", sample_at(f, k / rate, k)) for k in range(first, last)))
        out.flush()
        sys.exit(0)
    peak = max([tone["amp"] for tone in f.get("audio", []) if tone["t0"] < end and tone["t1"] > ss]
               + [f.get("noise", 0.0005)])
    say(f"[Parsed_volumedetect_0 @ 0x0] max_volume: {20 * math.log10(peak):.1f} dB")
    sys.exit(0)

if "-progress" in argv and "-filter_complex" in argv:
    log_argv()
    out = argv[-1]
    if "-n" in argv and os.path.exists(out):
        say(f"File '{out}' already exists. Exiting.")
        sys.exit(1)
    if "fail-render" in mode or ("fail-render-qsv" in mode and "h264_qsv" in argv):
        with open(out, "wb") as fh:
            fh.write(b"partial")
        say("[h264_qsv @ 0x0] Error during encoding: device failed (-17) (fake)")
        say("Conversion failed!")
        sys.exit(1)
    for i in range(5):
        print(f"out_time_us={i * 250000}", flush=True)
        print("progress=" + ("end" if i == 4 else "continue"), flush=True)
        if "slow-render" in mode:
            time.sleep(0.3)
    with open(out, "wb") as fh:
        fh.write(b"\0\0\0\x18ftypmp42fake-cut" + b"\0" * 2048)
    sys.exit(0)

if "-movflags" in argv:
    src, dst = argv[argv.index("-i") + 1], argv[-1]
    if "fail-remux" in mode:
        say("[mp4 @ 0x0] Could not write header (fake)")
        sys.exit(1)
    if "-n" in argv and os.path.exists(dst):
        say(f"File '{dst}' already exists. Exiting.")
        sys.exit(1)
    shutil.copyfile(src, dst)
    sys.exit(0)

if not any("ddagrab" in a for a in argv):
    say("fake_ffmpeg: unsupported invocation " + " ".join(argv))
    sys.exit(2)

out = argv[-1]
say("Input #0, lavfi, from 'ddagrab=...':")
say("  Stream #0:0: Video: wrapped_avframe, d3d11, 2560x1600 [SAR 1:1 DAR 8:5], 30 fps, 30 tbr, 1000k tbn")
tcp_inputs = [argv[i + 1] for i, a in enumerate(argv[:-1])
              if a == "-i" and argv[i + 1].startswith(("tcp://", "unix:@"))]
tcp_read = {}


def drain(url, sock):
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            tcp_read[url] = tcp_read.get(url, 0) + len(chunk)
    except OSError:
        pass


tcp_socks = []
for n, url in enumerate(tcp_inputs, start=1):
    try:
        if url.startswith("unix:@"):
            s_ = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s_.settimeout(5)
            s_.connect("\0" + url[len("unix:@"):])
        else:
            host, port = url[len("tcp://"):].rsplit(":", 1)
            s_ = socket.create_connection((host, int(port)), timeout=5)
    except OSError as exc:
        say(f"[tcp @ 0x0] Connection to {url} failed: {exc}")
        say(f"Error opening input file {url}.")
        sys.exit(1)
    s_.settimeout(None)
    tcp_socks.append(s_)
    tcp_read[url] = 0
    threading.Thread(target=drain, args=(url, s_), daemon=True).start()
    say(f"Input #{n}, f32le, from '{url}':")
if "fail-all" in mode or ("fail-qsv" in mode and "h264_qsv" in argv):
    say("[h264_qsv @ 0x0] Error initializing an internal MFX session: unsupported (-3) (fake)")
    say("Conversion failed!")
    sys.exit(1)
if "-n" in argv and out != "-" and os.path.exists(out):
    say(f"File '{out}' already exists. Exiting.")
    sys.exit(1)
if out != "-":
    with open(out, "wb") as fh:
        fh.write(b"\x1aE\xdf\xa3fake-matroska" + b"\0" * 1024)
say(f"Output #0, matroska, to '{out}':")
say("  Stream #0:0: Video: h264, nv12(tv, bt709/bt709/iec61966-2-1, progressive), 2560x1600, 30 fps")
say("Press [q] to stop, [?] for help")
start = time.monotonic()

if "-t" in argv:
    time.sleep(float(argv[argv.index("-t") + 1]) / 10)
    say("frame=   60 fps= 30 q=-0.0 Lsize=N/A time=00:00:02.00 bitrate=N/A speed=1x")
    sys.exit(0)

if "die-midway" in mode:
    time.sleep(1.0)
    say("[lavfi @ 0x0] ddagrab: Failed to capture frame (fake)")
    sys.exit(3)

while True:
    ch = sys.stdin.buffer.read(1)
    if ch == b"q" and "ignore-q" not in mode:
        break
    if ch == b"":
        time.sleep(0.05)   # closed pipe: real ffmpeg on Windows keeps recording
        continue

for s_ in tcp_socks:
    try:
        s_.close()
    except OSError:
        pass
for url in tcp_inputs:
    say(f"fake_ffmpeg: tcp {url} {tcp_read.get(url, 0)}")
elapsed = time.monotonic() - start
h, rem = divmod(elapsed, 3600)
m, s = divmod(rem, 60)
say(f"[out#0/matroska @ 0x0] video:389KiB audio:39KiB subtitle:0KiB other streams:0KiB")
say(f"frame=   43 fps= 30 q=-0.0 Lsize=     428KiB time={int(h):02d}:{int(m):02d}:{s:05.2f} bitrate=1200kbits/s speed=1x")
sys.exit(0)
