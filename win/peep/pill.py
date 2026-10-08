"""The REC pill and the agent's toasts: small always-on-top, click-through
windows, excluded from screen capture.

The pill (`● 00:12` on red) is visible to the user and absent from the
recording: `winapi.exclude_from_capture` sets WDA_EXCLUDEFROMCAPTURE. The
session-B probe (2026-10-05) drew squares with and without the affinity,
including this exact layered + click-through recipe, and grabbed frames
through ddagrab: the control square was captured, both excluded squares
were not. That is why `agent.pill` defaults to true.

If the affinity cannot be set on a given machine (the read-back is not
WDA_EXCLUDEFROMCAPTURE), the overlay refuses to show itself rather than
pollute the recording, and the agent reports it. "Optional" is the
`[agent] pill` config flag, never a code change.

Toasts reuse the same window recipe for short messages: a hotkey that
cannot be registered, "mark 2", "discarded", a failed recording. They
are excluded from capture too, since they can appear mid-recording.

Session C1c: the pill is two lines (mockups ratified 2026-10-07):

  No takes yet          ● 00:41  whole video kept
                        T start take · P pause · M mark
  Take open             ● 03:12  ◉ take 2 · 0:48
                        T close · ⌫ restart take · P pause
  Take just closed      ● 03:20  ○ 2 takes · 2:14 kept
                        T next take · ⌫ move close here
  …after one ⌫          ● 03:24  ○ 2 takes · 2:18 kept
                        T next take · ⌫ drop take 2, restart
  Paused                ❚❚ PAUSED 03:24  ○ 2 takes
                        P resume · nothing is recording

Session C1d (auto-takes) adds feedback lines for the learn keys (`⇥ end
screen learned · take 2 closed`) and for what the sampler saw (`◇ end screen
→ take 2 closed`, `◇ start screen gone → take 3 opened`, `◇ end screen seen
— no take open`), and the hint names a learn key where it fits after C1c's
keys: the one whose screen would make a take boundary now, from the same
decision function (events.learn_effects), shortened to `] end` / `[ start`
when the full words do not fit. Nothing else about the pill changes (the
expanded pill is session C1e).

The first line is the state; the second (the hint) says what each key does
now. The hint is computed by `events.next_effects` from the take state the
recorder publishes, the same function the recorder's event model decides
every take and correct press with, so the pill cannot describe a press
differently from what it does. Key labels come from the configured chords
(an F13 binding shows F13). After each press, accepted or ignored, its
feedback (`⌫ close moved +4.2 s`, `⌫ ignored — too fast`) replaces the hint
for FEEDBACK_S. `[agent] pill_hints = false` keeps the first line only; the
feedback still shows, as a second line, for its 1.5 s.

Pure helpers (tested on WSL): `format_elapsed`, `overlay_position`,
`pill_lines` and what it is built from (`state_line`, `hint_line`,
`feedback_text`, `key_labels`, `live_take_state`).

Sections:
  1. Pure helpers               (~line 60)
  2. The two-line pill (pure)   (~line 125)
  3. Tk overlays                (~line 290)
"""

from __future__ import annotations

import logging

from . import events
from .logsetup import event

log = logging.getLogger("peep.pill")

# ---------------------------------------------------------------------------
# 1. Pure helpers
# ---------------------------------------------------------------------------

COLORS = {"recording": "#C62828", "starting": "#6D4C41", "stopping": "#455A64",
          "info": "#263238", "warning": "#8D6E00", "error": "#B71C1C",
          # Session C1a: a hard pause must be unmistakable at a glance, so it is not red.
          "paused": "#E65100", "pausing": "#455A64", "resuming": "#6D4C41"}


def format_elapsed(seconds: float | None) -> str:
    """12.7 -> '00:12'; 3725 -> '1:02:05'; None/negative -> '00:00'."""
    s = 0 if seconds is None or seconds < 0 else int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def take_text(takes: int, take_open: bool) -> str:
    """'' with no take yet; '◉ take 3' while take 3 is open; '○ 3 takes' between takes."""
    if take_open:
        return f"◉ take {takes}"
    if takes:
        return f"○ {takes} take{'s' if takes != 1 else ''}"
    return ""


