"""The WSL `peep` shim: dispatch, path translation, install/sync, and the
rec key handling against a real child process (no interop involved)."""

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import WIN, TempDirMixin, load_shim

shim = load_shim()
CFG = {"python": "python.exe", "app_win": r"C:\Users\chord\AppData\Local\peep\app",
       "app_wsl": "/mnt/c/Users/chord/AppData/Local/peep/app"}


def completed(stdout="", rc=0, stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


class DispatchTest(TempDirMixin, unittest.TestCase):
    def run_main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = shim.main(list(argv), env=env or {"HOME": str(self.tmp)})
        return code, out.getvalue(), err.getvalue()

    def test_help_and_unknown(self):
        self.assertEqual(self.run_main("help")[0], 0)
        self.assertEqual(self.run_main()[0], 2)
        code, _, err = self.run_main("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("unknown command", err)

    def test_windows_argv(self):
        self.assertEqual(shim.windows_argv(CFG, "rename", ["last", "my demo"]),
                         ["python.exe", "-X", "utf8", CFG["app_win"] + "\\peepw.py", "rename", "last", "my demo"])
        with self.assertRaises(shim.ShimError):
            shim.windows_argv(CFG, "install", [])

    def test_passthrough_commands_reach_windows_verbatim(self):
        cfg = dict(CFG, app_wsl=str(self.tmp))
        for argv in (["ls", "--collection", "bale"], ["stop"], ["open", "last", "--folder"],
                     ["rename", "last", "Bale: pack demo"], ["paths"], ["config", "--init"],
                     ["mark", "--label", "step 2"], ["agent", "status"], ["agent", "install"],
                     ["agent", "restart"]):
            with self.subTest(argv=argv), \
                    mock.patch.object(shim, "load_shim_config", return_value=cfg), \
                    mock.patch.object(shim, "autosync"), \
                    mock.patch.object(shim.subprocess, "call", return_value=0) as call:
                self.assertEqual(self.run_main(*argv)[0], 0)
                sent = call.call_args.args[0]
                self.assertEqual(sent[:4], ["python.exe", "-X", "utf8", CFG["app_win"] + "\\peepw.py"])
                self.assertEqual(sent[4:], argv)
                self.assertEqual(call.call_args.kwargs["cwd"], str(self.tmp))

    def test_rec_goes_through_key_handler(self):
        with mock.patch.object(shim, "load_shim_config", return_value=CFG), \
                mock.patch.object(shim, "autosync"), \
                mock.patch.object(shim, "run_rec", return_value=0) as rr:
            self.assertEqual(self.run_main("rec", "demo", "--collection", "bale")[0], 0)
        self.assertEqual(rr.call_args.args[0][4:], ["rec", "demo", "--collection", "bale"])

    def test_windows_exit_code_is_propagated(self):
        with mock.patch.object(shim, "load_shim_config", return_value=CFG), \
                mock.patch.object(shim, "autosync"), \
                mock.patch.object(shim.subprocess, "call", return_value=1):
            self.assertEqual(self.run_main("stop")[0], 1)

    def test_shim_errors_are_one_line(self):
        with mock.patch.object(shim, "load_shim_config", side_effect=shim.ShimError("python.exe not found")):
            code, _, err = self.run_main("ls")
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "peep: error: python.exe not found")


class PathsAndConfigTest(TempDirMixin, unittest.TestCase):
    def test_win_to_wsl(self):
        run = mock.Mock(return_value=completed("/mnt/c/Users/chord/AppData/Local/peep/app\n"))
        self.assertEqual(shim.win_to_wsl(CFG["app_win"], run), CFG["app_wsl"])
        run = mock.Mock(side_effect=FileNotFoundError("wslpath"))
        self.assertEqual(shim.win_to_wsl(CFG["app_win"], run), CFG["app_wsl"])    # manual fallback
        with self.assertRaises(shim.ShimError):
            shim.win_to_wsl("relative\\path", mock.Mock(side_effect=FileNotFoundError()))

    def test_discover_localappdata(self):
        ok = mock.Mock(return_value=completed("C:\\Users\\chord\\AppData\\Local\r\n"))
        self.assertEqual(shim.discover_localappdata("python.exe", ok), "C:\\Users\\chord\\AppData\\Local")
        with self.assertRaisesRegex(shim.ShimError, "winget install Python.Python.3.12"):
            shim.discover_localappdata("python.exe", mock.Mock(side_effect=FileNotFoundError()))
        stub = mock.Mock(return_value=completed("", 9009, "Python was not found; run without arguments to install"))
        with self.assertRaisesRegex(shim.ShimError, "Store stub"):
            shim.discover_localappdata("python.exe", stub)

    def test_shim_config_is_discovered_once_then_cached(self):
        env = {"HOME": str(self.tmp)}
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[0] == "wslpath":
                return completed(CFG["app_wsl"] + "\n")
            return completed("C:\\Users\\chord\\AppData\\Local\n")

        with contextlib.redirect_stderr(io.StringIO()):
            first = shim.load_shim_config(env, run)
        self.assertEqual(first, CFG)
        self.assertEqual(json.loads((self.tmp / ".config" / "peep" / "shim.json").read_text()), CFG)
        n = len(calls)
        self.assertEqual(shim.load_shim_config(env, run), CFG)
        self.assertEqual(len(calls), n)                       # cache hit: no interop calls
        with contextlib.redirect_stderr(io.StringIO()):
            other = shim.load_shim_config({**env, "PEEP_PYTHON": "py.exe"}, run)
        self.assertEqual(other["python"], "py.exe")           # different interpreter: rediscovered


class SyncTest(TempDirMixin, unittest.TestCase):
    def test_source_files_cover_the_package(self):
        files = shim.source_files(WIN)
        self.assertIn("peepw.py", files)
        for mod in ("cli", "recorder", "ffmpeg_cmd", "catalog", "naming", "config", "flash", "winapi"):
            self.assertIn(f"peep/{mod}.py", files)
        self.assertFalse(any("__pycache__" in k for k in files))

    def test_sync_copies_then_is_idempotent_and_prunes(self):
        app = self.tmp / "app"
        self.assertTrue(shim.needs_sync(app, WIN))
        copied = shim.sync(app, WIN)
        self.assertIn("peep/cli.py", copied)
        self.assertEqual((app / "peep" / "cli.py").read_bytes(), (WIN / "peep" / "cli.py").read_bytes())
        self.assertFalse(shim.needs_sync(app, WIN))
        self.assertEqual(shim.sync(app, WIN), [])
        (app / "peep" / "old_module.py").write_text("x")       # a module since deleted from the repo
        (app / "config-notes.txt").write_text("keep me")       # anything else in app/ is not ours
        removed = shim.sync(app, WIN)
        self.assertEqual(removed, ["(removed) peep/old_module.py"])
        self.assertTrue((app / "config-notes.txt").exists())
        marker = json.loads((app / shim.APP_MARKER).read_text())
        self.assertEqual(marker["files"], shim.source_files(WIN))

    def test_marker_names_the_wsl_distro_for_the_agents_staleness_check(self):
        app = self.tmp / "app"
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}):
            shim.sync(app, WIN)
        marker = json.loads((app / shim.APP_MARKER).read_text())
        self.assertEqual(marker["wsl_distro"], "Ubuntu")
        self.assertEqual(marker["source"], str(WIN))
        for mod in ("agent", "agentcli", "hotkeys", "pill", "dialog", "ui", "lifecycle"):
            self.assertIn(f"peep/{mod}.py", marker["files"])          # B's modules travel with the sync

    def test_modified_or_deleted_installed_file_triggers_resync(self):
        app = self.tmp / "app"
        shim.sync(app, WIN)
        (app / "peep" / "naming.py").unlink()
        self.assertTrue(shim.needs_sync(app, WIN))
        self.assertEqual(shim.sync(app, WIN), ["peep/naming.py"])

    def test_autosync_can_be_disabled(self):
        cfg = dict(CFG, app_wsl=str(self.tmp / "app"))
        shim.autosync(cfg, {"PEEP_NO_AUTOSYNC": "1"})
        self.assertFalse((self.tmp / "app").exists())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            shim.autosync(cfg, {})
        self.assertIn("updated the Windows copy", err.getvalue())

    def test_link_self(self):
        bin_dir = self.tmp / "bin"
        self.assertIn("linked", shim.link_self(bin_dir))
        self.assertEqual((bin_dir / "peep").resolve(), Path(os.path.realpath(shim.__file__)))
        self.assertIn("already points here", shim.link_self(bin_dir))
        (bin_dir / "peep").unlink()
        (bin_dir / "peep").write_text("someone else's peep")
        self.assertIn("left alone", shim.link_self(bin_dir))
        self.assertIn("linked", shim.link_self(bin_dir, force=True))


