"""A stand-in for ffmpeg.exe, used by the recorder tests across a real
process boundary. It understands only what peep sends:

  capture   (argv contains ddagrab): prints the stream lines and
            "Output #0" to stderr, writes some bytes to the output file
            (last argv element), then waits for 'q' on stdin and prints a
            final stats line with time=..., exit 0. EOF on stdin is treated
            like ffmpeg does on a closed pipe: it keeps running until killed.
  remux     (argv contains -movflags): copies the input to the output.

Behaviour switches, via FAKE_FFMPEG_MODE (comma-separated):
  fail-qsv       exit 1 at start-up when the argv uses h264_qsv
  fail-all       exit 1 at start-up for every capture
  die-midway     exit 3 one second after becoming ready (ffmpeg crashed)
  ignore-q       never exit on 'q' (forces the recorder to kill it)
  fail-remux     remux exits 1 without writing
"""

import os
import shutil
import sys
import time

argv = sys.argv[1:]
mode = set(filter(None, os.environ.get("FAKE_FFMPEG_MODE", "").split(",")))
err = sys.stderr


def say(line):
    err.write(line + "\n")
    err.flush()


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

elapsed = time.monotonic() - start
h, rem = divmod(elapsed, 3600)
m, s = divmod(rem, 60)
say(f"[out#0/matroska @ 0x0] video:389KiB audio:39KiB subtitle:0KiB other streams:0KiB")
say(f"frame=   43 fps= 30 q=-0.0 Lsize=     428KiB time={int(h):02d}:{int(m):02d}:{s:05.2f} bitrate=1200kbits/s speed=1x")
sys.exit(0)
