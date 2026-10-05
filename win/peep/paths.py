"""Where peep keeps its own files (not the recordings — those live under
the configurable storage root, see `config.Config.root`).

Layout of the data directory, `%LOCALAPPDATA%\\peep` by default:

    config.toml          optional; defaults apply when absent
    logs/peep.log        rotating structured log (logsetup)
    state/active.json    the live recording, if any (control)
    state/stop-request   written by `peep stop`, consumed by the recorder
    app/                 the installed copy of this package (WSL shim `peep install`)

`PEEP_HOME` overrides the data directory; tests and a portable setup
use it. The function never creates anything — callers that write make
the directories they need.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "PEEP_HOME"
ENV_CONFIG = "PEEP_CONFIG"


def data_dir(environ: dict[str, str] | None = None) -> Path:
    """The peep data directory: $PEEP_HOME, else %LOCALAPPDATA%\\peep,
    else ~/.local/share/peep (non-Windows, used only by tests/dev)."""
    env = os.environ if environ is None else environ
    if env.get(ENV_HOME):
        return Path(env[ENV_HOME])
    if env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"]) / "peep"
    return Path.home() / ".local" / "share" / "peep"


def config_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(ENV_CONFIG):
        return Path(env[ENV_CONFIG])
    return data_dir(env) / "config.toml"


def logs_dir(environ: dict[str, str] | None = None) -> Path:
    return data_dir(environ) / "logs"


def state_dir(environ: dict[str, str] | None = None) -> Path:
    return data_dir(environ) / "state"
