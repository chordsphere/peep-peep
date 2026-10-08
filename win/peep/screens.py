"""Auto-takes (session C1d): what a learned screen looks like, and whether a
thumbnail of the screen shows it. Pure: no Win32, no files except the PNG
helpers, no clock. The agent's sampler (sampler.py) feeds it live GDI
thumbnails; the render (render.py) feeds it decoded frames, downscaled the
same way; the tests feed it synthetic ones.

A thumbnail is the whole captured screen averaged down to THUMB_WIDTH
pixels wide (64x40 on the laptop's 2560x1600), one byte of grey per pixel:
grey = (77 R + 150 G + 29 B + 128) >> 8, the same formula everywhere.

Two numbers compare two thumbnails (`distance`):
  mad          the mean absolute difference of their pixels, 0..255
  changed_pct  the share of pixels differing by more than `changed_level`
               (24), in percent

The laptop's numbers (probe, 2026-10-08, real browser pages, 64x40):
  the same page, mouse moving over it    mad <= 4.9, changed <= 3 %
  (GDI leaves the cursor out; what moved was the page's hover effects)
  the same page, mouse still             mad 0.0
  the same page scrolled three lines     mad 10.5-22, changed 17-37 %
  a different page                       mad 90-166, changed 71-86 %
  GDI vs ddagrab, same page              mad 3.3-3.5, changed 1 %
so the defaults sit in the gap: a sample *matches* at mad <= 8 and
changed <= 8 %; it is a clear *miss* at mad > 12 or changed > 15 %; in
between it is neither (a hysteresis band: it never toggles a screen).

Hysteresis (`Presence`): a screen appears after `hysteresis` (2) matching
samples in a row and goes away after as many misses, so a single noisy
sample never toggles a take. The event's time is the *first* sample of the
run: the screen's first frame (or the first frame after it) lies within
one sample period before it, which is where the render looks.

Sections:
  1. Thresholds                 (~line 50)
  2. Thumbnails + distance      (~line 100)
  3. Presence (hysteresis)      (~line 175)
  4. Stable capture (learning)  (~line 250)
  5. PNG + hex                  (~line 290)
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import asdict, dataclass, fields
from typing import Callable

# ---------------------------------------------------------------------------
# 1. Thresholds
# ---------------------------------------------------------------------------

THUMB_WIDTH = 64
KINDS = ("start", "end")               # the two learnable screens
OTHER = {"start": "end", "end": "start"}


@dataclass(frozen=True)
class Thresholds:
    """When a thumbnail shows a reference. Defaults from the probe's numbers
    (module docstring); recorded with every reference in the sidecar, so a
    render judges with the values the live sampler used."""
    match_mad: float = 8.0               # a match needs mad <= this ...
    match_changed_pct: float = 8.0       # ... and changed_pct <= this
    miss_mad: float = 12.0               # a clear miss: mad > this ...
    miss_changed_pct: float = 15.0       # ... or changed_pct > this
    changed_level: int = 24              # a pixel "changed" when it differs by more than this
    hysteresis: int = 2                  # consecutive samples to appear / to go away

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Thresholds":
        """From a sidecar or a request; unknown keys ignored, missing keys default."""
        if not isinstance(d, dict):
            return cls()
        names = {f.name: f.type for f in fields(cls)}
        out = {}
        for k, v in d.items():
            if k in names and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = int(v) if k in ("changed_level", "hysteresis") else float(v)
        return cls(**out)

    def validate(self) -> None:
        """Raise ValueError naming the first impossible value."""
        if not 0 < self.match_mad <= self.miss_mad <= 255:
            raise ValueError(f"need 0 < match_mad <= miss_mad <= 255, got {self.match_mad}, {self.miss_mad}")
        if not 0 < self.match_changed_pct <= self.miss_changed_pct <= 100:
            raise ValueError(f"need 0 < match_changed_pct <= miss_changed_pct <= 100, got "
                             f"{self.match_changed_pct}, {self.miss_changed_pct}")
        if not 1 <= self.changed_level <= 254:
            raise ValueError(f"changed_level must be 1..254, got {self.changed_level}")
        if not 1 <= self.hysteresis <= 20:
            raise ValueError(f"hysteresis must be 1..20, got {self.hysteresis}")

    def loosened(self, mad: float, pct: float) -> "Thresholds":
        """The same thresholds widened, for comparing a decoded (encoded, scaled by
        ffmpeg) frame with a GDI reference: the probe measured mad 3.5 between the
        two pipelines on the same page, before any encoding."""
        return Thresholds(self.match_mad + mad, self.match_changed_pct + pct, self.miss_mad + mad,
                          self.miss_changed_pct + pct, self.changed_level, self.hysteresis)


# ---------------------------------------------------------------------------
# 2. Thumbnails + distance
# ---------------------------------------------------------------------------


def thumb_size(screen_w: int, screen_h: int, width: int = THUMB_WIDTH) -> tuple[int, int]:
    """(w, h) of the thumbnail of a screen: `width` wide, the screen's aspect
    (2560x1600 -> 64x40)."""
    if screen_w <= 0 or screen_h <= 0:
        raise ValueError(f"screen size must be positive, got {screen_w}x{screen_h}")
    return width, max(1, round(width * screen_h / screen_w))


def grey_from_bgra(buf: bytes) -> bytes:
    """32-bit BGRA (a GDI DIB) -> one grey byte per pixel."""
    return bytes((77 * r + 150 * g + 29 * b + 128) >> 8 for b, g, r in zip(buf[0::4], buf[1::4], buf[2::4]))


def grey_from_rgb(buf: bytes) -> bytes:
    """rgb24 (an ffmpeg decode) -> one grey byte per pixel, the same formula."""
    return bytes((77 * r + 150 * g + 29 * b + 128) >> 8 for r, g, b in zip(buf[0::3], buf[1::3], buf[2::3]))


@dataclass(frozen=True)
class Distance:
    mad: float
    changed_pct: float

    def to_dict(self) -> dict:
        return {"mad": round(self.mad, 2), "changed_pct": round(self.changed_pct, 2)}


def distance(a: bytes, b: bytes, changed_level: int = 24) -> Distance:
    """How far apart two thumbnails of the same size are. Different sizes are
    a programming error (a reference from another screen size), raised."""
    if len(a) != len(b) or not a:
        raise ValueError(f"thumbnails differ in size ({len(a)} vs {len(b)} pixels)")
    total = changed = 0
    for p, q in zip(a, b):
        d = p - q if p >= q else q - p
        total += d
        if d > changed_level:
            changed += 1
    n = len(a)
    return Distance(total / n, 100.0 * changed / n)


def is_match(d: Distance, thr: Thresholds) -> bool:
    return d.mad <= thr.match_mad and d.changed_pct <= thr.match_changed_pct


def is_miss(d: Distance, thr: Thresholds) -> bool:
    return d.mad > thr.miss_mad or d.changed_pct > thr.miss_changed_pct


def same_screen(a: bytes, b: bytes, thr: Thresholds) -> bool:
    """Would one reference be taken for the other? (Learning a screen that
    matches the other kind's reference is refused: one screen cannot be both.)"""
    return len(a) == len(b) and is_match(distance(a, b, thr.changed_level), thr)


# ---------------------------------------------------------------------------
# 3. Presence (hysteresis)
# ---------------------------------------------------------------------------


class Presence:
    """Whether one reference is on screen, from a stream of samples.

    feed(qpc, thumb) returns None, or the change that just became certain:
      {"change": "appear" | "gone", "qpc": <first sample of the run>,
       "score": {"mad", "changed_pct"} of that first sample, "samples": n}
    A screen appears after `hysteresis` matching samples in a row; it goes
    away after as many clear misses. A sample in the band between match and
    miss continues neither run. `present` may start True: a screen learned
    while it is showing is present from the start (the appearance being
    learned on counts, and is not reported again)."""

    def __init__(self, reference: bytes, thr: Thresholds, present: bool = False):
        self.reference, self.thr, self.present = reference, thr, present
        self._run: list = []             # [(qpc, Distance)] of the run toward a change
        self.last: Distance | None = None
        self.samples = 0

    def feed(self, qpc: float, thumb: bytes) -> dict | None:
        d = distance(thumb, self.reference, self.thr.changed_level)
        self.last = d
        self.samples += 1
        toward = is_miss(d, self.thr) if self.present else is_match(d, self.thr)
        if not toward:
            self._run = []
            return None
        self._run.append((qpc, d))
        if len(self._run) < self.thr.hysteresis:
            return None
        first_qpc, first_d = self._run[0]
        self._run = []
        self.present = not self.present
        return {"change": "appear" if self.present else "gone", "qpc": first_qpc, "score": first_d.to_dict(),
                "samples": self.thr.hysteresis}


# Within one sample, the order the changes reach the model: the end screen arriving first, then
# the start screen going. A start screen that gives way straight to the end screen then opens no
# take at all (the model's "end-screen-up" guard), rather than an empty one.
CHANGE_ORDER = {("end", "appear"): 0, ("start", "gone"): 1, ("end", "gone"): 2, ("start", "appear"): 3}


def change_order(kind: str, change: str) -> int:
    return CHANGE_ORDER.get((kind, change), 9)


# ---------------------------------------------------------------------------
# 4. Stable capture (learning)
# ---------------------------------------------------------------------------


def stable_capture(grab: Callable[[], bytes], thr: Thresholds, *, wait_s: float = 1.0, step_s: float = 0.1,
                   sleep: Callable[[float], None] | None = None) -> tuple[bytes, bool, float]:
    """The reference to learn: the screen *now*, once it holds still. Takes a
    sample every `step_s` until two in a row match each other (stable), for at
    most `wait_s` (the brief's ~1 s: a page still loading or a transition
    still running). Returns (thumbnail, stable, waited_s); when it never
    settles, the last sample is used and `stable` is False (said in the
    sidecar and on the pill)."""
    import time
    sleep = sleep or time.sleep
    prev = grab()
    waited = 0.0
    while waited < wait_s - 1e-9:
        sleep(step_s)
        waited += step_s
        cur = grab()
        if is_match(distance(cur, prev, thr.changed_level), thr):
            return cur, True, round(waited, 3)
        prev = cur
    return prev, False, round(waited, 3)


# ---------------------------------------------------------------------------
# 5. PNG + hex
# ---------------------------------------------------------------------------

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_grey(w: int, h: int, pixels: bytes) -> bytes:
    """An 8-bit greyscale PNG of a thumbnail (filter 0 on every row), from the
    standard library: zlib + struct. C1e shows it in the expanded pill."""
    if len(pixels) != w * h:
        raise ValueError(f"{len(pixels)} pixels for a {w}x{h} image")
    raw = b"".join(b"\x00" + pixels[y * w:(y + 1) * w] for y in range(h))
    return PNG_SIG + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + \
        _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


def read_png_grey(data: bytes) -> tuple[int, int, bytes]:
    """(w, h, pixels) of a PNG written by png_grey (8-bit grey; filter 0 only,
    which is all png_grey writes). Anything else raises ValueError: the agent
    reads back only its own files (an agent restarted mid-recording)."""
    if not data.startswith(PNG_SIG):
        raise ValueError("not a PNG")
    pos, w = len(PNG_SIG), None
    idat = b""
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            if depth != 8 or ctype != 0:
                raise ValueError(f"not an 8-bit greyscale PNG (depth {depth}, colour type {ctype})")
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + n
    if w is None:
        raise ValueError("PNG without IHDR")
    raw = zlib.decompress(idat)
    rows = []
    for y in range(h):
        line = raw[y * (w + 1):(y + 1) * (w + 1)]
        if len(line) != w + 1 or line[0] != 0:
            raise ValueError(f"row {y}: unsupported PNG filter or short data")
        rows.append(line[1:])
    return w, h, b"".join(rows)


def to_hex(thumb: bytes) -> str:
    return thumb.hex()


def from_hex(text: str, w: int, h: int) -> bytes:
    """A thumbnail from the sidecar's hex; ValueError when it is not w*h pixels."""
    data = bytes.fromhex(text or "")
    if len(data) != w * h:
        raise ValueError(f"thumbnail has {len(data)} pixels, expected {w}x{h}")
    return data
