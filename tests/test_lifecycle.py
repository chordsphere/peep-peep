"""Startup-entry path construction and the shortcut script, the detached
spawn, and the installed-copy staleness check (all with fakes: no
PowerShell, no wsl.exe, no \\\\wsl.localhost)."""

import json
import subprocess
import unittest
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest import mock

from tests import WIN, TempDirMixin
from peep import lifecycle as lc

PY = r"C:\Users\chord\AppData\Local\Programs\Python\Python312\python.exe"
APP = PureWindowsPath(r"C:\Users\chord\AppData\Local\peep\app")
STARTUP = r"C:\Users\chord\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"


class StartupEntryTest(unittest.TestCase):
    def test_startup_dir_prefers_the_known_folder(self):
        self.assertEqual(lc.startup_dir({}, known_folder=lambda: r"D:\Redirected\Startup"),
                         PureWindowsPath(r"D:\Redirected\Startup"))

    def test_startup_dir_falls_back_to_appdata(self):
        env = {"APPDATA": r"C:\Users\chord\AppData\Roaming"}
        self.assertEqual(lc.startup_dir(env, known_folder=lambda: None), PureWindowsPath(STARTUP))
        with self.assertRaisesRegex(lc.LifecycleError, "Startup folder"):
            lc.startup_dir({}, known_folder=lambda: None)

    def test_agent_launch_is_pythonw_peepw_agent_run(self):
        target, args, cwd = lc.agent_launch(APP, PY)
        self.assertEqual(target, r"C:\Users\chord\AppData\Local\Programs\Python\Python312\pythonw.exe")
        self.assertEqual(args, [r"C:\Users\chord\AppData\Local\peep\app\peepw.py", "agent", "run"])
        self.assertEqual(cwd, str(APP))

    def test_ps_quote_neutralises_quotes_and_dollars(self):
        self.assertEqual(lc.ps_quote("it's $HOME"), "'it''s $HOME'")

    def test_shortcut_script(self):
        script = lc.shortcut_script(STARTUP + r"\peep agent.lnk", r"C:\Py\pythonw.exe",
                                    [r"C:\Users\O'Neil\app\peepw.py", "agent", "run"], r"C:\app", "desc")
        self.assertIn("CreateShortcut('" + STARTUP + r"\peep agent.lnk')", script)
        self.assertIn(r"$s.TargetPath='C:\Py\pythonw.exe'", script)
        self.assertIn(r"""$s.Arguments='C:\Users\O''Neil\app\peepw.py agent run'""", script)
        self.assertIn("$s.Save()", script)
        self.assertTrue(script.startswith("$ErrorActionPreference='Stop'"))

    def test_install_runs_powershell_and_reports_failure(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="ok\r\n", stderr="")

        link = lc.install_startup_entry(PureWindowsPath(STARTUP), APP, PY, run=run, exists=lambda p: True)
        self.assertEqual(link, STARTUP + r"\peep agent.lnk")
        self.assertEqual(calls[0][:3], ["powershell.exe", "-NoProfile", "-NonInteractive"])
        self.assertIn("agent run", calls[0][-1])
        fail = lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="Access denied")
        with self.assertRaisesRegex(lc.LifecycleError, "Access denied"):
            lc.install_startup_entry(PureWindowsPath(STARTUP), APP, PY, run=fail, exists=lambda p: True)
        with self.assertRaisesRegex(lc.LifecycleError, "winget install Python"):
            lc.install_startup_entry(PureWindowsPath(STARTUP), APP, PY, run=run, exists=lambda p: False)
        boom = lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError("powershell.exe"))
        with self.assertRaisesRegex(lc.LifecycleError, "could not run powershell"):
            lc.install_startup_entry(PureWindowsPath(STARTUP), APP, PY, run=boom, exists=lambda p: True)

    def test_remove(self):
        removed = []
        self.assertEqual(lc.remove_startup_entry(PureWindowsPath(STARTUP), remove=removed.append),
                         STARTUP + r"\peep agent.lnk")

        def missing(p):
            raise FileNotFoundError(p)
        self.assertIsNone(lc.remove_startup_entry(PureWindowsPath(STARTUP), remove=missing))

    def test_spawn_is_detached_with_no_inherited_handles(self):
        seen = {}

        def popen(argv, **kw):
            seen.update(argv=argv, **kw)
            return SimpleNamespace(pid=777)

        self.assertEqual(lc.spawn_agent(Path("/app"), PY, popen=popen, exists=lambda p: True), 777)
        self.assertTrue(seen["argv"][0].endswith("pythonw.exe"))
        self.assertEqual(seen["argv"][-2:], ["agent", "run"])
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)
        self.assertTrue(seen["close_fds"])
        with self.assertRaisesRegex(lc.LifecycleError, "could not start"):
            lc.spawn_agent(Path("/app"), PY, popen=lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
                           exists=lambda p: True)