def pill_text(state: str, elapsed_s: float | None, takes: int = 0, take_open: bool = False) -> str:
    """The pill's text. `elapsed_s` is the time captured so far (the segments before
    a pause plus the current one), so it stands still while paused."""
    t = take_text(takes, take_open)
    tail = f"  {t}" if t else ""
    if state == "recording":
        return f"● {format_elapsed(elapsed_s)}{tail}"
    if state == "paused":
        return f"❚❚ PAUSED {format_elapsed(elapsed_s)}{tail}"            # the C1c mockup's spacing
    if state == "pausing":
        return "❚❚ pausing…"
    if state == "resuming":
        return "● resuming…"
    if state == "starting":
        return "● starting…"
    if state == "stopping":
        return "■ saving…"
    return state


def overlay_position(position: str, screen_w: int, screen_h: int, w: int, h: int, margin: int,
                     stack: int = 0, gap: int = 8) -> tuple[int, int]:
    """Top-left pixel for a w x h overlay at `position` (config.PILL_POSITIONS).
    `stack` is how many overlay heights (plus gap) to move away from the edge,
    so a toast sits just inside the pill rather than on top of it."""
    vertical, _, horizontal = position.partition("-")
    if horizontal == "left":
        x = margin
    elif horizontal == "center":
        x = (screen_w - w) // 2
    else:
        x = screen_w - margin - w
    offset = stack * (h + gap)
    y = margin + offset if vertical == "top" else screen_h - margin - h - offset
    return max(0, x), max(0, y)


# ---------------------------------------------------------------------------
# 2. The two-line pill (pure)
# ---------------------------------------------------------------------------

FEEDBACK_S = 1.5                 # a press's feedback replaces the hint line this long (the ratified 1.5 s)
FEEDBACK_LATE_S = 1.0            # feedback first seen later than FEEDBACK_S + this after its press is not shown
HINT_MAX_CHARS = 44              # a longer hint drops its trailing keys, then is cut with "…"
KEY_SYMBOLS = {"Backspace": "⌫", "Delete": "Del", "Escape": "Esc", "Insert": "Ins", "PageUp": "PgUp",
               "PageDown": "PgDn", "LeftBracket": "[", "RightBracket": "]"}
# The CLI's wording of a feedback: no key label (it prints "↺ correct: <feedback>").
WORD_LABELS = {"take": "", "correct": "", "pause": "", "mark": ""}
LEARN_MARK, SEEN_MARK = "⇥", "◇"     # C1d: a learn press's feedback / what the sampler saw


def key_label(chord_text: str) -> str:
    """A chord as the hint shows it: its key without the modifiers,
    'Ctrl+Alt+T' -> 'T', 'Ctrl+Alt+Backspace' -> '⌫', 'F13' -> 'F13'."""
    key = str(chord_text).split("+")[-1].strip()
    return KEY_SYMBOLS.get(key, key)


def key_labels(agent_cfg) -> dict:
    """{"take", "correct", "pause", "mark"} -> label, from the configured chords.
    Two chords whose keys abbreviate alike (Ctrl+Alt+T and Ctrl+Shift+T) are
    shown in full, so the hint never names one key for two actions."""
    chords = {"take": agent_cfg.take_hotkey, "correct": agent_cfg.correct_hotkey,
              "pause": agent_cfg.pause_hotkey, "mark": agent_cfg.mark_hotkey}
    for action in ("learn_start", "learn_end"):              # C1d; an AgentConfig from before C1d has neither
        if getattr(agent_cfg, f"{action}_hotkey", None):
            chords[action] = getattr(agent_cfg, f"{action}_hotkey")
    short = {a: key_label(t) for a, t in chords.items()}
    clash = {v for v in short.values() if list(short.values()).count(v) > 1}
    return {a: (chords[a] if short[a] in clash else short[a]) for a in chords}


def format_short(seconds: float | None) -> str:
    """A take's or the kept time, as in the mockups: 48 -> '0:48', 134 -> '2:14',
    3725 -> '1:02:05' (whole seconds, truncated like format_elapsed)."""
    s = 0 if not isinstance(seconds, (int, float)) or seconds < 0 else int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def live_take_state(state: dict | None, since_s: float | None, recording: bool) -> dict | None:
    """The published take state, advanced by `since_s` seconds of capture: an
    open take's time (and, with no take yet, the kept whole) keeps growing
    between presses. Paused or not recording: as published."""
    if not isinstance(state, dict):
        return None
    out = dict(state)
    grow = state.get("growing") or {}
    extra = max(0.0, since_s) if recording and isinstance(since_s, (int, float)) else 0.0
    for key, flag in (("kept_s", "kept"), ("take_s", "take")):
        if extra and grow.get(flag) and isinstance(out.get(key), (int, float)):
            out[key] = out[key] + extra
    return out