CHILD = ("import sys; line = sys.stdin.readline(); "
         "print('child got', repr(line), flush=True); sys.exit(7)")


class RecKeysTest(unittest.TestCase):
    def run_rec_with(self, keys: bytes, close: bool):
        r, w = os.pipe()
        os.write(w, keys)
        if close:
            os.close(w)
        out = []

        def popen(argv, **kw):
            kw["stdout"] = subprocess.PIPE
            p = subprocess.Popen(argv, **kw)
            out.append(p)
            return p

        try:
            code = shim.run_rec([sys.executable, "-c", CHILD], None, stdin_fd=r, popen=popen)
        finally:
            os.close(r)
            if not close:
                os.close(w)
        text = out[0].stdout.read().decode()
        out[0].stdout.close()
        return code, text

    def test_q_sends_one_stop_line_and_returns_child_code(self):
        code, text = self.run_rec_with(b"xyq", close=False)
        self.assertEqual(code, 7)
        self.assertIn("child got 'stop\\n'", text)

    def test_enter_stops(self):
        self.assertIn("'stop\\n'", self.run_rec_with(b"\r", close=False)[1])

    def test_eof_on_our_stdin_stops_the_child(self):
        code, text = self.run_rec_with(b"", close=True)
        self.assertEqual(code, 7)
        self.assertIn("'stop\\n'", text)

    def test_terminal_hangup_read_error_stops_the_child(self):
        # A directory fd: select() says readable, read() raises OSError (EISDIR) —
        # the same path as EIO from a tty whose tab was closed mid-recording.
        fd = os.open(os.path.dirname(__file__), os.O_RDONLY)
        out = []

        def popen(argv, **kw):
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, **kw)
            out.append(p)
            return p

        try:
            code = shim.run_rec([sys.executable, "-c", CHILD], None, stdin_fd=fd, popen=popen)
        finally:
            os.close(fd)
        self.assertEqual(code, 7)
        self.assertIn("'stop\\n'", out[0].stdout.read().decode())
        out[0].stdout.close()

    def test_stop_keys(self):
        for k in (b"q", b"Q", b"\n", b"\r"):
            self.assertTrue(shim.is_stop_key(k))
        for k in (b"x", b" ", b"\x1b"):
            self.assertFalse(shim.is_stop_key(k))


