"""The stop dialog: name the take the moment it is saved, without a terminal.

Shown after a hotkey stop (never for `peep rec` + q or `peep stop`):

  Name         prefilled with the slug suggested from the foreground window
               captured at *start*, all selected, so typing replaces it
  Collection   prefilled with the last-used collection; Up/Down (from either
               field, so it is one keystroke away while typing the name) cycle
               the recent ones from the catalog; typing a new name creates it
  [row reserved for session C's post-processing toggles: `postprocess_row`]
  status       duration · size · path
  keys         Enter save · Esc keep the automatic name · Delete discard

Delete asks for one confirming keystroke (Delete again, or Y), because it
destroys the take; any other key cancels the confirm. Backspace still
edits text: Delete is the dialog's discard key, not a text-editing key,
which is the one behaviour a user might not expect from a text field (it
is printed on the dialog's hint line).

Everything that decides is a pure function (`decide`, `cycle`,
`validate_choice`, `status_line`, `collection_choices`) and is tested on
WSL; `StopDialog` is the thin Tk layer over them.

Sections:
  1. Pure decision logic        (~line 45)
  2. Tk dialog                  (~line 140)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from . import naming
from .logsetup import event

log = logging.getLogger("peep.dialog")

# ---------------------------------------------------------------------------
# 1. Pure decision logic
# ---------------------------------------------------------------------------

SAVE, KEEP, ARM, DISCARD, DISARM, NONE = "save", "keep", "arm", "discard", "disarm", "none"
CONFIRM_KEYS = ("Delete", "y", "Y")
MODIFIER_KEYSYMS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Caps_Lock",
                    "Win_L", "Win_R", "Super_L", "Super_R", "Num_Lock", "ISO_Level3_Shift"}
HINT = "Enter save  ·  Esc keep automatic name  ·  Delete discard  ·  ↑/↓ recent collections"
CONFIRM_PROMPT = "Discard this take for good?  Delete or Y = discard  ·  any other key = keep editing"


def decide(keysym: str, armed: bool) -> tuple[str, bool]:
    """One keystroke -> (action, armed afterwards).

    Not armed: Enter/KP_Enter save, Escape keeps, Delete arms the discard
    confirm, anything else is ordinary typing (NONE).
    Armed: Delete or Y discards, a bare modifier (Shift for a capital Y)
    leaves it armed, anything else disarms (and is swallowed, so a stray
    key after Delete does not also edit the name)."""
    if armed:
        if keysym in CONFIRM_KEYS:
            return DISCARD, False
        if keysym in MODIFIER_KEYSYMS:
            return NONE, True
        return DISARM, False
    if keysym in ("Return", "KP_Enter"):
        return SAVE, False
    if keysym == "Escape":
        return KEEP, False
    if keysym == "Delete":
        return ARM, True
    return NONE, False


def cycle(current: str, choices: list[str], direction: int) -> str:
    """Up (-1) / Down (+1) through `choices`, wrapping. From a typed value not
    in the list, Down goes to the first choice and Up to the last."""
    if not choices:
        return current
    norm = current.strip().lower()
    if norm in choices:
        return choices[(choices.index(norm) + direction) % len(choices)]
    return choices[0] if direction > 0 else choices[-1]


def collection_choices(current: str, recent: list[str], default: str, limit: int = 12) -> list[str]:
    """The picker's list: the prefilled collection first, then recent ones
    (most recent first), then the configured default; unique, valid only."""
    out: list[str] = []
    for name in [current, *recent, default]:
        try:
            norm = naming.validate_collection(name)
        except (naming.NamingError, AttributeError):
            continue
        if norm not in out:
            out.append(norm)
    return out[:limit]


@dataclass(frozen=True)
class Choice:
    name: str          # what the user typed (kept as the title)
    slug: str
    collection: str


def validate_choice(name: str, collection: str) -> tuple[Choice | None, str | None]:
    """(Choice, None) or (None, message for the dialog's status line)."""
    try:
        slug = naming.slug_from_user(name)
    except naming.NamingError as exc:
        return None, f"name: {exc}"
    try:
        coll = naming.validate_collection(collection)
    except naming.NamingError as exc:
        return None, str(exc)
    return Choice(name.strip(), slug, coll), None


def format_size(n: int | None) -> str:
    if n is None:
        return "? MB"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def status_line(duration_s: float | None, size_bytes: int | None, path: str) -> str:
    from .pill import format_elapsed
    dur = format_elapsed(duration_s) if isinstance(duration_s, (int, float)) else "?"
    return f"{dur}  ·  {format_size(size_bytes)}  ·  {path}"


@dataclass
class DialogModel:
    """What the dialog shows; built by the agent from the finished recording."""
    name: str                      # prefill (the automatic slug)
    collection: str                # prefill (the collection it was recorded into)
    choices: list[str]
    status: str
    title: str = "peep — name this recording"
    options: dict = field(default_factory=dict)    # session C's toggles land here


# ---------------------------------------------------------------------------
# 2. Tk dialog
# ---------------------------------------------------------------------------


class StopDialog:
    """`on_result(action, name, collection, options)` is called for SAVE, KEEP
    and DISCARD. It returns None when the action is done (the dialog closes)
    or a message to show (the dialog stays open, e.g. a file locked by a
    player). The agent routes it through its dispatcher."""

    def __init__(self, master, model: DialogModel, on_result: Callable[[str, str, str, dict], str | None],
                 focus: Callable[[int], str] | None = None):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.model, self.on_result = model, on_result
        self.armed = False
        self.closed = False
        top = self.top = tk.Toplevel(master)
        top.title(model.title)
        top.attributes("-topmost", True)
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", lambda: self._act(KEEP))

        frame = ttk.Frame(top, padding=14)
        frame.grid(sticky="nsew")
        ttk.Label(frame, text="Name").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        self.name_var = tk.StringVar(value=model.name)
        self.name_entry = ttk.Entry(frame, textvariable=self.name_var, width=48)
        self.name_entry.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="Collection").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        self.coll_var = tk.StringVar(value=model.collection)
        self.coll_box = ttk.Combobox(frame, textvariable=self.coll_var, values=model.choices, width=46)
        self.coll_box.grid(row=1, column=1, sticky="ew", pady=4)
        # Session C: post-processing toggles (trim to flashes, GIF/WebM, crop) go in this
        # row as Checkbuttons whose values are written into self.model.options, which
        # on_result receives. B leaves it empty: the dialog never offers what does not exist.
        self.postprocess_row = ttk.Frame(frame)
        self.postprocess_row.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.status = ttk.Label(frame, text=model.status, foreground="#455A64")
        self.status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.message = ttk.Label(frame, text=HINT, foreground="#607D8B")
        self.message.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        for widget in (self.name_entry, self.coll_box):
            widget.bind("<KeyPress>", self._on_key)
        for widget in (self.name_entry, self.coll_box):     # one keystroke away from either field
            widget.bind("<Up>", lambda e: self._cycle(-1))
            widget.bind("<Down>", lambda e: self._cycle(+1))

        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        top.geometry(f"+{(top.winfo_screenwidth() - w) // 2}+{(top.winfo_screenheight() - h) // 3}")
        top.deiconify()
        top.lift()
        self.name_entry.focus_force()
        self.name_entry.select_range(0, "end")
        self.name_entry.icursor("end")
        if focus is not None:
            from . import winapi
            top.update()
            focus(winapi.toplevel_hwnd(top.winfo_id()))
            self.name_entry.focus_force()

    # -- keys -------------------------------------------------------------------

    def _on_key(self, ev):
        action, self.armed = decide(ev.keysym, self.armed)
        if action == NONE:
            return "break" if self.armed else None     # armed + modifier: swallow; else type normally
        if action == ARM:
            self._say(CONFIRM_PROMPT, "#B71C1C")
        elif action == DISARM:
            self._say(HINT, "#607D8B")
        else:
            self._act(action)
        return "break"

    def _cycle(self, direction: int):
        if self.armed:
            self.armed = False
            self._say(HINT, "#607D8B")
        self.coll_var.set(cycle(self.coll_var.get(), list(self.model.choices), direction))
        self.coll_box.icursor("end")
        return "break"

    def _say(self, text: str, color: str) -> None:
        self.message.configure(text=text, foreground=color)

    def _act(self, action: str) -> None:
        if self.closed:
            return
        if action == SAVE:
            _, error = validate_choice(self.name_var.get(), self.coll_var.get())
            if error:
                self._say(error, "#B71C1C")
                return
        event(log, logging.INFO, "dialog.action", action=action)
        problem = self.on_result(action, self.name_var.get(), self.coll_var.get(), dict(self.model.options))
        if problem:
            self._say(problem, "#B71C1C")
            return
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.top.destroy()
            except self.tk.TclError as exc:
                event(log, logging.WARNING, "dialog.destroy_failed", error=str(exc))

    def resolve(self, action: str) -> None:
        """Close from outside (the agent: a new recording starts, or the agent exits)."""
        self._act(action)
