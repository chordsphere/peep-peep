"""`peep agent ...`: the resident agent's lifecycle from a terminal (WSL
through the shim, or any Windows console).

  agent run                 the agent itself, in the foreground (the Startup
                            shortcut runs this under pythonw.exe)
  agent start [--wait S]    launch it detached; report once it is ready
  agent stop [--timeout S]  ask it to exit (a recording in progress is
                            stopped and saved first; no dialog)
  agent restart             stop, then start: picks up code the shim just synced
  agent status [--json]     running?, hotkeys, pill, config, installed-copy state
  agent reload              re-read config.toml now (it also notices edits itself)
  agent install [--no-start]  Startup-folder shortcut (+ start it now)
  agent uninstall           stop it and remove the shortcut

Exit codes as the rest of the CLI: 0 ok, 1 failure (message on stderr).
`status` exits 1 when the agent is not running, so scripts can test it.
"""

from __future__ import annotations

import json
import logging
import sys

from . import lifecycle, paths
from .control import AgentControl, Control
from .logsetup import event

log = logging.getLogger("peep.agentcli")

VERBS = ("run", "start", "stop", "restart", "status", "reload", "install", "uninstall")


def add_parser(sub) -> None:
    a = sub.add_parser("agent", help="the resident hotkey agent: run/start/stop/restart/status/reload/install/uninstall")
    verbs = a.add_subparsers(dest="agent_command", required=True, metavar="VERB")
    verbs.add_parser("run", help="run the agent in the foreground (what the Startup shortcut does)")
    s = verbs.add_parser("start", help="start the agent detached")
    s.add_argument("--wait", type=float, default=15.0, help="seconds to wait for it to report ready")
    st = verbs.add_parser("stop", help="stop the agent (a running recording is saved first)")
    st.add_argument("--timeout", type=float, default=90.0)
    r = verbs.add_parser("restart", help="stop then start (loads freshly synced code)")
    r.add_argument("--wait", type=float, default=15.0)
    r.add_argument("--timeout", type=float, default=90.0)
    stat = verbs.add_parser("status", help="is it running, and how is it doing")
    stat.add_argument("--json", action="store_true")
    verbs.add_parser("reload", help="re-read config.toml now")
    i = verbs.add_parser("install", help="add the Startup-folder shortcut (and start the agent)")
    i.add_argument("--no-start", action="store_true")
    verbs.add_parser("uninstall", help="stop the agent and remove the Startup-folder shortcut")


def _out(msg: str = "") -> None:
    from .cli import _out as out
    out(msg)


def _err(msg: str) -> None:
    from .cli import _err as err
    err(msg)


def _agent_control() -> AgentControl:
    from . import winapi
    return AgentControl(paths.state_dir(), winapi.pid_alive)


def _disk_code() -> str | None:
    marker = lifecycle.read_marker(lifecycle.app_dir())
    return lifecycle.code_id(marker["files"]) if marker and marker.get("files") else None


def _link_path() -> str | None:
    try:
        return str(lifecycle.startup_dir() / lifecycle.LINK_NAME)
    except lifecycle.LifecycleError:
        return None


def status_lines(info: dict | None, disk_code: str | None, link: str | None, link_exists: bool,
                 recording: dict | None) -> list[str]:
    """The human-readable `peep agent status` (pure, so it is tested)."""
    lines = []
    if info is None:
        lines.append("agent: not running  (start it: peep agent start)")
    else:
        lines.append(f"agent: running, pid {info.get('pid')} since {str(info.get('since', '?'))[:19]}"
                     f" ({info.get('state', '?')}), peep {info.get('version', '?')}")
        for action, state in sorted((info.get("hotkeys") or {}).items()):
            lines.append(f"  hotkey {action:<8} {state}")
        lines.append(f"  pill            {info.get('pill', '?')}")
        cfg_line = f"  config          {info.get('config', '?')}"
        if info.get("config_loaded_at"):
            cfg_line += f" (loaded {str(info['config_loaded_at'])[:19]})"
        lines.append(cfg_line)
        if info.get("config_error"):
            lines.append(f"  config ERROR    {info['config_error']}  (the previous config is still in use)")
        inst = info.get("install") or {}
        lines.append(f"  installed copy  {inst.get('state', '?')}: {inst.get('detail', '')}".rstrip())
        if disk_code and info.get("code") and disk_code != info.get("code"):
            lines.append("  code            the running agent is older than the installed copy: peep agent restart")
    if recording is not None:
        lines.append(f"recording: {recording.get('status', 'recording')} → {recording.get('final')}"
                     f" (started by {recording.get('origin', 'terminal')})")
    if link:
        lines.append(f"startup entry: {'present' if link_exists else 'absent'}  {link}"
                     + ("" if link_exists else "  (peep agent install)"))
    return lines


# -- verbs ------------------------------------------------------------------------


