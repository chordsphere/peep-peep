"""Configuration: built-in defaults that work on the architect's laptop
unchanged, optionally overridden by a TOML file (`paths.config_path()`,
`%LOCALAPPDATA%\\peep\\config.toml` by default).

Unknown keys and wrong types are errors, not warnings: a typo in a
config key that silently falls back to a default is exactly the silent
skip AGENT.md calls a bug.

Sections:
  1. Defaults + dataclasses     (~line 26)
  2. Loading + validation       (~line 133)
  3. Template                   (~line 239)
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from . import paths

# ---------------------------------------------------------------------------
# 1. Defaults + dataclasses
# ---------------------------------------------------------------------------

# The dshow device name established by the session-A probe (2026-10-05):
# ffmpeg lists exactly this one audio capture device on the laptop. Its
# "alternative name" (@device_cm_{33D9A762-...}\wave_{85A9C569-...}) also
# opened; `peep doctor` prints both so the config can switch to the
# stable-but-opaque form if Windows renumbers the "(6- ...)" prefix.
DEFAULT_MIC = "Microphone Array on SoundWire Device (6- Realtek XU)"

PIPELINES = ("qsv", "qsv-download", "x264")
CONTAINERS = ("mp4", "mkv")
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SCALE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_WSL_UNC = re.compile(r"^[\\/]{2,}(wsl\.localhost|wsl\$)[\\/]", re.I)


def default_root(environ: dict[str, str] | None = None) -> str:
    """`%USERPROFILE%/Videos/peep` — C:/Users/chord/Videos/peep on the laptop."""
    env = os.environ if environ is None else environ
    profile = env.get("USERPROFILE")
    if profile:
        return profile.replace("\\", "/").rstrip("/") + "/Videos/peep"
    return "C:/Users/chord/Videos/peep"


@dataclass(frozen=True)
class VideoConfig:
    fps: int = 30
    pipeline: str = "qsv"                # qsv | qsv-download | x264 (see ffmpeg_cmd.filter_graph)
    fallback: tuple[str, ...] = ("qsv-download", "x264")   # tried in order if the pipeline fails to start
    scale: str = ""                      # "" = native (2560x1600 here), or "WxH", e.g. "1920x1200"
    output_idx: int = 0                  # ddagrab output (monitor) index
    draw_mouse: bool = True
    qsv_preset: str = "medium"
    qsv_global_quality: int = 25
    x264_preset: str = "veryfast"
    x264_crf: int = 20
    gop_seconds: int = 2


@dataclass(frozen=True)
class AudioConfig:
    enabled: bool = True
    device: str = DEFAULT_MIC
    bitrate: str = "160k"
    buffer_ms: int = 50                  # dshow audio_buffer_size; its default (500 ms) adds latency
    offset_ms: int = 0                   # -itsoffset on the mic input; tune after the sync smoke test


@dataclass(frozen=True)
class FlashConfig:
    enabled: bool = True
    start_color: str = "#FF00FF"         # magenta
    stop_color: str = "#00FF00"          # green: magenta's complement, measured exact through the pipeline
    duration_ms: int = 150               # measured: 4-5 frames at 30 fps
    settle_ms: int = 400                 # keep recording this long after the stop flash before sending q
    lead_ms: int = 300                   # wait after ffmpeg reports ready before the start flash


@dataclass(frozen=True)
class OutputConfig:
    container: str = "mp4"               # mp4 (remuxed from the Matroska capture on stop) | mkv


@dataclass(frozen=True)
class FfmpegConfig:
    path: str = "ffmpeg"                 # resolved on PATH unless absolute
    startup_timeout_s: float = 10.0
    stop_timeout_s: float = 20.0


@dataclass(frozen=True)
class Config:
    root: str = field(default_factory=default_root)
    default_collection: str = "inbox"
    log_level: str = "INFO"
    video: VideoConfig = field(default_factory=VideoConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    flash: FlashConfig = field(default_factory=FlashConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    ffmpeg: FfmpegConfig = field(default_factory=FfmpegConfig)
    source: str = "defaults"             # where this config came from (path or "defaults"); not a TOML key

    def root_path(self) -> Path:
        return Path(self.root)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("source")
        return d


def parse_scale(value: str) -> tuple[int, int] | None:
    """"" / "native" -> None; "1920x1200" -> (1920, 1200). Raises ConfigError otherwise."""
    if value in ("", "native"):
        return None
    m = _SCALE.match(value)
    if not m:
        raise ConfigError(f"scale must be 'native' or WIDTHxHEIGHT (e.g. 1920x1200), got {value!r}")
    w, h = int(m.group(1)), int(m.group(2))
    if w % 2 or h % 2:
        raise ConfigError(f"scale dimensions must be even for 4:2:0 video, got {value!r}")
    return w, h


# ---------------------------------------------------------------------------
# 2. Loading + validation
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    """A config file or override that cannot be used; message says why."""


_SECTIONS = {"video": VideoConfig, "audio": AudioConfig, "flash": FlashConfig,
             "output": OutputConfig, "ffmpeg": FfmpegConfig}
_TOP_KEYS = {"root": str, "default_collection": str, "log_level": str}


def _coerce(section: str, name: str, expected, value):
    where = f"{section}.{name}" if section else name
    if expected is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if expected == tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where} must be a list of strings, got {value!r}")
        return tuple(value)
    if expected is int and isinstance(value, bool):
        raise ConfigError(f"{where} must be an integer, got {value!r}")
    if not isinstance(value, expected):
        raise ConfigError(f"{where} must be {expected.__name__}, got {type(value).__name__} {value!r}")
    return value


def _field_types(cls) -> dict:
    hints = {"int": int, "str": str, "bool": bool, "float": float, "tuple[str, ...]": tuple[str, ...]}
    return {f.name: hints[f.type] if isinstance(f.type, str) else f.type for f in fields(cls)}


def from_mapping(data: dict, base: Config | None = None, source: str = "mapping") -> Config:
    """Overlay a parsed TOML mapping on `base` (defaults when None), validating as it goes."""
    cfg = base or Config()
    unknown = [k for k in data if k not in _TOP_KEYS and k not in _SECTIONS]
    if unknown:
        raise ConfigError(f"unknown top-level key(s) in {source}: {', '.join(sorted(unknown))}")
    top = {}
    for key, typ in _TOP_KEYS.items():
        if key in data:
            top[key] = _coerce("", key, typ, data[key])
    sections = {}
    for name, cls in _SECTIONS.items():
        if name not in data:
            continue
        table = data[name]
        if not isinstance(table, dict):
            raise ConfigError(f"[{name}] must be a table")
        types = _field_types(cls)
        bad = [k for k in table if k not in types]
        if bad:
            raise ConfigError(f"unknown key(s) in [{name}] of {source}: {', '.join(sorted(bad))}")
        updates = {k: _coerce(name, k, types[k], v) for k, v in table.items()}
        sections[name] = replace(getattr(cfg, name), **updates)
    cfg = replace(cfg, **top, **sections, source=source)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    v, a, f = cfg.video, cfg.audio, cfg.flash
    if v.pipeline not in PIPELINES:
        raise ConfigError(f"video.pipeline must be one of {PIPELINES}, got {v.pipeline!r}")
    for p in v.fallback:
        if p not in PIPELINES:
            raise ConfigError(f"video.fallback entries must be in {PIPELINES}, got {p!r}")
    if not 1 <= v.fps <= 240:
        raise ConfigError(f"video.fps must be 1..240, got {v.fps}")
    parse_scale(v.scale)
    if v.gop_seconds < 1:
        raise ConfigError("video.gop_seconds must be >= 1")
    for name in ("start_color", "stop_color"):
        if not _HEX_COLOR.match(getattr(f, name)):
            raise ConfigError(f"flash.{name} must be #RRGGBB, got {getattr(f, name)!r}")
    if not 20 <= f.duration_ms <= 2000:
        raise ConfigError(f"flash.duration_ms must be 20..2000, got {f.duration_ms}")
    if a.enabled and not a.device.strip():
        raise ConfigError("audio.device is empty; set it (see `peep doctor`) or set audio.enabled = false")
    if cfg.output.container not in CONTAINERS:
        raise ConfigError(f"output.container must be one of {CONTAINERS}, got {cfg.output.container!r}")
    if _WSL_UNC.match(cfg.root):
        raise ConfigError(f"root must be on the Windows filesystem, never under \\\\wsl.localhost: {cfg.root!r}")
    if cfg.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"log_level must be DEBUG/INFO/WARNING/ERROR, got {cfg.log_level!r}")


def load(path: Path | None = None, environ: dict[str, str] | None = None) -> Config:
    """Defaults, overlaid with the TOML file if it exists. A missing file is
    normal (defaults); an unreadable or invalid one raises ConfigError."""
    env = os.environ if environ is None else environ
    base = Config(root=default_root(env))
    p = Path(path) if path else paths.config_path(env)
    if not p.exists():
        return base
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{p}: cannot read: {exc}") from exc
    return from_mapping(data, base, source=str(p))


# ---------------------------------------------------------------------------
# 3. Template
# ---------------------------------------------------------------------------

TEMPLATE = '''\
# peep configuration. Every key is optional; anything left out keeps its
# built-in default (shown here). Unknown keys are an error, on purpose.

# root = "C:/Users/chord/Videos/peep"     # recordings land here, one folder per collection
# default_collection = "inbox"
# log_level = "INFO"                       # DEBUG also logs every ffmpeg stderr line

[video]
# fps = 30
# pipeline = "qsv"                # qsv (GPU end to end) | qsv-download | x264
# fallback = ["qsv-download", "x264"]
# scale = ""                      # "" = native 2560x1600, or "1920x1200"
# output_idx = 0
# draw_mouse = true
# qsv_preset = "medium"
# qsv_global_quality = 25         # lower = better quality, bigger files
# x264_preset = "veryfast"
# x264_crf = 20
# gop_seconds = 2

[audio]
# enabled = true
# device = "Microphone Array on SoundWire Device (6- Realtek XU)"
# bitrate = "160k"
# buffer_ms = 50
# offset_ms = 0                   # positive delays the mic track; set from the sync smoke test

[flash]
# enabled = true
# start_color = "#FF00FF"
# stop_color = "#00FF00"
# duration_ms = 150
# settle_ms = 400
# lead_ms = 300

[output]
# container = "mp4"               # mp4 | mkv

[ffmpeg]
# path = "ffmpeg"
# startup_timeout_s = 10.0
# stop_timeout_s = 20.0
'''