def state_line(status: str, elapsed_s: float | None, state: dict | None, takes: int = 0,
               take_open: bool = False) -> str:
    """The pill's first line. Without a take state (an active.json from before
    C1c, or a recorder that has not published one yet) it is C1a's text."""
    if not isinstance(state, dict) or status not in ("recording", "paused"):
        return pill_text(status, elapsed_s, takes, take_open)
    mode, n = state.get("mode"), int(state.get("takes") or 0)
    if status == "paused":
        if mode == events.OPEN:
            tail = f"  ◉ take {state.get('take')}"
        else:
            tail = f"  ○ {n} take{'s' if n != 1 else ''}" if n else ""
        return f"❚❚ PAUSED {format_elapsed(elapsed_s)}{tail}"
    head = f"● {format_elapsed(elapsed_s)}"
    if mode == events.OPEN:
        return f"{head}  ◉ take {state.get('take')} · {format_short(state.get('take_s'))}"
    if mode == events.CLOSED:
        return f"{head}  ○ {n} take{'s' if n != 1 else ''} · {format_short(state.get('kept_s'))} kept"
    return f"{head}  whole video kept"


def hint_words(state: dict) -> dict:
    """What each key would do now, in words: {"take": "close", "correct":
    "move close here", ...}; a key that would do nothing is absent. From
    events.next_effects: the decision the next press will actually get."""
    eff = events.next_effects(state)
    mode = state.get("mode")
    take, corr = eff[events.TAKE], eff[events.CORRECT]
    out = {"take": "close" if take["action"] == "close" else
           ("next take" if mode == events.CLOSED else "start take")}
    if corr["action"] == "moved-close":
        out["correct"] = "move close here"
    elif corr["action"] == "dropped-take":
        out["correct"] = "restart take" if mode == events.OPEN else f"drop take {corr['dropped']}, restart"
    if mode in (events.NONE, events.OPEN):
        out["pause"] = "pause"
    if mode == events.NONE:
        out["mark"] = "mark"
    return out


def _fit(items: list[str], limit: int = HINT_MAX_CHARS) -> str:
    """Join with ' · ', dropping trailing items (never the first two) and then
    cutting with '…' until it fits."""
    items = list(items)
    while len(" · ".join(items)) > limit and len(items) > 2:
        items.pop()
    text = " · ".join(items)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def learn_hints(state: dict) -> list:
    """C1d: which learn keys the hint names, in order, as (key, long words,
    short words): a screen not learned yet whose appearance now would make a
    take boundary (events.learn_effects: the decision the model will make when
    the screen is learned on, or appears). An end screen closes the open take
    (or the implicit whole-video one); a start screen opens one when it goes."""
    screens = state.get("screens") if isinstance(state.get("screens"), dict) else {}
    effects = events.learn_effects(state)
    out = []
    for kind, key in (("end", "learn_end"), ("start", "learn_start")):
        if (screens.get(kind) or {}).get("learned"):
            continue
        if effects[kind]["action"] in ("close", "pending"):
            out.append((key, f"{kind} screen", kind))
    return out


def hint_line(status: str, state: dict | None, labels: dict) -> str | None:
    """The pill's second line, or None when there is nothing to say (starting,
    saving, pausing…, or no take state)."""
    if status == "paused":
        return f"{labels.get('pause', 'P')} resume · nothing is recording"
    if status != "recording" or not isinstance(state, dict):
        return None
    words = hint_words(state)
    text = _fit([f"{labels.get(k, k)} {words[k]}" for k in ("take", "correct", "pause", "mark") if k in words])
    for key, long, short in learn_hints(state):     # C1d: only where they fit; never displacing C1c's keys
        if key not in labels:
            continue
        for words_ in (long, short):
            candidate = f"{text} · {labels[key]} {words_}"
            if len(candidate) <= HINT_MAX_CHARS and not text.endswith("…"):
                text = candidate
                break
    return text


