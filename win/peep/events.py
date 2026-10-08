"""The capture side of the editing model (session C1a, correction rule from
C1c): what each take, correction, pause, resume and mark press means,
decided as it happens and written down for the render (C1b) to cut from.
Pure: no clock, no files, no threads. The recorder feeds it presses and
segment boundaries; tests feed it the same and check every sequence.

The model, as ratified (2026-10-06; the correction chart 2026-10-07):

  session   Ctrl+Alt+R on/off, exactly as before; always captured whole.
  segment   hard pause stops capture; resume opens the next segment of the
            same session. Each segment is its own ffmpeg (own anchor, own
            flashes); the render joins them.
  take      one chord toggles: the first press opens a take, the next closes
            it. Only takes survive the render; a session with no take at
            all renders whole. Capture never stops for a take (the soft pause).
            Only an explicit Take press starts a take.
  correct   (C1c; C1a called it retake, which stays an accepted alias.) Each
            take keeps a two-level undo stack of its original presses, open
            then close. A correction pops the most recent one and re-issues
            the same kind of boundary at the press: popping a close moves the
            close here; popping an open drops the take and opens a fresh one
            here. Only the current (last) take is correctable: once the next
            take opens, the previous one is final. With no take yet there is
            nothing to correct (ignored, and said so).
  debounce  a hotkey press within `debounce_s` of the last accepted one is
            ignored (and recorded as ignored). Per chord for take and pause
            (C1a); the correction key, as the chart says, is measured from the
            previous accepted press, whatever it was (take, correct, mark,
            pause, resume; hotkey or WSL). WSL commands are deliberate and are
            never debounced themselves.
  pause     take state carries across it: a take open at the pause is open
            at the resume, and the segment boundary is a cut with nothing
            to remove. Pause / resume never touch the undo stack; a close
            moved later across a pause makes the take span the pause.

The chart is decided in one place, `next_effect(state, kind)`: the model's
own take and correct handlers call it to know what to do, and the pill calls
it on the snapshot the recorder publishes (`EventModel.take_state`) to say
what each key would do now. The two can never disagree.

Time. Every press carries the requester's QPC stamp (time.perf_counter on
Windows: QueryPerformanceCounter, one clock for every process, the clock
A.1's audio and every flash already use). `locate(qpc)` places it on the
segment timeline: inside segment k at `media_s` seconds from that segment's
first video frame (A.1's `video_epoch_qpc_est`), or in the pause after
segment k. A press during a pause takes effect at the boundary: an opening
boundary (a take opening, a fresh take after a dropped one) at the start of
the next segment, a closing boundary (a close, a moved close) at the end of
the previous one.

Old sidecars. The record keeps resolved boundaries, not press semantics:
each take's `open` / `close` position and `discarded_by`, and the summary's
`kept` intervals. The render reads only those (and each event's fiducial),
so a sidecar written under C1a's retake rule renders exactly as recorded,
with nothing to migrate. New sidecars say `take_rule: "correction/1"`;
its absence means C1a's rule decided the takes.

Sections:
  1. Records                    (~line 70)
  2. The chart (pure)           (~line 120)
  3. EventModel                 (~line 195)
  4. Summary (kept / total)     (~line 510)
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 1. Records
# ---------------------------------------------------------------------------

TAKE, CORRECT, PAUSE, RESUME, PAUSE_TOGGLE, MARK = "take", "correct", "pause", "resume", "pause-toggle", "mark"
RETAKE = "retake"                       # C1a's name for the correction key: accepted wherever a kind is read
KIND_ALIASES = {RETAKE: CORRECT}
KINDS = (TAKE, CORRECT, PAUSE, RESUME, PAUSE_TOGGLE, MARK)
# The chord a press belongs to, for the debounce: pause, resume and the toggle share one.
CHORD = {TAKE: "take", CORRECT: "correct", PAUSE: "pause", RESUME: "pause", PAUSE_TOGGLE: "pause", MARK: "mark"}
DEBOUNCED_CHORDS = ("take", "correct", "pause")
# Which accepted presses open each chord's debounce window. Take and pause: their own hotkey presses
# (C1a decision 5). Correct: the chart's "within 1 s of the previous accepted press", literally: the last
# accepted press of any kind, from any source (ANY_PRESS).
ANY_PRESS = "*"
DEBOUNCE_AGAINST = {"take": ("take",), "correct": ANY_PRESS, "pause": ("pause",)}
DEFAULT_DEBOUNCE_S = 1.0
TAKE_RULE = "correction/1"              # the sidecar's `take_rule`; absent in C1a's sidecars (the retake rule)


def canonical_kind(kind: str) -> str:
    """'retake' -> 'correct'; every other kind as it is."""
    return KIND_ALIASES.get(kind, kind)


@dataclass(frozen=True)
class Press:
    """One request, as a control file carries it. `kind` is canonical
    (`retake` arrives as `correct`, with `requested_as` saying so)."""
    kind: str
    qpc: float | None            # the requester's QPC at the press (None: an old request file)
    at: str | None = None        # the requester's wall clock, ISO
    source: str = "cli"          # hotkey | cli
    label: str = ""
    requested_as: str = ""       # the alias the request used ("retake"), when it used one

    @classmethod
    def from_request(cls, req: dict) -> "Press":
        qpc = req.get("requested_qpc")
        raw = str(req.get("kind") or MARK)
        kind = canonical_kind(raw)
        return cls(kind=kind, qpc=float(qpc) if isinstance(qpc, (int, float)) else None,
                   at=req.get("requested_at"), source=str(req.get("source") or "cli"),
                   label=str(req.get("label") or ""), requested_as=raw if raw != kind else "")


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


# ---------------------------------------------------------------------------
# 2. The chart (pure)
# ---------------------------------------------------------------------------
#
# The state the chart is written in: which take is current and its undo stack.
#   mode "none"    no take yet: Take opens take 1; Correction has nothing to correct
#   mode "open"    the current take is open, undo ["open"]
#   mode "closed"  the current take is closed, undo ["open", "close"] (as pressed) or ["open"]
#                  (its close was moved once already)

NONE, OPEN, CLOSED = "none", "open", "closed"


def take_mode(takes: list[dict]) -> tuple[str, dict | None]:
    """(mode, current take). The current take is the last one; a drop always
    opens a fresh take, so the last take is never a discarded one while
    recording (a defensive "none" covers it anyway)."""
    last = takes[-1] if takes else None
    if last is None or last.get("discarded_by") is not None:
        return NONE, None
    return (OPEN if last.get("close") is None else CLOSED), last


def next_effect(state: dict, kind: str) -> dict:
    """What one press of `kind` (take | correct) does in `state` (a take_state()
    snapshot, or the model's own): the chart, in one function.

      {"action": "open",  "take": n}                 a take opens (n: its id)
      {"action": "close", "take": n}                 take n closes
      {"action": "moved-close", "take": n}           take n's close moves to this press
      {"action": "dropped-take", "dropped": n, "take": m}
                                                     take n is dropped, fresh take m opens here
      {"action": "ignored", "reason": "nothing-to-correct"}

    Debounce is not here: it depends on time, not on the take state, and is
    decided before this (EventModel.press)."""
    kind = canonical_kind(kind)
    mode, take = state.get("mode", NONE), state.get("take")
    undo = list(state.get("undo") or [])
    fresh = state.get("next_take") or 1
    if kind == TAKE:
        if mode == OPEN:
            return {"action": "close", "take": take}
        return {"action": "open", "take": fresh, "previous": take if mode == CLOSED else None}
    if kind == CORRECT:
        if mode == NONE or not undo:
            return {"action": "ignored", "reason": "nothing-to-correct"}
        if undo[-1] == "close":
            return {"action": "moved-close", "take": take}
        return {"action": "dropped-take", "dropped": take, "take": fresh}
    raise ValueError(f"next_effect: {kind!r} is not a take-stack key (take | correct)")


def next_effects(state: dict) -> dict:
    """{"take": effect, "correct": effect}: what each take-stack key would do now."""
    return {TAKE: next_effect(state, TAKE), CORRECT: next_effect(state, CORRECT)}


def press_feedback(rec: dict, seq: int, mark: int | None = None) -> dict:
    """What the pill's feedback line says about one press, accepted or ignored,
    as data (pill.feedback_text words it with the configured keys): the event
    record's own outcome, never a re-derivation of it. `seq` numbers the
    presses of one recording, so the pill shows each feedback exactly once.
    `mark`: an accepted mark's number in the recording (the recorder knows it)."""
    fb = {"seq": seq, "event": rec.get("id"), "qpc": rec.get("qpc"), "kind": rec.get("kind"),
          "action": rec.get("action"), "ignored": rec.get("ignored"), "take": rec.get("take")}
    if rec.get("action") == "dropped-take":
        fb["dropped"] = rec.get("discarded_take")
    if rec.get("action") == "moved-close":
        fb["moved_s"] = (rec.get("correction") or {}).get("moved_s")
    if mark:
        fb["mark"] = mark
    if rec.get("segment") is None and rec.get("after_segment") is not None:
        fb["at_boundary"] = rec["after_segment"]          # pressed while paused: effective at the boundary
    return fb


# ---------------------------------------------------------------------------
# 3. EventModel
# ---------------------------------------------------------------------------


class EventModel:
    """Decides each press and keeps the record.

    The recorder calls, in order of what happens:
      segment_started(index, epoch_qpc, launch_qpc)   a segment's first frame is known
      press(Press, consumed_qpc) -> event record      every request, recording or paused
      segment_ended(index, end_qpc, duration_s)       its ffmpeg has stopped
      set_paused(bool)                                after a pause / resume took effect
      finish() -> summary                             at the end of the session
    and copies `events`, `takes`, `pauses` and the summary into the sidecar,
    and `take_state()` into active.json for the pill.

    The record of a press says what it did: `accepted`, `ignored` (why not),
    `action` (open / close for a take; moved-close / dropped-take for a
    correction; pause / resume; mark), the take it opened, closed or moved,
    and for a dropped take `discarded_take`. A correction also carries
    `correction`: {"action", "take", "boundary": the resulting boundary
    (side open|close and its position), "from" (a moved close's old
    position) or "dropped", "undo" (the stack after)}; an ignored one
    {"action": "ignored", "reason"}.

    Each take record: `id`, `open`, `close` (positions, see _position),
    `discarded_by` (the correction that dropped it), `status`, `undo` (its
    stack: ["open"], ["open", "close"], or [] once final) and, when its close
    was moved, `superseded_closes` (the positions it had before; their red
    patches now sit inside kept material, which the render conceals)."""

    def __init__(self, debounce_s: float = DEFAULT_DEBOUNCE_S):
        self.debounce_s = debounce_s
        self.events: list[dict] = []
        self.takes: list[dict] = []
        self.pauses: list[dict] = []
        self.segments: dict[int, dict] = {}     # index -> {"epoch_qpc", "launch_qpc", "end_qpc", "duration_s"}
        self.paused = False
        self.finished = False
        self._last_accepted: dict[str, float] = {}     # chord -> qpc of its last accepted hotkey press
        self._last_any: tuple[float, str] | None = None  # (qpc, kind) of the last accepted press of any kind

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
        the fiducial for accepted take/correct/mark presses."""
        kind = canonical_kind(p.kind)
        qpc = p.qpc if p.qpc is not None else consumed_qpc
        rec = {"id": len(self.events) + 1, "kind": kind, "source": p.source, "requested_at": p.at,
               "qpc": _r(qpc, 6), "consumed_qpc": _r(consumed_qpc, 6), "qpc_from": "press" if p.qpc is not None
               else "consumed", **self.locate(qpc), "accepted": False, "ignored": None, "action": None,
               "take": None, "discarded_take": None, "fiducial": None}
        if p.label:
            rec["label"] = p.label
        requested_as = p.requested_as or (p.kind if p.kind != kind else "")
        if requested_as:
            rec["requested_as"] = requested_as
        if kind not in KINDS:
            rec["ignored"] = "unknown-kind"
            self.events.append(rec)
            return rec
        chord = CHORD[kind]
        if p.source == "hotkey" and chord in DEBOUNCED_CHORDS:
            against = DEBOUNCE_AGAINST[chord]
            if against == ANY_PRESS:
                last, after = self._last_any or (None, None)
            else:
                lasts = [(self._last_accepted[c], c) for c in against if c in self._last_accepted]
                last, after = max(lasts) if lasts else (None, None)
            if last is not None and 0 <= qpc - last < self.debounce_s:
                rec["ignored"] = "debounce"
                rec["debounce"] = {"since_last_s": _r(qpc - last), "window_s": self.debounce_s}
                if after != chord:
                    rec["debounce"]["after"] = after
                if kind == CORRECT:
                    rec["correction"] = {"action": "ignored", "reason": "debounce"}
                self.events.append(rec)
                return rec
        handler = {TAKE: self._take, CORRECT: self._correct, MARK: self._mark}.get(kind, self._pause_resume)
        handler(rec)
        if rec["accepted"] and p.source == "hotkey" and chord in DEBOUNCED_CHORDS:
            self._last_accepted[chord] = qpc
        if rec["accepted"]:
            self._last_any = (qpc, kind)
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

    def _stack_state(self) -> dict:
        """The part of take_state() the chart reads: mode, current take, its undo stack."""
        mode, cur = take_mode(self.takes)
        return {"mode": mode, "take": cur["id"] if cur else None, "undo": list(cur.get("undo") or []) if cur else [],
                "next_take": len(self.takes) + 1}

    def _new_take(self, rec: dict) -> dict:
        for t in self.takes:            # "the previous one is final": undo depth is the current take only
            t["undo"] = []
        take = {"id": len(self.takes) + 1, "open": self._position(rec), "close": None, "discarded_by": None,
                "status": "open", "undo": ["open"]}
        self.takes.append(take)
        return take

    def _take(self, rec: dict) -> None:
        eff = next_effect(self._stack_state(), TAKE)
        if eff["action"] == "open":
            take = self._new_take(rec)
            rec.update(accepted=True, action="open", take=take["id"])
        else:
            cur = self.takes[-1]
            cur["close"] = self._position(rec)
            cur["status"] = "closed"
            cur["undo"] = ["open", "close"]
            rec.update(accepted=True, action="close", take=cur["id"])

    def _correct(self, rec: dict) -> None:
        """The chart's correction: pop the current take's undo stack and re-issue
        that boundary here (a close moves; an open drops the take and a fresh one
        opens)."""
        eff = next_effect(self._stack_state(), CORRECT)
        here = self._position(rec)
        if eff["action"] == "ignored":
            rec["ignored"] = eff["reason"]
            rec["correction"] = {"action": "ignored", "reason": eff["reason"]}
            return
        cur = self.takes[-1]
        if eff["action"] == "moved-close":
            old = cur["close"]
            cur.setdefault("superseded_closes", []).append(old)
            cur["close"] = here
            cur["undo"] = ["open"]
            durations = self.durations_at(rec["qpc"])
            moved = timeline_s(here, durations, opening=False) - timeline_s(old, durations, opening=False)
            rec.update(accepted=True, action="moved-close", take=cur["id"])
            rec["correction"] = {"action": "moved-close", "take": cur["id"], "from": old,
                                 "boundary": {"take": cur["id"], "side": "close", **here}, "undo": ["open"],
                                 "moved_s": _r(moved)}
            return
        cur["discarded_by"] = rec["id"]
        cur["status"] = "discarded"
        fresh = self._new_take(rec)
        rec.update(accepted=True, action="dropped-take", take=fresh["id"], discarded_take=cur["id"])
        rec["correction"] = {"action": "dropped-take", "dropped": cur["id"], "take": fresh["id"],
                             "boundary": {"take": fresh["id"], "side": "open", **here}, "undo": ["open"]}

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
                t["undo"] = []                       # the recording is over: nothing is correctable
        return summarize(self.takes, self.durations())

    def durations(self) -> dict[int, float]:
        return {k: float(v["duration_s"] or 0.0) for k, v in sorted(self.segments.items())}

    def counts(self) -> dict:
        """For the pill and active.json: how many takes so far, and whether one is open."""
        live = [t for t in self.takes if t["discarded_by"] is None]
        return {"takes": len(live), "open": self.open_take() is not None}

    def durations_at(self, at_qpc: float | None) -> dict[int, float]:
        """Each segment's length so far: the finished ones as ffmpeg counted them,
        the live one up to `at_qpc` (0 when there is no instant to measure to)."""
        out = {}
        for k, seg in sorted(self.segments.items()):
            if seg.get("duration_s") is not None:
                out[k] = float(seg["duration_s"])
            elif at_qpc is not None and seg.get("epoch_qpc") is not None:
                out[k] = max(0.0, at_qpc - seg["epoch_qpc"])
            else:
                out[k] = 0.0
        return out

    def take_state(self, at_qpc: float | None = None) -> dict:
        """The snapshot the pill shows and next_effect() reads, as of `at_qpc`
        (the recorder passes "now"):

          rule, mode, take, undo, next_take   the chart's state (see next_effect)
          takes                               live takes so far
          paused                              capture is stopped
          at_s                                captured seconds as of at_qpc
          kept_s                              kept seconds then, from the same
                                              kept_intervals() the summary uses
          take_s                              the current take's kept seconds
          growing                             {"kept", "take"}: whether each grows with
                                              capture from at_s on (an open take; with
                                              no take yet, the whole recording is kept)

        A pure function of the model, so a test can hold the pill's hint against
        what the next press does."""
        state = {"rule": TAKE_RULE, **self._stack_state()}
        live = [t for t in self.takes if t.get("discarded_by") is None]
        durations = self.durations_at(at_qpc)
        intervals = kept_intervals(self.takes, durations)
        cur = state["take"]
        state.update(takes=len(live), paused=self.paused, at_s=_r(sum(durations.values())),
                     kept_s=_r(sum(i["end_s"] - i["start_s"] for i in intervals)),
                     take_s=_r(sum(i["end_s"] - i["start_s"] for i in intervals if i["take"] == cur))
                     if cur is not None else None,
                     growing={"kept": not self.paused and (state["mode"] == OPEN or not self.takes),
                              "take": not self.paused and state["mode"] == OPEN})
        return state


# ---------------------------------------------------------------------------
# 4. Summary (kept / total)
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


def timeline_s(pos: dict, durations: dict[int, float], opening: bool) -> float:
    """A boundary as seconds of captured material (segments laid end to end,
    pauses removed): how the pill and the feedback measure time."""
    seg, at = resolve_position(pos, durations, opening)
    return sum(d for k, d in durations.items() if k < seg) + at


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
