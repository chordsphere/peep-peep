"""The capture side of the editing model (session C1a): what each take,
retake, pause, resume and mark press means, decided as it happens and
written down for session C1b to render from. Pure: no clock, no files, no
threads. The recorder feeds it presses and segment boundaries; tests feed it
the same and check every sequence.

The model, as ratified (2026-10-06):

  session   Ctrl+Alt+R on/off, exactly as before; always captured whole.
  segment   hard pause stops capture; resume opens the next segment of the
            same session. Each segment is its own ffmpeg (own anchor, own
            flashes); C1b concatenates them by stream copy.
  take      one chord toggles: the first press opens a take, the next closes
            it. Only takes survive C1b's render; a session with no take at
            all renders whole. Capture never stops for a take (the soft pause).
  retake    discard the most recent take, open or closed, and open a fresh
            one at this press. Undo depth one: the most recent take is always
            the one a previous retake opened, so a second retake redoes that.
  debounce  a hotkey press of the same chord within `debounce_s` of the last
            accepted one is ignored (and recorded as ignored). Per chord; WSL
            commands are deliberate and never debounced.
  pause     take state carries across it: a take open at the pause is open
            at the resume, and the segment boundary is a cut with nothing
            to remove.

Time. Every press carries the requester's QPC stamp (time.perf_counter on
Windows: QueryPerformanceCounter, one clock for every process, the clock
A.1's audio and every flash already use). `locate(qpc)` places it on the
segment timeline: inside segment k at `media_s` seconds from that segment's
first video frame (A.1's `video_epoch_qpc_est`), or in the pause after
segment k. A press during a pause takes effect at the boundary: an opening
press (take-open, retake) at the start of the next segment, a closing press
at the end of the previous one.

Sections:
  1. Records                    (~line 55)
  2. EventModel                 (~line 105)
  3. Summary (kept / total)     (~line 300)
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 1. Records
# ---------------------------------------------------------------------------

TAKE, RETAKE, PAUSE, RESUME, PAUSE_TOGGLE, MARK = "take", "retake", "pause", "resume", "pause-toggle", "mark"
KINDS = (TAKE, RETAKE, PAUSE, RESUME, PAUSE_TOGGLE, MARK)
# The chord a press belongs to, for the debounce: pause, resume and the toggle share one.
CHORD = {TAKE: "take", RETAKE: "retake", PAUSE: "pause", RESUME: "pause", PAUSE_TOGGLE: "pause", MARK: "mark"}
DEBOUNCED_CHORDS = ("take", "retake", "pause")
DEFAULT_DEBOUNCE_S = 1.0


@dataclass(frozen=True)
class Press:
    """One request, as a control file carries it."""
    kind: str
    qpc: float | None            # the requester's QPC at the press (None: an old request file)
    at: str | None = None        # the requester's wall clock, ISO
    source: str = "cli"          # hotkey | cli
    label: str = ""

    @classmethod
    def from_request(cls, req: dict) -> "Press":
        qpc = req.get("requested_qpc")
        return cls(kind=str(req.get("kind") or MARK), qpc=float(qpc) if isinstance(qpc, (int, float)) else None,
                   at=req.get("requested_at"), source=str(req.get("source") or "cli"),
                   label=str(req.get("label") or ""))


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


# ---------------------------------------------------------------------------
# 2. EventModel
# ---------------------------------------------------------------------------


class EventModel:
    """Decides each press and keeps the record.

    The recorder calls, in order of what happens:
      segment_started(index, epoch_qpc, launch_qpc)   a segment's first frame is known
      press(Press, consumed_qpc) -> event record      every request, recording or paused
      segment_ended(index, end_qpc, duration_s)       its ffmpeg has stopped
      set_paused(bool)                                after a pause / resume took effect
      finish() -> summary                             at the end of the session
    and copies `events`, `takes`, `pauses` and the summary into the sidecar.
    The record of a press says what it did: `accepted`, `ignored` (why not),
    `action` (open/close for a take; pause/resume), the take it opened or
    closed, and for a retake the take it discarded."""

    def __init__(self, debounce_s: float = DEFAULT_DEBOUNCE_S):
        self.debounce_s = debounce_s
        self.events: list[dict] = []
        self.takes: list[dict] = []
        self.pauses: list[dict] = []
        self.segments: dict[int, dict] = {}     # index -> {"epoch_qpc", "launch_qpc", "end_qpc", "duration_s"}
        self.paused = False
        self.finished = False
        self._last_accepted: dict[str, float] = {}

    # -- segments --------------------------------------------------------------

    def segment_started(self, index: int, epoch_qpc: float, launch_qpc: float | None = None) -> None:
        self.segments[index] = {"epoch_qpc": epoch_qpc, "launch_qpc": launch_qpc if launch_qpc is not None
                                else epoch_qpc, "end_qpc": None, "duration_s": None}

    def segment_ended(self, index: int, end_qpc: float | None, duration_s: float | None) -> None:
        seg = self.segments.setdefault(index, {"epoch_qpc": end_qpc or 0.0, "launch_qpc": end_qpc or 0.0})
        if duration_s is not None and seg.get("epoch_qpc") is not None:
            end_qpc = seg["epoch_qpc"] + duration_s       # the last frame, as ffmpeg counted it
        seg["end_qpc"], seg["duration_s"] = end_qpc, duration_s

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    @property
    def current_segment(self) -> int:
        return max(self.segments) if self.segments else 0

    def locate(self, qpc: float) -> dict:
        """Where `qpc` falls on the segment timeline:
          inside segment k   {"segment": k, "media_s", "since_ffmpeg_start_s"}, media_s
                             clamped to the segment (a press made while segment 1 was
                             starting lands at its first frame: "before_first_frame")
          in a pause         {"segment": None, "after_segment": k}: after segment k
                             ended and before the next one's first frame (or with no
                             next one yet). resolve_position() turns that into a cut."""
        order = sorted(self.segments)
        if not order:
            return {"segment": None, "after_segment": 0, "media_s": None, "since_ffmpeg_start_s": None}
        for i, k in enumerate(order):
            seg = self.segments[k]
            if qpc < seg["epoch_qpc"]:
                if i == 0:
                    return {"segment": k, "media_s": 0.0, "since_ffmpeg_start_s": _r(qpc - seg["launch_qpc"]),
                            "before_first_frame": True}
                return {"segment": None, "after_segment": order[i - 1], "media_s": None,
                        "since_ffmpeg_start_s": None}
            end = seg["end_qpc"]
            if end is None or qpc <= end:
                media = qpc - seg["epoch_qpc"]
                if seg.get("duration_s") is not None:
                    media = min(media, seg["duration_s"])
                return {"segment": k, "media_s": _r(media), "since_ffmpeg_start_s": _r(qpc - seg["launch_qpc"])}
        return {"segment": None, "after_segment": order[-1], "media_s": None, "since_ffmpeg_start_s": None}

    # -- presses ---------------------------------------------------------------

    def press(self, p: Press, consumed_qpc: float) -> dict:
        """Decide one press and record it. Returns the event record (also kept in
        `self.events`); the recorder acts on `action` for pause/resume and shows
        the fiducial for accepted take/retake/mark presses."""
        qpc = p.qpc if p.qpc is not None else consumed_qpc
        rec = {"id": len(self.events) + 1, "kind": p.kind, "source": p.source, "requested_at": p.at,
               "qpc": _r(qpc, 6), "consumed_qpc": _r(consumed_qpc, 6), "qpc_from": "press" if p.qpc is not None
               else "consumed", **self.locate(qpc), "accepted": False, "ignored": None, "action": None,
               "take": None, "discarded_take": None, "fiducial": None}
        if p.label:
            rec["label"] = p.label
        if p.kind not in KINDS:
            rec["ignored"] = "unknown-kind"
            self.events.append(rec)
            return rec
        chord = CHORD[p.kind]
        if p.source == "hotkey" and chord in DEBOUNCED_CHORDS:
            last = self._last_accepted.get(chord)
            if last is not None and 0 <= qpc - last < self.debounce_s:
                rec["ignored"] = "debounce"
                rec["debounce"] = {"since_last_s": _r(qpc - last), "window_s": self.debounce_s}
                self.events.append(rec)
                return rec
        handler = {TAKE: self._take, RETAKE: self._retake, MARK: self._mark}.get(p.kind, self._pause_resume)
        handler(rec)
        if rec["accepted"] and p.source == "hotkey" and chord in DEBOUNCED_CHORDS:
            self._last_accepted[chord] = qpc
        self.events.append(rec)
        return rec

    def _position(self, rec: dict) -> dict:
        keys = ("segment", "after_segment", "media_s")
        return {"event": rec["id"], "qpc": rec["qpc"], **{k: rec.get(k) for k in keys if k in rec}}

    def open_take(self) -> dict | None:
        """The take currently open (at most one), or None."""
        for t in reversed(self.takes):
            if t["close"] is None and t["discarded_by"] is None:
                return t
        return None

    def _new_take(self, rec: dict) -> dict:
        take = {"id": len(self.takes) + 1, "open": self._position(rec), "close": None, "discarded_by": None,
                "status": "open"}
        self.takes.append(take)
        return take

    def _take(self, rec: dict) -> None:
        open_ = self.open_take()
        if open_ is None:
            take = self._new_take(rec)
            rec.update(accepted=True, action="open", take=take["id"])
        else:
            open_["close"] = self._position(rec)
            open_["status"] = "closed"
            rec.update(accepted=True, action="close", take=open_["id"])

    def _retake(self, rec: dict) -> None:
        # "The most recent take, open or closed": always the last one, since every
        # retake opens a fresh take that becomes the last (undo depth one).
        last = self.takes[-1] if self.takes else None
        if last is not None and last["discarded_by"] is None:
            last["discarded_by"] = rec["id"]
            last["status"] = "discarded"
            rec["discarded_take"] = last["id"]
        take = self._new_take(rec)
        rec.update(accepted=True, action="open", take=take["id"])

    def _mark(self, rec: dict) -> None:
        rec.update(accepted=True, action="mark")

    def _pause_resume(self, rec: dict) -> None:
        want = rec["kind"]
        if want == PAUSE_TOGGLE:
            want = RESUME if self.paused else PAUSE
        if want == PAUSE and self.paused:
            rec["ignored"] = "already-paused"
        elif want == RESUME and not self.paused:
            rec["ignored"] = "not-paused"
        else:
            rec.update(accepted=True, action=want)
            if want == PAUSE:
                self.pauses.append({"after_segment": self.current_segment, "pause_event": rec["id"],
                                    "paused_qpc": rec["qpc"], "resume_event": None, "resumed_qpc": None,
                                    "seconds": None})
            else:
                pending = [p for p in self.pauses if p["resume_event"] is None]
                if pending:
                    pending[-1].update(resume_event=rec["id"], resumed_qpc=rec["qpc"])

    def cancel_pending_pause(self, why: str) -> int:
        """A pause that was accepted but never took effect (a stop arrived at the
        same moment, or the recording ended first): take it back out of `pauses`
        and mark its press as superseded, so the record never shows a pause that
        did not happen. Returns how many were cancelled."""
        if self.paused:
            return 0
        n = 0
        for p in [p for p in self.pauses if p["resume_event"] is None and p.get("capture_stopped_qpc") is None]:
            self.pauses.remove(p)
            for rec in self.events:
                if rec["id"] == p["pause_event"]:
                    rec.update(accepted=False, ignored=why, action=None)
            n += 1
        return n

    def note_pause_capture(self, after_segment: int, stopped_qpc: float | None, resumed_epoch_qpc: float | None
                           ) -> None:
        """Record when capture actually stopped and restarted around a pause, and
        how long nothing was recorded."""
        for p in self.pauses:
            if p["after_segment"] == after_segment:
                if stopped_qpc is not None:
                    p["capture_stopped_qpc"] = _r(stopped_qpc, 6)
                if resumed_epoch_qpc is not None:
                    p["capture_resumed_qpc"] = _r(resumed_epoch_qpc, 6)
                    if stopped_qpc is not None:
                        p["seconds"] = _r(resumed_epoch_qpc - stopped_qpc)

    # -- end of session ----------------------------------------------------------

    def finish(self) -> dict:
        """Close what is still open at the end of the session (an open take ends
        with the last segment) and return the summary."""
        if not self.finished:
            self.finished = True
            open_ = self.open_take()
            if open_ is not None:
                last = self.current_segment
                open_["close"] = {"event": None, "reason": "session-end", "segment": last,
                                  "media_s": (self.segments.get(last) or {}).get("duration_s"), "qpc": None}
                open_["status"] = "closed"
            for t in self.takes:
                t["status"] = "discarded" if t["discarded_by"] is not None else "kept"
        return summarize(self.takes, self.durations())

    def durations(self) -> dict[int, float]:
        return {k: float(v["duration_s"] or 0.0) for k, v in sorted(self.segments.items())}

    def counts(self) -> dict:
        """For the pill and active.json: how many takes so far, and whether one is open."""
        live = [t for t in self.takes if t["discarded_by"] is None]
        return {"takes": len(live), "open": self.open_take() is not None}


# ---------------------------------------------------------------------------
# 3. Summary (kept / total)
# ---------------------------------------------------------------------------


def resolve_position(pos: dict, durations: dict[int, float], opening: bool) -> tuple[int, float]:
    """A take boundary as (segment, seconds into it). A boundary set during the
    pause after segment k sits at the start of segment k+1 when it opens a take
    (and k+1 exists), otherwise at the end of segment k."""
    seg = pos.get("segment")
    if seg is not None:
        dur = durations.get(seg, 0.0)
        media = pos.get("media_s")
        return seg, min(max(float(media if media is not None else dur), 0.0), dur)
    first = min(durations) if durations else 1
    k = pos.get("after_segment")
    if not k or k not in durations:            # before any segment existed: the first one's start
        return first, 0.0
    if opening and (k + 1) in durations:
        return k + 1, 0.0
    return k, durations.get(k, 0.0)


def kept_intervals(takes: list[dict], durations: dict[int, float]) -> list[dict]:
    """The material C1b keeps, as per-segment intervals in order:
    [{"segment", "start_s", "end_s", "take"}]. With no take at all, every
    segment whole (the "zero take presses renders whole" rule)."""
    live = [t for t in takes if t.get("discarded_by") is None]
    if not takes:
        return [{"segment": k, "start_s": 0.0, "end_s": round(d, 3), "take": None} for k, d in durations.items()]
    out = []
    order = sorted(durations)
    for t in live:
        s1, a = resolve_position(t["open"], durations, opening=True)
        close = t.get("close") or {"segment": order[-1] if order else 1, "media_s": None}
        s2, b = resolve_position(close, durations, opening=False)
        if (s2, b) <= (s1, a):
            continue
        for k in order:
            if k < s1 or k > s2:
                continue
            start = a if k == s1 else 0.0
            end = b if k == s2 else durations[k]
            if end > start:
                out.append({"segment": k, "start_s": round(start, 3), "end_s": round(end, 3), "take": t["id"]})
    return out


def summarize(takes: list[dict], durations: dict[int, float]) -> dict:
    """{"segments", "takes", "takes_discarded", "kept_s", "total_s", "whole", "kept"}:
    computed from the event record alone (no rendering)."""
    total = round(sum(durations.values()), 3)
    intervals = kept_intervals(takes, durations)
    kept = round(sum(i["end_s"] - i["start_s"] for i in intervals), 3)
    live = [t for t in takes if t.get("discarded_by") is None]
    return {"segments": len(durations), "takes": len(live), "takes_discarded": len(takes) - len(live),
            "kept_s": kept, "total_s": total, "whole": not takes, "kept": intervals}


def format_mmss(seconds: float | None) -> str:
    """Same format as the pill and the dialog's duration (pill.format_elapsed:
    whole seconds, truncated), so one status line never shows 00:06 and 0:07
    for the same length."""
    if not isinstance(seconds, (int, float)):
        return "?"
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def summary_line(summary: dict | None) -> str | None:
    """The dialog's status-line fragment, e.g. '3 takes · 02:14 kept of 05:02 · 2 segments';
    None when there is nothing beyond what a plain recording shows (no takes, one segment)."""
    if not summary:
        return None
    parts = []
    if not summary.get("whole"):
        n = summary.get("takes", 0)
        parts.append(f"{n} take{'s' if n != 1 else ''}")
        parts.append(f"{format_mmss(summary.get('kept_s'))} kept of {format_mmss(summary.get('total_s'))}")
    if summary.get("segments", 1) > 1:
        parts.append(f"{summary['segments']} segments")
    return "  ·  ".join(parts) or None