def screen_feedback(fb: dict) -> str | None:
    """C1d: the feedback line of a learn press, a forget, or a visual event; None
    for what changes nothing worth a line (a screen going away that nothing
    waited for, an event from a reference already replaced)."""
    kind, screen = fb.get("kind"), fb.get("screen") or "?"
    ignored, action = fb.get("ignored"), fb.get("action")
    eff = fb.get("screen_effect") if isinstance(fb.get("screen_effect"), dict) else {}
    other = {"start": "end", "end": "start"}.get(screen, "other")
    if kind == "learn":
        if ignored:
            return {f"same-as-{other}-screen": f"{LEARN_MARK} not learned: that is the {other} screen",
                    "paused": f"{LEARN_MARK} {screen} screen: not while paused",
                    "no-thumbnail": f"{LEARN_MARK} {screen} screen: could not read it"}.get(
                ignored, f"{LEARN_MARK} {screen} screen not learned ({ignored})")
        text = f"{LEARN_MARK} {screen} screen " + ("re-learned" if action == "relearned" else "learned")
        if eff.get("action") == "close":
            text += f" · take {eff.get('take')} closed"
        elif eff.get("action") == "pending":
            text += " · opens a take when gone"
        elif eff.get("action") == "ignored":
            text += {"no-take-open": " · no take open", "take-already-open": " · take already open"}.get(
                eff.get("reason"), "")
        if fb.get("stable") is False:
            text += " (moving)"
        return text
    if kind == "forget":
        return f"{LEARN_MARK} {screen} screen " + ("was not learned" if ignored else "forgotten")
    if kind != "screen":
        return None
    change = fb.get("change")
    if ignored in events.QUIET_IGNORED:
        return None
    if change == "appear":
        if action == "close":
            return f"{SEEN_MARK} {screen} screen → take {fb.get('take')} closed"
        if action == "pending":
            return f"{SEEN_MARK} {screen} screen up → take opens when it goes"
        why = {"no-take-open": "no take open", "take-already-open": "take already open",
               "earlier-than-the-last-boundary": "a press came first"}.get(ignored, ignored)
        return f"{SEEN_MARK} {screen} screen seen — {why}"
    if change == "gone":
        if action == "open":
            return f"{SEEN_MARK} {screen} screen gone → take {fb.get('take')} opened"
        if ignored:
            why = {"take-already-open": "take already open", "end-screen-up": "the end screen is up",
                   "earlier-than-the-last-boundary": "a press came first"}.get(ignored, ignored)
            return f"{SEEN_MARK} {screen} screen gone — {why}"
    return None


def feedback_text(fb: dict | None, labels: dict) -> str | None:
    """One press's feedback line (events.press_feedback, worded): the mockups'
    `⌫ close moved +4.2 s`, `⌫ take 2 dropped · take 3 started`, `⌫ ignored —
    too fast`, `⌫ nothing to correct`, and the same for the other keys."""
    if not isinstance(fb, dict):
        return None
    kind, action, ignored = fb.get("kind"), fb.get("action"), fb.get("ignored")
    if kind in ("learn", "forget", "screen"):
        return screen_feedback(fb)
    chord = "pause" if kind in ("pause", "resume", "pause-toggle") else kind
    key = labels.get(chord, chord or "?")
    if ignored == "debounce":
        text = "ignored — too fast"
    elif ignored == "nothing-to-correct":
        text = "nothing to correct"
    elif ignored:
        text = f"ignored ({ignored})"
    elif action == "moved-close":
        moved = fb.get("moved_s")
        text = f"close moved {moved:+.1f} s" if isinstance(moved, (int, float)) else "close moved here"
    elif action == "dropped-take":
        text = f"take {fb.get('dropped')} dropped · take {fb.get('take')} started"
    elif action == "open":
        text = f"take {fb.get('take')} started"
    elif action == "close":
        text = f"take {fb.get('take')} closed"
    elif action == "mark":
        text = f"mark {fb['mark']}" if fb.get("mark") else "mark"
    elif action == "pause":
        text = "pausing…"
    elif action == "resume":
        text = "resuming…"
    else:
        return None
    if not ignored and fb.get("at_boundary") is not None and action in ("open", "close", "moved-close",
                                                                          "dropped-take"):
        # a take boundary pressed while paused: it lands at the boundary, said where
        text += " · at the resume" if action in ("open", "dropped-take") else " · at the pause"
    return f"{key} {text}".strip()


