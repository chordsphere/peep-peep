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
  screens   (C1d, auto-takes; ratified 2026-10-07) two learnable screens. The
            agent's sampler reports when a learned screen appears or goes
            away (kind "screen", source "visual"); a learn press reports the
            screen it captured (kind "learn"). The rules, decided by
            next_effect like every other boundary:
                                  no take open              take open
              end screen appears  ignored ("no take open")  the take closes at
                                                            the screen's first frame
              start screen        the take opens when it    ignored ("take already
              appears             goes away (the screen     open")
                                  itself is not kept)
            Implicit take: a recording with no takes counts as one take
            running from the start; the first end-screen appearance closes it
            (take 1, `implicit`), and the recording is in take mode from then
            on. Alternation falls out of the guards. Automatic boundaries go
            on the take's undo stack exactly like pressed ones, so a
            correction moves an automatic close or drops an automatically
            opened take. Learning counts the appearance it is learned on (not
            earlier ones). Learn presses and visual events are not take-key
            presses: neither opens the correction's debounce window, so a
            correction right after an automatic close is never "too fast".

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
  1. Records                    (~line 95)
  2. The chart (pure)           (~line 150)
  3. EventModel                 (~line 260)
  4. Summary (kept / total)     (~line 760)
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 1. Records
# ---------------------------------------------------------------------------

TAKE, CORRECT, PAUSE, RESUME, PAUSE_TOGGLE, MARK = "take", "correct", "pause", "resume", "pause-toggle", "mark"
RETAKE = "retake"                       # C1a's name for the correction key: accepted wherever a kind is read
KIND_ALIASES = {RETAKE: CORRECT}
# Session C1d (auto-takes): a learn press, a forget command, and a visual event from the agent's sampler.
LEARN, FORGET, SCREEN = "learn", "forget", "screen"
KINDS = (TAKE, CORRECT, PAUSE, RESUME, PAUSE_TOGGLE, MARK, LEARN, FORGET, SCREEN)
# The chord a press belongs to, for the debounce: pause, resume and the toggle share one.
CHORD = {TAKE: "take", CORRECT: "correct", PAUSE: "pause", RESUME: "pause", PAUSE_TOGGLE: "pause", MARK: "mark",
         LEARN: "learn", FORGET: "forget", SCREEN: "screen"}
DEBOUNCED_CHORDS = ("take", "correct", "pause")
# Which accepted presses open each chord's debounce window. Take and pause: their own hotkey presses
# (C1a decision 5). Correct: the chart's "within 1 s of the previous accepted press", literally: the last
# accepted press of any kind, from any source (ANY_PRESS).
ANY_PRESS = "*"
DEBOUNCE_AGAINST = {"take": ("take",), "correct": ANY_PRESS, "pause": ("pause",)}
DEFAULT_DEBOUNCE_S = 1.0
TAKE_RULE = "correction/1"              # the sidecar's `take_rule`; absent in C1a's sidecars (the retake rule)
AUTO_RULE = "auto-takes/1"              # the sidecar's auto_takes.rule (C1d), present once a screen is learned
SCREEN_KINDS = ("start", "end")
# What next_effect decides for the visual rules (C1d): an end screen appearing, a start screen
# appearing, and a counted start screen going away.
END_SCREEN, START_SCREEN, START_GONE = "end-screen", "start-screen", "start-screen-gone"
# Visual events that change nothing anyone should be told about: no feedback line, no new feedback seq.
QUIET_IGNORED = ("stale-reference", "already-present", "not-present", "unknown-change", "not-pending")
# The payload a learn / forget / screen request carries besides kind and time (Press.extra).
EXTRA_KEYS = ("screen", "change", "score", "ref", "thumb", "size", "thresholds", "stable", "waited_s",
              "same_as_current", "refused", "sampler", "samples")


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
    source: str = "cli"          # hotkey | cli | visual (C1d: the agent's sampler)
    label: str = ""
    requested_as: str = ""       # the alias the request used ("retake"), when it used one
    extra: dict = field(default_factory=dict, compare=False, hash=False)   # C1d: EXTRA_KEYS of the request

    @classmethod
    def from_request(cls, req: dict) -> "Press":
        qpc = req.get("requested_qpc")
        raw = str(req.get("kind") or MARK)
        kind = canonical_kind(raw)
        return cls(kind=kind, qpc=float(qpc) if isinstance(qpc, (int, float)) else None,
                   at=req.get("requested_at"), source=str(req.get("source") or "cli"),
                   label=str(req.get("label") or ""), requested_as=raw if raw != kind else "",
                   extra={k: req[k] for k in EXTRA_KEYS if k in req})


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
    """What one press of `kind` (take | correct), or one visual event (C1d:
    end-screen | start-screen | start-screen-gone), does in `state` (a
    take_state() snapshot, or the model's own): the chart and the auto-take
    rules, in one function.

      {"action": "open",  "take": n}                 a take opens (n: its id)
      {"action": "close", "take": n}                 take n closes
      {"action": "close", "take": 1, "implicit": True}
                                                     the implicit whole-video take closes (C1d)
      {"action": "pending", "take": n}               a start screen is up: take n opens when it goes (C1d)
      {"action": "moved-close", "take": n}           take n's close moves to this press
      {"action": "dropped-take", "dropped": n, "take": m}
                                                     take n is dropped, fresh take m opens here
      {"action": "ignored", "reason": "nothing-to-correct" | "no-take-open" | "take-already-open"
                                      | "not-pending" | "end-screen-up"}

    Debounce is not here: it depends on time, not on the take state, and is
    decided before this (EventModel.press). Neither is the ordering guard for
    a visual event that arrives after a later press (EventModel._too_early)."""
    kind = canonical_kind(kind)
    mode, take = state.get("mode", NONE), state.get("take")
    undo = list(state.get("undo") or [])
    fresh = state.get("next_take") or 1
    if kind in (END_SCREEN, START_SCREEN, START_GONE):
        return _screen_effect(state, kind, mode, take, fresh)
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


def _screen_effect(state: dict, kind: str, mode: str, take, fresh: int) -> dict:
    """The rules table (C1d, ratified 2026-10-07). `implicit`: no take exists
    yet, so the whole recording counts as one take from the start; an old
    snapshot without the key is read from next_take."""
    implicit = state.get("implicit", fresh == 1)
    if kind == END_SCREEN:
        if mode == OPEN:
            return {"action": "close", "take": take}
        if mode == NONE and implicit:
            return {"action": "close", "take": 1, "implicit": True}
        return {"action": "ignored", "reason": "no-take-open"}
    if kind == START_SCREEN:
        if mode == OPEN:
            return {"action": "ignored", "reason": "take-already-open"}
        return {"action": "pending", "take": fresh}
    if not state.get("start_pending"):
        return {"action": "ignored", "reason": "not-pending"}
    if mode == OPEN:                     # a Take press opened one while the start screen was up
        return {"action": "ignored", "reason": "take-already-open"}
    if ((state.get("screens") or {}).get("end") or {}).get("present"):
        # the start screen gave way straight to the end screen: no content came between, so no take
        # opens on top of the end screen (the review's finding)
        return {"action": "ignored", "reason": "end-screen-up"}
    return {"action": "open", "take": fresh, "previous": take if mode == CLOSED else None}


def next_effects(state: dict) -> dict:
    """{"take": effect, "correct": effect}: what each take-stack key would do now."""
    return {TAKE: next_effect(state, TAKE), CORRECT: next_effect(state, CORRECT)}


def is_quiet(rec: dict) -> bool:
    """A visual event that changed nothing (a screen going away that nothing waited
    for, one from a reference since replaced): recorded, but no feedback line, so
    it never wipes the pill's last line or `peep learn`'s answer."""
    return rec.get("kind") == SCREEN and (rec.get("action") == "gone" or rec.get("ignored") in QUIET_IGNORED)


def learn_effects(state: dict) -> dict:
    """{"end": effect, "start": effect}: what learning each screen *now* would do
    to the takes (C1d: the appearance it is learned on counts). The pill's hint
    names a learn key from this, the same function the model decides with."""
    return {"end": next_effect(state, END_SCREEN), "start": next_effect(state, START_SCREEN)}


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
    if rec.get("kind") in (LEARN, FORGET, SCREEN):        # C1d: which screen, and what it did to the takes
        for key in ("screen", "change", "screen_effect", "stable", "same_as_current"):
            if rec.get(key) is not None:
                fb[key] = rec[key]
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
        # C1d, auto-takes. The current reference of each screen (None: not learned, or forgotten), every
        # reference ever learned in order (the sidecar keeps them all: a take boundary names the one that
        # made it), whether each screen is on screen now, the counted start appearance waiting for its
        # screen to go away, and what the pill and C1e show per screen.
        self.references: dict[str, dict | None] = {k: None for k in SCREEN_KINDS}
        self.reference_log: list[dict] = []
        self.present: dict[str, bool] = {k: False for k in SCREEN_KINDS}
        self.start_pending: int | None = None
        self.screen_stats: dict[str, dict] = {k: {"seen": 0, "last_seen_qpc": None, "last_seen_at": None,
                                                  "last_effect": None, "learns": 0, "last_learn": None}
                                              for k in SCREEN_KINDS}

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
        if kind in (LEARN, FORGET, SCREEN):
            self._auto(rec, p)
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
        """The part of take_state() the chart reads: mode, current take, its undo stack;
        and for the auto-take rules (C1d) whether the implicit take still stands (no
        take yet) and whether a counted start screen is waiting to go away."""
        mode, cur = take_mode(self.takes)
        return {"mode": mode, "take": cur["id"] if cur else None, "undo": list(cur.get("undo") or []) if cur else [],
                "next_take": len(self.takes) + 1, "implicit": not self.takes,
                "start_pending": self.start_pending is not None,
                "screens": {k: {"learned": self.references[k] is not None, "present": self.present[k]}
                            for k in SCREEN_KINDS}}

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

    # -- auto-takes (C1d) ----------------------------------------------------------

    def _auto(self, rec: dict, p: Press) -> None:
        """A learn press, a forget, or a visual event: every one is recorded, with
        which screen and what it did (or why it did nothing)."""
        x = p.extra or {}
        screen = x.get("screen")
        rec["screen"] = screen
        if x.get("ref") is not None:
            rec["ref"] = x["ref"]
        if screen not in SCREEN_KINDS:
            rec["ignored"] = "unknown-screen"
            return
        {LEARN: self._learn, FORGET: self._forget, SCREEN: self._screen}[rec["kind"]](rec, x, screen)

    def _learn(self, rec: dict, x: dict, screen: str) -> None:
        """Learn (or re-learn) a screen. The agent captured it and already refused
        what it could judge (a screen that matches the other one); the model refuses
        a learn while paused (nothing is recorded, so there is no appearance). The
        appearance it is learned on counts, unless it is the same appearance of the
        same screen learned again (`same_as_current`)."""
        stats = self.screen_stats[screen]
        stats["learns"] += 1
        for key in ("stable", "waited_s", "same_as_current"):
            if key in x:
                rec[key] = x[key]
        reason = x.get("refused") or ("paused" if self.paused else None)
        size = x.get("size")
        if reason is None and (not x.get("thumb") or not isinstance(size, (list, tuple)) or len(size) != 2):
            reason = "no-thumbnail"
        if reason is not None:
            rec["ignored"] = reason
            stats["last_learn"] = {"event": rec["id"], "outcome": "refused", "reason": reason, "ref": x.get("ref")}
            return
        replaced = self.references[screen]
        ref = {"ref": x.get("ref") or f"{screen}-{rec['id']}", "screen": screen, "event": rec["id"],
               "qpc": rec["qpc"], "at": rec.get("requested_at"), "size": [int(size[0]), int(size[1])],
               "thumb": str(x["thumb"]), "stable": bool(x.get("stable", True)), "waited_s": x.get("waited_s"),
               "thresholds": x.get("thresholds") if isinstance(x.get("thresholds"), dict) else None,
               "sampler": x.get("sampler") if isinstance(x.get("sampler"), dict) else None,
               "replaces": replaced["ref"] if replaced else None}
        self.references[screen] = ref
        self.reference_log.append(ref)
        rec["ref"] = ref["ref"]
        rec.update(accepted=True, action="relearned" if replaced else "learned")
        stats["last_learn"] = {"event": rec["id"], "outcome": rec["action"], "ref": ref["ref"]}
        if x.get("same_as_current") and self.present[screen]:
            rec["screen_effect"] = {"action": "none", "reason": "same-appearance"}
            return
        if screen == "start" and self.start_pending is not None:
            self.start_pending = None            # the earlier start screen's pending take: superseded by this one
            rec["superseded_pending"] = True
        self.present[screen] = True
        stats["seen"] += 1
        stats["last_seen_qpc"], stats["last_seen_at"] = rec["qpc"], rec.get("requested_at")
        rec["screen_effect"] = self._appearance(screen, rec, learned=True)

    def _forget(self, rec: dict, x: dict, screen: str) -> None:
        if self.references[screen] is None:
            rec["ignored"] = "not-learned"
            return
        rec["ref"] = self.references[screen]["ref"]
        self.references[screen] = None
        self.present[screen] = False
        if screen == "start" and self.start_pending is not None:
            rec["dropped_pending"] = self.start_pending
            self.start_pending = None
        rec.update(accepted=True, action="forgotten")

    def _screen(self, rec: dict, x: dict, screen: str) -> None:
        """A learned screen appeared or went away (the sampler's hysteresis made
        it certain; `qpc` is the first sample of the run)."""
        change = x.get("change")
        rec["change"] = change
        rec["source"] = "visual"
        if isinstance(x.get("score"), dict):
            rec["score"] = x["score"]
        if x.get("samples") is not None:
            rec["samples"] = x["samples"]
        cur = self.references[screen]
        if cur is None or x.get("ref") != cur["ref"]:
            rec["ignored"] = "stale-reference"         # forgotten or re-learned since the sampler saw it
            return
        stats = self.screen_stats[screen]
        if change == "appear":
            if self.present[screen]:
                rec["ignored"] = "already-present"
                return
            self.present[screen] = True
            stats["seen"] += 1
            stats["last_seen_qpc"], stats["last_seen_at"] = rec["qpc"], rec.get("requested_at")
            eff = self._appearance(screen, rec, learned=False)
            rec["screen_effect"] = eff
            if eff["action"] == "ignored":
                rec["ignored"] = eff["reason"]
            else:
                rec.update(accepted=True, action=eff["action"], take=eff.get("take"))
            return
        if change != "gone":
            rec["ignored"] = "unknown-change"
            return
        if not self.present[screen]:
            rec["ignored"] = "not-present"
            return
        self.present[screen] = False
        if screen != "start":
            rec.update(accepted=True, action="gone")        # an end screen going away changes nothing
            return
        eff = next_effect(self._stack_state(), START_GONE)
        if self.start_pending is None:                      # nothing waits for this one: an observation
            rec["screen_effect"] = eff
            rec.update(accepted=True, action="gone")
            return
        pending, self.start_pending = self.start_pending, None
        rec["pending_from"] = pending
        if eff["action"] == "open" and self._too_early(rec):
            eff = {"action": "ignored", "reason": "earlier-than-the-last-boundary"}
        rec["screen_effect"] = eff
        stats["last_effect"] = {"event": rec["id"], **eff}
        if eff["action"] != "open":
            rec["ignored"] = eff["reason"]
            return
        take = self._new_take(rec)
        rec.update(accepted=True, action="open", take=take["id"])

    def _too_early(self, rec: dict) -> bool:
        """A visual event is stamped at its first matching sample but reaches the
        model up to a few hundred ms later, after any press made meanwhile. An
        automatic boundary earlier than the current take's last boundary would
        put the takes out of order: it is ignored instead (the press made while
        the screen was up stands)."""
        mode, cur = take_mode(self.takes)
        if cur is None or rec.get("qpc") is None:
            return False
        last = [b.get("qpc") for b in (cur.get("open"), cur.get("close")) if isinstance(b, dict)]
        last = [q for q in last if isinstance(q, (int, float))]
        return bool(last) and rec["qpc"] < max(last)

    def _opened_by_a_screen(self) -> bool:
        """The current take is open and a start screen's going opened it."""
        mode, cur = take_mode(self.takes)
        if mode != OPEN:
            return False
        ev = next((e for e in self.events if e["id"] == (cur.get("open") or {}).get("event")), None)
        return bool(ev and ev.get("kind") == SCREEN)

    def _appearance(self, screen: str, rec: dict, learned: bool) -> dict:
        """The rules table for one counted appearance at `rec` (a screen event, or a
        learn press: learning counts the appearance it is learned on). Returns the
        effect, already applied to the takes."""
        stats = self.screen_stats[screen]
        kind = END_SCREEN if screen == "end" else START_SCREEN
        eff = next_effect(self._stack_state(), kind)
        empty = False
        if eff["action"] in ("close", "pending") and self._too_early(rec):
            if eff["action"] == "close" and self._opened_by_a_screen():
                # The end screen was up before the start screen's going reached the model (an end screen
                # learned while its capture settled, say): an automatic open never wins over a screen,
                # so that take closes where it opened, empty (the review's finding).
                empty = True
            else:
                eff = {"action": "ignored", "reason": "earlier-than-the-last-boundary"}
        if eff["action"] == "close":
            if eff.get("implicit"):
                take = self._implicit_take(rec)
            else:
                take = self.takes[-1]
                take["close"] = {**take["open"], "event": rec["id"]} if empty else self._position(rec)
                take["status"] = "closed"
                take["undo"] = ["open", "close"]
                if empty:
                    eff = {**eff, "empty": True}
            if learned:
                rec["take"] = take["id"]
        elif eff["action"] == "pending":
            self.start_pending = rec["id"]
        stats["last_effect"] = {"event": rec["id"], **eff}
        return eff

    def _implicit_take(self, rec: dict) -> dict:
        """The implicit take made explicit (C1d): take 1 from the first frame of the
        recording to here. Only an end screen does this, and only while no take
        exists; from then on the recording is in take mode."""
        first = min(self.segments) if self.segments else 1
        take = {"id": len(self.takes) + 1, "open": {"event": None, "implicit": True, "qpc": None, "segment": first,
                                                   "media_s": 0.0},
                "close": self._position(rec), "discarded_by": None, "status": "closed", "undo": ["open", "close"],
                "implicit": True}
        self.takes.append(take)
        return take

    def auto_takes_state(self) -> dict:
        """What active.json's `auto_takes` block says per screen (C1e shows it):
        learned or not, when, its reference id, how often it has been seen, when
        last, and the last thing it did. The recorder adds the thumbnail's path."""
        out = {}
        for k in SCREEN_KINDS:
            ref, st = self.references[k], self.screen_stats[k]
            out[k] = {"learned": ref is not None, "ref": ref["ref"] if ref else None,
                      "learned_at": ref["at"] if ref else None, "learned_qpc": ref["qpc"] if ref else None,
                      "stable": ref["stable"] if ref else None, "present": self.present[k],
                      "seen": st["seen"], "last_seen_qpc": st["last_seen_qpc"], "last_seen_at": st["last_seen_at"],
                      "last_effect": st["last_effect"], "learns": st["learns"], "last_learn": st["last_learn"]}
        out["start"]["pending"] = self.start_pending is not None
        return out

    def auto_takes_record(self) -> dict | None:
        """The sidecar's `auto_takes` block, or None when no screen was ever learned
        (a recording that never learns one keeps exactly C1c's sidecar)."""
        if not self.reference_log:
            return None
        return {"rule": AUTO_RULE, "references": list(self.reference_log),
                "current": {k: (self.references[k] or {}).get("ref") for k in SCREEN_KINDS},
                "pending_start": self.start_pending}

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
