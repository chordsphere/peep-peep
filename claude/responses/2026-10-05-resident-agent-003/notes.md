# notes — 2026-10-05-resident-agent-003 (peep-peep session B)

Everything in the brief's scope list is built, on A's modules, with tests:
the agent process, the three hotkeys, the REC pill, the hotkey-only stop
dialog, marks into the sidecar, `peep mark`, and `peep agent
install|uninstall|start|stop|restart|status|reload|run`. The suite has 250
tests (A's 134 plus 116) and passes on Python 3.12 and 3.13 with no
display, no Win32 and no ffmpeg.exe.

Beyond the suite, I ran the whole agent stack on a real Tk 8.6 under Xvfb
in my sandbox. That means the real `Agent`, `TkUi`, `StopDialog`, pill
overlay and flasher, with A's real `Recorder` driving `fake_ffmpeg.py`.
Hotkey presses came from a non-UI thread, keystrokes were injected into
the dialog, and the Win32 calls were stubbed. That run is described below.
It is not part of the suite, and it proves nothing about Win32 itself.
README smoke-test steps 21–40 are the real acceptance test.

## What the probe established (relied on throughout)

The manifest asked for a probe first (`expects_probe: yes`), and one
paste-back probe ran.

- **Capture exclusion works on this laptop.** Three squares were drawn
  and frames grabbed through ddagrab:
  - the control square, no affinity, was captured as rgb(0, 0, 255);
  - the excluded plain window was captured as rgb(12, 12, 12), i.e. the
    background behind it;
  - the excluded square built exactly like the pill was also captured as
    rgb(12, 12, 12). The recipe is alpha 0.9 with layered, transparent,
    toolwindow and noactivate styles.

  Read-back was `0x11` for both excluded squares. That is why the pill
  ships on by default. "Optional" is still the `[agent] pill` flag, and
  the agent turns the pill off on its own if the read-back ever fails.
- **Hotkeys:** Ctrl+Alt+R, M and X were all free (each registered and
  released). No `pythonw.exe` was running.
- **ForegroundLockTimeout is 2147483647, the maximum.** Windows will only
  let the agent take the foreground when it received the last input. See
  "Look closely at".
- **Startup folder:** FOLDERID_Startup resolves to
  `C:\Users\chord\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup`.
  It is not redirected and holds only `desktop.ini`. PowerShell 5.1's
  WScript.Shell `CreateShortcut` works (0.3 s; the probe never saved).
- **Interop job:** the interop-launched `python.exe` is **not** in a job
  object, so a `pythonw.exe` it spawns detached outlives it, the shim and
  the terminal. No breakaway flag is needed.
- **Versions:** Windows 11 build 26200; Python 3.12.10 with `pythonw.exe`
  beside it; Tk 8.6. The repo is reachable from Windows at
  `\\wsl.localhost\Ubuntu\home\chordsphere\peep-peep` in 0.02 s, and
  `WSL_DISTRO_NAME=Ubuntu`.
- **Repo state:** the repo is clean at `7a606cb` (A's sweep). Every
  tracked file's hash matches the request's `base_files`; the only
  untracked files are two telemetry JSONs. The installed copy is at
  `7a606cb` with 15 files. WSL has no pytest, so `validation.sh` uses
  unittest.
- **config.toml** has `[flash] duration_ms = 200` and
  `[audio] offset_ms = 500`. I did not touch the audio setting; see
  "For A.1".

## Threading (brief item 2: what I did)

As A's Proposal 1 described:

- **One Tk interpreter**, created on the agent's main thread after
  `set_dpi_aware()`, owns everything visible: the pill, the toast, the
  dialog and the flash window. The flash is A's `TkFlasher` subclassed as
  `ui.ToplevelFlasher`; only `prepare()` changes, to make a Toplevel of
  the agent's root, and `flash()` is inherited unchanged.
- **The recorder** is A's `Recorder.record()`, unchanged, on a worker
  thread per recording. Its `flasher_factory` returns a
  `MarshalledFlasher`, which runs `prepare`/`flash` on the Tk thread
  through `UiDispatcher.call()` and blocks until the flash has been shown.
  So A's timeline stamps are still taken while the window is on screen. A
  flash that cannot be marshalled is logged and recorded as not shown;
  it never raises into the recording.
- **Stopping:** `stop_event` is a `threading.Event`. The hotkeys set it,
  with a `reason` attribute, so the sidecar says `stop_reason: hotkey` /
  `hotkey-discard` / `agent-exit`.
- **The hotkey listener** has its own Win32 message-loop thread.
  `RegisterHotKey(NULL, ...)` posts to the registering thread, and Tk's
  loop would never hand those messages to Python. The listener reads the
  foreground window the instant `WM_HOTKEY` arrives (A's Proposal 2) and
  posts the press to the Tk thread.
- **The dispatcher is deliberately non-reentrant.** A's flash calls
  `root.update()` for its 200 ms, which fires `after` callbacks. Without
  the guard, a queued hotkey or a second flash would run in the middle of
  the first. All agent logic, including the 250 ms tick, goes through the
  dispatcher.

In the Xvfb run, all seven flashes executed on `MainThread`, none on the
worker. The flash window was prepared once and reused.

## Decisions for you to ratify

Mechanism calls made under AGENT.md §4. None of them reopens A's
thirteen.

1. **The Startup entry is a `.lnk`, not a `.cmd`.** It is written by
   PowerShell's WScript.Shell object, and its target is `pythonw.exe
   "<app>\peepw.py" agent run`, per A's #7. A `.cmd` flashes a console at
   every login. The shortcut needs no admin and no Python dependency.
   Uninstall deletes the file.
2. **Marks use one request file per press.** The file is
   `state\mark-<ns>-<pid>.json`, not a single `mark` file, so two presses
   inside one 100 ms poll are two marks. `peep mark [--label]` writes the
   same file.
   - In the sidecar, each mark is
     `{t, label, since_ffmpeg_start_s, at, source, consumed_s, flash}`.
   - `t` is A's reserved key. It means seconds since the ffmpeg launch
     **at the keypress**: the requester's wall clock minus
     `ffmpeg_started_at`, so the poll delay doesn't shift it.
   - `flash` is the cyan flash's own stamp, on the same clock as
     `flash.start` and `flash.stop`. C finds that flash in the video.
   - The sidecar is rewritten as each mark lands, so a crash later keeps
     the marks.
3. **Terminal recordings never get the dialog, even when stopped by the
   hotkey.** The agent still shows the pill for any live recording. Its
   record hotkey requests a stop like `peep stop`, mark works, and discard
   requests a stop and then deletes. I read "the dialog appears only for
   hotkey stops" together with "terminal-driven recordings keep A's
   behaviour" as "the dialog belongs to recordings the agent started." If
   you'd rather a hotkey stop of a terminal recording also opened the
   dialog, that's a small change in `Agent._on_record`.
4. **Pressing the record hotkey while the dialog is open keeps the
   automatic name** (Esc semantics) and starts the next take. Pressing
   discard while the dialog is open discards that take.
5. **Delete is the dialog's discard key in both fields.** Backspace still
   edits text. The hint line says so. After the first Delete, Delete or Y
   confirms; any other key cancels and is swallowed, so it doesn't land in
   the name.
6. **Up/Down cycle the collection from either field.** This came out of
   the Xvfb run: with a combobox-only binding, Tk delivers keys to the
   focused name field, so reaching another collection took Tab+Down. The
   brief asked for one keystroke. The list is the take's current
   collection, then collections that hold recordings now (most recent
   activity first), then the default. A collection emptied by moves or
   discards drops out. Typing a new name creates it.
7. **Last-used collection** is persisted in
   `%LOCALAPPDATA%\peep\agent-prefs.json`. It is set by the dialog's Save
   (the chosen collection) or Esc (the collection it was recorded in).
   Terminal recordings don't change it. An invalid stored name is ignored
   with a warning.
8. **Discard is a catalog event.** It appends `discarded` (with `reason`
   and the removed file names), and the fold drops it. A's fold ignored
   unknown events, so without this change a discarded take would have
   stayed in `peep ls`. Files are deleted before the event is written. A
   file locked by a player raises, and the catalog never claims a deletion
   that didn't happen. Discard has no "undo" (no recycle bin). Flagging
   that in case you'd prefer the Recycle Bin, which needs `SHFileOperation`
   via ctypes.
9. **`active.json` gains fields:** `origin`, `status` (starting →
   recording → stopping), `recording_since` and `ffmpeg_started_at`
   (A's Proposal 3). The pill counts from `recording_since`, which is just
   after the start flash, so `00:00` lines up with the magenta flash.
10. **The agent's own control files** are `state\agent.json` (pid, state,
    hotkey states, pill, config + loaded-at, install state, code id) and
    `state\agent-command` (`stop`/`reload`). They use the same pattern as
    A's #6. A named mutex (`Local\peep-agent`) is the race-free
    single-instance guard. The pidfile is what a second agent names in its
    refusal, and what `peep agent status` reads.
11. **Hotkey registration retries for 60 s** (`[agent] hotkey_retry_s`),
    for the login race. The first failure and the final one are shown on
    screen and logged with the chord named; retries in between stay quiet.
    A chord on a bare key, or Shift+key, is refused at config load because
    it would swallow typing everywhere. Two actions on one chord are
    refused too.
12. **Config reload** happens on `peep agent reload` or within ~2 s of
    saving `config.toml` (an mtime check). An invalid file keeps the old
    config, shows the error and puts it in `agent.json`. Hotkeys are
    re-registered only when they changed. One limit: `log_level` changes
    need an agent restart.
13. **Overlays are secured before anything is drawn.** Tk creates the
    top-level HWND only on first map, so the pill and toast are mapped
    fully transparent, given the affinity and click-through styles, then
    withdrawn. This happens at agent start, before any recording. Toasts
    are excluded from capture too, since they can appear mid-recording. If
    the toast can't be secured, a plain fallback is used only while
    nothing is recording.
14. **Flash default is 200 ms**, as briefed. I added a `mark_color` key
    (cyan) and validation that the three colours differ.
15. **`setup_logging` takes a `file_name`**, so `agent run` writes
    `logs\agent.log` beside `peep.log` with the same format and rotation.
    The console handler is skipped when there is no stderr (pythonw).
16. **Staleness (your friction point).** Every `peep` command from WSL
    already syncs the installed copy first. So **`peep agent restart` is
    the one command that syncs and loads new code**, and that's what the
    README tells you to use.
    - `peep agent start` and `status` say when the running agent is older
      than the copy on disk. The agent records a fingerprint of
      `INSTALLED.json` at start.
    - The agent's own check compares the copy with the repo over
      `\\wsl.localhost`, at start and only if `wsl --list --running`
      already names the distro. Touching `\\wsl.localhost` would boot WSL
      at login.
    - It never syncs. To find the repo, the shim now writes `wsl_distro`
      into `INSTALLED.json`.
    - Until the next sync rewrites that marker, the check reports
      "unknown". The first `peep` command after applying this response
      syncs, since files changed.

## Look closely at

- **Dialog focus (★).** With ForegroundLockTimeout at its maximum,
  `winapi.force_foreground` tries `SetForegroundWindow` first. Then it
  tries AttachThreadInput to the current foreground thread, then an
  Alt-tap. It logs which step worked (`foreground.force step=...`). The
  hotkey press usually qualifies the process, but the dialog appears after
  the stop flash, settle and remux (about 1–2 s). Typing elsewhere in that
  gap could cost the rights. Smoke steps 24 and 38.
- **`winapi.py` section 5.** These are hand-declared ctypes signatures for
  `GetAncestor`, `Set/GetWindowLongW`, `Set/GetWindowDisplayAffinity`,
  `CreateMutexW` and `SHGetKnownFolderPath`. They follow the probe's
  working code where the probe exercised them (affinity, styles, known
  folder). Mutex creation and the foreground fallbacks were not probed.
- **`recorder.py`:** the diff is additive, but it sits inside A's record
  flow.
  - The mark sweep runs once per 100 ms poll, through `on_tick`.
  - One extra sweep runs right after a stop request, so a mark pressed at
    the last instant counts. Then the stop flash.
  - `_wait_for_stop` returns `stop_event.reason` when set, else
    `"terminal"` as before.
- **The full-screen pill/flash interplay.** The flash is a topmost
  full-screen Toplevel, and the pill is also topmost. During the 200 ms
  flash you may see either on top. Both orders are fine for the recording,
  since the pill isn't captured either way.

## The sandbox Tk run (what it did and did not show)

Under Xvfb with Tk 8.6, I ran three takes through the real stack.

1. **Take 1:** record hotkey → the pill showed `● 00:00` → a mark → stop
   → dialog. The prefill was `chrome-pull-requests-peep-peep`, all
   selected, in collection `inbox`, and the status line read
   `00:02 · 1 KB · …`. Delete armed the confirm and `n` disarmed it (and
   was swallowed). A new name and collection `bale` + Enter → the take was
   renamed to `bale/2026-10-05-bale-pack-demo.mp4`. Its sidecar has one
   hotkey mark with a cyan flash and `stop_reason: hotkey`.
2. **Take 2:** the dialog prefilled `bale` (the last used). Down from the
   name field gave `inbox` and Up gave `bale` back. Delete, Delete
   discarded the take.
3. **Take 3:** the discard hotkey mid-recording deleted the take, with no
   dialog.

After that, `peep agent stop` (through the command file) quit cleanly.
`agent.log` had no warnings or errors. Win32 was stubbed throughout, so
this says nothing about RegisterHotKey, capture exclusion or focus. Those
are the ★ smoke steps.

## For A.1 (audio; not touched here)

`ffmpeg_cmd.py` and `AudioConfig` are byte-identical to A's;
`validation.sh` asserts the module's hash. The one audio-relevant fact
the probe saw: your `config.toml` has `[audio] offset_ms = 500`. Per A's
template, positive delays the mic, which is the right sign for "voice
leads by ~500 ms". If the lead still shows with that setting, A.1 should
first confirm that `-itsoffset` lands on the dshow input in the logged
argv. `peep.log` has every argv verbatim.

## Validation notes

- **Two checks pass on the unmodified tree as well:** the byte-identity
  of A's untouched modules, and `bin/peep`'s exec bit. Both pin outcomes
  the brief fixed, so they pass both ways by nature. The other five
  session assertions fail on the unmodified tree and pass with the
  change; I ran both.
- **The `event()` keyword sweep is there for a reason.** The tests caught
  `event(..., level=...)` in the agent's toast path, and the sweep then
  found `name=` in the new Windows-only mutex code. Both are fixed. No
  WSL test can execute that mutex path, so the sweep guards it.
- **pyflakes is clean on every file this session created or changed**,
  apart from three pre-existing nits I left alone in A's code. pyflakes
  was run in my sandbox, not shipped. The nits: an unused `FlashRecord`
  import in `recorder.py`, an unused `os` in `tests/__init__.py`, and an
  f-string without placeholders in `tests/fake_ffmpeg.py`.

No paths outside the write forecast (`.`). Nothing in `out_of_scope` was
touched: there is no audio change, no post-processing, no tray icon and
no new dependency. No light question blocks were used.

## Proposals

**C: read marks from `t`, cut on the cyan flash.**

- **Why:** `t` is the keypress on the same agent clock as the flashes. The
  cyan flash shows up 0–100 ms (poll) plus the flash's own latency later.
  Searching ±1 s around `marks[i].flash.since_ffmpeg_start_s` for a
  full-frame cyan median gives the exact frame, the same way A proposed
  for the start/stop flashes. The probe measured cyan nowhere; expect
  about (0, 254, 254) by analogy with A's green.
- **Scope hints:** the sidecar's `marks`; `label` is free text from
  `peep mark --label`.

**C: hang the dialog's toggles off `postprocess_row` and `model.options`.**

- **Why:** the row exists, empty, at grid row 2 of `StopDialog`.
  `on_result` already receives `options` (always `{}` today), and the
  agent's `dialog_result` is the one place to act on them after the
  rename.
- **Scope hints:** `dialog.py` (row + Checkbuttons), `agent.py`
  (`dialog_result`). Keep keys working without the mouse, e.g. Alt+letter
  per toggle.

**A per-collection hotkey (later).**

- **Why:** you'll probably end up with two or three collections you record
  into repeatedly. A fourth chord that starts in a named collection would
  skip the dialog's collection step entirely.
- **Scope hints:** `[agent]` would grow a small `collection_hotkeys`
  table, plus a new action in `hotkeys.ACTIONS`. Only worth it after a
  week of real use.

**Discard to the Recycle Bin instead of deleting.**

- **Why:** Ctrl+Alt+X is one chord with no confirm, by design. A
  mis-press costs a take.
- **Scope hints:** `SHFileOperationW` with `FOF_ALLOWUNDO` via ctypes, in
  `winapi`, called from `catalog.discard`. It stays stdlib-only.

**`peep doctor` should check the agent too.**

- **Why:** the shim's doctor is the first thing you run when something is
  off. Adding "agent running? hotkeys registered? startup entry present?
  installed copy current?" there would mirror `peep agent status`.
- **Scope hints:** `doctor.py` reading `AgentControl` and
  `lifecycle.install_status`. I left `doctor.py` byte-identical in this
  session.
