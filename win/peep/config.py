"""Configuration: built-in defaults that work on the architect's laptop
unchanged, optionally overridden by a TOML file (`paths.config_path()`,
`%LOCALAPPDATA%\\peep\\config.toml` by default).

Unknown keys and wrong types are errors, not warnings: a typo in a
config key that silently falls back to a default is exactly the silent
skip AGENT.md calls a bug.

Sections:
  1. Defaults + dataclasses     (~line 25)
  2. Loading + validation       (~line 163)
  3. Template                   (~line 330)
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
AUDIO_SOURCES = ("system", "mic", "both", "none")     # system = what the computer plays (WASAPI loopback)
MIC_BACKENDS = ("wasapi", "dshow")
AUDIO_MIXES = ("mix", "separate")
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
    """Session A.1. Both sources are captured by our own WASAPI child processes
    (wasapi.py) and start at the same instant as the video by construction;
    see the README's audio section for the measurements behind the defaults."""
    sources: str = "system"              # system (computer audio) | mic | both | none; `peep rec --audio` overrides
    enabled: bool = True                 # false = no audio at all, whatever `sources` says (A's switch, kept)
    system_device: str = ""              # output device to capture: "" = the Windows default output (see `peep doctor`)
    device: str = ""                     # microphone: "" = the Windows default recording device, or a name from `peep doctor`
    mic_backend: str = "wasapi"          # wasapi (aligned) | dshow (A's path: starts ~300 ms early and drifts; fallback only)
    mix: str = "mix"                     # with both: one mixed track | separate: two tracks, system first
    system_gain: float = 1.0
    mic_gain: float = 1.0
    bitrate: str = "160k"
    offset_ms: int = 0                   # manual trim for every source: positive delays the audio
    clap: bool = True                    # a short tone with the start flash when system audio is recorded
    epoch_lead_ms: int = 58              # measured: ddagrab's first frame comes this long before ffmpeg prints "Input #0"
    dshow_align_ms: int = 300            # dshow backend only: delay applied to its measured start lead
    buffer_ms: int = 50                  # dshow backend only: audio_buffer_size (dshow's default 500 ms adds latency)


@dataclass(frozen=True)
class FlashConfig:
    enabled: bool = True
    start_color: str = "#FF00FF"         # magenta
    stop_color: str = "#00FF00"          # green: magenta's complement, measured exact through the pipeline
    mark_color: str = "#00FFFF"          # cyan: a mark (agent hotkey or `peep mark`), distinct from both
    duration_ms: int = 200               # 150 measured 4-5 frames at 30 fps; 200 for margin (smoke test, 2026-10-04)
    settle_ms: int = 400                 # keep recording this long after the stop flash before sending q
    lead_ms: int = 300                   # wait after ffmpeg reports ready before the start flash
    # Session C1a: take, retake and mark show a corner patch (captured, for C1b to find)
    # instead of a full-screen strobe. Session/segment start and stop keep the full flash.
    take_open_color: str = "#0000FF"     # blue: a take opens
    take_close_color: str = "#FF0000"    # red: a take closes
    retake_color: str = "#FFFF00"        # yellow: the last take is discarded and a fresh one opens
    mark_style: str = "patch"            # patch (corner, mark_color) | full (B's full-screen cyan flash)
    patch_size_px: int = 200             # side of the square patch, physical pixels
    patch_corner: str = "auto"           # auto = the corner opposite the pill | top-left | top-right | bottom-left | bottom-right
    patch_margin_px: int = 0             # distance from the screen edges


@dataclass(frozen=True)
class OutputConfig:
    container: str = "mp4"               # mp4 (remuxed from the Matroska capture on stop) | mkv


@dataclass(frozen=True)
class FfmpegConfig:
    path: str = "ffmpeg"                 # resolved on PATH unless absolute
    startup_timeout_s: float = 10.0
    stop_timeout_s: float = 20.0


@dataclass(frozen=True)
class AgentConfig:
    """Session B's resident agent (`peep agent run`). Hotkeys are chords like
    "Ctrl+Alt+R"; see hotkeys.parse_chord for the accepted key names."""
    record_hotkey: str = "Ctrl+Alt+R"    # start a recording / stop it (then the naming dialog)
    mark_hotkey: str = "Ctrl+Alt+M"      # drop a mark (cyan flash + sidecar entry) while recording
    discard_hotkey: str = "Ctrl+Alt+X"   # stop and delete the current take
    # Session C1a (the probe found all three free on the laptop, 2026-10-07):
    pause_hotkey: str = "Ctrl+Alt+P"     # hard pause / resume: capture stops; resume opens the next segment
    take_hotkey: str = "Ctrl+Alt+T"      # open a take / close it (only takes survive C1b's render)
    retake_hotkey: str = "Ctrl+Alt+Backspace"   # discard the most recent take, open a fresh one now
    debounce_ms: int = 1000              # a second press of the same chord within this is ignored (and logged)
    hotkey_retry_s: int = 60             # keep retrying chords that failed to register (login race)
    dialog: bool = True                  # false: hotkey stops keep the automatic name, no dialog
    pill: bool = True                    # the REC pill (excluded from capture; probe-verified 2026-10-05)
    pill_position: str = "top-right"     # top-right | top-left | top-center | bottom-right | bottom-left | bottom-center
    pill_margin_px: int = 24


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
    agent: AgentConfig = field(default_factory=AgentConfig)
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
             "output": OutputConfig, "ffmpeg": FfmpegConfig, "agent": AgentConfig}
