"""Test suite for peep-peep. Runs on the WSL side with no ffmpeg.exe, no
display and no Windows interop: the process boundary is either mocked or
crossed with `fake_ffmpeg.py`, a stand-in that speaks just enough of
ffmpeg's stderr and stdin protocol.

Tests are unittest.TestCase classes so they run under pytest
(`python3 -m pytest`) and, where pytest is not installed, under the
standard library (`python3 -m unittest discover -s tests -t .`).

Importing this package puts `win/` on sys.path (the Windows package) and
offers `load_shim()` for the extension-less `bin/peep`.
"""

import importlib.machinery
import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WIN = REPO / "win"
SHIM = REPO / "bin" / "peep"
FAKE_FFMPEG = Path(__file__).resolve().parent / "fake_ffmpeg.py"

if str(WIN) not in sys.path:
    sys.path.insert(0, str(WIN))


def load_shim():
    loader = importlib.machinery.SourceFileLoader("peep_shim", str(SHIM))
    spec = importlib.util.spec_from_loader("peep_shim", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def make_fake_ffmpeg(directory: Path) -> Path:
    """An executable `ffmpeg` in `directory` that runs fake_ffmpeg.py."""
    exe = Path(directory) / "ffmpeg"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_FFMPEG}" "$@"\n', encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


class TempDirMixin:
    """self.tmp: a fresh Path per test, removed afterwards."""

    def setUp(self):
        super().setUp()
        self._td = tempfile.TemporaryDirectory(prefix="peep-test-")
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()
        super().tearDown()
