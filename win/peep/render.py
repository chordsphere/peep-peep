"""The render (session C1b): the event record C1a wrote, turned into a cut
file beside the original. The original is never modified; the cut is
`<stem>.cut.mp4`, in the `.cut.*` namespace C1a reserved.

What the cut is, start to finish:

  segments   every segment that finished cleanly, in order, joined
  flashes    each segment trimmed to its own clapper flashes: kept material
             starts at the first frame after the last magenta frame and ends
             at the last frame before the first green one
  takes      cut to `summary.kept` (no take at all: everything is kept). At
             each take boundary the corner patch is found the same way and
             its frames are excluded with the material on the far side
  seams      at every take boundary and every join, silence is trimmed off
             the kept side (render.silence_*) and the cut is snapped to the
             quietest 10 ms within render.snap_ms; the picture cuts hard on a
             frame, the audio crossfades across the join (render.crossfade_ms)
  marks      chapters in the cut, and their output times in the sidecar. A
             mark is never a cut: its corner patch, inside kept material, is
             concealed instead (that corner held from the frame before the
             patch for the patch's ~200 ms; a full-screen mark flash, the
             whole frame), so no fiducial colour survives into the cut
  screens   (C1d, auto-takes) a take edge made by a learned screen has no
             patch: the edge is refined to the exact frame by comparing
             decoded thumbnails (screens.py, the same 64x40 grey the sampler
             used) with the screen's reference: an end screen's first frame
             (followed back as far as render.screen_lookback_s when it was
             learned while showing), a start screen's last. The screen's own
             frames are never in the cut; a refinement that fails falls back
             to the sample time, erring toward cutting more, loudly
  strays     (C1c) every other patch that is not the fiducial of a kept edge
             is concealed the same way when it lands in kept material: the
             red patch of a take close that a correction moved later, a
             yellow correction patch, any take patch the cut keeps across
  output     the original's resolution, frame rate, encoder settings
             (ffmpeg_cmd's pipeline -> encoder choice) and audio track layout,
             `-movflags +faststart`

Every boundary is *found in the video*, never assumed: the sidecar's stamp
says where to look (+-render.search_s) and detection decides. When detection
fails the stamp is used instead, loudly: logged, listed in the result, and
recorded in the sidecar's `render` block with the reason. So a render never
silently uses an unverified boundary.

Standard library only. ffmpeg does the heavy lifting (decode, crop, scale,
resample); Python judges small amounts of raw data: a 16x10 thumbnail of a
flash window, a 4x4 median of a patch, 16 kHz mono PCM around a seam. No
numpy, no audioop (gone in 3.13): `struct` and arithmetic.

The pipeline, each stage testable on its own:

  build_plan(sidecar)            pure: segments, kept intervals, boundaries with
                                 their stamps and fiducials, marks, track layout
  resolve(plan, analyzer, cfg)   decides each boundary from decoded frames and PCM
                                 (an Analyzer: ffmpeg in production, synthetic data
                                 in tests): detection, fallback, silence trim, seam
                                 snap, frame quantisation, crossfades, chapters,
                                 the clap calibration
  build_render_argv(...)         pure: the one ffmpeg command (trim / atrim per
                                 piece, concat for the picture, acrossfade chain
                                 for each audio track)
  Renderer.render(ref)           runs it: lock, analysis, encode with progress, an
                                 x264 retry if QSV fails, the cut moved into place,
                                 the sidecar's `render` block written
  RenderQueue                    the agent's background renders, one at a time

Sections:
  1. Constants + small helpers         (~line 95)
  2. The plan (pure)                   (~line 145)
  3. Detection (pure)                  (~line 420)
  4. Audio: levels, seams, onset       (~line 535)
  5. Resolve                           (~line 660)
  6. argv + chapters (pure)            (~line 1195)
  7. Analyzer (ffmpeg)                 (~line 1375)
  8. Cut status                        (~line 1425)
  9. Renderer                          (~line 1480)
 10. RenderQueue + policy              (~line 1830)
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import math
import os
import re
import struct
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import catalog, events, ffmpeg_cmd, naming, screens
from .config import Config, RenderConfig
from .logsetup import event

log = logging.getLogger("peep.render")

# ---------------------------------------------------------------------------
# 1. Constants + small helpers
# ---------------------------------------------------------------------------

CUT_SUFFIX = ".cut.mp4"            # the cut (C1a's reserved namespace: naming.OWNED_REST)
PART_SUFFIX = ".cut.part.mp4"      # being written; moved to CUT_SUFFIX when complete
META_SUFFIX = ".cut.part.ffmeta"   # the chapters handed to ffmpeg, removed afterwards
COLOR_TOL = 70                     # a frame "is" a fiducial colour within this per channel (measured within 2)
FIDUCIAL_LAG_S = 0.04              # shown stamp -> first captured frame: 38 ms magenta, 48 ms green (probe)
FALLBACK_MARGIN_S = 0.10           # extra margin when a boundary falls back to its stamp
APPROX_LAUNCH_TO_FRAME_S = 0.10    # ffmpeg launch -> first frame (probe: Input #0 at +153 ms, minus 58)
FLASH_GRID = (16, 10)              # full-frame flash: averaged down to 16x10
PATCH_GRID = (4, 4)                # corner patch: its rect, inset, down to 4x4, median
PATCH_INSET = 0.15                 # stay off the patch's edges (chroma subsampling blurs them)
PCM_RATE = 16000                   # analysis audio: mono 16 kHz s16
LEVEL_HOP_S = 0.01                 # 10 ms RMS frames
MIN_XFADE_S = 0.004                # a join with less room than this gets a plain concat
MARK_SNAP_S = 1.5                  # a mark pressed this soon before a piece begins starts its chapter there
DEFAULT_FPS = 30
STALL_S = 300.0                    # an encode with no progress for this long is killed
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000


class RenderError(RuntimeError):
    """A render that cannot happen or did not succeed; the message is user-facing."""


class AnalysisError(RuntimeError):
    """Decoding a window for detection or levels failed."""


def hex_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def parse_size(text) -> tuple[int, int] | None:
    m = re.match(r"^(\d+)x(\d+)$", str(text or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def fmt_mmss(seconds) -> str:
    return events.format_mmss(seconds)


def _r(x, nd=3):
    return None if x is None else round(float(x), nd)


# ---------------------------------------------------------------------------
# 2. The plan (pure)
# ---------------------------------------------------------------------------


@dataclass
class Fiducial:
    """What was shown on screen at a boundary, and when the sidecar says it was."""
    color: str | None              # "#RRGGBB" as shown; None: nothing was shown there
    style: str                     # "full" | "patch" | "none"
    stamp_s: float | None          # media time (seconds from the segment's first frame) it went up
    stamp_quality: str             # qpc | launch | approximate | press | none
    duration_s: float = 0.2        # how long it was up
    rect: dict | None = None       # patch rect, physical pixels of `screen`
    screen: dict | None = None
    # C1d: style "screen", an edge made by a learned screen: {"screen": start|end, "ref", "thumb" (bytes),
    # "size", "thresholds", "change": appear|gone, "from_learn", "period_s"} (see _screen_fiducial)
    reference: dict | None = None


NO_FIDUCIAL = Fiducial(None, "none", None, "none", 0.0)


@dataclass
class Boundary:
    side: str                      # "start" (kept material begins) | "end" (it ends)
    kind: str                      # segment-start | segment-end | take-open | take-close
    segment: int
    nominal_s: float               # where the event model put it (summary.kept)
    fiducial: Fiducial
    event: int | None = None       # the press that made it
    adjustable: bool = False       # silence trim / snap may move it (into the kept side only)


@dataclass
class Interval:
    segment: int
    start_s: float
    end_s: float
    take: int | None
    start: Boundary
    end: Boundary


@dataclass
class SegmentInfo:
    index: int
    file: str
    duration_s: float
    fps: float
    size: tuple | None             # output size of this segment's file
    capture_size: tuple | None
    tracks: list                   # what each audio track holds, e.g. ["system"], ["system", "mic"]
    pipeline: str
    start_flash: Fiducial
    stop_flash: Fiducial
    clap: dict | None              # {"requested_s", "played"}: media time of the 1 kHz tone request


@dataclass
class Plan:
    uid: str
    stem: str
    segments: list
    intervals: list
    marks: list                    # [{"segment", "media_s", "label", "event", "after"}]
    tracks: list                   # the output's audio track layout (labels)
    fps: float
    size: tuple | None
    pipeline: str
    audio_bitrate: str
    whole: bool                    # no take at all: keep everything, trimmed to the flashes
    digest: str                    # what the cut is a function of (see source_digest)
    notes: list = field(default_factory=list)       # plan-level facts worth saying (failed segments...)
    # C1c: take / correct patches that are no kept edge's fiducial (a superseded close, a correction
    # patch, a dropped take's patches): [{"segment", "media_s", "fiducial", "event", "what"}].
    # The render conceals any of them that lands in kept material, exactly as it does marks.
    patches: list = field(default_factory=list)

    def seg(self, index: int) -> SegmentInfo:
        for s in self.segments:
            if s.index == index:
                return s
        raise KeyError(index)

    @property
    def total_s(self) -> float:
        return round(sum(s.duration_s for s in self.segments), 3)


def media_time(qpc, since_launch, epoch, launch) -> tuple[float | None, str]:
    """A fiducial's stamp as media time (seconds from its segment's first frame):
    from its QPC stamp (A.1 and later), else from seconds-since-launch shifted by
    the segment's measured launch->first-frame, else by the typical value."""
    if isinstance(qpc, (int, float)) and isinstance(epoch, (int, float)):
        return qpc - epoch, "qpc"
    if isinstance(since_launch, (int, float)):
        if isinstance(epoch, (int, float)) and isinstance(launch, (int, float)):
            return since_launch - (epoch - launch), "launch"
        return since_launch - APPROX_LAUNCH_TO_FRAME_S, "approximate"
    return None, "none"


def _fiducial_from(rec: dict | None, epoch, launch, *, default_style: str = "full") -> Fiducial:
    if not isinstance(rec, dict) or rec.get("shown") is False or not rec.get("color"):
        return NO_FIDUCIAL
    stamp, quality = media_time(rec.get("shown_qpc"), rec.get("since_ffmpeg_start_s"), epoch, launch)
    dur = rec.get("actual_ms") or rec.get("duration_ms") or 200
    return Fiducial(color=str(rec["color"]).upper(), style=rec.get("style") or default_style, stamp_s=stamp,
                    stamp_quality=quality, duration_s=float(dur) / 1000, rect=rec.get("rect"),
                    screen=rec.get("screen"))


def _tracks(audio) -> list:
    """What each audio track of a segment's file holds. A.1 records it; an
    A-era sidecar with an audio block had one (dshow) track; none: no audio."""
    if not isinstance(audio, dict):
        return []
    if isinstance(audio.get("tracks"), list):
        return [str(t.get("content") or f"track{t.get('index', i)}") for i, t in enumerate(audio["tracks"])]
    return ["audio"]


def source_digest(v2: dict) -> str:
    """What the cut is a function of: the segments' timing and flashes, the event
    record and the marks. File names are left out on purpose, so a rename (which
    moves the cut with the recording) does not make the cut stale."""
    segs = [{"index": s.get("index"), "status": s.get("status"), "duration_s": s.get("duration_s"),
             "epoch": s.get("video_epoch_qpc_est"), "flash": s.get("flash")} for s in v2.get("segments") or []]
    body = {"segments": segs, "events": v2.get("events"), "takes": v2.get("takes"),
            "kept": (v2.get("summary") or {}).get("kept"),
            "marks": [[m.get("segment"), m.get("media_s"), m.get("label")] for m in v2.get("marks") or []]}
    if isinstance(v2.get("auto_takes"), dict):     # C1d: only when a screen was learned (else C1c's digest)
        body["auto_takes"] = [[r.get("ref"), r.get("thumb"), r.get("thresholds")]
                              for r in v2["auto_takes"].get("references") or []]
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


SCREEN_EVENT_KINDS = ("screen", "learn")          # C1d: events whose take edge is a learned screen


def _screen_fiducial(ev: dict, refs: dict, epoch, launch, side: str) -> Fiducial | None:
    """The fiducial of a take edge made by a learned screen (C1d), or None when
    the sidecar does not hold its reference (then the edge is bare, said in
    the plan's notes). `stamp_s`: the event's QPC on this segment's clock, the
    first sample that saw the change (or the learn press)."""
    r = refs.get(ev.get("ref"))
    if not isinstance(r, dict):
        return None
    try:
        w, h = (int(v) for v in r.get("size") or ())
        thumb = screens.from_hex(r.get("thumb") or "", w, h)
    except (TypeError, ValueError):
        return None
    stamp, quality = media_time(ev.get("qpc"), ev.get("since_ffmpeg_start_s"), epoch, launch)
    hz = (r.get("sampler") or {}).get("hz") if isinstance(r.get("sampler"), dict) else None
    period = 1.0 / float(hz) if isinstance(hz, (int, float)) and hz > 0 else 0.25
    change = "gone" if ev.get("change") == "gone" else "appear"
    return Fiducial(None, "screen", stamp, quality, 0.0,
                    reference={"screen": r.get("screen") or ev.get("screen"), "ref": r.get("ref"), "thumb": thumb,
                               "size": (w, h), "thresholds": r.get("thresholds"), "change": change,
                               "from_learn": ev.get("kind") == "learn", "period_s": period, "side": side,
                               "waited_s": ev.get("waited_s") if ev.get("kind") == "learn" else None})


