# peep-peep

A screen recorder built for one machine: this Windows 11 laptop, driven from
WSL. The goal is the least possible friction between "I want to record this"
and a named file in a predictable place. `start.md` has the original intent.

```
Ctrl+Alt+R               # in any app: magenta flash, REC pill, recording starts
                         # ...do the thing... (Ctrl+Alt+M drops a mark)
                         # (Ctrl+Alt+T opens/closes a take, Ctrl+Alt+Backspace corrects it,
                         #  Ctrl+Alt+P pauses: see "Editing while you record")
Ctrl+Alt+R               # green flash; a dialog asks for the name
bale pack demo ⏎         # typed over the suggested name; Enter saves. Done.
                         # (in the background: <name>.cut.mp4, the edited cut, appears beside it)
```

or, from a terminal:

```
peep rec                 # magenta flash, recording starts
                         # ...do the thing...
q                        # green flash, file finalized
peep rename last "bale pack demo"
peep open last
```

## The plan (three sessions)

The pixels live on Windows and WSL cannot see the Windows desktop, so the
recorder is a **Windows-side capture engine driven from WSL through
interop**. **Session A** is that engine plus the naming and
catalog storage core and a terminal-driven `peep` shim, so the engine is
proven before any UX sits on it. **Session B** (landed) adds a resident Windows
agent: launched at login, global hotkeys, a REC pill hidden from capture,
marks, and a stop dialog that prefills the name. **Session C** is post-processing:
finding the clapper flashes, trimming to them, mark-based cuts, GIF/WebM
presets and per-collection crop. B and C import A's modules unchanged.
**Session A.1** (landed between B and C) is the audio pipeline: computer
audio by default, captured by our own WASAPI code, a microphone option that
no longer leads the picture, and `peep config set` so the config never needs
a text editor. See [Audio](#audio). **Session C1a** (landed) is the capture
side of editing: hard pause as segments of one recording, takes and retakes
recorded as events, corner-patch fiducials, and a naming audit. **Session
C1b** (landed) renders from that record: the cut, `<stem>.cut.mp4`, beside the
original, which is never modified. **Session C1c** replaces C1a's retake with
the ratified correction chart (a two-level undo of the current take) and
makes the REC pill say what each key does next. See [Editing while you record](#editing-while-you-record)
and [The cut](#the-cut-session-c1b).

## Install

On Windows (once):

```
winget install Gyan.FFmpeg
winget install Python.Python.3.12
```

Then restart WSL (`wsl --shutdown` from Windows, reopen the terminal) so the
new Windows PATH reaches interop.

From WSL, in the repo:

```
./bin/peep install       # copies win/ to %LOCALAPPDATA%\peep\app, links ~/.local/bin/peep
peep doctor              # checks everything; prints the exact winget line for anything missing
peep doctor --capture    # also runs a 2-second real capture into ffmpeg's null output
peep agent install       # start the hotkey agent now and at every login (see "The resident agent")
```

### Why the code is copied to `%LOCALAPPDATA%\peep\app`

The repo lives on the WSL ext4 disk. Windows Python *can* run it from
`\\wsl.localhost\...` (the probe confirmed it), but that path only exists
while WSL is running, every import crosses the 9P bridge, and session B
must start the agent at Windows login, before WSL is up. So the Windows
side always runs from a Windows-local copy, and the shim keeps that copy
current: before each command it compares file hashes against
`app\INSTALLED.json` and re-copies anything that changed (one stderr line
when it does; `PEEP_NO_AUTOSYNC=1` turns that off). The repo stays the
only source of truth, nothing is pip-installed, and B's startup entry
points at `pythonw.exe %LOCALAPPDATA%\peep\app\peepw.py`.

## Commands

| Command | What it does |
|---|---|
| `peep rec [slug] [-c NAME]` | Record the screen and the computer's audio. Stop with **q**, **Enter** or **Ctrl-C** in that terminal, or `peep stop` from another. With no slug the name comes from the foreground window (from a terminal at `~/peep-peep` that is `terminal-peep-peep`). Options: `--audio system\|mic\|both\|none` (this recording only; the default is `audio.sources`), `--no-mic` (takes the mic out: both → system, mic → none), `--no-flash`, `--scale 1920x1200`, `--encoder qsv\|qsv-download\|x264`, `--fps N`. After `✓ saved` it renders the cut in that terminal, printing progress (`render.auto`); `--no-render` skips that. |
| `peep stop` | Ask the running recording to stop and wait until its file is finalized; prints the path. `--no-wait` returns immediately. |
| `peep mark [--label TEXT]` | Drop a mark in the running recording (a cyan corner patch + an entry in the sidecar's `marks`). Same as **Ctrl+Alt+M**. |
| `peep pause` / `peep resume` | Hard pause: capture stops and the segment is saved; resume starts the next segment of the same recording. Each waits until it has happened (`--no-wait` doesn't). Same as **Ctrl+Alt+P**. |
| `peep take` | Open a take, or close the open one. Same as **Ctrl+Alt+T**. |
| `peep correct` | Correct the current take ([the chart](#the-correction-key-session-c1c)): a closed take's close moves here; otherwise the take is dropped and a fresh one opens here. Prints what it did (`↺ correct: close moved +4.2 s; …`). `peep retake` is the same command, by its old name. Same as **Ctrl+Alt+Backspace**. |
| `peep agent install\|uninstall` | Add (and start) or remove the hotkey agent's Startup-folder entry. |
| `peep agent start\|stop\|restart\|status\|reload` | Control the running agent; see below. |
| `peep render [last\|STEM] [--force] [--dry-run]` | Render the cut, `<stem>.cut.mp4`, from the recording's event record (session C1b; see [The cut](#the-cut-session-c1b)). An up-to-date cut is left alone unless `--force`. `--dry-run` finds every flash and patch, places every seam, and prints the plan and the ffmpeg command without writing anything. A failed render says why, leaves the original untouched, and running it again retries. |
| `peep ls [-c NAME] [--all] [--json]` | List recordings from the catalog (newest last). `--all` includes failed captures. Each one shows what editing it has: `[2 seg · 3 takes · cut]` (or `cut (stale)`, `render failed`, `rendering`). |
| `peep open [last\|STEM] [--raw] [--folder]` | Open with the Windows default player, or show it in Explorer. It plays the cut when there is a current one; otherwise the original, and a recording of several segments that has no cut yet plays as a playlist of every segment in order. `--raw` plays the original (segment 1). |
| `peep rename last NAME [-c NAME]` | Rename (the date prefix stays), optionally moving it to another collection. The sidecar and every segment move with it. A name that is taken gets a counter, and the command says so. |
| `peep doctor [--capture]` | Check Python, ffmpeg, ddagrab/dshow, encoders, the audio devices (with a 1.5 s loopback capture while a short test tone plays), the storage root and tkinter. It never installs anything. |
| `peep config [--init]` | Show the effective config, or write a commented template. |
| `peep config get [KEY]` | One key's value (`peep config get audio.sources` → `"system"`), or every key with whether it comes from the file or the default. |
| `peep config set KEY VALUE` | Change one key, e.g. `peep config set audio.sources both`. The file's comments stay; a commented-out template line is uncommented in place; the value is checked by the same loader the recorder uses, and a bad one leaves the file untouched. The agent picks the change up within ~2 s. |
| `peep config unset KEY` | Back to the built-in default (the template's commented line comes back). |
| `peep paths` | Where config, logs, state and recordings live. |

Every command except `install` runs on the Windows side, and the same
commands work directly from a Windows console:
`python %LOCALAPPDATA%\peep\app\peepw.py ls`.

## The resident agent (hotkeys, REC pill, naming dialog)

`peep agent install` puts `peep agent.lnk` in your Startup folder
(`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`), pointing at
`pythonw.exe %LOCALAPPDATA%\peep\app\peepw.py agent run`, and starts the
agent. From then on it starts at every login. It has no console window and
no tray icon; the REC pill and short on-screen messages are its only UI.

| Hotkey | Does |
|---|---|
| **Ctrl+Alt+R** | Not recording: start, named after the window you are in at the keypress, in the last-used collection. Recording: stop, then the naming dialog; once it is answered (Enter or Esc), the cut renders in the background. |
| **Ctrl+Alt+M** | Drop a mark: a cyan corner patch (captured, for session C) and an entry in the sidecar's `marks`. |
| **Ctrl+Alt+X** | Stop and delete the take: file and sidecar are removed, and the catalog records it as `discarded`. |
| **Ctrl+Alt+P** | Hard pause / resume (session C1a). |
| **Ctrl+Alt+T** | Open a take / close it (session C1a). |
| **Ctrl+Alt+Backspace** | Correct the current take: move its close here, or drop it and restart it ([the chart](#the-correction-key-session-c1c), session C1c). |

All six are set in `[agent]` in `config.toml`. If another app already
owns a chord, the agent says so on screen and in `peep agent status`,
naming the chord. It retries for a minute, because at login the shell may
still be settling, and then tells you which config key to change.

**The REC pill** (`● 00:12`, top-right by default; two lines since session
C1c, [below](#the-pill-session-c1c)) is excluded from screen
capture with `WDA_EXCLUDEFROMCAPTURE`. You see it, the recording does not.
The session-B probe checked this on this laptop: ddagrab captured an
ordinary window but not the excluded ones. If Windows ever refuses the
exclusion, the agent turns the pill off instead of letting it into a
recording, and `peep agent status` says why. `pill = false` hides it
anyway, and `pill_position` moves it.

**The naming dialog** appears after a hotkey stop:

| Key | Does |
|---|---|
| typing | Replaces the suggested name (it starts selected). |
| **↑ / ↓** | Cycle the collection through recent ones (works from either field). Typing a new collection name creates it. |
| **Enter** | Save: rename and move (`peep rename`'s path; the date prefix stays). |
| **Esc** | Keep the automatic name in the current collection. |
| **Delete**, then **Delete** or **Y** | Discard the take. Any other key after the first Delete cancels. In this dialog Delete is the discard key, not a text-editing key; Backspace still edits. |

The name is prefilled with the name the file actually has, counter
included (`…-2` when the automatic name was taken). A green line under it
shows, as you type, what Enter will save as: `saves as
movies/2026-10-06-black-swan-2.mp4 (2026-10-06-black-swan is taken, so a
counter is added)`. The status line shows duration, then (when the
recording has takes or was paused) `3 takes · 02:14 kept of 05:02 · 2
segments`, then size and path. If the rename fails (say the file is open in a
player), the dialog stays open with the reason. `dialog = false` skips the
dialog and keeps automatic names.

**Terminal recordings keep their old behaviour.** `peep rec` + q and
`peep stop` never show the dialog. The agent still sees them: the pill
shows, Ctrl+Alt+M marks them, Ctrl+Alt+R stops them (like `peep stop`)
and Ctrl+Alt+X stops then deletes them.

| Command | |
|---|---|
| `peep agent status` | Running or not, each hotkey's state, the pill, which config is loaded, and whether the installed copy matches the repo. Exits 1 when the agent is not running. `--json` for scripts. |
| `peep agent start` / `stop` / `restart` | Stop waits for a recording in progress to be saved first (no dialog). |
| `peep agent reload` | Re-read `config.toml` now. The agent also notices saves on its own within ~2 s. An invalid file is reported, and the old config stays in use. |
| `peep agent install [--no-start]` / `uninstall` | The Startup entry. Uninstall also stops the agent. |
| `peep agent run` | The agent in the foreground (what the Startup entry runs; handy for debugging from a Windows console). |

**After you change the code:** the agent runs the Windows-local copy, and
only the WSL shim updates that copy. Every `peep` command syncs it first,
so `peep agent restart` from WSL is the one step that syncs *and* loads new
code. When WSL is already running, the agent checks at start whether its
copy matches the repo, and `peep agent status` shows the result. It never
starts WSL to find out.

## Editing while you record

Session C1a records *what to keep*; session C1b cuts it (the cut,
`<stem>.cut.mp4`). Nothing you press while recording can lose footage: the
session is always captured whole, the original is never modified, and the
cuts are decided at render from the events below.

| | Chord | WSL | What it means |
|---|---|---|---|
| **Session** | Ctrl+Alt+R | `peep rec` / `peep stop` | On and off, as before. |
| **Hard pause** | Ctrl+Alt+P (toggles) | `peep pause`, `peep resume` | Capture stops: the segment's file is finished (stop flash, `q`, remux). Nothing is recorded while paused. Resume starts a new segment of the same recording (start flash, fresh ffmpeg and audio children, their own anchor). The pill shows **❚❚ PAUSED** in orange, with the time captured so far standing still. |
| **Take** | Ctrl+Alt+T (toggles) | `peep take` | The first press opens a take (a **blue** corner patch), the next closes it (**red**). Only material inside takes survives the render. With no take at all, the whole recording is kept, so a quick recording behaves as before. A take is also the soft pause: capture continues and the gap is cut at render. |
| **Correction** | Ctrl+Alt+Backspace | `peep correct` (old name: `peep retake`) | A two-level undo of the current take (**yellow**): the first correction after a close moves the close here; otherwise the take is dropped and a fresh one opens here. [The chart](#the-correction-key-session-c1c) is the spec. |
| **Mark** | Ctrl+Alt+M | `peep mark` | A chapter or attention point (**cyan**), never a cut. |
| **Discard** | Ctrl+Alt+X | | The whole recording, every segment. |

- **Takes carry across a pause.** A take open when you pause is still open
  when you resume; the segment boundary is a cut with nothing to remove.
  Presses while paused count, at the boundary: a take opened while paused
  starts with the next segment, one closed while paused ends with the last.
- **Debounce.** A second press of the same chord within `debounce_ms` (1 s)
  is ignored: on the status line (`· take ignored (debounce)`), in the log,
  in the sidecar, and on the pill (`T ignored — too fast`). A correction is
  measured from the previous accepted press of either take key, Take or
  Correction (the chart's last row), so a correction a moment after closing
  a take is ignored too. The WSL commands are never debounced.
- **The pill** shows the take state and what each key does now: see
  [The pill](#the-pill-session-c1c).
- **Corner patches.** Take, correction and mark show a solid 200×200 px square
  for 200 ms in the corner opposite the pill (bottom-left by default). It is
  captured on purpose, like the flashes, so C1b can find each event in the
  video; it avoids a full-screen strobe every few sentences. Session and
  segment start/stop keep the full-screen magenta and green flashes. All
  fiducial colours are in `[flash]` and must differ clearly from each other;
  `mark_style = "full"` brings back B's full-screen cyan for marks.
- **Chords** are in `[agent]` (`pause_hotkey`, `take_hotkey`,
  `correct_hotkey`, `debounce_ms`; the old name `retake_hotkey` still works, as
  does `[flash] retake_color` for `correct_color`). Besides letters and F1–F24, the keypad
  works (`Ctrl+Alt+Num1`, `NumAdd`…, with NumLock on), and **F13–F24 may be
  bound alone**, since no keyboard types them: a macro pad, or mouse software
  that maps a side button to F13, gives you a one-button take.

### The correction key (session C1c)

**The chart is the spec** (ratified 2026-10-07). It replaced C1a's retake
rule; everything else in C1a's event model stands. In words: **each take
keeps a two-level undo stack of its original presses — open, then close. A
correction pops the most recent one and re-issues the same kind of boundary
at the press.** Popping a close moves the close to the press; popping an open
drops the take and opens a fresh take at the press.

| You're in… | You press… | Result | Undo stack after |
|---|---|---|---|
| No take yet (session just started, or everything since the last take is already cut) | **Take** | Take 1 opens here | `[open]` |
| | **Correction** | Ignored — nothing to correct (toast says so) | — |
| **Take open** | **Take** | Take closes here | `[open, close]` |
| | **Correction** | Take dropped from its open onward; a **fresh take opens here** | `[open]` (fresh) |
| | **Correction** again | Drops that fresh take too; another opens here ("flubbed again") | `[open]` (fresh) |
| **Take closed** (last press was its close) | **Take** | Next take opens here; the previous one is final | `[open]` |
| | **Correction** ① | The close **moves here** — the gap since the early close is back in | `[open]` |
| | **Correction** ② | Pops the open: the whole take is dropped; a **fresh take opens here** | `[open]` (fresh) |
| | **Correction** ③ | Drops that fresh take, opens another — same as the open-take row | `[open]` (fresh) |
| Any state | **Pause** / **Resume** | Doesn't touch the stack. A close moved later across a pause makes the take span the pause. | unchanged |
| Any state | **Mark** | Never a boundary; a mark inside dropped material is dropped with it | unchanged |
| Any state | **Correction** within 1 s of the previous accepted press | Debounced, ignored | unchanged |

What follows from it:

- **Only an explicit Take press starts a take.** Zero take presses keeps the
  whole recording between its flashes, as before; corrections alone never
  start one.
- **Once Take has been pressed, the recording is in take mode.** If
  corrections then drop every take, nothing is kept: the original is saved
  and no cut is rendered.
- **"Previous one is final":** once the next take opens, earlier takes are no
  longer correctable. Undo depth is the current take only.
- **Closing a fresh take immediately** (①②, then Take) yields an empty or
  near-empty take, which the render drops (`render.min_interval_ms`); if it
  was the only one, the render says `nothing left to render` and the original
  is all there is. That is the way to throw a take away without stopping.
- **"Toast says so"** is the pill's feedback line (`⌫ nothing to correct`).
- **The patch for a correction is yellow.** The sidecar records which action
  it was (`moved-close`, `dropped-take`, or `ignored` and why) and the
  boundary it left. When a close moves, the original close's red patch now
  sits inside kept material: the render hides it the way it hides a mark's
  patch, and any other patch that lands inside kept material too.
- **Pressed while paused** a correction takes effect at the boundary: a moved
  close lands at the pause, a fresh take opens at the resume.

### The pill (session C1c)

The pill is two lines: the state, then what each key does now. These are
the ratified mockups, and the tests reproduce each from a real recording
state:

```
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

Feedback (1.5 s)      ⌫ close moved +4.2 s
                      ⌫ take 2 dropped · take 3 started
                      ⌫ ignored — too fast
                      ⌫ nothing to correct
```

- **The hint can't lie.** The second line is computed from the take state
  the recorder publishes, by the same function (`events.next_effect`) the
  recorder decides every Take and Correction press with. A key that would do
  nothing (Correction with no take yet) is not offered.
- **Key labels come from your chords:** `T` for Ctrl+Alt+T, `⌫` for
  Backspace, `F13` for a bare F13 binding. Two chords on the same key with
  different modifiers are shown in full. A hint too long for the pill drops
  its last keys, then is cut with `…`.
- **Feedback.** After every press, accepted or ignored, a feedback line
  replaces the hint for 1.5 s, then the hint returns: `⌫ close moved +4.2 s`,
  `⌫ take 2 dropped · take 3 started`, `⌫ ignored — too fast`, `⌫ nothing to
  correct`, `T take 3 started`, `M mark 2`.
- **Time.** `◉ take 2 · 0:48` is the open take's length so far; `2:14 kept`
  is what the cut will keep, from the same kept intervals the dialog's
  status line and the render use, live.
- **`[agent] pill_hints = false`** gives the one-line pill. The feedback still
  shows, as a second line for its 1.5 s.
- The pill is still excluded from capture, and still in the corner opposite
  the fiducial patch (patch bottom-left when the pill is top-right): it grows
  away from its corner, never toward the patch. Smoke step 78 checks both.

What lands on disk for a recording with two pauses:

```
inbox\2026-10-06-terminal-peep-peep.mp4        segment 1 (the catalog points here)
inbox\2026-10-06-terminal-peep-peep.seg2.mp4   segment 2
inbox\2026-10-06-terminal-peep-peep.seg3.mp4   segment 3
inbox\2026-10-06-terminal-peep-peep.json       one sidecar: the segments and every event
inbox\2026-10-06-terminal-peep-peep.cut.mp4    the cut (session C1b): the segments joined, the takes kept
```

Each segment is remuxed to MP4 when it ends, so a paused recording is
already safe: it is in the catalog and on disk while you are away. `peep
open` plays the cut once it exists, and every segment in order (a playlist)
until then. A recording that was
never paused has exactly the files it had before. While a recording is live
(recording or paused) it cannot be renamed or discarded from the CLI: `peep
rename last …` says so and does nothing, since the next segment is still to
be written under its name. Rename it after it stops.

## The cut (session C1b)

Every recording gets a cut beside it, `<stem>.cut.mp4`, rendered from the
event record. The original files are never modified; the cut is a new file
in the recording's own name family, so `peep rename` moves it along and
discard deletes it with the rest.

**What the cut contains**, in order:

1. **Every segment that finished cleanly, joined.** A pause is a join.
2. **Each segment trimmed to its own flashes.** The cut starts at the first
   frame after the last magenta frame and ends at the last frame before the
   first green one. No warm-up, no flash, no clap tone: the tone plays during
   the magenta.
3. **Only the takes**, when there are takes (`summary.kept`); with no take
   at all, everything. Each take's corner patches (blue, red, yellow) are
   excluded with the material outside the take. A patch left inside a take
   (C1c: the red patch of a close that a correction moved later) is hidden
   like a mark's, below.
4. **Clean seams.** At every take edge and every join:
   - **Silence trim.** Silence is trimmed off the kept side, keeping
     `silence_keep_ms` (250 ms) of it before the first sound and after the
     last, and at most `silence_max_trim_ms` (1 s). A take that is silent
     from start to end is never trimmed for silence.
   - **Snap.** Where there is no silence to trim, the seam moves to the
     quietest 10 ms within `snap_ms` (300 ms).
   - **Crossfade.** The picture cuts hard on a frame; the audio crossfades
     over `crossfade_ms` (40 ms), centred on the cut, so it stays exactly as
     long as the picture.
5. **Marks become chapters** (the player's chapter list), titled with their
   label or `mark N`. A mark is never a cut. Its cyan corner patch is hidden
   instead: for those ~200 ms the corner shows what it showed the frame
   before. A patch right at a take's edge is cut off that edge instead, since
   the material beside it is excluded anyway. A mark pressed while paused
   starts a chapter at the resume.

No fiducial colour reaches the cut. A take pressed during a flash still
starts after the magenta and ends before the green, whatever its own patch
says.

**A zero-press recording** (no take, no pause) is simply the original
trimmed to its flashes: about 1 s of warm-up and the flashes are gone,
nothing else changes.

**How the boundaries are found.**
- **Read from the video.** Every flash and patch is looked for in the video
  itself, within `search_s` (1 s) of where the sidecar says it was shown: a
  thumbnail of the frame for flashes, a median of the patch's rect for
  patches.
- **Falling back to the stamp.** If one cannot be found (the corner already
  showed that colour, or a frame decode failed), the sidecar's stamp is used
  instead, erring toward cutting a little more. This is never silent: the
  render's output says `N fallback(s)`, the log has a
  `render.boundary_fallback` warning, and the sidecar's `render` block
  records the reason.
- **Inspecting a render.** `peep render --dry-run` shows every boundary,
  detected or not, before anything is written.

**The output** keeps the original's resolution, frame rate and audio tracks:
one mixed track, or two titled tracks with `mix = "separate"`. It uses the
original's encoder settings, `-movflags +faststart`, and chapters.
- **Speed.** The render is a full re-encode through A's encoder choice
  (`h264_qsv`). On this laptop the probe measured ~6x real time at
  2560x1600, so a 10-minute recording takes ~1.5 minutes.
- **QSV fallback.** If QSV fails, the render is redone with libx264 (~2.5x
  real time), and says so.
- **Priority.** ffmpeg runs at below-normal priority, so a recording started
  meanwhile is not starved.

**When it renders** (`render.auto`):
- **Agent recordings** render in the background, after the dialog's Enter
  or Esc, or right after the stop with `dialog = false`. A toast says when
  it starts and when the cut is ready. You can start a new recording at
  once; renders queue, one at a time. `peep agent status` shows the progress.
- **Terminal recordings** (`peep rec`) render in that terminal after
  `✓ saved`, printing progress, however the recording was stopped (q,
  Enter, Ctrl-C, `peep stop`, Ctrl+Alt+R). `peep rec --no-render` skips it.
- **The setting.** `render.auto = "takes"` renders only recordings with
  takes or pauses; `"never"` leaves it to `peep render`.
- **Failures.** A render that fails leaves the original untouched, shows
  why (a toast, or the terminal), and `peep render` retries it.

**`peep open`** plays the cut when it is current. Current means it was
rendered from the sidecar's present event record and is the size the render
recorded. Otherwise `peep open` plays the original and says why the cut was
not used. `--raw` always plays the original.

**The sidecar's `render` block** records everything the render decided:
- **The run.** `status` and `file`; when it ran and how long it took; the
  `source_digest` it was made from (segments, events, takes and marks, but
  not file names, so a rename keeps the cut current); and the `[render]`
  settings used.
- **Boundaries.** Each one, with its stamp and what was detected: the first
  and last frame, the measured colour, the difference in ms, or the
  fallback and why.
- **Seams.** Each one, with its offset and how it was placed: `silence`,
  `silence-max`, `snap` or `none`.
- **Joins and marks.** The crossfades, the chapters, the concealed mark
  patches, and the marks that fell in cut material.
- **Leftovers.** Dropped pieces and every fallback, in words.
- **The output.** Its duration, size, sha256, encoder and argv, plus each
  encode attempt.
- **`calibration`.** The clap's A/V residual per segment: the 1 kHz onset
  against the first magenta frame, minus what the stamps predicted. It is
  measured and recorded; `av_calibration = "apply"` also corrects it when
  it exceeds a frame.

```toml
[render]
auto = "always"            # always | takes | never
search_s = 1.0             # look for each flash / patch this far either side of its stamp
snap_ms = 300              # move a seam up to this far into the kept side, to the quietest 10 ms
crossfade_ms = 40          # audio crossfade at every join
silence_db = -45.0         # quieter than this (10 ms RMS, dBFS) is silence
silence_max_trim_ms = 1000 # at most this much silence trimmed per edge; 0 = never
silence_keep_ms = 250      # silence kept before the first sound / after the last
min_interval_ms = 250      # a piece shorter than this after trimming is dropped (and said so)
av_calibration = "measure" # measure | apply | off
```

## Where files land

```
C:\Users\chord\Videos\peep\            storage root (config: root)
  catalog.jsonl                        append-only index: recorded / failed / renamed events
  inbox\                               one folder per collection (default: inbox)
    2026-10-05-terminal-peep-peep.mp4
    2026-10-05-terminal-peep-peep.json sidecar
    2026-10-05-terminal-peep-peep-2.mp4   same name, same day: counter suffix
    2026-10-05-demo.seg2.mp4             a later segment of 2026-10-05-demo (session C1a)

%LOCALAPPDATA%\peep\                   peep's own files
  config.toml                          optional; defaults apply without it
  logs\peep.log                        rotating, structured; every ffmpeg argv logged verbatim
  logs\agent.log                       the agent's own log, same format
  state\active.json, stop-request      control files between `rec` and `stop` (active.json now has a status)
  state\mark-*.json                    one per mark request, consumed by the recorder
  state\agent.json, agent-command      the agent's pid/status, and `peep agent stop|reload`
  agent-prefs.json                     the agent's last-used collection
  clap.wav                             the 120 ms start tone (written on first use; also doctor's test tone)
  app\                                 the installed Windows package
```

Recordings never go under `\\wsl.localhost`; config validation refuses such
a root.

**Nothing is ever overwritten (session C1a).** A recording's files all start
with its stem: `<stem>.mp4`, `.seg<k>.mp4`, `.json`, and `.cut.mp4` and the
rest of the `.cut.*` family (C1b: the cut, and while it renders
`.cut.part.mp4`, `.cut.part.ffmeta` and the `.cut.lock` that keeps a rename
or discard away until it is done). A stem is free only
if no file in the folder starts with it (compared ignoring case, as Windows
does) and the catalog lists no recording with it, even one whose file has
gone. Otherwise the next counter is used: for `peep rec`, the hotkey, `peep
rename` and the dialog's save. Sidecars are created exclusively, moves refuse
an existing target, and a rename that cannot move one file (a player holding
it) puts back the ones it moved. Every place that shows the name shows the
final name. Names are ASCII, kebab-case, at most 48 characters, and never a
Windows device name (`con`, `nul`, `com1`…); collections follow the same
rule.

**The sidecar** (`peep.sidecar/2` since C1a; `/1` before) records the title, slug, collection,
creation time, duration, the foreground window and process at start, the
video pipeline, encoder, capture and output size, the audio device, the
flash colours with their agent-side timestamps (wall clock and seconds
since ffmpeg launched), stop reason, ffmpeg's argv and exit code, and
`trim: null`. Since A.1 the `audio` block also holds `sources`, `mix`,
`tracks` (what each audio track contains), `epoch` (the QPC stamps the
alignment was built from: ffmpeg's launch, its `Input #0` line, the anchor),
one block per source (`system`, `mic`: device, format, each connection's
`first_sample_qpc`, the capture statistics, exit code, any error) and `clap`.
Every flash gains `shown_qpc` on the same clock, so session C can relate a
flash and a sound exactly. A's four `audio` keys keep their meaning
(`device` is the microphone, `null` without one), so `peep.sidecar/1` is
unchanged for existing readers. `crop` is reserved for session C. `marks` is filled by
session B: each mark has `t` (seconds since the ffmpeg launch, at the
keypress), its `label`, `source` (hotkey / cli) and its cyan flash's own
stamp, on the same clock as the start and stop flashes.

**The event record (`peep.sidecar/2`, session C1a)** is what C1b renders
from. The top-level `video`, `audio`, `flash`, `timeline` and `ffmpeg` blocks
describe segment 1, so a one-segment recording reads exactly as before.
`duration_s` is the whole recording (all segments). New keys:

- `segments`: one per capture, in order. Each has `index`, `file`, `status`,
  `duration_s`, `started_at`, `ffmpeg_started_qpc`, `input0_qpc`,
  `video_epoch_qpc_est` (the QPC instant of its first frame: media t=0),
  `stop_reason` (`pause` or how the recording ended), and its own `flash`
  (start/stop), `timeline`, `video`, `audio` (its own anchor and clap) and
  `ffmpeg`.
- `events`: every take, correction (C1a: retake), pause, resume and mark
  press, accepted or not, in order: `kind`, `source` (`hotkey`/`cli`), `qpc` (the press,
  stamped by whoever pressed), `segment` and `media_s` (seconds from that
  segment's first frame), or `after_segment` for a press made while paused,
  `accepted`, `ignored` (`debounce`, `already-paused`, `nothing-to-correct`,
  …), `action` (`open`/`close`/`moved-close`/`dropped-take`/`pause`/`resume`/`mark`),
  `take`, `discarded_take`, for a correction `correction` (the action, the
  take, the `boundary` it left, a moved close's `from` and `moved_s`, the
  `undo` stack after), and the `fiducial` it showed (`color`, `style: patch`, `rect` and `screen` in
  physical pixels, `shown_qpc`, `since_ffmpeg_start_s`).
- `takes`: derived: `id`, `status` (`kept`/`discarded`), `open` and `close`
  (event id, `segment`/`after_segment`, `media_s`; a take still open at the
  end closes with `reason: session-end`), `discarded_by`, `undo` (C1c: its
  correction stack; `[]` once final) and `superseded_closes` (the positions a
  moved close had before).
- `take_rule` (C1c): `correction/1`. A C1a sidecar has none: its takes were
  decided by the retake rule, and it renders exactly as recorded, because the
  render reads only `takes` and `summary.kept`.
- `pauses`: `after_segment`, the pause and resume events, when capture
  stopped and restarted (QPC), and the `seconds` nothing was recorded.
- `summary`: `segments`, `takes`, `takes_discarded`, `kept_s`, `total_s`,
  `whole` (no take at all: keep everything), and `kept`: the intervals to
  keep, `[{segment, start_s, end_s, take}]`.
- `marks` keep B's shape, plus `segment`, `media_s` and `event`. A mark's
  `flash` is now its fiducial: a patch (`style: patch`) unless `mark_style =
  "full"`.
- `render` (session C1b): what the last render decided and produced; see
  [The cut](#the-cut-session-c1b). A `/1` sidecar gets the block too and
  stays `/1`.

*Migration:* `/1` sidecars still load. Readers call `catalog.as_v2()`, which
presents one as a single segment with no takes (the whole recording kept);
the file itself is never rewritten.

**The catalog** lets `ls`, `last` and `rename` work without walking folders.
Each recording has a `uid` that survives renames.

## How a recording works

1. The name is taken from the foreground window (or your slug) and a
   collision-free stem is picked (free in the folder, ignoring case, and in
   the catalog). The sidecar is created straight away, refusing to replace
   anything, which reserves the stem.
2. One capture child per audio source starts (see [Audio](#audio)), then
   ffmpeg starts into `<stem>.recording.mkv`. If the pipeline fails during
   start-up, the next one in `video.fallback` is tried, and you are told; the
   audio children carry over to the next attempt.
3. Once ffmpeg reports its output open, a 200 ms magenta full-screen flash
   is shown, with a 120 ms 1 kHz tone when system audio is recorded
   (`audio.clap`). It is captured on purpose, as a fiducial for session C.
   Marks during the recording show a 200 ms cyan flash.
4. On stop: a 200 ms green flash, a short settle, then `q` on ffmpeg's stdin.
   The audio children keep feeding ffmpeg until it has exited, and only then
   are they stopped.
5. The Matroska capture is remuxed to MP4 (streams copied, index at the
   front). The capture container is Matroska because a capture cut short by
   a crash is still playable. If ffmpeg dies mid-recording, the partial
   `.mkv` is kept and catalogued as failed.
6. A pause (C1a) runs steps 4–5 for the current segment, catalogues the
   recording so far, and waits. Resume runs steps 2–5 again into
   `<stem>.seg<k>.recording.mkv`. If a later segment fails, the earlier
   ones still make the recording, and you are told.

The video pipelines were measured on this laptop (ffmpeg 9.0.2, Intel Arc
140V), with colours read back from captured frames:

| `video.pipeline` | Chain | Notes |
|---|---|---|
| `qsv` (default) | ddagrab → QSV (zero-copy) → `vpp_qsv` BGRA→NV12 BT.709 → `h264_qsv` | GPU end to end; colours exact |
| `qsv-download` | ddagrab → download → swscale BT.709 NV12 → `h264_qsv` | first fallback; same output |
| `x264` | ddagrab → download → swscale BT.709 → `libx264` | software fallback |

Without the explicit conversion, the GPU path tags the stream as RGB/full
range and players show black as dark grey. `ffmpeg_cmd.filter_graph` is the
one place that decides this.

Ctrl-C in the `rec` terminal never reaches interop, because the Windows
child runs in its own session. The shim turns q/Enter/Ctrl-C into one
`stop` line on the child's stdin. If the shim or its terminal dies, the
child sees end-of-file and stops cleanly too. If the Windows process itself
dies, a kill-on-close Job Object takes ffmpeg down with it rather than
leaving it recording forever.

## Audio

```
peep config set audio.sources system   # default: what the computer plays (videos, apps, notification sounds)
peep config set audio.sources both     # computer audio + your voice, mixed into one track
peep config set audio.sources mic      # your voice only
peep rec --audio none                  # this recording only, silent
```

**How it works.** ffmpeg on this laptop has no WASAPI input and there is no
Stereo Mix, so peep captures audio itself, with no third-party tool:
`win/peep/wasapi.py` talks to WASAPI through `ctypes` COM (standard library
only). Each source runs as a small child process, `python -m peep.wasapi
serve --source system|mic`:

- **system** is a *loopback* capture of the default output device (on this
  laptop `FxSound Speakers`, because FxSound is running). Loopback delivers
  nothing while nothing plays, so the child fills the gaps with silence
  against the QPC clock: the track is always as long as the video.
- **mic** is a plain capture of the default recording device (the
  microphone array), through the same code.

ffmpeg's stdin is reserved for `q`, so each child serves raw PCM on a
localhost TCP port that ffmpeg reads with `-rw_timeout 3000000`. If a child
ever hangs with its socket open, ffmpeg gives up on that read within 3 s and
the stop still completes. A child that cannot start is reported on the
status line, in the log and in the sidecar, and the recording goes on
without that source. If one crashes (say, a COM error), only the child
dies: the recorder and the resident agent survive and report its exit code.

**Alignment.** ffmpeg puts its first screen frame at video t=0 and the
first audio byte it reads at audio t=0. Each child keeps the last 15 s of
audio, and when ffmpeg connects it starts the stream at the sample captured
at the instant of the first screen frame. That instant is taken from ffmpeg
itself: the first frame comes 58 ms before ffmpeg prints `Input #0` (measured over
ten runs: 45–68 ms, σ 7 ms; timing from the ffmpeg launch instead varied
twice as much). The recorder stamps that line and sends the instant to the
children. The two t=0s are then the same moment by construction, for both
sources.

**What was measured** (A.1 probes, 2026-10-06, a flash and a 1 kHz beep
recorded together, the screen read frame by frame):

| | audio vs picture (+ = audio early) |
|---|---|
| system audio, aligned as above (4 runs) | −15.9, −3.3, −14.6, +7.0 ms: within half a frame |
| microphone through WASAPI (same anchor) | the same numbers: both sources share the instant |
| microphone through A's dshow path | +240 → +431, +248 → +349, +345 ms (and one +974): starts ~300 ms early **and grows during the recording** |
| speaker → microphone, acoustically (FxSound + room) | 102–126 ms |

That last dshow row is "the first bug". The dshow mic track starts
300-ish ms ahead of the picture and keeps losing ground: the mic tracks of
the A-era `sync` recordings are 0.41–0.64 s shorter than their video. No
constant offset can fix that, which is why the microphone now goes through
WASAPI. And the `+500` in `sync5` did nothing visible because `-itsoffset`
survived A's MP4 remux only as an edit-list entry (an empty edit of 477 ms),
which the player used for those checks evidently ignored. Offsets are now applied
to the samples themselves, never to timestamps.

**`audio.offset_ms`** is a manual trim for every source (positive delays the
audio). The default needs none. **If your config still has `offset_ms =
500` from the sync tests, remove it:** `peep config unset audio.offset_ms`.
`peep doctor` flags it.

**dshow is still there** as `audio.mic_backend = "dshow"`, for a microphone
WASAPI cannot open. It gets a fixed 300 ms delay (`audio.dshow_align_ms`),
which cannot follow its drift.

**Picking devices.** `peep doctor` lists the active output and recording
devices by name. `audio.system_device` and `audio.device` take a name (or
a unique part of one) or an id; empty means the Windows default. To record
what reaches the real speakers after FxSound's processing, use
`peep config set audio.system_device "Speakers (Realtek XU)"`. Its tone
arrives ~150 ms later than on FxSound's own device, but the alignment
measures from capture time, so that does not matter.

**Both sources** mix into one track (`amix`, each source at its own level;
`audio.system_gain` and `audio.mic_gain` adjust). `audio.mix = "separate"`
keeps two tracks in the file instead, system first and titled.

## Configuration

`peep config --init` writes `%LOCALAPPDATA%\peep\config.toml` with every key
commented out at its default. Unknown keys and wrong types are errors, so a
typo can't silently fall back to a default. You rarely need an editor:
`peep config set audio.sources both`, `peep config get`, `peep config unset
audio.offset_ms`. Edits keep every comment, and they are validated before
the file is written. The keys you are most likely to touch:

```toml
default_collection = "inbox"
[video]
scale = "1920x1200"        # default "" = native 2560x1600
pipeline = "qsv"
[audio]
sources = "system"         # system | mic | both | none
mix = "mix"                # with both: one track, or "separate"
system_device = ""         # "" = the Windows default output
device = ""                # microphone; "" = the Windows default recording device
[flash]
duration_ms = 200          # 150 measured 4-5 frames; raised for margin after the smoke test
[agent]
record_hotkey = "Ctrl+Alt+R"
pill_position = "top-right"
take_hotkey = "Ctrl+Alt+T"     # also pause_hotkey, correct_hotkey; debounce_ms = 1000; pill_hints = true
```

If Windows renumbers the mic (the `(6- ...)` part), an empty `audio.device`
(the default) follows the Windows default recording device and needs no
change. With `mic_backend = "dshow"`, `peep doctor` lists the dshow names and
the stable `@device_cm_{...}` alternative name, and either works.

## Tests

```
python3 -m pytest                                   # if pytest is installed (sudo apt install python3-pytest)
python3 -m unittest discover -s tests -t .          # standard library only, same tests
```

The suite runs on the WSL side with no ffmpeg.exe, no display and no
interop. The recorder tests cross a real process boundary to
`tests/fake_ffmpeg.py`, a stand-in that speaks ffmpeg's stderr markers and
honours `q`. That covers the happy path, fallback, crash mid-recording, a
hung ffmpeg, remux failure, and `peep stop`. Since A.1, `fake_ffmpeg.py`
also connects to every audio socket input as ffmpeg does, and
`tests/fake_wasapi.py` runs the real capture server (real sockets, anchors
and gap-filling timeline) on a synthetic capture client. The tests use
abstract Unix sockets rather than the production localhost TCP, because bale
validates inside a network-off sandbox where a localhost TCP connect fails.
One test exercises the real TCP listener and skips, saying why, inside that
sandbox. Every wait in the suite is bounded, and `validation.sh` caps the run,
so a failure there fails rather than hangs. That covers the
audio path end to end on Linux: anchoring, every `sources` value, pipeline
fallback with a reused child, a failing or crashing child, the clap, and a
stalled PCM writer that must not hang the stop. The COM calls themselves
run only on Windows: the A.1 probes exercised them on the laptop, and the
checklist below covers them.

Session C1a added four test files. `test_events.py` drives the event state
machine through every take / retake / pause / resume sequence, the debounce
and the kept/total arithmetic. `test_segments.py` records real
multi-segment sessions through `fake_ffmpeg.py`. `test_naming_audit.py`
checks every naming path against a case-insensitive fake folder and the
catalog, plus both sidecar versions. `test_c1a_surface.py` covers the chords,
the pill, the dialog, the WSL commands and the reproduction of the
2026-10-06 naming report. The Tk windows themselves (the patch, the
dialog's preview line) need a desktop: smoke steps 51–58.

Session C1c added three test files. `test_correction.py` has every row of the
correction chart as its own test, the consequences, and the pill's promise:
the hint's prediction held against what the next press does, for every row
and for 300 random sequences. `test_c1c_pill.py` reproduces each pill mockup
from a real recording state, the 1.5 s feedback through the agent,
`pill_hints`, key labels, the pill's corner against the patch, and the
renamed config keys. `test_correction_render.py` extends C1b's fuzzer to the
chart's sequences (no fiducial frame in any cut) and renders sidecars
written under C1a's rule exactly as recorded.

Session C1b added five test files and `tests/render_fixtures.py`, which
builds recordings through the real `EventModel` and describes their media as
a scene. `fake_ffmpeg.py` synthesises the analysis output from that scene:
frames with the flashes and patches where a capture puts them, and audio
with the clap and "speech" tones.
- `test_render_plan.py`: the plan for every event shape and both sidecar
  versions.
- `test_render_detect.py`: detection, fallbacks, seam snapping and silence
  trimming against synthetic frames and PCM, the clap onset, and resolve().
- `test_render_argv.py`: the filtergraph and argv as pure functions,
  including the invariant that the cut's audio is exactly as long as its
  picture.
- `test_render_run.py`: the renderer across the process boundary: failures,
  the x264 retry, cancel, the lock, rename and discard. Its one real-ffmpeg
  test runs where WSL has an ffmpeg with libx264 and the filters the cut uses
  (otherwise it skips and says what is missing): it frame-counts the cut and
  scans it for fiducial colours, in a child process
  (`tests/real_ffmpeg_scenario.py`) with stdin on /dev/null, in a session of
  its own and killed at 120 s; every ffmpeg it starts says `-nostdin`. It
  takes about 4 s.
- `test_render_surface.py`: the queue, the agent's background renders (a
  recording started mid-render included), the CLI, the shim and the config.

## Smoke-test checklist (real capture path, on the laptop)

Run these in order after `peep install`. Note anything that differs. Items
marked ★ are the open questions the build could not answer itself.

1. `peep doctor` shows all checks ok. `peep doctor --capture` passes too.
2. In a terminal at `~/peep-peep`, run `peep rec`. You should see a
   **magenta** flash and then `● recording → C:\Users\chord\Videos\peep\inbox\<date>-terminal-peep-peep.mp4`.
3. Move a window around and talk for ~20 s, then press **q**. You should see a
   **green** flash, `■ stopping…`, and `✓ saved … (20.xs)` within a couple
   of seconds.
4. `peep ls` lists it. `peep open last` plays it. Check:
   - colours are right: black is black, and a window matches what was on screen;
   - the cursor is visible;
   - computer audio is audible (play something during the recording).
5. Step frame by frame at the start and end (mpv: `.`/`,`). You should find
   about 6 magenta frames near the start and about 6 green frames near the
   end (200 ms at 30 fps; 150 ms showed 4 green frames on 2026-10-04). If a
   flash shows only 1–2 frames, raise `flash.duration_ms`.
6. A/V sync: see steps 42–43 (A.1). The probe measured system audio within
   half a frame of the picture.
7. Check the stream properties:
   `ffprobe.exe -v error -show_entries stream=codec_name,pix_fmt,width,height,color_space,color_range,r_frame_rate -of compact "<file>"`
   should show `h264|yuv420p|2560|1600|bt709|tv|30/1` plus an `aac` stream
   (48000 Hz with system audio).
8. Open the sidecar `.json` next to the file. Check `status: ok`, that
   `capture_size` is 2560x1600, that the flash `since_ffmpeg_start_s`
   values are plausible, and `stop_reason: terminal`.
9. Run `peep rec demo -c test` in tab A and `peep stop` in tab B. B should
   print `saved …\test\<date>-demo.mp4`, and A should return to its prompt.
10. Run `peep rec` and press **Ctrl-C**. It should stop exactly like q.
11. Run `peep rec`, then close the terminal tab. After ~5 s, `peep ls` in a
    new tab should show the recording finalized.
12. `peep rename last "bale pack demo"` renames the file and sidecar, and
    `peep ls` shows the new name. `peep rename last x -c bale` moves it.
13. `peep rec --scale 1920x1200` gives a 1920x1200 output.
14. `peep rec --encoder qsv-download` and `peep rec --encoder x264` both
    produce good files (the fallbacks).
15. `peep rec --no-flash --audio none` gives no flashes and no audio stream.
16. Run `peep rec` twice the same day without a slug. The second file
    should end in `-2`.
17. While one recording runs, `peep rec` in another tab should refuse with
    `already recording`.
18. `%LOCALAPPDATA%\peep\logs\peep.log` should contain an `ffmpeg.argv` line
    for every capture and remux, verbatim.
19. ★ Record for 10 minutes. Note CPU/GPU load in Task Manager, the file
    size, and how long the stop takes (the remux).
20. ★ Lock the screen (Win+L) or trigger a UAC prompt mid-recording. Note
    what happens. ddagrab may lose the desktop; peep should keep a partial
    `.mkv` and catalog it as failed rather than hang.

### The resident agent (session B)

21. From WSL: `peep agent install`. It should print the Startup entry path
    and `agent started (pid N)` with three `registered` hotkeys.
    `peep agent status` agrees, and shows `installed copy current`.
22. In a browser (not the terminal), press **Ctrl+Alt+R**. You should see
    a magenta flash, then a red `● 00:01` pill top-right, counting.
23. Press **Ctrl+Alt+M** twice a few seconds apart. You should see a cyan
    flash and a brief `◆ mark` each time.
24. Press **Ctrl+Alt+R**. You should see a green flash, `■ saving…`, then
    the dialog. Check that it **has keyboard focus** without a click (★
    could not be verified here; your ForegroundLockTimeout is at its
    maximum). The name should be `chrome-<page title>` or similar, all
    selected, and the collection the last one you used.
25. Type `agent demo`, press **↓** once (the collection changes), then
    **↑** (back), then **Enter**. `peep ls` shows
    `<collection>/<date>-agent-demo.mp4`.
26. ★ Open that file and step through it. **The pill must not appear in
    any frame**, while the magenta, cyan and green flashes do. The probe
    showed ddagrab honours the exclusion for this window recipe; this is
    the end-to-end check. Also check that no on-screen message
    (`◆ mark`) appears in the video.
27. Open its sidecar. `marks` should have two entries with `t` values
    matching when you pressed (±0.1 s), each with a cyan `flash`;
    `timeline.origin` is `agent` and `stop_reason` is `hotkey`.
28. Record again, then press **Esc** in the dialog. The automatic name
    stays.
29. Record again, then press **Delete** (the dialog asks to confirm),
    then any letter: cancelled, and the letter is not typed. Press
    **Delete**, **Delete**: the take is gone from disk and from `peep ls`.
30. Record, then press **Ctrl+Alt+X** mid-recording. There is no dialog,
    the take is deleted, and `discarded` is shown.
31. Press **Ctrl+Alt+M** and **Ctrl+Alt+X** with nothing recording. Each
    says `nothing is recording`.
32. In a terminal: `peep rec`. The pill shows. **Ctrl+Alt+M** marks it.
    Then q: **no dialog**, the terminal prints `✓ saved` as before.
    Repeat, stopping with **Ctrl+Alt+R** instead: the terminal prints
    saved and there is still no dialog.
33. Open the dialog and play the last file in a player that locks it,
    then press Enter with a new name. The dialog should say
    `could not rename: … WinError 32 …` and stay open. Close the player
    and press Enter again.
34. Edit `config.toml`: `[agent] record_hotkey = "Ctrl+Alt+F9"`, and save.
    Within ~2 s a `config reloaded (hotkeys re-registered)` message
    appears, and the new chord works. Set it back.
35. Put an invalid key in `[agent]`, save, and run `peep agent reload`.
    It should report `config not reloaded: unknown key…`, and the old
    hotkeys still work. Fix it.
36. Run `pythonw %LOCALAPPDATA%\peep\app\peepw.py agent run` while the
    agent runs. The second one exits at once, and `agent.log` has
    `agent.second_instance`.
37. ★ Sign out and back in (or reboot). The agent starts at login.
    `peep agent status` shows all three hotkeys `registered`. If one shows
    `retrying`/`FAILED`, note which app holds it.
38. ★ Press **Ctrl+Alt+R** while a full-screen app (a video in full
    screen, or a game) has focus. The recording works and the dialog
    comes to the front with focus.
39. Edit any file under `win/`, then run `peep agent status`. The shim
    syncs, and the line `the running agent is older than the installed
    copy` appears. `peep agent restart` clears it.
40. `peep agent stop` while recording should say it is saving first; the
    file is kept and there is no dialog. Then `peep agent uninstall`: the
    Startup entry is gone.

### Audio (session A.1)

41. `peep config unset audio.offset_ms` (the 500 from the sync tests), then
    `peep doctor`. You should hear a short tone and see `system audio:
    'FxSound Speakers (FxSound Audio Enhancer)' (Windows default; 48000 Hz, 2
    ch, f32le)` and `loopback capture: … test tone heard`. No `audio offset`
    warning remains.
42. ★ System sync: `peep rec sysync`, play a video with a clear visual and
    audible beat (or tap a key in an app that clicks), stop, and step
    through. You should hear the tone at the magenta flash and the beats on
    their frames. The sidecar's `audio.system.connections[0].start_source`
    should be `anchor`.
43. ★ Mic sync: `peep config set audio.sources mic`, then `peep rec micsync`,
    and say "now" as you click something visible. The voice should be on
    the click, not half a second ahead. Repeat after 60 s of talking: it
    should not drift. Listen for tiny dropouts, and note `audio.mic.stats`
    (`gap_fills`, `gap_fill_frames`) in the sidecar: probe 3 saw up to five
    gaps under 60 ms per 10 s run while dshow held the same microphone, and
    the timeline fills each with silence. Then `peep config set
    audio.sources system`.
44. `peep rec --audio both` while talking over a playing video: one mixed
    track, both audible at sane levels. With `peep config set audio.mix
    separate`: two tracks (system, mic) in the `.mp4`
    (`ffprobe.exe -v error -show_entries stream=index,codec_type:stream_tags=title -of compact "<file>"`).
    Unset it afterwards.
45. Silence: `peep rec quiet` with nothing playing for 30 s, then play a
    sound and stop. The audio track should be as long as the video (sidecar
    `duration_s` vs `ffprobe` audio duration), with the sound where it
    happened.
46. ★ The status line under `● recording` names the audio (`audio: system
    (FxSound Speakers …)`). With the agent, a source that cannot start shows
    a toast.
47. ★ Device switch: start a recording, plug in or unplug headphones, keep
    going, stop. Note what happens. The capture child may end; the recording
    should still finish, with a warning, and the sidecar's
    `audio.system.error` should say why.
48. ★ Firewall: the first recording after A.1 opens localhost-only TCP
    listeners in `python.exe`. Note whether Windows shows a firewall prompt.
    The A.1 probes ran six of these without one.
49. `peep config set audio.sources speakers` is refused with the list of
    valid values, and `config.toml` is unchanged. `peep config get` lists
    every key, marking the ones set in the file.

### Pause, takes and naming (session C1a)

50. Run `peep agent restart` (it syncs and loads the new code). `peep agent
    status` should list six hotkeys `registered`: record, mark, discard,
    pause (`Ctrl+Alt+P`), take (`Ctrl+Alt+T`) and retake
    (`Ctrl+Alt+Backspace`; since C1c listed as `correct`). The C1a probe found P, T and Backspace free on
    this laptop (and `Ctrl+Alt+E` taken by another app).
51. **Takes.** Press Ctrl+Alt+R, talk for 5 s, press **Ctrl+Alt+T**: a blue
    square flashes bottom-left and the pill shows `◉ take 1`. Talk, press
    Ctrl+Alt+T: a red square, `○ 1 take`. Press Ctrl+Alt+R. The dialog's
    status line reads `1 take · 00:0x kept of 00:xx`. In the sidecar,
    `events` has an open and a close, and `summary.kept` one interval.
52. **Retake.** Superseded by C1c's correction chart: steps 71–75.
53. **Debounce.** Double-tap Ctrl+Alt+T quickly: one patch, the pill opens
    only one take, and the sidecar shows the second press with `ignored:
    debounce`. Wait a second and press again: it closes.
54. **Patches in the video.** Step through the recording from step 51 at the
    take presses: about 6 frames with a solid blue (then red, yellow) square
    in the bottom-left corner, 200×200 at 2560×1600, and **no** pill in any
    frame. The colours ★ were not measured through the pipeline: note what
    the frames show (expect about (0,0,254), (254,0,0), (254,254,0)).
55. **Pause across the dialog.** Record, open a take, press **Ctrl+Alt+P**:
    a green flash, then the pill turns orange, `❚❚ PAUSED 00:0x ◉ take 1`,
    standing still. Wait 30 s (the bathroom break). `peep ls` already lists
    the recording. Press Ctrl+Alt+P again: a magenta flash, the pill counts
    on from where it stopped. Close the take, stop with Ctrl+Alt+R. The
    dialog shows `… · 2 segments`. Save under a new name: both
    `<name>.mp4` and `<name>.seg2.mp4` (and the `.json`) are renamed.
56. **Pause from WSL.** `peep rec demo`, then in another tab `peep pause`
    (it prints `paused after segment 1 …`), `peep take` (`◉ take: 1 take(s),
    the last one open (paused: …)`), `peep resume`, `peep take`, then q in
    the first tab. The sidecar's take opens at the start of segment 2.
57. ★ **Pause timing.** Note how long the pause takes (stop flash to the
    orange pill: the remux of the segment so far) and the resume (Ctrl+Alt+P
    to the magenta flash). Both should be a couple of seconds.
58. **Naming.** Record twice from the same browser tab into one collection.
    The second dialog prefills `…-2` (before C1a it showed the first file's
    name, which is what looked like an overwrite on 2026-10-06), and typing
    the first one's name shows `saves as …-2 (… is taken, so a counter is
    added)`. Enter keeps `-2`; the first file is untouched.
59. **Naming, case and the catalog.** Rename a file in Explorer to change
    only its capitals (`…-Demo.mp4`), then `peep rename last demo`: it gets
    a counter and says so. `peep rename last con` is refused (`CON is a
    device name Windows reserves`).

### The cut (session C1b)

60. Run `peep agent restart` (it syncs and loads the new code), then `peep
    ls`. Recordings show `[2 seg · 3 takes]` where they have them.
61. **Zero presses.** `peep rec quick`, talk for 10 s, press **q**. After
    `✓ saved`, the terminal prints `✂ rendering the cut…`, the percentages,
    and `✓ cut: …quick.cut.mp4 (00:0x of 00:1x, 1 piece(s), …)` with no
    fallbacks. Step through the cut's first and last frames (mpv `.`/`,`):
    no magenta, no green, no warm-up. Its start is the first frame after the
    magenta in the original.
62. **Render the C1a smoke recording** (step 51 or 55):
    `peep render <its stem> --dry-run`. Every boundary should read `detected
    6 frame(s)`, with a difference of a few tens of ms. ★ **Note the
    `measured_rgb` of the blue, red and yellow patches** in the sidecar's
    `render.boundaries` once rendered: this is the first measurement of the
    patch colours through the pipeline (expected about (0,0,254),
    (254,0,0), (254,254,0)). Then `peep render <stem>`.
63. **The seams, by ear and eye.** Play the cut (`peep open`). At each join,
    the voice should resume cleanly: no clipped first or last word, no click,
    and no take patch in any frame. The pause seam (step 55) should be one
    continuous take with a hard picture cut. Step frame by frame across
    each seam: no blue, red, yellow, magenta or green.
64. **Marks.** Record with two marks (Ctrl+Alt+M) inside a take, render, and
    open the cut in a player that shows chapters (mpv: the chapter list). It
    has `start` and the marks. Step through a mark: the cyan corner is
    hidden. That corner holds still for ~200 ms instead.
65. ★ **Speed and the agent.** Record ~2 minutes with the hotkey, press
    Enter in the dialog, and at once start another recording with
    Ctrl+Alt+R. The `✂ rendering …` toast appears, the new recording starts
    normally, and its picture shows no stutter. Note how long the first
    render takes (`render.elapsed_s` in its sidecar; the probe predicts
    ~6x real time, about 20 s). Stop the second recording; its render
    queues after the first.
66. **Failure path.** Open the cut of step 61 in a player that locks it,
    then `peep render quick --force`. It should fail with `could not put the
    cut in place … open in a player?`, the original untouched. Close the
    player and run it again: `✓ cut`.
67. **Open.** `peep open last` plays the cut; `peep open last --raw` the
    original. For a paused recording with `render.auto = "never"` (set it
    for one recording, then unset it), `peep open` plays both segments in
    order (a playlist) and says it is not rendered yet.
68. **Format.** `ffprobe.exe -v error -show_entries stream=codec_name,width,height,r_frame_rate,color_space,color_range,color_transfer:stream_tags=title -show_chapters -of compact "<cut>"`
    should match the original's line from step 7, with chapters when there
    were marks. With `audio.mix = "separate"` it should show two titled
    audio tracks.
69. ★ **Clap calibration.** In a rendered recording's sidecar,
    `render.calibration[].residual_ms` is the A/V residual the clap measured
    (A.1 measured -16..+7 ms). Note a few values; if they sit consistently
    beyond ±33 ms, consider `peep config set render.av_calibration apply`.

### Corrections and the pill (session C1c)

70. Run `peep agent restart` (it syncs and loads the new code). `peep agent
    status` lists `correct` on `Ctrl+Alt+Backspace` `registered`. If your
    config.toml sets `retake_hotkey`, it still loads (the old name).
71. **No take yet.** Press Ctrl+Alt+R. The pill reads `● 00:0x  whole video
    kept` over `T start take · P pause · M mark`. Press **Ctrl+Alt+Backspace**:
    no yellow square, and for ~1.5 s the second line reads `⌫ nothing to
    correct`, then the hint returns.
72. **① Move the close.** Press T, talk, press T early (red square; `○ 1 take
    · 0:0x kept`, hint `T next take · ⌫ move close here`). Keep talking ~4 s,
    press ⌫: a yellow square, `⌫ close moved +4.x s`, the kept time ~4 s
    longer, hint `T next take · ⌫ drop take 1, restart`.
73. **② Drop it and restart.** Wait a second, press ⌫ again: yellow,
    `⌫ take 1 dropped · take 2 started`, the first line `◉ take 2 · 0:0x`,
    hint `T close · ⌫ restart take · P pause`.
74. **③ Flubbed again.** Press ⌫ once more: `⌫ take 2 dropped · take 3
    started`. Talk, press T, stop with Ctrl+Alt+R. The dialog's status line
    reads `1 take · …`. In the sidecar: `take_rule: "correction/1"`; the
    events' actions read open, close, moved-close, dropped-take,
    dropped-take, close; take 1 has `superseded_closes`.
75. **Debounce.** Close a take with T and press ⌫ within a second: `⌫ ignored
    — too fast`, no square, and the close stays where it was. Double-tap ⌫
    quickly: only the first acts. From WSL, `peep correct` twice in a row
    both act (`↺ correct: …` each time); `peep retake` does the same.
76. **The render hides the old close.** Record: T, talk, T (early), talk 3 s,
    ⌫ (①), T (a new take), talk, T, stop. `peep render <stem> --dry-run`
    lists `the superseded close patch of event 2: patch concealed`. Render,
    then step through the cut where the first close was (mpv `.`): the corner
    holds still for ~200 ms, with no red. No blue, red or yellow in any frame.
77. **Across a pause.** T, talk, T early, Ctrl+Alt+P, wait, Ctrl+Alt+P, talk,
    ⌫: the close moves into segment 2, the pill's kept time includes both
    sides. The cut is one take across the pause join, with no red square.
78. ★ **The enlarged pill.** On screen the two-line pill sits flush in its
    corner (top-right) while squares flash bottom-left, never overlapping.
    Step through any recording from steps 71–77: **no pill in any frame**,
    neither line, nor the feedback line. With `peep config set
    agent.pill_position bottom-left` (record 10 s with a take, then `peep
    config unset agent.pill_position`) the squares move top-right.
79. **Hints off and labels.** `peep config set agent.pill_hints false`: one
    line; a press still shows its feedback as a second line for 1.5 s.
    `peep config unset agent.pill_hints`. If a mouse button is mapped to F13,
    `peep config set agent.take_hotkey F13`: the hint reads `F13 start take`;
    unset it after.

## Troubleshooting

- **The cut is missing a word at a seam, or keeps too much silence:**
  `peep render <stem> --dry-run` shows where each seam went and why
  (`silence`, `snap`). Raise `render.silence_keep_ms`, lower
  `render.silence_db` (more counts as sound), or set
  `render.silence_max_trim_ms = 0`, then `peep render <stem> --force`.
- **`N fallback(s)` after a render:** a flash or patch was not found where
  the sidecar placed it. The cut used the stamp instead and erred toward
  cutting a little more. The sidecar's `render.fallbacks` says which one and
  why. `peep render --dry-run` shows the colour it saw instead.
- **A render failed:** the original is untouched. The toast or the terminal
  says why. `peep render` retries, and `peep render --dry-run` shows the plan.

- **`python.exe` opens the Microsoft Store, or isn't found:** run
  `winget install Python.Python.3.12`, then `wsl --shutdown`.
- **The qsv pipeline fails:** peep falls back on its own and says so. To
  make it permanent, set `[video] pipeline = "qsv-download"` or `"x264"`.
- **No computer audio in a recording:** run `peep doctor`. `loopback
  capture` says whether the test tone reached the output device peep
  listens to. If Windows plays through another device, set
  `audio.system_device` to the name doctor lists. The sidecar's
  `audio.system` block has the capture's error and statistics.
- **Microphone not found:** `peep doctor` lists the recording devices by
  name; `peep config set audio.device "<name>"`, or unset it to follow the
  Windows default. With the dshow backend it lists the dshow names (and
  `@device_cm_...` alternatives).
- **Voice early or late:** it should not be with the default WASAPI mic.
  If you measure a constant offset, `peep config set audio.offset_ms N`
  (positive delays the audio) and note it for session C.
- **A take or pause press seemed to do nothing:** a second press within a
  second of the first is ignored on purpose (debounce, `· take ignored` on
  the status line, `ignored: debounce` in the sidecar, `ignored — too fast`
  on the pill). A correction within a second of a take press counts too.
  `[agent] debounce_ms` changes it.
- **The pill is too wide:** `peep config set agent.pill_hints false` keeps
  only its first line (each press's feedback still shows briefly).
- **A hotkey does nothing:** `peep agent status` says whether the agent is
  running and whether each chord registered. A chord another app holds
  shows `FAILED: Ctrl+Alt+R is already taken`; pick another in `[agent]`.
- **The dialog appears behind other windows or without focus:** note it
  in the smoke test (step 24). `agent.log` has a `foreground.force` line
  saying which method was tried.
- **Anything else:** `peep paths` shows both log locations, and every error
  is in the log with context.