PILL_POSITIONS = ("top-right", "top-left", "top-center", "bottom-right", "bottom-left", "bottom-center")
PATCH_CORNERS = ("auto", "top-left", "top-right", "bottom-left", "bottom-right")
MARK_STYLES = ("patch", "full")
FIDUCIAL_COLOR_KEYS = ("start_color", "stop_color", "mark_color", "take_open_color", "take_close_color",
                       "retake_color")
MIN_COLOR_DISTANCE = 96       # some channel must differ by this much, so C1b cannot confuse two fiducials
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
    validate_flash(f)
    validate_audio(a)
    if cfg.output.container not in CONTAINERS:
        raise ConfigError(f"output.container must be one of {CONTAINERS}, got {cfg.output.container!r}")
    if _WSL_UNC.match(cfg.root):
        raise ConfigError(f"root must be on the Windows filesystem, never under \\\\wsl.localhost: {cfg.root!r}")
    if cfg.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"log_level must be DEBUG/INFO/WARNING/ERROR, got {cfg.log_level!r}")
    validate_agent(cfg.agent)


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def validate_flash(f: FlashConfig) -> None:
    for name in FIDUCIAL_COLOR_KEYS:
        if not _HEX_COLOR.match(getattr(f, name)):
            raise ConfigError(f"flash.{name} must be #RRGGBB, got {getattr(f, name)!r}")
    colors = [(name, getattr(f, name).upper()) for name in FIDUCIAL_COLOR_KEYS]
    for i, (n1, c1) in enumerate(colors):
        for n2, c2 in colors[i + 1:]:
            if max(abs(a - b) for a, b in zip(_rgb(c1), _rgb(c2))) < MIN_COLOR_DISTANCE:
                raise ConfigError(f"flash.{n1} {c1} and flash.{n2} {c2} are too alike: every fiducial colour must "
                                  f"differ from the others by at least {MIN_COLOR_DISTANCE} in some channel "
                                  f"(session C tells them apart by colour)")
    if not 20 <= f.duration_ms <= 2000:
        raise ConfigError(f"flash.duration_ms must be 20..2000, got {f.duration_ms}")
    if f.mark_style not in MARK_STYLES:
        raise ConfigError(f"flash.mark_style must be one of {MARK_STYLES}, got {f.mark_style!r}")
    if f.patch_corner not in PATCH_CORNERS:
        raise ConfigError(f"flash.patch_corner must be one of {PATCH_CORNERS}, got {f.patch_corner!r}")
    if not 32 <= f.patch_size_px <= 1600:
        raise ConfigError(f"flash.patch_size_px must be 32..1600, got {f.patch_size_px}")
    if not 0 <= f.patch_margin_px <= 1000:
        raise ConfigError(f"flash.patch_margin_px must be 0..1000, got {f.patch_margin_px}")


def validate_audio(a: AudioConfig) -> None:
    for name in ("device", "system_device"):
        value = getattr(a, name)
        if value and not value.strip():
            raise ConfigError(f"audio.{name} is empty (only spaces); unset it for the Windows default, "
                              f"or name a device from `peep doctor`")
    if a.sources not in AUDIO_SOURCES:
        raise ConfigError(f"audio.sources must be one of {AUDIO_SOURCES}, got {a.sources!r}")
    if a.mic_backend not in MIC_BACKENDS:
        raise ConfigError(f"audio.mic_backend must be one of {MIC_BACKENDS}, got {a.mic_backend!r}")
    if a.mix not in AUDIO_MIXES:
        raise ConfigError(f"audio.mix must be one of {AUDIO_MIXES}, got {a.mix!r}")
    for name in ("system_gain", "mic_gain"):
        if not 0.0 <= getattr(a, name) <= 8.0:
            raise ConfigError(f"audio.{name} must be 0.0..8.0, got {getattr(a, name)}")
    if not -5000 <= a.offset_ms <= 5000:
        raise ConfigError(f"audio.offset_ms must be -5000..5000, got {a.offset_ms}")
    if not 0 <= a.epoch_lead_ms <= 1000:
        raise ConfigError(f"audio.epoch_lead_ms must be 0..1000, got {a.epoch_lead_ms}")
    if not -2000 <= a.dshow_align_ms <= 3000:
        raise ConfigError(f"audio.dshow_align_ms must be -2000..3000, got {a.dshow_align_ms}")
    if not 10 <= a.buffer_ms <= 1000:
        raise ConfigError(f"audio.buffer_ms must be 10..1000, got {a.buffer_ms}")