def build_plan(sc: dict, *, stem: str = "") -> Plan:
    """The render plan, from any sidecar (`/1` through catalog.as_v2): which
    segments, which intervals of each, every boundary with what was shown there
    and when the sidecar says so. Pure: no files, no ffmpeg."""
    v2 = catalog.as_v2(sc)
    notes = []
    segments = []
    for s in v2.get("segments") or []:
        idx = int(s.get("index") or 1)
        if s.get("status") != "ok" or not s.get("file"):
            if s.get("file"):
                notes.append(f"segment {idx} did not finish cleanly ({s.get('status')}); its "
                             f"{s.get('file')} is kept but not in the cut")
            continue
        video = s.get("video") or {}
        audio = s.get("audio") or None
        epoch, launch = s.get("video_epoch_qpc_est"), s.get("ffmpeg_started_qpc")
        flash = s.get("flash") or {}
        clap = None
        if isinstance(audio, dict) and isinstance(audio.get("clap"), dict):
            c = audio["clap"]
            req, _ = media_time(c.get("requested_qpc"), c.get("since_ffmpeg_start_s"), epoch, launch)
            clap = {"requested_s": req, "played": bool(c.get("played"))}
        segments.append(SegmentInfo(
            index=idx, file=str(s["file"]), duration_s=float(s.get("duration_s") or 0.0),
            fps=float(video.get("fps") or DEFAULT_FPS), size=parse_size(video.get("output_size")),
            capture_size=parse_size(video.get("capture_size")), tracks=_tracks(audio),
            pipeline=str(video.get("pipeline") or "qsv"),
            start_flash=_fiducial_from(flash.get("start"), epoch, launch),
            stop_flash=_fiducial_from(flash.get("stop"), epoch, launch), clap=clap))
    durations = {s.index: s.duration_s for s in segments}
    takes = v2.get("takes") or []
    summary = v2.get("summary") or {}
    whole = not takes
    if isinstance(summary.get("kept"), list):
        kept = [k for k in summary["kept"] if k.get("segment") in durations]
    else:
        if takes:
            notes.append("the sidecar has no summary (the recording did not end cleanly); the kept "
                         "intervals were derived from its takes")
        kept = events.kept_intervals(takes, durations)
    take_by_id = {t.get("id"): t for t in takes}
    ev_by_id = {e.get("id"): e for e in v2.get("events") or []}
    epochs = {int(s.get("index") or 1): (s.get("video_epoch_qpc_est"), s.get("ffmpeg_started_qpc"))
              for s in v2.get("segments") or []}
    spans = segment_spans(v2)
    auto = v2.get("auto_takes") if isinstance(v2.get("auto_takes"), dict) else {}
    refs = {r.get("ref"): r for r in auto.get("references") or [] if isinstance(r, dict)}

    def shown_in(ev_id) -> int | None:
        """The segment the press's patch was shown in (not where the press was placed: a
        press made while resuming is placed in the pause but its patch shows in the next
        segment, after its start flash)."""
        ev = ev_by_id.get(ev_id) or {}
        return fiducial_segment(ev.get("fiducial"), spans)

    def event_fiducial(ev_id, seg_index, press_s):
        """What the press showed at this edge of this segment: its patch, unless the
        patch went up in another segment (a press held over a pause shows it in the
        next one), in which case nothing was shown here and the edge is bare."""
        ev = ev_by_id.get(ev_id)
        fid = (ev or {}).get("fiducial")
        epoch, launch = epochs.get(seg_index, (None, None))
        if (ev or {}).get("kind") in SCREEN_EVENT_KINDS and ev.get("segment") == seg_index:
            sf = _screen_fiducial(ev, refs, epoch, launch, side="end" if ev.get("change") != "gone" else "start")
            if sf is not None:
                return sf
            notes.append(f"event {ev_id} ({ev.get('screen')} screen) names reference {ev.get('ref')!r}, which the "
                         f"sidecar does not hold: its edge falls back to the sample time")
            stamp, quality = media_time(ev.get("qpc"), ev.get("since_ffmpeg_start_s"), epoch, launch)
            return Fiducial(None, "screen", stamp if stamp is not None else press_s, quality, 0.0,
                            reference={"missing": True, "screen": ev.get("screen"), "ref": ev.get("ref"),
                                       "change": "gone" if ev.get("change") == "gone" else "appear",
                                       "from_learn": ev.get("kind") == "learn", "period_s": 0.25})
        shown = fiducial_segment(fid, spans)
        if isinstance(fid, dict) and shown in (None, seg_index):
            f = _fiducial_from(fid, epoch, launch, default_style="full")
            if f.style != "none":
                return f
        return Fiducial(None, "none", press_s, "press", 0.0)

    intervals = []
    for k in kept:
        seg = next(s for s in segments if s.index == k["segment"])
        a, b = float(k["start_s"]), float(k["end_s"])
        take = take_by_id.get(k.get("take"))
        # A take whose press was made in this segment has its own patch at this edge, even when
        # the press came before the first frame (the model clamps it to 0, but the recorder still
        # shows the patch, right after the start flash). So does one pressed while resuming into
        # this segment (placed at the boundary; its patch shows here, after the start flash: C1c
        # review). The flash itself is enforced in resolve().
        o = (take.get("open") or {}) if take else {}
        cl = (take.get("close") or {}) if take else {}
        opened_here = take is not None and o.get("event") is not None \
            and (o.get("segment") == seg.index or shown_in(o.get("event")) == seg.index)
        closed_here = take is not None and cl.get("event") is not None \
            and (cl.get("segment") == seg.index or shown_in(cl.get("event")) == seg.index)
        if (a <= 1e-6 and not opened_here) or take is None:
            start = Boundary("start", "segment-start", seg.index, 0.0, seg.start_flash)
            if a > 1e-6:          # an interval not at a segment start without a take: keep its start as-is
                start = Boundary("start", "take-open", seg.index, a, Fiducial(None, "none", a, "press", 0.0))
        else:
            ev_id = (take.get("open") or {}).get("event")
            start = Boundary("start", "take-open", seg.index, a, event_fiducial(ev_id, seg.index, a), event=ev_id)
        if (b >= seg.duration_s - 1e-3 and not closed_here) or take is None:
            end = Boundary("end", "segment-end", seg.index, seg.duration_s, seg.stop_flash)
            if b < seg.duration_s - 1e-3:
                end = Boundary("end", "take-close", seg.index, b, Fiducial(None, "none", b, "press", 0.0))
        else:
            ev_id = (take.get("close") or {}).get("event")
            end = Boundary("end", "take-close", seg.index, b, event_fiducial(ev_id, seg.index, b), event=ev_id)
        intervals.append(Interval(seg.index, a, b, k.get("take"), start, end))
    # Silence trim and seam snap: at every take boundary, and at every join. The outer edges of a
    # recording with no takes are trimmed to its flashes only (the brief's zero-press rule).
    for i, iv in enumerate(intervals):
        iv.start.adjustable = (not whole) or i > 0
        iv.end.adjustable = (not whole) or i < len(intervals) - 1
        if i == 0 and iv.start.kind == "segment-start" and (take_by_id.get(iv.take) or {}).get("implicit"):
            iv.start.adjustable = False        # C1d: the implicit take starts as a no-take recording does
    marks = []
    for m in v2.get("marks") or []:
        seg_i = m.get("segment") or m.get("after_segment")
        if seg_i is None:
            continue
        after = m.get("segment") is None
        epoch, launch = epochs.get(seg_i, (None, None))
        shown = fiducial_segment(m.get("flash"), spans)
        # A mark whose patch went up in another segment than its press (held over a pause, or pressed
        # while resuming) is hidden where it was shown, as a stray patch (stray_patches), not here.
        fid = NO_FIDUCIAL if after or shown not in (None, seg_i) else \
            _fiducial_from(m.get("flash"), epoch, launch, default_style="full")
        ms = None if after else m.get("media_s")
        if ms is None and not after:            # a session-B mark (/1): its press, from seconds since launch
            ms = media_time(None, m.get("t"), epoch, launch)[0]
            if ms is None:
                ms = fid.stamp_s
        marks.append({"segment": seg_i, "media_s": ms,
                      "label": m.get("label") or "", "event": m.get("event"), "after": after, "fiducial": fid})
    layout = next((s.tracks for s in segments if s.tracks), [])
    first = segments[0] if segments else None
    audio_top = v2.get("audio") if isinstance(v2.get("audio"), dict) else {}
    return Plan(uid=str(v2.get("uid") or ""), stem=stem, segments=segments, intervals=intervals, marks=marks,
                tracks=list(layout), fps=first.fps if first else DEFAULT_FPS, size=first.size if first else None,
                pipeline=first.pipeline if first else "qsv", audio_bitrate=str(audio_top.get("bitrate") or "160k"),
                whole=whole, digest=source_digest(v2), notes=notes,
                patches=stray_patches(v2, intervals, {s.index for s in segments}, epochs))


STRAY_KINDS = ("take", "correct", "retake")       # event kinds whose patch can sit in kept material (C1c)
SHOWN_SLACK_S = (0.1, 0.5)                        # a patch's QPC may sit this far before / after its segment


def segment_spans(v2: dict) -> dict:
    """{segment: (epoch_qpc, duration_s)}: where each segment sits on the QPC clock."""
    return {int(s.get("index") or 1): (s.get("video_epoch_qpc_est"), s.get("duration_s"))
            for s in v2.get("segments") or []}


def fiducial_segment(fid: dict | None, spans: dict) -> int | None:
    """The segment a press's patch was shown in: as the recorder recorded it
    (`fiducial.segment`, C1c), else from its QPC stamp against each segment's
    span (older sidecars). None when nothing was shown."""
    if not isinstance(fid, dict) or fid.get("shown") is False or not fid.get("color"):
        return None
    if isinstance(fid.get("segment"), int):
        return fid["segment"]
    q = fid.get("shown_qpc")
    if not isinstance(q, (int, float)):
        return None
    before, after = SHOWN_SLACK_S
    for idx, (epoch, dur) in sorted(spans.items()):
        if isinstance(epoch, (int, float)) and epoch - before <= q <= epoch + float(dur or 0.0) + after:
            return idx
    return None


def stray_patches(v2: dict, intervals: list, segment_ids: set, epochs: dict) -> list:
    """Every patch that is not the fiducial of a kept edge: the render excludes
    edge patches with the material beside them, and must conceal any other one
    the cut keeps. Under C1c's correction chart that is the red patch of a
    close moved later (now inside its take), and in general any patch a later
    decision left inside kept material. Each is placed in the segment it was
    *shown* in: a press made while resuming is recorded in the pause, and one
    held over a pause is recorded in the segment before, but either shows its
    patch in the next segment, after the start flash (marks too; a mark shown
    where it was pressed is plan.marks' to hide).
    Patches in cut material are listed too; resolve() finds they are in no
    piece and skips them. Works for a C1a sidecar (`retake` events) unchanged."""
    # An edge owns a patch only in the segment where it found it: (event, segment) of every edge
    # with a fiducial. The same press's patch shown in another segment is a stray there.
    edges = {(bd.event, bd.segment) for iv in intervals for bd in (iv.start, iv.end)
             if bd.event is not None and bd.fiducial.style != "none"}
    superseded = {(c or {}).get("event") for t in v2.get("takes") or [] for c in t.get("superseded_closes") or []}
    spans = segment_spans(v2)
    out = []
    for ev in v2.get("events") or []:
        fid = ev.get("fiducial")
        kind = ev.get("kind")
        if not ev.get("accepted") or not isinstance(fid, dict):
            continue
        seg_i = fiducial_segment(fid, spans)
        if seg_i not in segment_ids or (ev.get("id"), seg_i) in edges:
            continue
        if kind not in STRAY_KINDS and not (kind == "mark" and ev.get("segment") != seg_i):
            continue                              # a mark shown where it was pressed: plan.marks hides it
        epoch, launch = epochs.get(seg_i, (None, None))
        f = _fiducial_from(fid, epoch, launch, default_style="patch")
        if f.style == "none":
            continue
        role = "superseded close" if ev.get("id") in superseded else (fid.get("kind") or kind)
        out.append({"segment": seg_i, "media_s": f.stamp_s if f.stamp_s is not None else ev.get("media_s"),
                    "fiducial": f, "event": ev.get("id"), "what": f"the {role} patch of event {ev.get('id')}"})
    return out


# ---------------------------------------------------------------------------
# 3. Detection (pure)
# ---------------------------------------------------------------------------


@dataclass
class Detection:
    found: bool
    reason: str | None = None
    first_s: float | None = None   # first frame showing the colour
    last_s: float | None = None    # last one
    next_s: float | None = None    # the frame after it (where kept material starts)
    frames: int = 0
    rgb: tuple | None = None       # the run's median colour, as decoded
    runs: int = 0


def is_color(rgb, target, tol: int = COLOR_TOL) -> bool:
    return max(abs(a - b) for a, b in zip(rgb, target)) <= tol


def mean_rgb(buf: bytes) -> tuple[int, int, int]:
    n = len(buf) // 3
    if not n:
        return (0, 0, 0)
    return tuple(round(sum(buf[c::3]) / n) for c in range(3))