def pill_lines(status: str, elapsed_s: float | None, state: dict | None, labels: dict, *, hints: bool = True,
               feedback: dict | None = None, takes: int = 0, take_open: bool = False) -> str:
    """The whole pill text, lines joined by newline: the state line, then the
    feedback while it is fresh (the caller passes it only then), else the hint
    when hints are on. One line with hints off and no fresh feedback. A pause
    or resume press's feedback lasts only while that transition is under way."""
    lines = [state_line(status, elapsed_s, state, takes, take_open)]
    if feedback and feedback.get("action") in ("pause", "resume") and \
            status != {"pause": "pausing", "resume": "resuming"}[feedback["action"]]:
        feedback = None             # it has taken effect: the first line says so, and the hint is due
    second = feedback_text(feedback, labels) if feedback else None
    if second is None and hints:
        second = hint_line(status, state, labels)
    if second:
        lines.append(second)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Tk overlays
# ---------------------------------------------------------------------------


class Overlay:
    """One click-through, capture-excluded, topmost window of one or two lines.

    The first line of the text is the main label (bold for the pill); any
    further line goes in a second, lighter label under it (C1c: the pill's
    hint or feedback line). Both left-aligned, on the same background. The
    window is placed from its requested size each time, so a two-line pill
    stays flush in its corner (opposite the fiducial patch).

    `excluded` is None until the window is first shown; False means the
    affinity did not take, and the overlay will not show itself again."""

    def __init__(self, master, font=("Segoe UI", 11, "bold"), alpha: float = 0.92, padx: int = 14, pady: int = 6,
                 require_exclusion: bool = True, sub_font=("Segoe UI", 10)):
        import tkinter as tk
        self.require_exclusion = require_exclusion
        self.tk = tk
        self.top = tk.Toplevel(master)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", alpha)
        self.top.configure(bg=COLORS["info"])
        self.label = tk.Label(self.top, text="", fg="white", bg=COLORS["info"], font=font, padx=padx, pady=pady,
                              justify="left", anchor="w")
        self.label.pack(fill="x")
        self.sub = tk.Label(self.top, text="", fg="white", bg=COLORS["info"], font=sub_font, padx=padx, pady=0,
                            justify="left", anchor="w")
        self.sub_shown = False
        self.excluded: bool | None = None
        self.visible = False

    def _secure(self) -> bool:
        """Apply click-through + capture exclusion once, on first show."""
        if self.excluded is not None:
            return self.excluded
        from . import winapi
        # Tk creates the top-level wrapper HWND only when the window is first
        # mapped. Map it fully transparent, secure it, then withdraw: nothing
        # un-excluded is ever drawn, so nothing can land in a recording.
        alpha = self.top.attributes("-alpha")
        self.top.attributes("-alpha", 0.0)
        self.top.deiconify()
        self.top.update()
        hwnd = winapi.toplevel_hwnd(self.top.winfo_id())
        winapi.make_click_through(hwnd)
        self.excluded = winapi.exclude_from_capture(hwnd)
        self.top.withdraw()
        self.top.attributes("-alpha", alpha)
        if not self.excluded:
            event(log, logging.ERROR, "overlay.not_excluded", hwnd=hwnd)
        return self.excluded

    def prepare(self) -> bool:
        """Secure the window up front (the agent does this at start, before any
        recording), so the first real show is instant. Returns `excluded`."""
        return self._secure()

    def show(self, text: str, color: str, position: str, margin: int, stack: int = 0) -> bool:
        """Show or update; False (and nothing shown) if capture exclusion failed
        and this overlay requires it (the pill and the in-recording toast do)."""
        if not self._secure() and self.require_exclusion:
            return False
        first, _, rest = text.partition("\n")
        if self.label.cget("text") != first or self.label.cget("bg") != color:
            self.label.configure(text=first, bg=color)
            self.top.configure(bg=color)
        if rest:
            if self.sub.cget("text") != rest or self.sub.cget("bg") != color:
                self.sub.configure(text=rest, bg=color)
            if not self.sub_shown:
                self.sub.pack(fill="x", pady=(0, 6))
                self.sub_shown = True
        elif self.sub_shown:
            self.sub.pack_forget()
            self.sub_shown = False
        self.top.update_idletasks()
        w, h = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
        x, y = overlay_position(position, self.top.winfo_screenwidth(), self.top.winfo_screenheight(),
                                w, h, margin, stack)
        self.top.geometry(f"+{x}+{y}")
        if not self.visible:
            self.top.deiconify()
            self.top.attributes("-topmost", True)
            self.visible = True
        return True

    def hide(self) -> None:
        if self.visible:
            self.top.withdraw()
            self.visible = False

    def destroy(self) -> None:
        try:
            self.top.destroy()
        except self.tk.TclError as exc:
            event(log, logging.WARNING, "overlay.destroy_failed", error=str(exc))