def cmd_run(args, cfg) -> int:
    from .agent import run_agent
    return run_agent(cfg)


def cmd_start(args, cfg) -> int:
    actl = _agent_control()
    info = actl.live()
    if info is not None:
        _out(f"the agent is already running (pid {info.get('pid')})")
        disk = _disk_code()
        if disk and info.get("code") and disk != info.get("code"):
            _out("  it is running older code than the installed copy; `peep agent restart` loads the new code")
        return 0
    pid = lifecycle.spawn_agent(lifecycle.app_dir(), sys.executable)
    event(log, logging.INFO, "agentcli.started", pid=pid)
    ready = actl.wait_for(lambda i: i.get("state") == "ready", args.wait)
    if ready is None:
        _err(f"the agent (pid {pid}) did not report ready within {args.wait:g}s; "
             f"see {paths.logs_dir() / 'agent.log'}")
        return 1
    _out(f"agent started (pid {ready.get('pid')})")
    for line in status_lines(ready, _disk_code(), None, False, None)[1:]:
        _out(line)
    return 0


def cmd_stop(args, cfg) -> int:
    actl = _agent_control()
    try:
        info = actl.send("stop")
    except LookupError as exc:
        _out(str(exc))
        return 0
    pid = int(info["pid"])
    recording = Control(paths.state_dir(), actl.pid_alive).live_recording()
    if recording is not None and recording.get("pid") == pid:
        _out("a recording is in progress; it is being stopped and saved first")
    if not actl.wait_gone(pid, args.timeout):
        _err(f"the agent (pid {pid}) is still running after {args.timeout:g}s; see {paths.logs_dir() / 'agent.log'}")
        return 1
    _out(f"agent stopped (pid {pid})")
    return 0


def cmd_restart(args, cfg) -> int:
    code = cmd_stop(args, cfg)
    return code if code else cmd_start(args, cfg)


def cmd_status(args, cfg) -> int:
    actl = _agent_control()
    info = actl.live()
    recording = Control(paths.state_dir(), actl.pid_alive).live_recording()
    link = _link_path()
    link_exists = bool(link) and lifecycle.exists(link)
    if args.json:
        _out(json.dumps({"agent": info, "recording": recording, "startup_entry": link,
                         "startup_entry_exists": link_exists, "installed_code": _disk_code()},
                        indent=2, ensure_ascii=False))
    else:
        for line in status_lines(info, _disk_code(), link, link_exists, recording):
            _out(line)
    return 0 if info is not None else 1


def cmd_reload(args, cfg) -> int:
    actl = _agent_control()
    try:
        before = actl.send("reload")
    except LookupError as exc:
        _err(str(exc))
        return 1
    after = actl.wait_for(lambda i: i.get("config_loaded_at") != before.get("config_loaded_at")
                          or i.get("config_error"), 5.0)
    if after is None:
        _err("the agent did not acknowledge the reload within 5s; see the agent log")
        return 1
    if after.get("config_error"):
        _err(f"config not reloaded: {after['config_error']}")
        return 1
    _out(f"config reloaded ({after.get('config')})")
    return 0


def cmd_install(args, cfg) -> int:
    link = lifecycle.install_startup_entry(lifecycle.startup_dir(), lifecycle.app_dir(), sys.executable)
    _out(f"startup entry: {link}")
    ag = cfg.agent
    _out("  the agent will start at every login (hotkeys: "
         f"{ag.record_hotkey} record/stop, {ag.mark_hotkey} mark, {ag.discard_hotkey} discard, "
         f"{ag.pause_hotkey} pause/resume, {ag.take_hotkey} take, {ag.retake_hotkey} retake)")
    if args.no_start:
        return 0
    args.wait = 15.0
    return cmd_start(args, cfg)


def cmd_uninstall(args, cfg) -> int:
    args.timeout = 90.0
    code = cmd_stop(args, cfg)
    removed = lifecycle.remove_startup_entry(lifecycle.startup_dir())
    _out(f"removed {removed}" if removed else "no startup entry to remove")
    return code


VERB_COMMANDS = {"run": cmd_run, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
                 "status": cmd_status, "reload": cmd_reload, "install": cmd_install, "uninstall": cmd_uninstall}


def cmd_agent(args, cfg) -> int:
    try:
        return VERB_COMMANDS[args.agent_command](args, cfg)
    except lifecycle.LifecycleError as exc:
        event(log, logging.ERROR, "agentcli.lifecycle_error", verb=args.agent_command, error=str(exc))
        _err(str(exc))
        return 1


def log_file_for(args) -> str:
    """The resident agent logs to agent.log; every other command to peep.log."""
    from .logsetup import AGENT_LOG_FILE_NAME, LOG_FILE_NAME
    if getattr(args, "command", None) == "agent" and getattr(args, "agent_command", None) == "run":
        return AGENT_LOG_FILE_NAME
    return LOG_FILE_NAME