def median_rgb(buf: bytes) -> tuple[int, int, int]:
    out = []
    for c in range(3):
        vals = sorted(buf[c::3])
        out.append(vals[len(vals) // 2] if vals else 0)
    return tuple(out)


def detect_run(frames: list, target: tuple, expect_s: float, max_frames: int, frame_s: float, *,
               window: tuple | None = None, file_end_s: float | None = None,
               max_offset_s: float | None = None) -> Detection:
    """Find the run of frames in `target`'s colour nearest `expect_s`.

    `frames` is [(pts_s, (r, g, b))] in order. A run longer than `max_frames`
    (the area is that colour anyway: a blue desktop corner) or touching the edge
    of the search window (the real run may extend past it) is not trusted. The
    caller falls back to the stamp, loudly, with the reason given here.
    `max_offset_s`: with a trustworthy stamp, a run further than this from it
    is another fiducial of the same colour (the real one hidden or merged),
    not this one."""
    if not frames:
        return Detection(False, "no frames decoded in the search window")
    hits = [i for i, (_, rgb) in enumerate(frames) if is_color(rgb, target)]
    tgt = "#%02X%02X%02X" % tuple(target)
    if not hits:
        closest = min((f for f in frames), key=lambda f: max(abs(a - b) for a, b in zip(f[1], target)))
        return Detection(False, f"no {tgt} frame in the search window (closest colour {closest[1]} at "
                                f"{closest[0]:.3f}s)")
    runs, cur = [], [hits[0]]
    for i in hits[1:]:
        if i == cur[-1] + 1:
            cur.append(i)
        else:
            runs.append(cur)
            cur = [i]
    runs.append(cur)
    run = min(runs, key=lambda r: abs(frames[r[0]][0] - expect_s))
    n = len(run)
    off = frames[run[0]][0] - expect_s
    if max_offset_s is not None and abs(off) > max_offset_s:
        return Detection(False, f"the nearest {tgt} run is {off * 1000:+.0f} ms from the stamp (more than "
                                f"{max_offset_s * 1000:.0f}): another fiducial, not this one", runs=len(runs), frames=n)
    if n > max_frames:
        return Detection(False, f"{n} consecutive {tgt} frames (at most {max_frames} expected): the area may "
                                f"simply be that colour", runs=len(runs), frames=n)
    lo, hi = run[0], run[-1]
    win_lo, win_hi = (window or (None, None))
    if lo == 0 and win_lo is not None and win_lo > frame_s / 2:
        return Detection(False, "the run starts at the edge of the search window", runs=len(runs), frames=n)
    if hi == len(frames) - 1 and win_hi is not None and (file_end_s is None or win_hi < file_end_s - frame_s):
        return Detection(False, "the run reaches the edge of the search window", runs=len(runs), frames=n)
    first, last = frames[lo][0], frames[hi][0]
    nxt = frames[hi + 1][0] if hi + 1 < len(frames) else last + frame_s
    colors = [frames[i][1] for i in run]
    med = tuple(sorted(c[k] for c in colors)[len(colors) // 2] for k in range(3))
    return Detection(True, None, first, last, nxt, n, med, len(runs))


def patch_crop(fid: Fiducial, seg: SegmentInfo, inset: float = PATCH_INSET) -> tuple[int, int, int, int] | None:
    """(w, h, x, y) for ffmpeg's crop: the patch rect (physical pixels of the
    screen it was shown on) scaled to this segment's output, inset a little
    and kept even (4:2:0)."""
    r = fid.rect
    if not isinstance(r, dict):
        return None
    scr = fid.screen if isinstance(fid.screen, dict) else None
    src = (scr["w"], scr["h"]) if scr else (seg.capture_size or seg.size)
    out = seg.size or seg.capture_size or src
    if not src or not out:
        return None
    sx, sy = out[0] / src[0], out[1] / src[1]
    x, y, w, h = r["x"] * sx, r["y"] * sy, r["w"] * sx, r["h"] * sy
    dx, dy = w * inset, h * inset
    x, y, w, h = x + dx, y + dy, w - 2 * dx, h - 2 * dy

    def even(v):
        return max(0, int(v) // 2 * 2)
    x, y, w, h = even(x), even(y), max(2, even(w)), max(2, even(h))
    w, h = min(w, out[0] - x), min(h, out[1] - y)
    if w < 2 or h < 2:
        return None
    return w, h, x, y


# ---------------------------------------------------------------------------
# 4. Audio: levels, seams, onset (pure)
# ---------------------------------------------------------------------------


def db(x: float) -> float:
    return -120.0 if x <= 1e-9 else 20 * math.log10(x)


def rms_levels(samples, rate: int = PCM_RATE, hop_s: float = LEVEL_HOP_S) -> list:
    """dBFS of each 10 ms frame of s16 samples (-120 for digital silence)."""
    h = max(1, int(round(rate * hop_s)))
    out = []
    for i in range(0, len(samples) - h + 1, h):
        blk = samples[i:i + h]
        out.append(round(db(math.sqrt(sum(v * v for v in blk) / h) / 32768), 1))
    return out


def place_seam(side: str, bound: float, limit: float, levels: list, t0: float, rcfg: RenderConfig,
               hop_s: float = LEVEL_HOP_S, has_sound: bool = True) -> tuple[float, str, dict]:
    """Where to cut at one adjustable boundary, given 10 ms levels starting at
    `t0`. Only ever moves into the kept side: later for a start, earlier for an
    end, never past `limit` (the interval's middle), so a fiducial or the
    discarded material is never re-admitted.

      silence      silence before the first sound (start) / after the last
                   (end): trimmed, keeping render.silence_keep_ms of it, at most
                   render.silence_max_trim_ms
      silence-max  nothing but silence for the whole max trim: that much is cut
      snap         otherwise the quietest 10 ms within render.snap_ms (inside
                   the silence when there is some), ties to the bound
      none         no usable audio here: the bound stands
    `has_sound` False (the whole interval never rises above the threshold: a
    silent screen walkthrough) turns trimming off, so a take with no sound at
    all is never shortened for being silent.
    Returns (seam, how, detail)."""
    W, T = rcfg.snap_ms / 1000, (rcfg.silence_max_trim_ms / 1000 if has_sound else 0.0)
    keep, thr = rcfg.silence_keep_ms / 1000, rcfg.silence_db
    reach = seam_reach(rcfg) if T > 0 else W
    sign = 1 if side == "start" else -1
    if side == "start":
        lo, hi = bound, min(limit, bound + reach)
    else:
        lo, hi = max(limit, bound - reach), bound

    def t_of(i):
        return t0 + i * hop_s
    idx = [i for i in range(len(levels)) if t_of(i) >= lo - 1e-6 and t_of(i) + hop_s <= hi + 1e-6]
    if not idx:
        return bound, "none", {"why": "no audio decoded at this boundary"}
    if side == "end":
        idx = idx[::-1]                        # walk away from the bound, into the kept side
    loud = next((n for n, i in enumerate(idx) if levels[i] >= thr), None)
    detail = {"threshold_db": thr}

    def dist(i):                               # how far a hop's middle lies from the bound
        return abs(t_of(i) + hop_s / 2 - bound)
    if loud is None:
        if T > 0 and (hi - lo) >= T + keep - hop_s / 2:      # no sound within max trim + keep: trim the max
            return bound + sign * T, "silence-max", detail
        return bound, "none", {**detail, "why": "only silence here (no sound to keep a margin from)"}
    if loud > 0:                               # some silence between the bound and the first sound
        edge = t_of(idx[loud]) if side == "start" else t_of(idx[loud]) + hop_s
        detail["sound_at_s"] = round(edge, 3)
        cand = edge - sign * keep
        if T > 0 and sign * (cand - bound) > hop_s / 2:
            cand = bound + sign * min(sign * (cand - bound), T)
            return cand, "silence", detail
        quiet = [i for i in idx[:loud] if dist(i) <= W + 1e-6]
    else:
        quiet = [i for i in idx if dist(i) <= W + 1e-6]
    if not quiet:
        return bound, "none", {**detail, "why": "no room to snap"}
    best = min(quiet, key=lambda i: (round(levels[i]), dist(i)))
    seam = t_of(best) + hop_s / 2
    detail["level_db"] = levels[best]
    return seam, "snap", detail


def seam_reach(rcfg: RenderConfig) -> float:
    """How far into the kept side a seam's audio is examined: the snap window,
    or the max trim plus the silence kept before the sound, whichever is more."""
    return max(rcfg.snap_ms, rcfg.silence_max_trim_ms + rcfg.silence_keep_ms) / 1000


def goertzel_db(block, rate: int, freq: float) -> float:
    w = 2 * math.pi * freq / rate
    c = 2 * math.cos(w)
    s1 = s2 = 0.0
    for v in block:
        s0 = v / 32768 + c * s1 - s2
        s2, s1 = s1, s0
    p = s1 * s1 + s2 * s2 - c * s1 * s2
    return db(math.sqrt(max(p, 0.0)) * 2 / len(block))


def tone_onset(samples, rate: int, t0: float, freq: float = 1000.0, win_s: float = 0.02, hop_s: float = 0.005,
               min_db: float = -35.0, hold_s: float = 0.06) -> float | None:
    """When a steady `freq` tone starts: the first 20 ms window where the tone is
    above `min_db` and dominates the window's level, held for 60 ms (the hold
    rejects clicks; A.1's probe used the same shape). The render probe found the
    clap 5 ms before the first magenta frame with exactly this detector."""
    w, h = int(rate * win_s), max(1, int(rate * hop_s))
    trace = []
    for i in range(0, len(samples) - w + 1, h):
        blk = samples[i:i + w]
        level = db(math.sqrt(sum(v * v for v in blk) / w) / 32768)
        trace.append((t0 + i / rate, goertzel_db(blk, rate, freq), level))
    need = max(1, int(round(hold_s / hop_s)))
    for j in range(len(trace) - need + 1):
        if all(trace[j + q][1] > min_db and trace[j + q][1] > trace[j + q][2] - 6 for q in range(need)):
            # The first window that hears the tone holds only a sliver of it. A window
            # sliding over a step at T reads half the plateau amplitude when it is
            # centred on T, so report that crossing: unbiased to within a hop.
            plateau = 10 ** (sorted(tr[1] for tr in trace[j:j + need])[need // 2] / 20)
            k = j
            while k > 0 and 10 ** (trace[k - 1][1] / 20) >= plateau / 2:
                k -= 1
            while k < j + need - 1 and 10 ** (trace[k][1] / 20) < plateau / 2:
                k += 1
            return round(trace[k][0] + win_s / 2, 4)
    return None


# ---------------------------------------------------------------------------
# 5. Resolve
# ---------------------------------------------------------------------------


@dataclass
class Piece:
    """One kept interval, on its segment's frame grid: frames [k1, k2), frame k
    at phase + k/fps (phase: where the grid sits, measured from decoded frames;
    0 for the recorder's own files, whose first frame is at 0)."""
    segment: int
    take: int | None
    k1: int
    k2: int
    fps: float
    out_start_s: float = 0.0
    phase: float = 0.0
    interval: int | None = None      # the plan interval it came from (records refer to it)

    @property
    def start_s(self) -> float:
        return self.phase + self.k1 / self.fps

    @property
    def end_s(self) -> float:
        return self.phase + self.k2 / self.fps

    @property
    def duration_s(self) -> float:
        return (self.k2 - self.k1) / self.fps


@dataclass
class Resolved:
    pieces: list
    xfades: list                   # seconds, one per join
    audio_offsets: dict            # segment -> seconds the audio is read later (calibration, apply mode)
    boundaries: list
    seams: list
    chapters: list
    marks_excluded: list
    calibration: list
    fallbacks: list                # every boundary or decision that did not go as designed, in words
    dropped: list
    concealed: list = field(default_factory=list)   # mark and stray patches hidden inside pieces (_conceal_patches)
    phases: dict = field(default_factory=dict)      # segment -> frame-grid phase (s), when not 0

    @property
    def output_duration_s(self) -> float:
        return round(sum(p.duration_s for p in self.pieces), 3)


class Analyzer:
    """What resolve() needs decoded. FfmpegAnalyzer is the real one; tests feed
    synthetic frames and PCM through the same interface."""

    def frames(self, seg: SegmentInfo, start_s: float, dur_s: float, crop, grid: tuple) -> list:
        """[(pts_s, rgb)] for the frames in [start_s, start_s + dur_s): the whole frame
        (crop None) averaged to grid, or the crop's median."""
        raise NotImplementedError

    def pcm(self, seg: SegmentInfo, start_s: float, dur_s: float, track: int | None = None) -> list:
        """Mono s16 samples at PCM_RATE from start_s; track None mixes every track."""
        raise NotImplementedError

    def max_db(self, seg: SegmentInfo, start_s: float, dur_s: float) -> float:
        """The loudest sample over the window, dBFS, every track mixed."""
        raise NotImplementedError

    def thumbs(self, seg: SegmentInfo, start_s: float, dur_s: float, size: tuple) -> list:
        """C1d: [(pts_s, grey bytes)] for the frames in [start_s, start_s + dur_s), each
        averaged down to `size` and made grey with screens' formula."""
        raise NotImplementedError


def _frame_index(t: float, fps: float, phase: float = 0.0) -> int:
    """The first frame at or after `t` (frames sit at phase + k/fps; a quarter
    frame of slack absorbs timestamp rounding)."""
    return int(math.ceil((t - phase) * fps - 0.25))


def grid_phase(frames: list, fps: float) -> float:
    """Where a file's frame grid sits: the median offset of decoded frame times
    from k/fps, in seconds (0 when within 2% of a frame, as for every file the
    recorder writes: the render probe found its first frame at 0.000). A file
    whose video starts later (an MP4 remuxed by another tool can start at
    0.021 s) gets its own grid, so quantising never lands a frame off."""
    offs = sorted(t * fps - math.floor(t * fps + 0.5) for t, _ in frames if isinstance(t, (int, float)))
    if not offs:
        return 0.0
    med = offs[len(offs) // 2]
    return 0.0 if abs(med) < 0.02 else med / fps


def _resolve_boundary(bd: Boundary, seg: SegmentInfo, analyzer: Analyzer, rcfg: RenderConfig,
                      cache: dict, run_of: int = 1, floor: float | None = None
                      ) -> tuple[float, dict, Detection | None]:
    """The time kept material starts (side start: the frame after the fiducial)
    or ends (side end: the fiducial's first frame), from detection, else from
    the stamp. Returns (time, sidecar record, detection)."""
    fid = bd.fiducial
    rec = {"side": bd.side, "kind": bd.kind, "segment": bd.segment, "event": bd.event,
           "nominal_s": _r(bd.nominal_s), "color": fid.color, "style": fid.style,
           "stamped_s": _r(fid.stamp_s), "stamp_quality": fid.stamp_quality}
    if fid.style == "none":
        rec.update(method="nothing-shown", bound_s=_r(bd.nominal_s), fallback=None)
        return bd.nominal_s, rec, None
    if fid.style == "screen":
        return _resolve_screen(bd, seg, analyzer, rcfg, rec, floor=floor)
    key = (bd.segment, bd.kind, bd.event)
    center = fid.stamp_s if fid.stamp_s is not None else bd.nominal_s
    half = rcfg.search_s * (2 if fid.stamp_quality in ("approximate", "press") else 1)
    ss = max(0.0, center - half)
    dur = max(0.1, min(center + half, seg.duration_s + 0.1) - ss)
    frame_s = 1 / seg.fps
    max_frames = (int(math.ceil((fid.duration_s + 0.15) * seg.fps)) + 1) * max(1, run_of)
    det = cache.get(key)
    if det is None:
        try:
            if fid.style == "patch":
                crop = patch_crop(fid, seg)
                if crop is None:
                    raise AnalysisError("the patch rect is missing or off the frame")
                frames = analyzer.frames(seg, ss, dur, crop, PATCH_GRID)
            else:
                frames = analyzer.frames(seg, ss, dur, None, FLASH_GRID)
            if frames:
                cache.setdefault(("phase", seg.index), grid_phase(frames, seg.fps))
            # A QPC stamp is when the window went up: the first frame follows within a frame or two
            # (probe: 38-48 ms). Allow a generous 300 ms past the fiducial's own length, no more.
            trusted = fid.stamp_quality in ("qpc", "launch")
            det = detect_run(frames, hex_rgb(fid.color), center + FIDUCIAL_LAG_S, max_frames, frame_s,
                             window=(ss, ss + dur), file_end_s=seg.duration_s,
                             max_offset_s=(0.3 + fid.duration_s * max(0, run_of - 1)) if trusted else None)
        except AnalysisError as exc:
            det = Detection(False, f"could not decode the search window: {exc}")
        cache[key] = det
    if det.found:
        bound = det.next_s if bd.side == "start" else det.first_s
        rec.update(method="detected", detected_s=_r(det.first_s), last_frame_s=_r(det.last_s), frames=det.frames,
                   measured_rgb=list(det.rgb) if det.rgb else None,
                   delta_ms=_r((det.first_s - fid.stamp_s) * 1000, 1) if fid.stamp_s is not None else None,
                   fallback=None)
    else:
        if fid.stamp_s is None:
            bound = bd.nominal_s
        elif bd.side == "start":
            bound = fid.stamp_s + FIDUCIAL_LAG_S + fid.duration_s + FALLBACK_MARGIN_S
        else:
            bound = fid.stamp_s
        rec.update(method="stamp", fallback=det.reason)
    bound = min(max(bound, 0.0), seg.duration_s)
    rec["bound_s"] = _r(bound)
    return bound, rec, det


SCREEN_CHUNK_S = 2.0               # a screen run reaching the window's edge is followed this much further at a time
SCREEN_FOLLOW_S = 10.0             # ... up to this far past a sampled (not learned-on) event's stamp
SELF_ANCHOR_MIN_FRAMES = 8         # a screen found from the video's own frame must hold at least this long


def screen_edge(frames: list, ref: dict, stamp: float, side: str, thr: "screens.Thresholds",
                loose: "screens.Thresholds", anchor_t: float | None = None
                ) -> tuple[int | None, int | None, int | None, str | None]:
    """Where a learned screen's run of frames is around one event, in decoded
    thumbnails [(pts, grey)]: (first, last, anchor) indices of the run, or
    (None, None, None, reason).

    The anchor is a frame that matches the live reference within `loose` (the
    decoded frames went through the encoder and ffmpeg's scaler; the reference
    through GDI): for an end screen appearing, the matching frame nearest the
    stamp (the first sample that saw it); for a start screen going away, the
    matching frame nearest half a sample period before the stamp (the stamp is
    the first sample that no longer saw it). `anchor_t` instead takes the frame
    nearest that time as the anchor, unchecked against the reference (the
    video's own frame where the screen is known to be up). The run is then
    followed in both directions while frames match the *anchor* (same
    pipeline, so the live thresholds hold; loose ones, so a fade's half-way
    frames count as the screen and are cut with it)."""
    if not frames:
        return None, None, None, "no frames decoded in the search window"
    for t, g in frames:
        if len(g) != len(ref["thumb"]):
            return None, None, None, f"decoded thumbnail has {len(g)} pixels, the reference {len(ref['thumb'])}"
    if anchor_t is not None:
        best = min(range(len(frames)), key=lambda i: abs(frames[i][0] - anchor_t))
    else:
        target = stamp if side == "end" else stamp - ref.get("period_s", 0.25) / 2
        best = None
        for i, (t, g) in enumerate(frames):
            if side == "start" and t >= stamp + 1e-6:
                continue                       # the screen was already gone by the stamp
            if screens.is_match(screens.distance(g, ref["thumb"], thr.changed_level), loose):
                if best is None or abs(t - target) < abs(frames[best][0] - target):
                    best = i
        if best is None:
            closest = min((screens.distance(g, ref["thumb"], thr.changed_level).mad, t) for t, g in frames)
            return None, None, None, (f"no frame matches the {ref.get('screen')} screen (closest mean difference "
                                      f"{closest[0]:.1f} at {closest[1]:.3f}s)")
    lo, hi = run_around(frames, best, thr, loose)
    return lo, hi, best, None


def run_around(frames: list, anchor: int, thr: "screens.Thresholds", loose: "screens.Thresholds") -> tuple[int, int]:
    """The run of frames matching frame `anchor` (within `loose`), as (first, last) indices."""
    a = frames[anchor][1]

    def same(i):
        return screens.is_match(screens.distance(frames[i][1], a, thr.changed_level), loose)
    lo = hi = anchor
    while lo > 0 and same(lo - 1):
        lo -= 1
    while hi < len(frames) - 1 and same(hi + 1):
        hi += 1
    return lo, hi


def _resolve_screen(bd: Boundary, seg: SegmentInfo, analyzer: Analyzer, rcfg: RenderConfig,
                    rec: dict, floor: float | None = None) -> tuple[float, dict, None]:
    """C1d: an edge made by a learned screen, to the exact frame. An end screen:
    kept material ends at its first frame, followed back past the window when
    the run reaches its edge, down to `floor` (where the interval starts: what
    lies before is cut anyway) and at most render.screen_lookback_s for a screen
    learned while it was showing (SCREEN_FOLLOW_S otherwise). A start screen:
    kept material starts at the frame after its last one.

    When no decoded frame matches the live reference (another colour pipeline,
    say), an end screen is found from the video's own frame where it is known
    to be up: the learn's capture, or the first sample that saw it (loud, in
    the fallbacks). Fallback, loud: an end at the stamp less a sample period and
    the margin, a start at the stamp plus the margin, both erring toward
    cutting more. For a screen learned while showing, a fallback cannot know
    where it began: the line says that its earlier frames may remain."""
    fid = bd.fiducial
    ref = fid.reference or {}
    rec.update(screen=ref.get("screen"), ref=ref.get("ref"), change=ref.get("change"),
               from_learn=bool(ref.get("from_learn")))
    frame_s = 1 / seg.fps
    stamp = fid.stamp_s if fid.stamp_s is not None else bd.nominal_s
    side = "end" if bd.side == "end" else "start"
    period = ref.get("period_s", 0.25)

    def fallback(reason: str) -> tuple[float, dict, None]:
        bound = stamp - period - FALLBACK_MARGIN_S if side == "end" else stamp + FALLBACK_MARGIN_S
        if side == "end" and ref.get("from_learn"):
            reason += "; it was learned while showing, so its frames before the press may remain"
        rec.update(method="stamp", fallback=reason)
        bound = min(max(bound, 0.0), seg.duration_s)
        rec["bound_s"] = _r(bound)
        return bound, rec, None

    if ref.get("missing") or not ref.get("thumb"):
        return fallback("the sidecar does not hold this screen's reference")
    thr = screens.Thresholds.from_dict(ref.get("thresholds"))
    loose = thr.loosened(rcfg.screen_slack_mad, rcfg.screen_slack_pct)
    floor_s = max(0.0, floor or 0.0) if side == "end" else 0.0
    reach = rcfg.screen_lookback_s if (side == "end" and ref.get("from_learn")) else SCREEN_FOLLOW_S
    w0 = max(floor_s, stamp - rcfg.search_s - (period if side == "end" else 0.0))
    w1 = min(seg.duration_s, stamp + rcfg.search_s)
    limit_lo = max(floor_s, stamp - reach) if side == "end" else w0
    limit_hi = min(seg.duration_s, stamp + reach) if side == "start" else w1
    if w1 - w0 < frame_s:
        return fallback("no room to look: the edge sits at the start of its interval")
    warning = None
    try:
        frames = list(analyzer.thumbs(seg, w0, w1 - w0, ref["size"]))
        lo, hi, anchor, reason = screen_edge(frames, ref, stamp, side, thr, loose)
        if reason is not None and side == "end" and frames and "pixels" not in reason:
            # Where the screen is known to be up, as the video shows it (FIDUCIAL_LAG_S: a change reaches
            # the capture ~40 ms after it happens): the learn's capture, or the middle of the samples
            # that confirmed a sampled appearance, never its very first one (review, second pass).
            if ref.get("from_learn"):
                at = stamp + float(ref.get("waited_s") or 0.0) + FIDUCIAL_LAG_S
            else:
                at = stamp + period * (max(1, thr.hysteresis) - 1) / 2 + FIDUCIAL_LAG_S + frame_s
            lo2, hi2, anchor2, _ = screen_edge(frames, ref, stamp, side, thr, loose, anchor_t=at)
            began = frames[lo2][0] if anchor2 is not None else None
            # a sampled appearance began after the last sample that did not see it
            plausible = ref.get("from_learn") or (began is not None and began >= stamp - 2 * period - 0.1)
            if anchor2 is not None and hi2 - lo2 + 1 >= SELF_ANCHOR_MIN_FRAMES and plausible:
                warning = (f"the {ref.get('screen')} screen's live reference did not match the decoded video "
                           f"({reason}); found it from the video's own frame at {frames[anchor2][0]:.3f}s instead")
                rec["anchored"] = "video"
                lo, hi, anchor, reason = lo2, hi2, anchor2, None
        while reason is None and side == "end" and lo == 0 and w0 > limit_lo + frame_s / 2:
            nw0 = max(limit_lo, w0 - SCREEN_CHUNK_S)
            more = [f for f in analyzer.thumbs(seg, nw0, w0 - nw0, ref["size"]) if f[0] < frames[0][0] - frame_s / 2]
            w0 = nw0
            if not more:
                break
            frames, anchor = more + frames, anchor + len(more)
            lo, hi = run_around(frames, anchor, thr, loose)
        while reason is None and side == "start" and hi == len(frames) - 1 and w1 < limit_hi - frame_s / 2:
            nw1 = min(limit_hi, w1 + SCREEN_CHUNK_S)
            more = [f for f in analyzer.thumbs(seg, w1, nw1 - w1, ref["size"]) if f[0] > frames[-1][0] + frame_s / 2]
            w1 = nw1
            if not more:
                break
            frames = frames + more
            lo, hi = run_around(frames, anchor, thr, loose)
    except AnalysisError as exc:
        reason = f"could not decode the search window: {exc}"
    if reason is not None:
        return fallback(reason)
    first, last = frames[lo][0], frames[hi][0]
    nxt = frames[hi + 1][0] if hi + 1 < len(frames) else last + frame_s
    if side == "end":
        bound = first
        if lo == 0 and first > floor_s + frame_s / 2 and w0 > floor_s + frame_s / 2:
            warning = (f"the {ref.get('screen')} screen was already up {stamp - first:.1f} s before; followed it "
                       f"no further (render.screen_lookback_s), so earlier frames of it may remain")
    else:
        bound = nxt
        if hi == len(frames) - 1 and w1 < seg.duration_s - frame_s:
            bound = w1                                  # still up past the window: cut up to where we looked
            warning = (f"the {ref.get('screen')} screen was still up {last - stamp:.1f} s after the sampler saw it "
                       f"go; the cut starts after the frames examined")
    rec.update(method="detected", detected_s=_r(first), last_frame_s=_r(last), frames=hi - lo + 1,
               delta_ms=_r((bound - stamp) * 1000, 1), fallback=None)
    if warning:
        rec["warning"] = warning
    bound = min(max(bound, 0.0), seg.duration_s)
    rec["bound_s"] = _r(bound)
    return bound, rec, None


def _decode_levels(analyzer: Analyzer, seg: SegmentInfo, start: float, end: float) -> tuple[list, float]:
    start = max(0.0, start)
    end = min(end, seg.duration_s)
    if end - start < LEVEL_HOP_S:
        return [], start
    samples = analyzer.pcm(seg, start, end - start, None)
    return rms_levels(samples, PCM_RATE), start


def resolve(plan: Plan, analyzer: Analyzer, rcfg: RenderConfig, *, say: Callable[[str], None] = lambda s: None
            ) -> Resolved:
    """Decide every boundary and seam of `plan` from the media itself."""
    fallbacks, boundaries, seams, dropped = [], [], [], []
    cache: dict = {}
    pieces = []
    flash_bounds: dict = {}

    def record(rec, n):
        rec["interval"] = n
        boundaries.append(rec)
        if rec.get("warning"):                  # C1d: found, but with a caveat worth saying as loudly
            fallbacks.append(f"segment {rec['segment']} {rec['kind']}: {rec['warning']}")
            event(log, logging.WARNING, "render.screen_warning", segment=rec["segment"], event=rec.get("event"),
                  warning=rec["warning"])
        if rec["method"] == "stamp":
            what = {"segment-start": "start flash", "segment-end": "stop flash", "take-open": "take patch",
                    "take-close": "take-close patch"}[rec["kind"]]
            if rec.get("style") == "screen":
                what = f"{rec.get('screen')} screen" + (" (gone)" if rec.get("change") == "gone" else "")
            fallbacks.append(f"segment {rec['segment']} {what} near {fmt_mmss(rec['stamped_s'])}: "
                             f"not found ({rec['fallback']}); used the stamp")
            event(log, logging.WARNING, "render.boundary_fallback", **{k: rec[k] for k in
                  ("segment", "kind", "event", "stamped_s", "bound_s", "fallback")})

    def segment_flashes(seg):
        """Where the segment's flashes leave clean material: (after the magenta,
        before the green, their records). Every piece of the segment stays
        inside these, whatever its own boundaries are: a take pressed during a
        flash must not carry the flash into the cut."""
        if seg.index not in flash_bounds:
            lo, lrec, _ = _resolve_boundary(Boundary("start", "segment-start", seg.index, 0.0, seg.start_flash),
                                            seg, analyzer, rcfg, cache)
            hi, hrec, _ = _resolve_boundary(Boundary("end", "segment-end", seg.index, seg.duration_s,
                                                     seg.stop_flash), seg, analyzer, rcfg, cache)
            flash_bounds[seg.index] = [lo, hi, lrec, hrec, False, False]
        return flash_bounds[seg.index]

    for n, iv in enumerate(plan.intervals):
        seg = plan.seg(iv.segment)
        say(f"interval {n + 1}/{len(plan.intervals)}: segment {seg.index} {fmt_mmss(iv.start_s)}-{fmt_mmss(iv.end_s)}")
        a, arec, _ = _resolve_boundary(iv.start, seg, analyzer, rcfg, cache)
        b, brec, _ = _resolve_boundary(iv.end, seg, analyzer, rcfg, cache, floor=a)
        fb = segment_flashes(seg)
        record(arec, n)
        record(brec, n)
        if iv.start.kind == "segment-start":
            fb[4] = True
        elif a < fb[0]:                        # a take opened during the start flash: start after it
            arec["clamped_to"] = "start flash"
            arec["bound_s"] = _r(fb[0])
            a = fb[0]
            if not fb[4]:
                record(dict(fb[2]), n)
                fb[4] = True
        if iv.end.kind == "segment-end":
            fb[5] = True
        elif b > fb[1]:                        # a take closed after the stop flash: end before it
            brec["clamped_to"] = "stop flash"
            brec["bound_s"] = _r(fb[1])
            b = fb[1]
            if not fb[5]:
                record(dict(fb[3]), n)
                fb[5] = True
        sa, sb = a, b
        has_sound = True
        if plan.tracks and (iv.start.adjustable or iv.end.adjustable) and rcfg.silence_max_trim_ms > 0:
            try:
                peak = analyzer.max_db(seg, a, b - a)
                has_sound = peak >= rcfg.silence_db
                if not has_sound:
                    fallbacks.append(f"interval {n + 1} (segment {seg.index}) is silent throughout (peak "
                                     f"{peak:.0f} dBFS): its edges were not trimmed for silence")
            except AnalysisError as exc:
                has_sound = False
                fallbacks.append(f"interval {n + 1} (segment {seg.index}): loudness not measured ({exc}); "
                                 f"silence not trimmed")
        for bd, bound, other in ((iv.start, a, b), (iv.end, b, a)):
            if not bd.adjustable or b - a <= rcfg.min_interval_ms / 1000 or not plan.tracks:
                continue
            reach = seam_reach(rcfg)
            lo, hi = (bound, bound + reach) if bd.side == "start" else (bound - reach, bound)
            try:
                levels, t0 = _decode_levels(analyzer, seg, lo, hi)
            except AnalysisError as exc:
                levels, t0 = [], lo
                fallbacks.append(f"segment {seg.index} seam at {fmt_mmss(bound)}: audio not decoded ({exc}); "
                                 f"cut at the fiducial")
            limit = (a + b) / 2
            seam, how, detail = place_seam(bd.side, bound, limit, levels, t0, rcfg, has_sound=has_sound)
            seams.append({"interval": n, "side": bd.side, "kind": bd.kind, "segment": seg.index,
                          "bound_s": _r(bound), "seam_s": _r(seam), "offset_ms": _r((seam - bound) * 1000, 1),
                          "how": how, **detail})
            if bd.side == "start":
                sa = seam
            else:
                sb = seam
        phase = cache.get(("phase", seg.index), 0.0)
        k1, k2 = _frame_index(sa, seg.fps, phase), _frame_index(sb, seg.fps, phase)
        k1 = max(k1, _frame_index(a, seg.fps, phase))    # quantising never re-admits a fiducial frame
        k2 = min(k2, _frame_index(b, seg.fps, phase))
        if (k2 - k1) / seg.fps < rcfg.min_interval_ms / 1000:
            dropped.append({"interval": n, "segment": seg.index, "take": iv.take, "kept_ms": _r(
                max(0, k2 - k1) / seg.fps * 1000, 0)})
            fallbacks.append(f"interval {n + 1} (segment {seg.index}, {fmt_mmss(iv.start_s)}) dropped: "
                             f"{max(0, k2 - k1)} frame(s) left after excluding the fiducials")
            continue
        pieces.append(Piece(seg.index, iv.take, k1, k2, seg.fps, phase=phase, interval=n))
    concealed = _conceal_patches(plan, pieces, analyzer, rcfg, cache, fallbacks, seams, dropped)
    out = 0.0
    for p in pieces:
        p.out_start_s = round(out, 6)
        out += p.duration_s
    calibration, offsets = _calibrate(plan, analyzer, rcfg, cache, fallbacks)
    xfades = []
    for j in range(len(pieces) - 1):
        p, q = pieces[j], pieces[j + 1]
        want = rcfg.crossfade_ms / 1000
        room_after = plan.seg(p.segment).duration_s - (p.end_s + offsets.get(p.segment, 0.0))
        room_before = q.start_s + offsets.get(q.segment, 0.0)
        half = min(want / 2, room_after, room_before, p.duration_s / 2, q.duration_s / 2)
        d = 2 * half if 2 * half >= MIN_XFADE_S else 0.0
        if want > 0 and d < want - 1e-6:
            fallbacks.append(f"join {j + 1}: crossfade {round(d * 1000)} ms instead of {rcfg.crossfade_ms} "
                             f"(no room)")
        xfades.append(round(d, 6))
    chapters, excluded = map_marks(plan.marks, pieces)
    phases = {k[1]: round(v, 6) for k, v in cache.items() if isinstance(k, tuple) and k[0] == "phase" and v}
    return Resolved(pieces=pieces, xfades=xfades, audio_offsets=offsets, boundaries=boundaries, seams=seams,
                    chapters=chapters, marks_excluded=excluded, calibration=calibration, fallbacks=fallbacks,
                    dropped=dropped, concealed=concealed, phases=phases)


def conceal_rect(fid: Fiducial, out_size: tuple, margin: int = 2) -> tuple[int, int, int, int] | None:
    """(w, h, x, y) in output pixels covering a patch, a little larger than it
    (chroma subsampling bleeds its colour a pixel), even-aligned, on the frame."""
    r, scr = fid.rect, fid.screen
    if not isinstance(r, dict) or not out_size:
        return None
    src = (scr["w"], scr["h"]) if isinstance(scr, dict) else out_size
    sx, sy = out_size[0] / src[0], out_size[1] / src[1]
    x0, y0 = max(0, int(r["x"] * sx) - margin), max(0, int(r["y"] * sy) - margin)
    x1 = min(out_size[0], int(math.ceil((r["x"] + r["w"]) * sx)) + margin)
    y1 = min(out_size[1], int(math.ceil((r["y"] + r["h"]) * sy)) + margin)
    x0, y0 = x0 // 2 * 2, y0 // 2 * 2
    x1, y1 = min(out_size[0], (x1 + 1) // 2 * 2), min(out_size[1], (y1 + 1) // 2 * 2)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return x1 - x0, y1 - y0, x0, y0


def _concealables(plan: Plan) -> list:
    """Marks and stray take / correction patches, as one list in press order
    within each kind: {"mark": n or None, "what", "segment", "media_s",
    "fiducial", "event", "after"}."""
    items = []
    for n, m in enumerate(plan.marks, 1):
        items.append({"mark": n, "what": f"mark {n}", "segment": m.get("segment"), "media_s": m.get("media_s"),
                      "fiducial": m.get("fiducial") or NO_FIDUCIAL, "event": m.get("event"),
                      "after": bool(m.get("after"))})
    for sp in plan.patches:
        items.append({"mark": None, "what": sp["what"], "segment": sp["segment"], "media_s": sp.get("media_s"),
                      "fiducial": sp["fiducial"], "event": sp.get("event"), "after": False})
    return items


def _conceal_patches(plan: Plan, pieces: list, analyzer: Analyzer, rcfg: RenderConfig, cache: dict,
                     fallbacks: list, seams: list | None = None, dropped: list | None = None) -> list:
    """Hide every fiducial that sits inside kept material without being a kept
    edge's own: mark patches (a mark is never a cut) and, since C1c, stray take
    and correction patches (a close moved later leaves its red patch inside the
    take). Find its frames like any fiducial and hide them: in the cut, the
    patch's corner shows what it showed on the frame before the patch, for the
    patch's ~200 ms (a full-screen mark flash, `mark_style = "full"`, holds the
    whole frame). A patch that is not found is left as it is, and said so.

    Two cases are cut rather than concealed: a patch touching a piece's edge
    (a mark pressed just before a take closes or a flash) has those few
    frames trimmed off that edge, since the material beside it is excluded
    anyway. And patches shown back to back in one colour make one long run,
    which is found and hidden once. Runs before trimming, so the pieces'
    output times are computed from the final pieces.

    Each entry: {"mark": n | None, "what", "event", "piece", ...}; `mark` is
    the mark's number for a mark and None for a stray patch."""
    out, seen = [], set()
    items = _concealables(plan)
    for item in items:
        fid = item["fiducial"]
        n, what = item["mark"], item["what"]
        if item["after"] or fid.style == "none" or not isinstance(item.get("media_s"), (int, float)):
            continue
        try:
            seg = plan.seg(item["segment"])
        except KeyError:
            continue
        at = fid.stamp_s if fid.stamp_s is not None else item["media_s"]
        reach = rcfg.search_s + fid.duration_s + 0.1
        if not any(p.segment == seg.index and p.start_s - reach <= at <= p.end_s + reach for p in pieces):
            continue                                  # nowhere near kept material: not in the cut
        kind = "mark" if n is not None else "patch"
        bd = Boundary("start", kind, seg.index, item["media_s"], fid, event=item.get("event"))
        near = sum(1 for o in items if o is not item and o["segment"] == seg.index and not o["after"]
                   and o["fiducial"].stamp_s is not None and o["fiducial"].color == fid.color
                   and abs(o["fiducial"].stamp_s - at) < 2 * (fid.duration_s + 0.1))
        _, _, det = _resolve_boundary(bd, seg, analyzer, rcfg, cache, run_of=1 + near)
        where = f"{what} (segment {seg.index} at {fmt_mmss(item['media_s'])})"
        if det is None or not det.found:
            fallbacks.append(f"{where}: its {fid.color} patch was not found "
                             f"({det.reason if det else 'not searched'}); if it is in the cut, it is left there")
            continue
        phase = cache.get(("phase", seg.index), 0.0)
        first, last = _frame_index(det.first_s, seg.fps, phase), _frame_index(det.last_s, seg.fps, phase)
        hit = [(j, p) for j, p in enumerate(pieces) if p.segment == seg.index and first < p.k2 and last >= p.k1]
        if not hit:
            continue                                  # the patch fell in excluded material
        if (seg.index, first, last) in seen:
            continue                                  # one run for patches shown together: hidden once
        seen.add((seg.index, first, last))
        j, piece = hit[0]
        if len(hit) > 1:
            fallbacks.append(f"{where}: its patch spans two pieces; it is left in the cut")
            continue
        base = {"mark": n, "what": what, "event": item.get("event")}
        if first <= piece.k1 or last >= piece.k2 - 1:
            # At the piece's edge: cut those frames off that edge instead (the material on the
            # other side is excluded anyway), as long as the piece stays long enough.
            k1, k2 = (last + 1, piece.k2) if first <= piece.k1 else (piece.k1, first)
            if (k2 - k1) / piece.fps >= rcfg.min_interval_ms / 1000:
                event(log, logging.INFO, "render.patch_trimmed", what=what, segment=seg.index,
                      frames=piece.k2 - piece.k1 - (k2 - k1))
                side = "start" if k1 != piece.k1 else "end"
                before = piece.start_s if side == "start" else piece.end_s
                piece.k1, piece.k2 = k1, k2
                after = piece.start_s if side == "start" else piece.end_s
                if seams is not None:              # the seam record says where the edge really is
                    rec = next((x for x in seams if x["interval"] == piece.interval and x["side"] == side), None)
                    if rec is None:
                        rec = {"interval": piece.interval, "side": side, "kind": f"{kind}-patch",
                               "segment": seg.index, "bound_s": _r(before), "how": "none"}
                        seams.append(rec)
                    rec.update(seam_s=_r(after), moved_by="mark patch" if n is not None else "stray patch",
                               offset_ms=_r((after - rec["bound_s"]) * 1000, 1))
                out.append({**base, "piece": j, "trimmed": True, "frames": det.frames,
                            "detected_s": _r(det.first_s), "style": fid.style})
            else:                                     # hardly anything but the patch: drop the piece
                fallbacks.append(f"{where}: its patch fills most of a {piece.duration_s * 1000:.0f} ms piece; "
                                 f"the piece is dropped")
                if dropped is not None:
                    dropped.append({"interval": piece.interval, "segment": seg.index, "take": piece.take,
                                    "kept_ms": _r(piece.duration_s * 1000, 0), "why": f"{what}'s patch"})
                piece.k2 = piece.k1
            continue
        rect = None
        if fid.style == "patch":
            rect = conceal_rect(fid, plan.size or seg.size)
            if rect is None:
                fallbacks.append(f"{where}: its patch rect is unusable; it is left in the cut")
                continue
        out.append({**base, "piece": j, "abs_first": first, "abs_last": last, "rect": list(rect) if rect else None,
                    "detected_s": _r(det.first_s), "frames": det.frames, "style": fid.style})
    for c in out:                              # frame numbers within the piece, once every edge trim is done
        if c.get("trimmed"):
            continue
        p = pieces[c["piece"]]
        first, last = c.pop("abs_first"), c.pop("abs_last")
        if first <= p.k1 or last >= p.k2 - 1:  # a later edge trim reached it: say so rather than guess
            c.update(trimmed=False, first=None, last=None, replace=None, skipped=True)
            fallbacks.append(f"{c['what']}: its patch ended up at a piece's edge; it is left in the cut")
            continue
        c.update(first=first - p.k1, last=last - p.k1, replace=first - p.k1 - 1)
    # pieces a patch emptied go, and the entries follow their pieces' new positions
    keep = [j for j, p in enumerate(pieces) if p.k2 > p.k1]
    where = {old: new for new, old in enumerate(keep)}
    pieces[:] = [pieces[j] for j in keep]
    out = [c for c in out if not c.get("skipped") and c["piece"] in where]
    for c in out:
        c["piece"] = where[c["piece"]]
    return out


def _calibrate(plan: Plan, analyzer: Analyzer, rcfg: RenderConfig, cache: dict, fallbacks: list
               ) -> tuple[list, dict]:
    """The clap residual per segment (A.1's proposal): the 1 kHz onset in the
    system track against the first magenta frame, minus what the stamps
    predicted (tone request vs flash shown). Recorded always (mode measure);
    applied, as a per-segment audio offset, only in mode apply and only when it
    exceeds a frame. See notes.md for why measure is the default."""
    out, offsets = [], {}
    if rcfg.av_calibration == "off" or not plan.tracks:
        return out, offsets
    for seg in plan.segments:
        rec = {"segment": seg.index}
        clap = seg.clap or {}
        sys_track = next((i for i, t in enumerate(seg.tracks) if "system" in t), None)
        why = None
        if not clap.get("played") or clap.get("requested_s") is None:
            why = "no clap was played"
        elif seg.start_flash.style == "none" or seg.start_flash.stamp_s is None:
            why = "no start flash"
        elif sys_track is None:
            why = "no system-audio track"
        if why:
            out.append({**rec, "status": "skipped", "why": why})
            continue
        bd = Boundary("start", "segment-start", seg.index, 0.0, seg.start_flash)
        _, _, det = _resolve_boundary(bd, seg, analyzer, rcfg, cache)
        if det is None or not det.found:
            out.append({**rec, "status": "skipped", "why": "start flash not detected"})
            continue
        t0 = max(0.0, clap["requested_s"] - 0.2)
        try:
            samples = analyzer.pcm(seg, t0, 0.8, sys_track)
        except AnalysisError as exc:
            out.append({**rec, "status": "skipped", "why": f"audio not decoded: {exc}"})
            continue
        onset = tone_onset(samples, PCM_RATE, t0)
        if onset is None:
            out.append({**rec, "status": "skipped", "why": "no 1 kHz onset found"})
            continue
        predicted = clap["requested_s"] - seg.start_flash.stamp_s
        measured = onset - det.first_s
        residual = measured - predicted
        rec.update(status="measured", onset_s=_r(onset, 4), first_magenta_s=_r(det.first_s, 4),
                   measured_ms=_r(measured * 1000, 1), predicted_ms=_r(predicted * 1000, 1),
                   residual_ms=_r(residual * 1000, 1), applied=False)
        if rcfg.av_calibration == "apply" and abs(residual) > 1 / seg.fps:
            offsets[seg.index] = residual
            rec["applied"] = True
            fallbacks.append(f"segment {seg.index}: audio shifted {residual * 1000:+.0f} ms (clap calibration)")
        out.append(rec)
    return out, offsets


def map_marks(marks: list, pieces: list) -> tuple[list, list]:
    """Marks as chapters of the cut: each at its output time, titled with its
    label (or "mark N"), running to the next one; a leading "start" chapter
    when the first mark is not at the beginning. Marks in excluded material
    are listed, not lost."""
    placed, excluded = [], []
    for n, m in enumerate(marks, 1):
        t = m.get("media_s")
        title = m.get("label") or f"mark {n}"
        out_t = None
        if m.get("after"):                     # pressed while paused: the chapter starts at the resume
            nxt = next((p for p in pieces if p.segment > m["segment"]), None)
            out_t = nxt.out_start_s if nxt is not None else None
        elif isinstance(t, (int, float)):
            hit = next((p for p in pieces if p.segment == m["segment"] and p.start_s - 1e-6 <= t < p.end_s), None)
            out_t = hit.out_start_s + (t - hit.start_s) if hit is not None else None
            if hit is None:
                # Pressed in the moments just before a piece begins (with the take press: the patches
                # and the trimmed silence come first): the chapter starts with the piece.
                nxt = next((p for p in pieces if p.segment == m["segment"] and 0 < p.start_s - t <= MARK_SNAP_S),
                           None)
                if nxt is not None and not any(p.segment == m["segment"] and t < p.start_s and p.end_s > t
                                               and p is not nxt and p.start_s < nxt.start_s for p in pieces):
                    out_t = nxt.out_start_s
        if out_t is None:
            excluded.append({"mark": n, "segment": m.get("segment"), "media_s": _r(t), "label": m.get("label"),
                             "after_pause": bool(m.get("after"))})
            continue
        placed.append({"title": title, "start_s": round(out_t, 3),
                       "source": {"segment": m["segment"], "media_s": _r(t), "after_pause": bool(m.get("after"))},
                       "mark": n})
    placed.sort(key=lambda c: c["start_s"])
    merged = []
    for c in placed:                           # marks at the same instant: one chapter, both titles
        if merged and abs(c["start_s"] - merged[-1]["start_s"]) < 0.001:
            merged[-1]["title"] += f" · {c['title']}"
            merged[-1].setdefault("also", []).append(c["mark"])
        else:
            merged.append(c)
    placed = merged
    if not placed:
        return [], excluded
    total = round(sum(p.duration_s for p in pieces), 3)
    if placed[0]["start_s"] > 0.5:
        placed.insert(0, {"title": "start", "start_s": 0.0, "source": None, "mark": None})
    for i, c in enumerate(placed):
        c["end_s"] = placed[i + 1]["start_s"] if i + 1 < len(placed) else total
    return [c for c in placed if c["end_s"] > c["start_s"]], excluded


# ---------------------------------------------------------------------------
# 6. argv + chapters (pure)
# ---------------------------------------------------------------------------


def ffmetadata(chapters: list) -> str:
    """FFMETADATA1 text carrying the chapters (ms timebase)."""
    def esc(s):
        return re.sub(r"([=;#\\\n])", r"\\\1", str(s))
    lines = [";FFMETADATA1"]
    for c in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(c['start_s'] * 1000))}",
                  f"END={int(round(c['end_s'] * 1000))}", f"title={esc(c['title'])}"]
    return "\n".join(lines) + "\n"


def pix_fmt_for(pipeline: str) -> str:
    return "yuv420p" if pipeline == "x264" else "nv12"


def build_filtergraph(plan: Plan, res: Resolved, inputs: dict, pipeline: str) -> tuple[str, list]:
    """The filter_complex for the cut and the -map args for its streams.

    Picture: each piece trimmed on its frame grid ([k1, k2), half a frame of
    slack either side so no rounding can gain or lose a frame), timestamps
    reset, concatenated, converted for the encoder. Audio, per output track:
    each piece read over the same span plus half the crossfade on each joined
    side (so after the acrossfade overlaps them the audio is exactly as long as
    the picture), padded to that length if the source falls short, then the
    pieces chained through acrossfade (a plain concat where a join has no room).
    A segment without one of the output's tracks contributes silence."""
    parts, vlabels = [], []
    n = len(res.pieces)
    for j, p in enumerate(res.pieces):
        seg = plan.seg(p.segment)
        i = inputs[p.segment]
        vs, ve = max(0.0, p.start_s - 0.5 / p.fps), p.end_s - 0.5 / p.fps
        scale = ""
        if plan.size and seg.size and seg.size != plan.size:
            scale = f",scale={plan.size[0]}:{plan.size[1]}"
        mine = [c for c in res.concealed if c["piece"] == j and not c.get("trimmed")]
        head = f"[{i}:v:0]trim=start={vs:.6f}:end={ve:.6f},setpts=PTS-STARTPTS{scale},setsar=1"
        parts.append(head + (f"[v{j}c0]" if mine else f"[v{j}]"))
        for q, c in enumerate(mine):
            src, dst = f"v{j}c{q}", (f"v{j}" if q == len(mine) - 1 else f"v{j}c{q + 1}")
            freeze = f"freezeframes=first={c['first']}:last={c['last']}:replace={c['replace']}"
            if c["rect"] is None:                        # a full-screen mark flash: hold the whole frame
                parts.append(f"[{src}]split=2[v{j}m{q}][v{j}f{q}];[v{j}m{q}][v{j}f{q}]{freeze}[{dst}]")
            else:                                        # a corner patch: hold just that corner
                w, h, x, y = c["rect"]
                parts.append(f"[{src}]split=3[v{j}m{q}][v{j}a{q}][v{j}b{q}];"
                             f"[v{j}a{q}][v{j}b{q}]{freeze},crop={w}:{h}:{x}:{y}[v{j}p{q}];"
                             f"[v{j}m{q}][v{j}p{q}]overlay={x}:{y}:enable='between(n,{c['first']},{c['last']})'"
                             f"[{dst}]")
        vlabels.append(f"[v{j}]")
    fmt = pix_fmt_for(pipeline)
    if n == 1:
        parts.append(f"[v0]format={fmt}[vout]")
    else:
        parts.append("".join(vlabels) + f"concat=n={n}:v=1:a=0,format={fmt}[vout]")
    maps = ["-map", "[vout]"]
    for t, label in enumerate(plan.tracks):
        names = []
        for j, p in enumerate(res.pieces):
            seg = plan.seg(p.segment)
            pre = res.xfades[j - 1] / 2 if j > 0 else 0.0
            post = res.xfades[j] / 2 if j < n - 1 else 0.0
            off = res.audio_offsets.get(p.segment, 0.0)
            a0, a1 = p.start_s - pre + off, p.end_s + post + off
            want = a1 - a0
            name = f"a{t}_{j}"
            ti = seg.tracks.index(label) if label in seg.tracks else None
            if ti is None:
                parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={want:.6f}[{name}]")
            else:
                lead = max(0.0, -a0)
                delay = f",adelay=delays={int(round(lead * 1000))}:all=1" if lead > 0 else ""
                parts.append(f"[{inputs[p.segment]}:a:{ti}]atrim=start={max(0.0, a0):.6f}:end={a1:.6f},"
                             f"asetpts=PTS-STARTPTS{delay},apad=whole_dur={want:.6f}[{name}]")
            names.append(name)
        cur = names[0]
        for j in range(1, n):
            d = res.xfades[j - 1]
            nxt = f"x{t}_{j}"
            if d > 0:
                parts.append(f"[{cur}][{names[j]}]acrossfade=d={d:.6f}:c1=tri:c2=tri[{nxt}]")
            else:
                parts.append(f"[{cur}][{names[j]}]concat=n=2:v=0:a=1[{nxt}]")
            cur = nxt
        parts.append(f"[{cur}]anull[aout{t}]")
        maps += ["-map", f"[aout{t}]"]
    return ";".join(parts), maps


def build_render_argv(plan: Plan, res: Resolved, *, cfg: Config, ffmpeg: str, folder: Path, output: Path,
                      pipeline: str, meta_path: Path | None = None) -> list:
    """The one ffmpeg command that writes the cut. `-n`: it never overwrites
    (the renderer writes a fresh part file and moves it into place)."""
    used = []
    for p in res.pieces:
        if p.segment not in used:
            used.append(p.segment)
    inputs = {k: i for i, k in enumerate(used)}
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning", "-progress", "pipe:1", "-n"]
    for k in used:
        argv += ["-i", str(Path(folder) / plan.seg(k).file)]
    if meta_path is not None:
        argv += ["-f", "ffmetadata", "-i", str(meta_path)]
    graph, maps = build_filtergraph(plan, res, inputs, pipeline)
    argv += ["-filter_complex", graph] + maps
    argv += ["-map_chapters", str(len(used)) if meta_path is not None else "-1"]
    spec = ffmpeg_cmd.spec_from_config(cfg, ffmpeg=ffmpeg, output=str(output), pipeline=pipeline,
                                       fps=int(round(plan.fps)))
    argv += ffmpeg_cmd.video_codec_args(spec) + ["-fps_mode", "cfr", "-r", f"{plan.fps:g}"]
    if plan.tracks:
        argv += ["-c:a", "aac", "-b:a", plan.audio_bitrate]
        if len(plan.tracks) > 1:
            for t, label in enumerate(plan.tracks):
                argv += [f"-metadata:s:a:{t}", f"title={label}"]
    argv += ["-movflags", "+faststart", "-f", "mp4", str(output)]
    return argv


def frames_argv(ffmpeg: str, path: str, start_s: float, dur_s: float, crop, grid: tuple) -> list:
    """Decode a window to tiny rgb24 frames on stdout, with showinfo printing each
    frame's original timestamp (-copyts: the probe confirmed seeking keeps them)."""
    vf = (f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else "") + \
        f"scale={grid[0]}:{grid[1]}:flags=area,format=rgb24,showinfo"
    return [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-copyts", "-ss", f"{start_s:.3f}",
            "-t", f"{dur_s:.3f}", "-i", path, "-map", "0:v:0", "-vf", vf, "-f", "rawvideo", "-"]


def pcm_argv(ffmpeg: str, path: str, start_s: float, dur_s: float, tracks: int, track: int | None) -> list:
    """Decode a window of audio to mono s16 at PCM_RATE on stdout: one track, or
    every track mixed (levels at a seam should hear the voice and the computer)."""
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-ss", f"{start_s:.3f}", "-t",
            f"{dur_s:.3f}", "-i", path]
    if track is not None or tracks <= 1:
        argv += ["-map", f"0:a:{track or 0}"]
    else:
        ins = "".join(f"[0:a:{i}]" for i in range(tracks))
        argv += ["-filter_complex", f"{ins}amix=inputs={tracks}:normalize=0[m]", "-map", "[m]"]
    return argv + ["-ac", "1", "-ar", str(PCM_RATE), "-f", "s16le", "-"]


_PTS = re.compile(rb"pts_time:\s*(-?[\d.]+)")
_PTS_INT = re.compile(rb"\bn:\s*\d+\s+pts:\s*(-?\d+)")
_TIME_BASE = re.compile(rb"config in time_base:\s*(\d+)/(\d+)")
_MAX_VOLUME = re.compile(rb"max_volume:\s*(-?[\d.]+|-inf) dB")


def volume_argv(ffmpeg: str, path: str, start_s: float, dur_s: float, tracks: int) -> list:
    """The loudest point of a window (volumedetect; every track mixed), to the null muxer."""
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-ss", f"{start_s:.3f}", "-t",
            f"{dur_s:.3f}", "-i", path]
    if tracks <= 1:
        argv += ["-map", "0:a:0", "-af", "volumedetect"]
    else:
        ins = "".join(f"[0:a:{i}]" for i in range(tracks))
        argv += ["-filter_complex", f"{ins}amix=inputs={tracks}:normalize=0,volumedetect[m]", "-map", "[m]"]
    return argv + ["-f", "null", "-"]


def parse_thumbs(raw: bytes, stderr: bytes, size: tuple, start_s: float, fps: float) -> list:
    """C1d: rawvideo rgb24 + showinfo -> [(pts_s, grey bytes)], grey by screens' formula."""
    n = size[0] * size[1] * 3
    pts = _frame_pts(stderr)
    return [(pts[i] if i < len(pts) else start_s + i / fps, screens.grey_from_rgb(raw[i * n:(i + 1) * n]))
            for i in range(len(raw) // n)]


def _frame_pts(stderr: bytes) -> list:
    """Each decoded frame's time from showinfo: the exact integer pts and time base
    when every frame has one, else pts_time (6 significant digits)."""
    tb = _TIME_BASE.search(stderr)
    ints = [int(m.group(1)) for m in _PTS_INT.finditer(stderr)]
    if tb and int(tb.group(2)) and len(ints) == len(_PTS.findall(stderr)):
        num, den = int(tb.group(1)), int(tb.group(2))
        return [v * num / den for v in ints]
    return [float(m.group(1)) for m in _PTS.finditer(stderr)]


def parse_frames(raw: bytes, stderr: bytes, grid: tuple, start_s: float, fps: float, median: bool) -> list:
    """rawvideo + showinfo stderr -> [(pts_s, rgb)]. A frame without a showinfo
    line (never seen) gets its time from its position."""
    size = grid[0] * grid[1] * 3
    tb = _TIME_BASE.search(stderr)
    ints = [int(m.group(1)) for m in _PTS_INT.finditer(stderr)]
    if tb and int(tb.group(2)) and len(ints) == len(_PTS.findall(stderr)):
        # pts_time is printed with 6 significant digits (10 ms steps past 1000 s): use the exact pts
        num, den = int(tb.group(1)), int(tb.group(2))
        pts = [v * num / den for v in ints]
    else:
        pts = [float(m.group(1)) for m in _PTS.finditer(stderr)]
    out = []
    for i in range(len(raw) // size):
        buf = raw[i * size:(i + 1) * size]
        t = pts[i] if i < len(pts) else start_s + i / fps
        out.append((t, median_rgb(buf) if median else mean_rgb(buf)))
    return out


# ---------------------------------------------------------------------------
# 7. Analyzer (ffmpeg)
# ---------------------------------------------------------------------------


def _popen_kwargs() -> dict:
    return {"creationflags": CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS} if sys.platform == "win32" else {}


class FfmpegAnalyzer(Analyzer):
    """Decodes small windows with ffmpeg (argv logged at DEBUG; failures raise
    AnalysisError with ffmpeg's last lines)."""

    def __init__(self, ffmpeg: str, folder: Path, run: Callable = subprocess.run, timeout_s: float = 120):
        self.ffmpeg, self.folder, self.run, self.timeout_s = ffmpeg, Path(folder), run, timeout_s

    def _run(self, argv: list) -> tuple[bytes, bytes]:
        event(log, logging.DEBUG, "render.analysis_argv", argv=argv)
        try:
            res = self.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=self.timeout_s,
                           **_popen_kwargs())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AnalysisError(repr(exc)) from exc
        if res.returncode != 0:
            tail = (res.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-2:]
            raise AnalysisError(f"ffmpeg exit {res.returncode}: {' | '.join(tail)}")
        return res.stdout or b"", res.stderr or b""

    def frames(self, seg, start_s, dur_s, crop, grid):
        out, err = self._run(frames_argv(self.ffmpeg, str(self.folder / seg.file), start_s, dur_s, crop, grid))
        return parse_frames(out, err, grid, start_s, seg.fps, median=crop is not None)

    def pcm(self, seg, start_s, dur_s, track=None):
        out, _ = self._run(pcm_argv(self.ffmpeg, str(self.folder / seg.file), start_s, dur_s, len(seg.tracks),
                                    track))
        n = len(out) // 2
        return list(struct.unpack(f"<{n}h", out[:n * 2]))

    def thumbs(self, seg, start_s, dur_s, size):
        out, err = self._run(frames_argv(self.ffmpeg, str(self.folder / seg.file), start_s, dur_s, None, size))
        return parse_thumbs(out, err, size, start_s, seg.fps)

    def max_db(self, seg, start_s, dur_s):
        argv = volume_argv(self.ffmpeg, str(self.folder / seg.file), start_s, dur_s, len(seg.tracks))
        _, err = self._run(argv)
        m = _MAX_VOLUME.search(err)
        if not m:
            raise AnalysisError("volumedetect reported no max_volume")
        return float(m.group(1))


# ---------------------------------------------------------------------------
# 8. Cut status
# ---------------------------------------------------------------------------


@dataclass
class CutStatus:
    state: str                     # none | current | stale | failed | running | missing
    path: Path
    block: dict | None

    @property
    def current(self) -> bool:
        return self.state == "current"

    def describe(self) -> str:
        return {"none": "", "current": "cut", "stale": "cut (stale)", "failed": "render failed",
                "running": "rendering", "missing": "cut missing"}[self.state]


def cut_status(sc: dict, folder: Path, stem: str) -> CutStatus:
    """Is there a cut, and is it the render of this recording's current event
    record? Current means: the sidecar's render block says ok, was made from the
    same source digest (segments, events, takes, marks), and the file is there
    at the recorded size."""
    path = Path(folder) / f"{stem}{CUT_SUFFIX}"
    block = sc.get("render") if isinstance(sc.get("render"), dict) else None
    if block is None:
        return CutStatus("stale" if path.exists() else "none", path, None)
    status = block.get("status")
    if status == "running":
        holder = catalog.render_holder(folder, stem)
        if holder is not None:
            return CutStatus("running", path, block)
    if status == "failed":
        return CutStatus("failed", path, block)
    if status != "ok":
        return CutStatus("stale" if path.exists() else "none", path, block)
    if not path.exists():
        return CutStatus("missing", path, block)
    size = (block.get("output") or {}).get("size_bytes")
    try:
        same_size = size is None or path.stat().st_size == size
    except OSError:
        same_size = False
    if block.get("source_digest") != source_digest(catalog.as_v2(sc)) or not same_size:
        return CutStatus("stale", path, block)
    return CutStatus("current", path, block)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 9. Renderer
# ---------------------------------------------------------------------------


@dataclass
class RenderResult:
    ok: bool
    message: str
    uid: str | None = None
    cut_path: Path | None = None
    up_to_date: bool = False
    dry_run: bool = False
    duration_s: float | None = None          # the cut
    source_s: float | None = None            # every segment of the original
    elapsed_s: float | None = None
    fallbacks: list = field(default_factory=list)
    report: list = field(default_factory=list)


def describe(plan: Plan, res: Resolved | None = None, argv: list | None = None) -> list:
    """Human-readable plan (and decisions, once resolved), for --dry-run and the log."""
    lines = [f"plan: {len(plan.segments)} segment(s), {fmt_mmss(plan.total_s)} recorded, "
             + ("no takes: everything, trimmed to the flashes" if plan.whole else
                f"{len(plan.intervals)} kept interval(s)")
             + (f", audio {' + '.join(plan.tracks)}" if plan.tracks else ", no audio")]
    lines += [f"  note: {n}" for n in plan.notes]
    for n, iv in enumerate(plan.intervals, 1):
        lines.append(f"  {n}. segment {iv.segment} {fmt_mmss(iv.start_s)}-{fmt_mmss(iv.end_s)}"
                     + (f" (take {iv.take})" if iv.take else "")
                     + f": starts at {iv.start.kind}, ends at {iv.end.kind}")
    if res is None:
        return lines
    for b in res.boundaries:
        how = (f"detected {b['frames']} frame(s) from {b['detected_s']}s (stamp {b['stamped_s']}s, "
               f"{b['delta_ms']:+.0f} ms)" if b["method"] == "detected" and b.get("delta_ms") is not None
               else f"detected {b.get('frames')} frame(s) from {b.get('detected_s')}s" if b["method"] == "detected"
               else f"FALLBACK to the stamp: {b['fallback']}" if b["method"] == "stamp"
               else "nothing was shown there")
        shown = b["color"] or (f"{b.get('screen')} screen" + (" gone" if b.get("change") == "gone" else "")
                               + (", learned on" if b.get("from_learn") else "") if b.get("style") == "screen"
                               else "-")
        lines.append(f"  boundary seg{b['segment']} {b['kind']} ({shown}): {how} -> {b['bound_s']}s"
                     + (f" [{b['warning']}]" if b.get("warning") else ""))
    for s in res.seams:
        lines.append(f"  seam seg{s['segment']} {s['side']} {s['bound_s']}s -> {s['seam_s']}s ({s['how']}, "
                     f"{s['offset_ms']:+.0f} ms)")
    for c in res.calibration:
        lines.append(f"  clap seg{c['segment']}: " + (f"residual {c['residual_ms']:+.1f} ms"
                                                       + (" (applied)" if c.get("applied") else "")
                                                       if c["status"] == "measured" else f"skipped ({c['why']})"))
    for ch in res.chapters:
        lines.append(f"  chapter {fmt_mmss(ch['start_s'])} {ch['title']}")
    for c in res.concealed:
        how = "trimmed off the piece's edge" if c.get("trimmed") else "concealed"
        lines.append(f"  {c.get('what') or ('mark ' + str(c.get('mark')))}: patch {how} "
                     f"({c['frames']} frames from {c['detected_s']}s)")
    lines.append(f"  output: {len(res.pieces)} piece(s), {fmt_mmss(res.output_duration_s)}"
                 f" ({res.output_duration_s:.2f} s), crossfades {[round(x * 1000) for x in res.xfades]} ms")
    lines += [f"  fallback: {f}" for f in res.fallbacks]
    if argv:
        lines.append("  ffmpeg: " + subprocess.list2cmdline([str(a) for a in argv]))
    return lines


def nothing_left(rcfg: RenderConfig, res: Resolved) -> str:
    return (f"nothing left to render: every kept interval was shorter than render.min_interval_ms "
            f"({rcfg.min_interval_ms} ms) once the fiducials were excluded ({len(res.dropped)} dropped)")


def _default_pid_alive(pid: int) -> bool:
    from .winapi import pid_alive
    return pid_alive(pid)


class Renderer:
    """Renders one recording's cut. Thread-compatible (one render per instance);
    `cancel()` from another thread kills the encode."""

    def __init__(self, cfg: Config, *, popen: Callable = subprocess.Popen, run: Callable = subprocess.run,
                 which: Callable | None = None, job_factory: Callable | None = None,
                 pid_alive: Callable[[int], bool] | None = None, analyzer: Analyzer | None = None,
                 clock: Callable[[], float] = time.monotonic):
        import shutil
        self.cfg = cfg
        self.popen, self.run = popen, run
        self.which = which or shutil.which
        self.job_factory = job_factory
        self.pid_alive = pid_alive or _default_pid_alive
        self.analyzer = analyzer
        self.clock = clock
        self._proc = None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            event(log, logging.WARNING, "render.cancel", pid=proc.pid)
            try:
                proc.kill()
            except OSError as exc:
                event(log, logging.WARNING, "render.cancel_failed", error=repr(exc))

    # -- the flow ------------------------------------------------------------------

    def render(self, ref, *, force: bool = False, dry_run: bool = False, live_uid: str | None = None,
               progress: Callable[[str, float | None], None] | None = None) -> RenderResult:
        progress = progress or (lambda stage, frac: None)
        root = self.cfg.root_path()
        cat = catalog.Catalog(root)
        entry = catalog.resolve_ref(cat, ref)
        catalog.refuse_if_live(entry, live_uid, "render")
        if entry.status != "ok" or not entry.file:
            raise RenderError(f"{entry.file or entry.uid[:8]} is a failed capture; there is nothing to render")
        media = entry.media_path(root)
        folder, stem = media.parent, naming.stem_of(media.name)
        side = catalog.sidecar_path(media)
        try:
            sc = catalog.read_sidecar(side)
        except FileNotFoundError:
            raise RenderError(f"{side.name} is missing: the render needs the sidecar's event record") from None
        cut = folder / f"{stem}{CUT_SUFFIX}"
        status = cut_status(sc, folder, stem)
        if status.current and not force and not dry_run:
            return RenderResult(True, f"up to date: {cut} (`peep render --force` renders it again)", uid=entry.uid,
                                cut_path=cut, up_to_date=True)
        plan = build_plan(sc, stem=stem)
        if not plan.segments:
            raise RenderError(f"{media.name}: no segment finished cleanly; there is nothing to render")
        if not plan.intervals:
            raise RenderError(f"{media.name}: nothing is kept (every take was discarded, or no take has any "
                              f"length); there is nothing to render")
        for s in plan.segments:
            if not (folder / s.file).exists():
                raise RenderError(f"segment {s.index}'s file is missing: {folder / s.file}")
        from .recorder import resolve_ffmpeg
        ffmpeg = resolve_ffmpeg(self.cfg.ffmpeg.path, self.which)
        if not ffmpeg:
            raise RenderError(f"ffmpeg not found ({self.cfg.ffmpeg.path!r}); run `peep doctor`")
        analyzer = self.analyzer or FfmpegAnalyzer(ffmpeg, folder, run=self.run)
        event(log, logging.INFO, "render.begin", uid=entry.uid, stem=stem, segments=len(plan.segments),
              intervals=len(plan.intervals), whole=plan.whole, force=force, dry_run=dry_run)
        if dry_run:
            progress("analysing", None)
            res = resolve(plan, analyzer, self.cfg.render)
            if not res.pieces:
                raise RenderError(nothing_left(self.cfg.render, res))
            argv = build_render_argv(plan, res, cfg=self.cfg, ffmpeg=ffmpeg, folder=folder,
                                     output=folder / f"{stem}{PART_SUFFIX}", pipeline=plan.pipeline,
                                     meta_path=(folder / f"{stem}{META_SUFFIX}") if res.chapters else None)
            return RenderResult(True, "dry run: nothing was written", uid=entry.uid, cut_path=cut, dry_run=True,
                                duration_s=res.output_duration_s, source_s=plan.total_s,
                                fallbacks=res.fallbacks, report=describe(plan, res, argv))
        lock = catalog.acquire_render_lock(folder, stem, pid_alive=self.pid_alive)
        started_at, t0 = catalog.now_iso(), self.clock()
        part, meta = folder / f"{stem}{PART_SUFFIX}", folder / f"{stem}{META_SUFFIX}"
        block = {"status": "running", "file": cut.name, "started_at": started_at, "pid": os.getpid(),
                 "source_digest": plan.digest}
        try:
            self._write_block(side, block)
            for leftover in (part, meta):               # ours: a render that died under this lock's predecessor
                if leftover.exists():
                    event(log, logging.WARNING, "render.leftover_removed", path=str(leftover))
                    leftover.unlink()
            progress("analysing", None)
            res = resolve(plan, analyzer, self.cfg.render,
                          say=lambda s: event(log, logging.INFO, "render.resolving", step=s))
            if not res.pieces:
                raise RenderError(nothing_left(self.cfg.render, res))
            for line in describe(plan, res):
                event(log, logging.INFO, "render.plan", line=line)
            if res.chapters:
                meta.write_text(ffmetadata(res.chapters), encoding="utf-8")
            pipelines = [plan.pipeline] + (["x264"] if plan.pipeline != "x264" else [])
            attempts, argv, used = [], None, None
            for n, pipeline in enumerate(pipelines):
                argv = build_render_argv(plan, res, cfg=self.cfg, ffmpeg=ffmpeg, folder=folder, output=part,
                                         pipeline=pipeline, meta_path=meta if res.chapters else None)
                rc, tail = self._encode(argv, res.output_duration_s, progress)
                attempts.append({"pipeline": pipeline, "exit_code": rc, "stderr_tail": tail})
                if rc == 0 and part.exists() and part.stat().st_size > 0:
                    used = pipeline
                    break
                if part.exists():
                    part.unlink()
                if self._cancelled:
                    raise RenderError("the render was cancelled")
                if n + 1 < len(pipelines):
                    res.fallbacks.append(f"the {pipeline} encoder failed (exit {rc}); re-encoded with "
                                         f"{pipelines[n + 1]}")
                    event(log, logging.WARNING, "render.encoder_fallback", failed=pipeline, exit_code=rc,
                          stderr_tail=tail, next=pipelines[n + 1])
            if used is None:
                last = attempts[-1]
                raise RenderError(f"ffmpeg failed (exit {last['exit_code']}): "
                                  + (" | ".join(last["stderr_tail"][-3:]) or "no output; see the log"))
            size = part.stat().st_size
            digest = sha256_file(part)
            try:
                os.replace(part, cut)
            except OSError as exc:
                raise RenderError(f"could not put the cut in place ({exc}); is the old cut open in a player?") \
                    from exc
            elapsed = round(self.clock() - t0, 1)
            block = {"status": "ok", "file": cut.name, "started_at": started_at, "finished_at": catalog.now_iso(),
                     "elapsed_s": elapsed, "source_digest": plan.digest, "config": asdict(self.cfg.render),
                     "whole": plan.whole, "notes": plan.notes,
                     "plan": [{"segment": p.segment, "take": p.take, "start_s": _r(p.start_s), "end_s": _r(p.end_s),
                               "frames": p.k2 - p.k1, "out_start_s": _r(p.out_start_s)} for p in res.pieces],
                     "boundaries": res.boundaries, "seams": res.seams,
                     "crossfades_ms": [_r(x * 1000, 1) for x in res.xfades], "chapters": res.chapters,
                     "marks_excluded": res.marks_excluded, "calibration": res.calibration,
                     "audio_offsets_ms": {str(k): _r(v * 1000, 1) for k, v in res.audio_offsets.items()},
                     "dropped": res.dropped, "concealed": res.concealed, "fallbacks": res.fallbacks,
                     "grid_phase_ms": {str(k): _r(v * 1000, 2) for k, v in res.phases.items()},
                     "output": {"duration_s": res.output_duration_s, "source_duration_s": plan.total_s,
                                "size_bytes": size, "sha256": digest, "pipeline": used,
                                "encoder": ffmpeg_cmd.ENCODERS[used], "tracks": plan.tracks, "argv": argv},
                     "attempts": attempts}
            self._write_block(side, block)
            event(log, logging.INFO, "render.done", uid=entry.uid, cut=str(cut), duration_s=res.output_duration_s,
                  elapsed_s=elapsed, fallbacks=len(res.fallbacks), pipeline=used)
            fb = f"; {len(res.fallbacks)} fallback(s), see `peep render --dry-run` or the sidecar" \
                if res.fallbacks else ""
            msg = (f"cut: {cut} ({fmt_mmss(res.output_duration_s)} of {fmt_mmss(plan.total_s)}, "
                   f"{len(res.pieces)} piece(s), {elapsed:.0f}s{fb})")
            return RenderResult(True, msg, uid=entry.uid, cut_path=cut, duration_s=res.output_duration_s,
                                source_s=plan.total_s, elapsed_s=elapsed, fallbacks=list(res.fallbacks))
        except BaseException as exc:
            why = str(exc) if isinstance(exc, RenderError) else repr(exc)
            event(log, logging.ERROR, "render.failed", uid=entry.uid, error=why)
            for leftover in (part,):
                try:
                    if leftover.exists():
                        leftover.unlink()
                except OSError as rm_exc:
                    event(log, logging.WARNING, "render.cleanup_failed", path=str(leftover), error=repr(rm_exc))
            failed = {"status": "failed", "file": cut.name, "started_at": started_at,
                      "finished_at": catalog.now_iso(), "source_digest": plan.digest, "error": why}
            try:
                self._write_block(side, failed)
            except OSError as w_exc:
                event(log, logging.ERROR, "render.block_write_failed", error=repr(w_exc))
            if isinstance(exc, RenderError):
                raise
            if isinstance(exc, Exception):
                raise RenderError(f"render failed: {why} (the original is untouched)") from exc
            raise
        finally:
            try:
                if meta.exists():
                    meta.unlink()
            except OSError as exc:
                event(log, logging.WARNING, "render.cleanup_failed", path=str(meta), error=repr(exc))
            catalog.release_render_lock(lock)

    # -- steps -------------------------------------------------------------------

    def _write_block(self, side: Path, block: dict) -> None:
        """Re-read the sidecar and set its `render` block (atomic write), so
        nothing else in the sidecar is replaced by a stale copy."""
        sc = catalog.read_sidecar(side)
        sc["render"] = block
        catalog.write_sidecar(side, sc)

    def _close_pipes(self, proc, drain: threading.Thread) -> None:
        """Let the stderr reader finish, then close both pipes (never left to the GC)."""
        drain.join(5)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError, AttributeError):
                pass
        self._proc = None

    def _encode(self, argv: list, total_s: float, progress) -> tuple[int | None, list]:
        """Run the encode, reporting progress from `-progress pipe:1`; returns
        (exit code, stderr tail). An encode that reports nothing for STALL_S is
        killed rather than left to hang a queue."""
        event(log, logging.INFO, "ffmpeg.argv", argv=argv, cmdline=subprocess.list2cmdline([str(a) for a in argv]),
              purpose="render")
        tail = collections.deque(maxlen=40)
        try:
            proc = self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **_popen_kwargs())
        except OSError as exc:
            return None, [repr(exc)]
        self._proc = proc
        if self.job_factory is not None:
            try:
                self.job_factory().assign(proc)
            except Exception as exc:          # the job is a safety net; rendering goes on without it
                event(log, logging.WARNING, "render.job_assign_failed", error=repr(exc))
        last = {"t": self.clock()}

        def drain_err():
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    tail.append(line)
                    log.debug("ffmpeg| %s", line)

        def watchdog():
            while proc.poll() is None:
                if self.clock() - last["t"] > STALL_S:
                    event(log, logging.ERROR, "render.stalled", seconds=STALL_S)
                    tail.append(f"(peep: no progress for {STALL_S:g} s; killed)")
                    proc.kill()
                    return
                time.sleep(0.5)
        threads = [threading.Thread(target=drain_err, name="render-stderr", daemon=True),
                   threading.Thread(target=watchdog, name="render-watchdog", daemon=True)]
        for t in threads:
            t.start()
        try:
            progress("encoding", 0.0)
            try:
                for raw in iter(proc.stdout.readline, b""):
                    # Any progress block is a sign of life: while trim decodes up to a late first
                    # piece, ffmpeg reports out_time_us=N/A, and that must not count as a stall.
                    last["t"] = self.clock()
                    line = raw.decode("utf-8", "replace").strip()
                    key, _, value = line.partition("=")
                    if key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit():
                        if total_s > 0:
                            progress("encoding", max(0.0, min(1.0, int(value) / 1e6 / total_s)))
                    elif key == "progress" and value == "end":
                        progress("encoding", 1.0)
            except (OSError, ValueError) as exc:
                event(log, logging.WARNING, "render.progress_read_failed", error=repr(exc))
        finally:
            if proc.poll() is None and (self._cancelled or sys.exc_info()[0] is not None):
                # Leaving with ffmpeg still writing (an exception, Ctrl-C): never orphan it.
                event(log, logging.WARNING, "render.ffmpeg_killed", pid=proc.pid)
                try:
                    proc.kill()
                    proc.wait(10)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    event(log, logging.ERROR, "render.kill_failed", error=repr(exc))
            if sys.exc_info()[0] is not None:
                # The exception goes on up; the pipes still get closed here.
                self._close_pipes(proc, threads[0])
        rc = proc.wait()
        self._close_pipes(proc, threads[0])
        event(log, logging.INFO if rc == 0 else logging.ERROR, "render.ffmpeg_exit", exit_code=rc,
              stderr_tail=list(tail)[-5:])
        return rc, list(tail)[-12:]


# ---------------------------------------------------------------------------
# 10. RenderQueue + policy
# ---------------------------------------------------------------------------


def should_render(auto: str, summary: dict | None, segments: int = 1) -> bool:
    """render.auto after a recording: always; never; or takes, only when there is
    something beyond the flashes to cut (a take, or more than one segment)."""
    s = summary or {}
    if auto == "never" or s.get("kept") == []:          # every take discarded: there is nothing to cut
        return False
    if auto == "always":
        return True
    return (not s.get("whole", True)) or int(s.get("segments") or segments or 1) > 1


class RenderQueue:
    """The agent's background renders: one worker thread, one render at a time,
    in submission order, so a burst of recordings never runs several encodes
    against each other. `notify(kind, info)` is called on the worker thread with
    kind in queued | start | progress | done | failed; the agent marshals it to
    its UI thread. Nothing here blocks the caller."""

    def __init__(self, render: Callable[[str, Callable], RenderResult], notify: Callable[[str, dict], None], *,
                 cancel: Callable[[], None] | None = None):
        self._render, self._notify, self._cancel = render, notify, cancel
        self._jobs: collections.deque = collections.deque()
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self.current: dict | None = None
        self.stopped = False

    def submit(self, uid: str, name: str) -> int:
        """Queue a render; returns how many renders are ahead of it (0: starts now)."""
        job = {"uid": uid, "name": name, "queued_at": catalog.now_iso()}
        with self._cv:
            if self.stopped:
                raise RuntimeError("the render queue is stopped")
            ahead = len(self._jobs) + (1 if self.current else 0)
            self._jobs.append(job)
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, name="render-queue", daemon=True)
                self._thread.start()
            self._cv.notify_all()
        event(log, logging.INFO, "render.queued", uid=uid, file=name, ahead=ahead)
        self._safe_notify("queued", {**job, "ahead": ahead})
        return ahead

    def pending(self) -> list:
        with self._cv:
            return list(self._jobs)

    def stop(self) -> list:
        """Stop taking work: cancel the running render (it can be redone with
        `peep render`) and drop what is queued. Returns the dropped jobs."""
        with self._cv:
            self.stopped = True
            dropped = list(self._jobs)
            self._jobs.clear()
            running = self.current
            self._cv.notify_all()
        if running is not None and self._cancel is not None:
            self._cancel()
        event(log, logging.INFO, "render.queue_stopped", running=(running or {}).get("uid"),
              dropped=[j["uid"] for j in dropped])
        return dropped

    def wait_idle(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._cv:
            while self._jobs or self.current:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True

    def _safe_notify(self, kind: str, info: dict) -> None:
        try:
            self._notify(kind, info)
        except Exception as exc:           # a notification problem must not kill the worker
            event(log, logging.ERROR, "render.notify_failed", kind=kind, error=repr(exc))

    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._jobs and not self.stopped:
                    self._cv.wait(1.0)
                if self.stopped:
                    return
                job = self._jobs.popleft()
                self.current = job
            self._safe_notify("start", job)
            try:
                result = self._render(job["uid"], lambda stage, frac: self._safe_notify(
                    "progress", {**job, "stage": stage, "fraction": frac}))
            except Exception as exc:
                if not isinstance(exc, (RenderError, LookupError, ValueError, OSError)):
                    log.exception("render job raised")
                self._safe_notify("failed", {**job, "error": str(exc) or repr(exc)})
            else:
                self._safe_notify("done" if result.ok else "failed",
                                  {**job, "result": result, **({} if result.ok else {"error": result.message})})
            finally:
                with self._cv:
                    self.current = None
                    self._cv.notify_all()