def effective_sources(a: AudioConfig, override: str | None = None, no_mic: bool = False) -> str:
    """The sources a recording uses: the override (`peep rec --audio`) or
    `audio.sources`, `none` when audio.enabled is false (unless overridden),
    and `--no-mic` taking the microphone out of whatever remains."""
    if override is not None and override not in AUDIO_SOURCES:
        raise ConfigError(f"--audio must be one of {AUDIO_SOURCES}, got {override!r}")
    src = override if override is not None else (a.sources if a.enabled else "none")
    if no_mic:
        src = {"both": "system", "mic": "none"}.get(src, src)
    return src


def source_set(sources: str) -> tuple[str, ...]:
    """'both' -> ('system', 'mic'); 'none' -> (); others -> (themselves,). System first, always."""
    return {"both": ("system", "mic"), "none": ()}.get(sources, (sources,))


def validate_agent(ag: AgentConfig) -> None:
    from . import hotkeys     # pure module; imported here to keep config importable on its own
    try:
        hotkeys.table_from_agent_config(ag)
    except hotkeys.HotkeyError as exc:
        raise ConfigError(f"[agent] {exc}") from exc
    if ag.pill_position not in PILL_POSITIONS:
        raise ConfigError(f"agent.pill_position must be one of {PILL_POSITIONS}, got {ag.pill_position!r}")
    if not 0 <= ag.pill_margin_px <= 1000:
        raise ConfigError(f"agent.pill_margin_px must be 0..1000, got {ag.pill_margin_px}")
    if not 0 <= ag.hotkey_retry_s <= 3600:
        raise ConfigError(f"agent.hotkey_retry_s must be 0..3600, got {ag.hotkey_retry_s}")
    if not 0 <= ag.debounce_ms <= 10000:
        raise ConfigError(f"agent.debounce_ms must be 0..10000, got {ag.debounce_ms}")


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

[audio]                           # `peep config set audio.KEY VALUE` edits this file and keeps these comments
# sources = "system"              # system (computer audio) | mic | both | none; `peep rec --audio` overrides once
# enabled = true                  # false: no audio at all
# system_device = ""              # "" = the Windows default output; or a name from `peep doctor`
# device = ""                     # microphone: "" = the Windows default recording device; or a name from `peep doctor`
# mic_backend = "wasapi"          # wasapi (aligned to the video) | dshow (old path: starts ~300 ms early, drifts)
# mix = "mix"                     # with both: one mixed track | "separate": two tracks, system first
# system_gain = 1.0
# mic_gain = 1.0
# bitrate = "160k"
# offset_ms = 0                   # manual trim for every source: positive delays the audio (default needs none)
# clap = true                     # short tone with the start flash when system audio is recorded (sync marker)
# epoch_lead_ms = 58              # measured 2026-10-06; only change it after re-measuring
# dshow_align_ms = 300            # dshow backend only
# buffer_ms = 50                  # dshow backend only

[flash]
# enabled = true
# start_color = "#FF00FF"
# stop_color = "#00FF00"
# mark_color = "#00FFFF"         # marks (agent hotkey / `peep mark`)
# duration_ms = 200
# settle_ms = 400
# lead_ms = 300
# take_open_color = "#0000FF"     # corner patch when a take opens (all fiducial colours must differ)
# take_close_color = "#FF0000"    # ... when a take closes
# retake_color = "#FFFF00"        # ... on a retake
# mark_style = "patch"            # patch: marks show a corner patch | full: the full-screen flash
# patch_size_px = 200             # the patch's side, physical pixels
# patch_corner = "auto"           # auto (opposite the pill) | top-left | top-right | bottom-left | bottom-right
# patch_margin_px = 0

[output]
# container = "mp4"               # mp4 | mkv

[ffmpeg]
# path = "ffmpeg"
# startup_timeout_s = 10.0
# stop_timeout_s = 20.0

[agent]                           # the resident agent (`peep agent install`); `peep agent reload` after editing
# record_hotkey = "Ctrl+Alt+R"    # start / stop (then the naming dialog)
# mark_hotkey = "Ctrl+Alt+M"      # drop a mark while recording
# discard_hotkey = "Ctrl+Alt+X"   # stop and delete the take
# pause_hotkey = "Ctrl+Alt+P"     # hard pause / resume (capture stops; resume opens the next segment)
# take_hotkey = "Ctrl+Alt+T"      # open / close a take
# retake_hotkey = "Ctrl+Alt+Backspace"   # discard the last take, open a fresh one
# debounce_ms = 1000              # a repeat of the same chord within this is ignored
# hotkey_retry_s = 60             # keep retrying a chord another app holds, for this long after start
# dialog = true                   # false: hotkey stops keep the automatic name
# pill = true                     # the REC pill (hidden from the recording itself)
# pill_position = "top-right"     # top-right | top-left | top-center | bottom-right | bottom-left | bottom-center
# pill_margin_px = 24
'''
