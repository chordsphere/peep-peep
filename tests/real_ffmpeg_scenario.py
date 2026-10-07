"""The real-ffmpeg check behind tests.test_render_run.RealFfmpegTest, run as its
own process: `python3 -B -m tests.real_ffmpeg_scenario <empty dir> <ffmpeg>`.

The test starts it in a new session with stdin on /dev/null and kills its whole
process group at SCENARIO_TIMEOUT_S, so no ffmpeg here can reach a terminal (a
background ffmpeg that touches the tty is stopped by SIGTTOU, and with it the
whole process group, the Python waiting on it included: the hang bale saw) and
nothing here can outlive its cap. Every ffmpeg runs with -nostdin, stdin on
/dev/null, a timeout of its own, and -y/-n against a fresh path.

Exit 0: the cut is exact. Exit 1: a problem, printed. Whether it can run at all
(find_ffmpeg) is the test's call, in its own process, before starting this."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import render_fixtures as rf   # noqa: E402  (puts win/ on sys.path)
from peep import config as c, render       # noqa: E402

STEM = "2026-10-07-demo"
SCENARIO_TIMEOUT_S = 120     # the whole child (A.1's per-check cap); it takes ~5 s
CALL_TIMEOUT_S = 60          # any one ffmpeg/ffprobe inside it

# What the scenario needs; ffmpeg 6.1 (bale's sandbox, Ubuntu 24.04) has all of it.
ENCODERS = ("libx264", "aac")
FILTERS = ("color", "aevalsrc", "drawbox", "trim", "atrim", "setpts", "asetpts", "setsar", "concat", "format",
           "apad", "acrossfade", "freezeframes", "crop", "overlay", "scale", "showinfo", "volumedetect", "amix")


def run(argv: list, timeout: float = CALL_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)


def find_ffmpeg() -> tuple[str | None, str]:
    """(ffmpeg, "") when the scenario can run here, else (None, why not)."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return None, "no ffmpeg on PATH; the fake covers the process boundary, README smoke steps 60-69 cover ffmpeg.exe"
    if not shutil.which("ffprobe"):
        return None, "no ffprobe beside ffmpeg on PATH"
    try:
        enc = run([exe, "-hide_banner", "-nostdin", "-encoders"], 20).stdout
        fil = run([exe, "-hide_banner", "-nostdin", "-filters"], 20).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"ffmpeg did not answer -encoders/-filters ({exc!r})"
    missing = [e for e in ENCODERS if f" {e} ".encode() not in enc]
    names = {line.split()[1] for line in fil.decode("utf-8", "replace").splitlines() if len(line.split()) > 2}
    missing += [f for f in FILTERS if f not in names]
    if missing:
        return None, f"this ffmpeg lacks {', '.join(missing)}"
    return exe, ""


def scenario(tmp: Path, ffmpeg: str) -> list[str]:
    """Synthesised media (flashes, patches, tones) through the real ffmpeg: the cut
    has exactly the planned frames, equally long audio, the chapters, and not one
    frame of any fiducial colour. Returns the problems found."""
    root = tmp / "root"
    sc = rf.build_sidecar(STEM, [14.0, 12.0], [("take", 1, 3.0), ("retake", 1, 6.0), ("take", 1, 10.0),
                                                 ("take", 2, 2.5), ("mark", 2, 4.0, "part two"), ("take", 2, 9.0)],
                          output_size="640x400", pipeline="x264")
    rf.write_recording(root, sc)
    scene = rf.scene_for(sc, {1: [(6.9, 9.3)], 2: [(3.2, 8.5)]})
    for s in sc["segments"]:
        out = root / "inbox" / s["file"]
        out.unlink()                            # write_recording's placeholder: a fresh path for -y
        p = run(rf.synth_media_argv(ffmpeg, scene["files"][s["file"]], out))
        if p.returncode:
            return [f"synthesising {s['file']} failed ({p.returncode}): {p.stderr.decode(errors='replace')[-800:]}"]
    cfg = c.from_mapping({"root": str(root), "ffmpeg": {"path": ffmpeg}})
    res = render.Renderer(cfg, run=lambda *a, **kw: subprocess.run(*a, **{**kw, "timeout": CALL_TIMEOUT_S})
                          ).render("last")
    problems = []
    if res.fallbacks:
        problems.append(f"detection fell back to stamps: {res.fallbacks}")
    block = json.loads((root / "inbox" / f"{STEM}.json").read_text())["render"]
    cut = root / "inbox" / f"{STEM}.cut.mp4"
    info = json.loads(run([shutil.which("ffprobe"), "-v", "error", "-show_entries",
                           "stream=codec_type,nb_frames,duration", "-show_chapters", "-of", "json", str(cut)]).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    planned = sum(p["frames"] for p in block["plan"])
    if int(v["nb_frames"]) != planned:
        problems.append(f"the cut has {v['nb_frames']} frames, the plan {planned}")
    if abs(float(a["duration"]) - float(v["duration"])) > 0.025:
        problems.append(f"audio {a['duration']} s against video {v['duration']} s")
    titles = [ch["tags"]["title"] for ch in info["chapters"]]
    if titles != ["start", "part two"]:
        problems.append(f"chapters {titles}")
    pixels = []
    for vf in ("crop=40:40:5:355,scale=1:1:flags=area,format=rgb24", "scale=1:1:flags=area,format=rgb24"):
        raw = run([ffmpeg, "-nostdin", "-v", "error", "-i", str(cut), "-vf", vf, "-f", "rawvideo", "-"]).stdout
        pixels += [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    if not pixels:
        problems.append("no frames decoded from the cut")
    loud = [px for px in pixels if max(abs(x - 60) for x in px) > 40]
    if loud:
        problems.append(f"a fiducial colour survived into the cut: {loud[:5]}")
    return problems


def main(argv: list) -> int:
    tmp, ffmpeg = Path(argv[0]), argv[1]
    render.STALL_S = 30.0                     # a stalled encode fails well inside the cap
    try:
        problems = scenario(tmp, ffmpeg)
    except subprocess.TimeoutExpired as exc:
        problems = [f"an ffmpeg call ran past its {exc.timeout:g} s timeout: {exc.cmd}"]
    for p in problems:
        print(p, flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
