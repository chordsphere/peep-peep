"""`peep doctor`: check everything a recording depends on and report.
It never installs anything; for each gap it prints the exact fix
(a `winget install ...` line where one exists).

Checks: Python version, config file, data directory, ffmpeg presence and
version, the capture inputs (ddagrab filter, dshow device), the encoders
and GPU filters each pipeline needs, the configured microphone among the
dshow audio devices, the storage root, tkinter for the flash, and —
with --capture — a two-second real capture of the configured pipeline
into ffmpeg's null muxer (nothing written).

The ffmpeg runner is injected so the parsing is tested against the
outputs the session-A probe captured on the laptop.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import ffmpeg_cmd, paths
from .config import Config

WINGET_FFMPEG = "winget install Gyan.FFmpeg"
WINGET_PYTHON = "winget install Python.Python.3.12"

PIPELINE_NEEDS = {
    "qsv": {"encoders": ["h264_qsv"], "filters": ["hwmap", "vpp_qsv"]},
    "qsv-download": {"encoders": ["h264_qsv"], "filters": ["hwdownload"]},
    "x264": {"encoders": ["libx264"], "filters": ["hwdownload"]},
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str | None = None
    warn_only: bool = False      # reported, but does not fail the run

    def render(self) -> str:
        mark = "ok  " if self.ok else ("warn" if self.warn_only else "FAIL")
        line = f"[{mark}] {self.name}: {self.detail}"
        if not self.ok and self.fix:
            line += f"\n         fix: {self.fix}"
        return line


Runner = Callable[[list[str], float], tuple[int, str, str]]


def default_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


def parse_dshow_audio(listing: str) -> list[tuple[str, str | None]]:
    """(friendly name, alternative name) for each dshow audio device in an
    `ffmpeg -list_devices true -f dshow -i dummy` listing."""
    out, pending = [], None
    for line in listing.replace("\r", "").split("\n"):
        m = re.search(r'"([^"]+)" \(audio\)', line)
        if m:
            if pending is not None:
                out.append((pending, None))
            pending = m.group(1)
            continue
        m = re.search(r'Alternative name "([^"]+)"', line)
        if m and pending is not None:
            out.append((pending, m.group(1)))
            pending = None
        elif re.search(r'" \((video|none)\)', line) and pending is not None:
            out.append((pending, None))
            pending = None
    if pending is not None:
        out.append((pending, None))
    return out


def _listed(name: str, text: str) -> bool:
    """True if `name` appears as a whole entry name in an ffmpeg -encoders/-filters/-devices listing."""
    return re.search(rf"^\s*[A-Z.|]*\s+{re.escape(name)}\s", text, re.M) is not None


class Doctor:
    def __init__(self, cfg: Config, run: Runner = default_runner, which=shutil.which,
                 environ: dict | None = None, python_version=sys.version_info, tk_import=None):
        self.cfg, self.run, self.which = cfg, run, which
        self.env = os.environ if environ is None else environ
        self.py = python_version
        self.tk_import = tk_import or (lambda: __import__("tkinter"))
        self.ffmpeg: str | None = None

    def checks(self, capture_test: bool = False) -> list[Check]:
        out = [self.check_python(), self.check_config(), self.check_data_dir()]
        out.append(self.check_ffmpeg())
        if self.ffmpeg:
            out += self.check_capabilities()
            out.append(self.check_mic())
        out.append(self.check_storage())
        out.append(self.check_tk())
        if capture_test and self.ffmpeg:
            out.append(self.check_capture())
        return out

    # -- individual checks -----------------------------------------------------

    def check_python(self) -> Check:
        v = f"{self.py[0]}.{self.py[1]}.{self.py[2]}"
        ok = tuple(self.py[:2]) >= (3, 12)
        return Check("python", ok, f"{v} at {sys.executable}", None if ok else WINGET_PYTHON)

    def check_config(self) -> Check:
        p = paths.config_path(self.env)
        detail = f"{p} ({'loaded' if self.cfg.source != 'defaults' else 'absent; built-in defaults in use'})"
        return Check("config", True, detail)

    def check_data_dir(self) -> Check:
        d = paths.data_dir(self.env)
        parent = d if d.exists() else d.parent
        ok = parent.exists() and os.access(parent, os.W_OK)
        return Check("data dir", ok, f"{d} (logs, state, installed app)",
                     None if ok else f"make {parent} writable")

    def check_ffmpeg(self) -> Check:
        path = self.cfg.ffmpeg.path
        found = path if os.path.isabs(path) and os.path.exists(path) else self.which(path)
        if not found:
            return Check("ffmpeg", False, f"{path!r} not found on PATH",
                         f"{WINGET_FFMPEG}  (then open a new terminal, or `wsl --shutdown` so interop sees PATH)")
        rc, so, se = self.run([found, "-hide_banner", "-version"], 15)
        first = (so or se).strip().splitlines()[0] if (so or se).strip() else f"exit {rc}"
        if rc != 0:
            return Check("ffmpeg", False, f"{found} failed to run: {first}", WINGET_FFMPEG)
        self.ffmpeg = found
        return Check("ffmpeg", True, f"{first} ({found})")

    def check_capabilities(self) -> list[Check]:
        f = self.ffmpeg
        _, filters, fe = self.run([f, "-hide_banner", "-filters"], 15)
        _, devices, de = self.run([f, "-hide_banner", "-devices"], 15)
        _, encoders, ee = self.run([f, "-hide_banner", "-encoders"], 15)
        filters, devices, encoders = filters + fe, devices + de, encoders + ee
        out = [
            Check("input ddagrab", _listed("ddagrab", filters), "Desktop Duplication screen grabber",
                  f"{WINGET_FFMPEG} (needs a full build with ddagrab)"),
            Check("input dshow", _listed("dshow", devices), "DirectShow microphone capture", WINGET_FFMPEG),
        ]
        order = [self.cfg.video.pipeline] + [p for p in self.cfg.video.fallback if p != self.cfg.video.pipeline]
        for i, p in enumerate(order):
            needs = PIPELINE_NEEDS[p]
            missing = [e for e in needs["encoders"] if not _listed(e, encoders)]
            missing += [x for x in needs["filters"] if not _listed(x, filters)]
            role = "pipeline" if i == 0 else "fallback"
            ok = not missing
            out.append(Check(f"{role} {p}", ok,
                             "encoder " + ", ".join(needs["encoders"]) + " + " + ", ".join(needs["filters"])
                             + ("" if ok else f" — missing: {', '.join(missing)}"),
                             WINGET_FFMPEG, warn_only=(i > 0)))
        return out

    def check_mic(self) -> Check:
        a = self.cfg.audio
        if not a.enabled:
            return Check("microphone", True, "audio disabled in config (audio.enabled = false)")
        rc, so, se = self.run(ffmpeg_cmd.build_list_devices_argv(self.ffmpeg), 15)
        devices = parse_dshow_audio(so + se)
        names = {n for n, _ in devices} | {alt for _, alt in devices if alt}
        listing = "; ".join(f"{n!r} (alt {alt})" if alt else repr(n) for n, alt in devices) or "none"
        if a.device in names:
            return Check("microphone", True, f"{a.device!r} is listed by dshow")
        fix = ("set [audio] device in the config to one of the names listed, or audio.enabled = false"
               if devices else "connect/enable a microphone in Windows sound settings")
        return Check("microphone", False, f"{a.device!r} not among dshow audio devices: {listing}", fix)

    def check_storage(self) -> Check:
        root = Path(self.cfg.root)
        probe = root if root.exists() else next((p for p in root.parents if p.exists()), None)
        if probe is None:
            return Check("storage root", False, f"{root}: no existing parent", "set `root` in the config")
        writable = os.access(probe, os.W_OK)
        try:
            free_gb = shutil.disk_usage(probe).free / 1e9
            free = f", {free_gb:.0f} GB free"
        except OSError:
            free_gb, free = None, ""
        state = "exists" if root.exists() else f"will be created under {probe}"
        ok = writable and (free_gb is None or free_gb > 2)
        return Check("storage root", ok, f"{root} ({state}{free})",
                     None if ok else f"make {probe} writable / free space, or set `root` in the config")

    def check_tk(self) -> Check:
        try:
            tk = self.tk_import()
            return Check("flash (tkinter)", True, f"Tk {getattr(tk, 'TkVersion', '?')}")
        except Exception as exc:
            return Check("flash (tkinter)", False, f"tkinter unavailable: {exc!r}",
                         f"{WINGET_PYTHON} (the python.org build includes tkinter), "
                         f"or set [flash] enabled = false", warn_only=True)

    def check_capture(self) -> Check:
        spec = ffmpeg_cmd.spec_from_config(self.cfg, ffmpeg=self.ffmpeg, output="-", output_format="null",
                                           duration_s=2)
        argv = ffmpeg_cmd.build_capture_argv(spec)
        argv.remove("-n")
        rc, so, se = self.run(argv, 30)
        tail = [l for l in (so + se).replace("\r", "").splitlines() if l.strip()][-3:]
        return Check(f"capture test ({spec.pipeline}{' + mic' if spec.audio_device else ''})", rc == 0,
                     f"2 s into the null muxer, exit {rc}" + ("" if rc == 0 else ": " + " | ".join(tail)),
                     "try --encoder qsv-download or x264 on `peep rec`, and see the log")


def render(checks: list[Check]) -> tuple[str, int]:
    """Report text and exit code (1 if any non-warn check failed)."""
    lines = [c.render() for c in checks]
    failed = [c for c in checks if not c.ok and not c.warn_only]
    lines.append(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed"
                 + ("" if not failed else f"; {len(failed)} need attention"))
    return "\n".join(lines), 1 if failed else 0