class StalenessTest(TempDirMixin, unittest.TestCase):
    def test_parse_running_distros(self):
        self.assertEqual(lc.parse_running_distros("Ubuntu\r\ndocker-desktop\r\n".encode("utf-16-le")),
                         ["Ubuntu", "docker-desktop"])
        self.assertEqual(lc.parse_running_distros("\ufeffUbuntu\r\n".encode("utf-16-le")), ["Ubuntu"])
        self.assertEqual(lc.parse_running_distros(b"Ubuntu\n"), ["Ubuntu"])
        self.assertEqual(lc.parse_running_distros(b""), [])

    def test_running_distros(self):
        ok = lambda argv, **kw: SimpleNamespace(returncode=0, stdout="Ubuntu\r\n".encode("utf-16-le"))
        self.assertEqual(lc.running_distros(ok), ["Ubuntu"])
        self.assertEqual(lc.running_distros(lambda argv, **kw: SimpleNamespace(returncode=1, stdout=b"")), [])
        self.assertIsNone(lc.running_distros(lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError())))

    def test_repo_unc(self):
        self.assertEqual(str(lc.repo_unc("Ubuntu", "/home/chordsphere/peep-peep/win")),
                         r"\\wsl.localhost\Ubuntu\home\chordsphere\peep-peep\win")

    def test_package_files_match_the_shims_set(self):
        from tests import load_shim
        self.assertEqual(lc.package_files(WIN), load_shim().source_files(WIN))
        self.assertEqual(lc.code_id(lc.package_files(WIN)), lc.code_id(dict(lc.package_files(WIN))))
        self.assertEqual(len(lc.code_id({})), 12)

    def marker(self, **extra):
        app = self.tmp / "app"
        app.mkdir(exist_ok=True)
        data = {"files": {"peepw.py": "aa", "peep/cli.py": "bb"}, "source": "/home/chordsphere/peep-peep/win",
                **extra}
        (app / "INSTALLED.json").write_text(json.dumps(data))
        return app

    def status(self, app, running=("Ubuntu",), repo_files=None, reachable=True):
        run = lambda argv, **kw: SimpleNamespace(returncode=0, stdout="\r\n".join(running).encode("utf-16-le"))
        files = repo_files if repo_files is not None else {"peepw.py": "aa", "peep/cli.py": "bb"}
        with mock.patch.object(Path, "exists", lambda self: reachable or not str(self).endswith("peepw.py")):
            return lc.install_status(app, run=run, files_at=lambda repo: files)

    def test_status_matrix(self):
        self.assertEqual(lc.install_status(self.tmp / "none")["state"], "unknown")
        self.assertIn("written before session B", self.status(self.marker())["detail"])
        app = self.marker(wsl_distro="Ubuntu")
        down = self.status(app, running=())
        self.assertEqual(down["state"], "unknown")
        self.assertIn("never starts it", down["detail"])
        self.assertEqual(self.status(app)["state"], "current")
        stale = self.status(app, repo_files={"peepw.py": "aa", "peep/cli.py": "CHANGED", "peep/agent.py": "new"})
        self.assertEqual(stale["state"], "stale")
        self.assertEqual(stale["changed"], ["peep/agent.py", "peep/cli.py"])
        self.assertIn("peep agent restart", stale["detail"])
        self.assertIn("not reachable", self.status(app, reachable=False)["detail"])

    def test_status_never_raises(self):
        app = self.marker(wsl_distro="Ubuntu")
        boom = lambda repo: (_ for _ in ()).throw(PermissionError("9P hiccup"))
        run = lambda argv, **kw: SimpleNamespace(returncode=0, stdout=b"Ubuntu\n")
        with mock.patch.object(Path, "exists", lambda self: True), \
                self.assertLogs("peep.lifecycle", "WARNING"):
            self.assertEqual(lc.install_status(app, run=run, files_at=boom)["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