class ExternalStopThroughShimTest(TempDirMixin, unittest.TestCase):
    """Session A.2, end to end on the WSL side: run_rec drives a real `rec` child
    (cli.main on this interpreter, fake recorder) whose recording is stopped from
    outside — `peep stop`, or the agent's hotkey on a terminal recording. No key is
    pressed and our stdin stays open, so the shim sends nothing and keeps the
    child's stdin open until it exits. The child must exit 0 with empty stderr."""

    def test_external_stop_exits_cleanly(self):
        from tests.test_cli import rec_child_argv, rec_child_env, stop_externally_when_recording
        flag = self.tmp / "stop-request.flag"
        r, w = os.pipe()                          # our terminal: open, silent
        procs, out = [], []

        def popen(argv, **kw):
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=rec_child_env(self.tmp), **kw)
            procs.append(p)
            stop_externally_when_recording(p, flag, out)
            return p

        try:
            code = shim.run_rec(rec_child_argv(flag), None, stdin_fd=r, popen=popen)
        finally:
            os.close(r)
            os.close(w)
        err = procs[0].stderr.read()
        procs[0].stderr.close()
        procs[0].stdout.close()
        self.assertTrue(procs[0].stdin.closed)    # run_rec closed it after the exit
        self.assertIn("stopped by external request", out)
        self.assertEqual(err, b"", err.decode("utf-8", "replace"))
        self.assertEqual(code, 0)


class ShimDoctorTest(TempDirMixin, unittest.TestCase):
    def test_reports_python_and_copy(self):
        env = {"HOME": str(self.tmp), "PATH": f"/usr/bin:{self.tmp}/.local/bin"}
        app = self.tmp / "app"

        def run(argv, **kw):
            return completed("Python 3.12.10\n")

        with mock.patch.object(shim, "load_shim_config", return_value=dict(CFG, app_wsl=str(app))):
            lines, cfg = shim.shim_checks(env, run)
        text = "\n".join(lines)
        self.assertIn("[ok  ] windows python: Python 3.12.10", text)
        self.assertIn("out of date", text)
        self.assertIn("[ok  ] shim on PATH", text)
        self.assertIsNotNone(cfg)

    def test_store_stub_is_a_failure_with_fix(self):
        run = mock.Mock(return_value=completed("", 9009, "Python was not found"))
        lines, cfg = shim.shim_checks({"HOME": str(self.tmp)}, run)
        self.assertIn("FAIL", lines[0])
        self.assertIn("winget install Python.Python.3.12", lines[0])


if __name__ == "__main__":
    unittest.main()
