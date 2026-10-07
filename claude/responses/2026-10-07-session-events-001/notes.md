# notes — 2026-10-07-session-events-001 (peep-peep session C1a)

Everything in the brief's scope list is built, with tests. The suite has
475 tests: the 357 from before plus 118 new ones. It passes on Python
3.12.3 (your WSL version) and on 3.13, and inside a network-off namespace
(`unshare -rn`, the closest I can get to bale's sandbox), where the one
loopback-TCP test skips as before.

Nothing renders. A recording with zero presses ends exactly as today: the
same files, the same catalog event, the same status lines. The one
difference is a sidecar that now says `peep.sidecar/2` and carries an empty
event record.

## The overwrite, reproduced from the laptop's evidence

**No file was overwritten.** The probe checked three things:

- **The catalog.** It replayed all 32 events and looked for a recording
  arriving at a path another live recording held, compared
  case-insensitively. There were none.
- **The sidecars.** Each of the 28 sidecars' uids matches the catalog entry
  for the media beside it.
- **The folders.** There is no media file the catalog doesn't know.

Every `file.rename` in `peep.log`/`agent.log` went to a fresh name, and both
ffmpeg commands already ran with `-n`.

**What happened is a display defect in the stop dialog's save path.** From
`agent.log` on 2026-10-06, in `movies`:

- 21:55:26: you saved `2026-10-06-brave-watch-black-swan-online-free.mp4`.
- 21:56:12: the next recording of the same tab was correctly allocated
  `…-free-2` (`record.begin stem=…-free-2`).
- 22:25:44: its dialog opened with `prefill=brave-watch-black-swan-online-free`.
  `Agent._open_dialog` built the prefill with `naming.split_stem`, which drops
  the counter, so the dialog offered the exact name of the file next to it.
  Pressing Enter would *not* have overwritten anything: `rename` would have
  kept `-2`. But nothing on screen said so.
- 22:25:47: you typed `brave-watch-black-swan-online-free2` by hand to dodge
  the apparent overwrite. That rename is in the log.

The 19:57 recording shows the same pattern (prefill without the `-2` it had).

**The fix:**

- **The prefill is now the file's real name, counter included**
  (`naming.slug_of_stem`).
- **A live "saves as" line under the fields.** It recomputes as you type,
  with the same allocation the rename then performs (`catalog.plan_rename`),
  and says when and why a counter is added.
- **The toasts and `peep rename` name the final file** and say when it got a
  counter.

`ArchitectReproductionTest` in `tests/test_c1a_surface.py` replays the
sequence through the real `Agent`. One validation assertion covers it too.

**The audit also found real gaps,** none of which had bitten yet:

- **Case.** Allocation checked existence with `os.path.exists`, which is
  case-sensitive in WSL tests and only incidentally case-insensitive on NTFS.
- **The catalog.** It wasn't consulted, so a recording whose file you moved
  away by hand could get a twin name.
- **Renaming a failed capture that never started.** Its `file` is `""`.
  `rename` would have resolved that to the storage root folder itself and
  tried to move it. It now refuses.
- **Plain `os.rename`.** `rename` and the recorder's Matroska keep-paths used
  it. That refuses an existing target on Windows but silently replaces one
  on POSIX, and nothing checked capitalisation.

## What the probe established (relied on)

One paste-back probe ran (413 lines, integrity trailer matched).

- **Repo.** Clean at `1b3d0b8` (A.2's sweep). The only untracked files are
  two telemetry JSONs.
- **`config.toml`.** It sets only `[flash] duration_ms = 200`, so no colour
  or chord in your file conflicts with the new defaults.
- **The agent.** Running (pid 28196), with R/M/X registered.
- **Chord availability** (RegisterHotKey, released at once):
  - **free:** `Ctrl+Alt+P`, `T`, `Z`, `Y`, `Space`, `Backspace`, `F9`–`F12`,
    `Num0`–`Num3`, and bare `F13`–`F15`;
  - **taken:** `Ctrl+Alt+E`, by another app.
- **Evidence.** The catalog, the sidecars and the logs, as above.

## Decisions for you to ratify

Mechanism calls under AGENT.md §4. The ratified decisions of A, B, A.1 and
A.2 stand; where one is extended, it says so.

1. **The sidecar is `peep.sidecar/2`, not an additive `/1`.** A `/1`
   reader may assume the `.mp4` beside the sidecar is the whole recording,
   and `flash.stop` is its end. With segments, neither holds any longer, and
   a version bump is the honest way to say so. The top-level blocks still
   describe segment 1, so such a reader keeps working for the first
   segment. **Migration:** `/1` files are never rewritten (a rename keeps
   the version). `catalog.as_v2()` presents one as a single segment with no
   takes and a summary that keeps the whole recording. `read_sidecar`
   accepts both.
2. **The `.mp4` remux (A #3) now happens per segment, when each segment
   ends.** It is not deferred to C1b. A paused recording is therefore
   already playable and safe on disk while you are away. The remux runs
   during the pause, when nobody is waiting, and the final stop is exactly
   as fast as today.
3. **Segment files.** Segment 1 keeps A's names exactly. Segment k ≥ 2 is
   `<stem>.seg<k>.recording.mkv` → `<stem>.seg<k>.mp4`. The catalog points
   at segment 1, so `peep open` plays segment 1 only until C1b concatenates.
   The catalog event gains `segments: n` when there is more than one.
4. **Default chords:**
   - pause/resume: `Ctrl+Alt+P`;
   - take: `Ctrl+Alt+T`;
   - retake: `Ctrl+Alt+Backspace`.

   All three were free on the laptop. I avoided `Ctrl+Alt+Y` for retake even
   though it is free: it sits next to T, and a slip there would discard a
   take. Backspace reads as "back up, again".
5. **Debounce (`[agent] debounce_ms = 1000`).**
   - It is per chord, measured from the last *accepted* press of that chord.
   - Pause and resume share one chord.
   - It applies to hotkey presses only: `peep take` twice from WSL is
     deliberate, so it is never debounced.
   - Ignored presses are kept in the sidecar (`ignored: "debounce"`) and
     logged.
6. **Fiducials.**
   - **Patch colours:** take-open blue `#0000FF`, take-close red `#FF0000`,
     retake yellow `#FFFF00`, mark cyan as before. Opening and closing a
     take get different colours, so C1b can tell them apart from the video
     alone.
   - **Patch geometry:** 200×200 physical px, flush in the corner opposite
     the pill (`patch_corner = "auto"` → bottom-left), 200 ms.
   - **Marks moved to the patch.** `mark_style = "full"` brings back B's
     full-screen cyan; it cost one config key.
   - **A config break to know about.** Config validation now requires every
     pair of the six fiducial colours to differ by at least 96 in some
     channel. A config that had set `mark_color = "#FFFF00"` (B's test did)
     now collides with the retake colour and is refused with a message
     naming both keys. Yours doesn't set it.
7. **Presses while paused take effect at the boundary.** A take opened (or
   a retake) during a pause starts with the next segment. A take closed
   during a pause ends with the last one. Marks while paused are recorded
   with `after_segment`. No patch is shown, since nothing is being
   captured.
8. **Crash safety while paused.** At each pause the sidecar is rewritten
   with `status: "paused"`, and a `recorded` catalog event is appended with
   `in_progress: "paused"`. The final stop appends another one for the same
   uid, and the fold keeps the last. A crash or power cut while paused
   therefore leaves the finished segments in `peep ls`.
9. **A later segment failing does not fail the recording.** If segment 1 is
   good, the session is `ok`. The failed segment's partial Matroska is kept
   (`.seg2.mkv`), and you are told on the status line. A resume whose
   pipelines all fail ends the recording with what was captured. A
   single-segment failure behaves exactly as before.
10. **The naming rule is a namespace.** A stem `S` is taken if:
    - any file in the folder is `S` or starts with `S.`, compared
      case-insensitively; this covers every segment, the sidecar, its `.tmp`,
      C1b's `.cut.*` family and even a hand-made `S.notes.txt`;
    - or the catalog holds a live recording with that stem in that
      collection, failed ones and ones with missing files included.

    Rename and discard act on peep's own family only (`naming.OWNED_REST`).
    Discard leaves `S.notes.txt` alone. Rename moves every segment and render
    together and puts back what it moved if one file cannot move.
11. **Slug rules.** ASCII-fold, kebab, cap at 48 (as before), and refuse
    Windows device names (`con`, `nul`, `com1`…, also before an extension)
    for slugs and collections. A window titled "CON" becomes
    `recording-con`.
12. **Time.** Every request carries the requester's `requested_qpc`. The
    agent stamps it on the hotkey thread the instant `WM_HOTKEY` arrives; the
    CLI stamps it when the command runs. A press's `media_s` is measured from
    its segment's `video_epoch_qpc_est`: A.1's `Input #0` − 58 ms, or the
    ffmpeg launch when `Input #0` was never seen.
13. **The pill shows captured time**: the segments so far plus the current
    one. It stands still while paused (orange, `❚❚ PAUSED 01:12  ○ 2
    takes`). For an unpaused recording this is B's count unchanged.
14. **Bare `F13`–`F24` are allowed as chords; every other key still needs
    Ctrl, Alt or Win.** Nothing types F13–F24, and mouse or macro-pad
    software can emit them, which is the cheap route to a one-button take
    (see Proposals). Numpad keys are `Num0`–`Num9`, `NumAdd`, … (NumLock on).
15. **`peep pause` / `peep resume` wait** (60 s default, `--no-wait`) until
    `active.json` shows the new status. `peep take` / `retake` wait up to
    3 s for the recorder to consume the request, then print the take count
    it published.

## Look closely at

- **`recorder.py`, `_run_segment` and `_pause_wait`.** The ordering inside a
  segment is A's: flash, settle, `q`, wait, children, then finalize. What is
  new is the loop around it, plus `_handle_press` (patch, sidecar,
  active.json). Presses that arrive after an accepted pause, or with a stop
  while paused, are held in `sess.pending` and still recorded. A test covers
  each path.
- **`TkFlasher.patch`.** It shrinks the existing flash window to the patch
  geometry, shows it, then restores full screen in a `finally`. Under Xvfb
  the geometry read back as `200x200+0+1400`, and the following full flash
  still worked. On Windows, an overrideredirect window's geometry change
  while withdrawn should behave the same. ★ smoke step 54.
- **QPC across processes.** A `peep take` from WSL is stamped by
  `python.exe` in its own process, and the recorder compares that stamp with
  its own. QueryPerformanceCounter is system-wide, so that holds, and A.1
  already relies on it with its capture children. But this is the first time
  a *requester's* stamp is used.
- **`catalog.move_no_clobber` on Windows** is plain `os.rename`, which refuses
  an existing target there. The POSIX path (`link` + `unlink`) is what the
  tests exercise.
- **The live-recording guard** (review finding 1) relies on `active.json`'s
  uid and a live pid, the same signal `peep stop` uses.

## Uncertain (so you don't inherit false confidence)

- **The patch colours through the pipeline** were not measured. By analogy
  with A's magenta and green they should decode near (0,0,254), (254,0,0)
  and (254,254,0). ★ smoke step 54 asks you to note the frames.
- **Pause and resume latency.** I expect a couple of seconds each, dominated
  by the segment's remux and ffmpeg's start-up, but I couldn't measure it.
  ★ smoke step 57.
- **The patch over a full-screen exclusive app**: same question as B's
  dialog over one. ★ smoke step 38 covers the class.

## An independent review before shipping

I had a separate agent, one that had not written the code, review the
diff for concrete defects. It found two, both reproduced, and two
record-keeping slips. All four are fixed and covered by tests.

1. **Renaming a recording while it was paused split it.** A paused
   recording is catalogued (decision 8), so `peep rename last x` while
   paused moved segment 1 and the live sidecar away. The recorder then
   wrote `<oldstem>.seg2.mp4` and a fresh `<oldstem>.json`, and its final
   catalog event pointed at the old, now missing, `.mp4`. Now
   `catalog.rename` / `discard` / `plan_rename` take the live recording's
   uid (from `active.json`) and refuse it with `RecordingInProgress` ("…
   is still recording (or paused); rename it after it stops"). The CLI and
   the agent pass it. After a crash the recorder's pid is dead, so the
   recording is renamable again.
2. **A 32-character stem was taken for a uid.** It happens whenever a slug
   is 21 characters, e.g. `peep rename 2026-10-05-abcdefghij-klmnopqrst
   x`. My `plan_rename` had it, and so did B's `discard` all along (`len(ref)
   < 32`). Both now go through `catalog.resolve_ref`: 32 hex digits are
   tried as a uid first, then as a name.
3. **A stop that beat a pause left a pause in `pauses` that never
   happened.** The event model now takes such a pause back
   (`cancel_pending_pause`) and marks the press `ignored:
   "superseded-by-stop"`. The same applies when the segment failed while
   pausing, or the recording ended with a pause press held.
4. **The failed-pause path left the catalog's `reason` null.** It now says
   `ffmpeg-exited`.

The reviewer also checked and found correct: the naming audit, the move
rollback, the child-process cleanup, stops during a pause, the event model
arithmetic, and that a never-paused recording is unchanged.

## The sandbox Tk run (not part of the suite)

I installed `python3-tk` in my sandbox and ran the real `Agent`, `TkUi`,
`StopDialog` and `ToplevelFlasher` on Tk 8.6 under Xvfb. The recorder was the
real one, driving `fake_ffmpeg.py` and the fake WASAPI child, with Win32
stubbed. The run, through `agent.on_hotkey` as the listener would call it:

- **Record.**
- **Take, then a bounce.** The bounce was ignored, recorded as `debounce`.
- **Take again**, which closed the take.
- **Pause.** The pill read `❚❚ PAUSED 00:03 ○ 1 take`.
- **Take while paused.** The pill read `◉ take 2`.
- **Resume.**
- **Retake.** It discarded take 2 and opened take 3.
- **Mark, then stop.**

Every flash and patch ran on `MainThread`. The dialog showed `00:06 · 2
takes · 00:03 kept of 00:06 · 2 segments · 2 KB · …`, and its "saves as"
line updated as text was typed. Enter (a real `<KeyPress Return>`) renamed
both segments and the sidecar. A second recording saved under the same name
became `-2`, with the toast saying so. `agent.log` had no warnings. Win32 was
stubbed, so this says nothing about capture exclusion, focus or
RegisterHotKey.

## Tests changed outside the new files

- **`tests/test_config.py`** (B's): `test_overrides` used `mark_color =
  "#FFFF00"`, now the retake colour; it uses `#FF8000`. The colour-clash
  message changed from "must all differ" to "too alike".
- **`tests/test_hotkeys.py`** (B's): the default table and the registered
  set now include the three new chords.

B's other tests (`test_marks`, `test_agent`, `test_dialog`) pass unchanged.
`test_marks` exercises the fallback where a flasher without `patch()` shows
the full flash and logs `flash.patch_unsupported`.

## Forecast, scope, validation

- **Forecast.** All 22 paths sit inside the write forecast (`.`): 17
  modified and 5 created (`win/peep/events.py` and four test files).
- **Out of scope, untouched:** no rendering or concatenation, no
  GIF/WebM/crop/presets, no dialog toggles, no Recycle Bin, no dshow
  changes, and no mouse hook (a proposal below). `wasapi.py` (including
  A.2's stdin-reader proposal), `ffmpeg_cmd.py`, `winapi.py`, `lifecycle.py`
  and `doctor.py` are byte-identical, and `validation.sh` asserts it.
- **No new stdin reader.**
- **`validation.sh`.**
  - It runs the suite capped at 900 s, plus nine session assertions and the
    exec-bit check (`bin/peep` changed, so `apply.sh` restores its bit).
  - I ran it on the changed tree (all pass, under `unshare -rn` too) and on
    the unmodified tree. There, the eight assertions that test the change
    fail and the two pins pass, by nature: the byte-identity of the
    untouched modules and the exec bit.
  - It writes only `.validation-logs/<stamp>/`. Its Python snippets run with
    `-I -B`, because `-I` ignores `PYTHONDONTWRITEBYTECODE`, and my first
    draft left `__pycache__` in the tree.
- **pyflakes** (run in my sandbox, not shipped) is clean on every file this
  session changed. That includes `recorder.py`'s old unused `FlashRecord`
  import, gone with the rewrite of that section.
- **Shapes used:** one paste-back probe; no light question block; no
  clarification.
- **`model_identity`** is the session's configured model identifier; this
  surface does not show me the serving model's string.

## Proposals

**C1b: render from `summary.kept`, and find the patches where the sidecar
says they are.**
- **What:** take `segments[]` in order and concatenate them by stream copy.
  Trim each one to its own magenta and green flashes. Cut to
  `summary.kept`'s intervals. For each kept boundary, search ±1 s around the
  event's `media_s` for the patch: the median of `fiducial.rect`, scaled by
  `capture_size` to `output_size` when scaled. Then snap the seam to
  low-energy audio.
- **Why:** each event's `media_s` and fiducial stamp already place it within
  a few frames. The patch rect is recorded in physical pixels, and its
  colour encodes open, close or retake, so detection doesn't need the
  sidecar to tell them apart.
- **Scope hints:** read-only on `peep.sidecar/2`; call `as_v2()` so `/1`
  recordings render too.

**Take, pause and retake keys in the `peep rec` terminal.**
- **What:** `t`, `p`, `r` in the `rec` terminal, like `q`.
- **Why:** it is the terminal-recording friction point: today, from WSL, you
  need a second tab for `peep take`.
- **Scope hints:** this needs a small stdin protocol. Today the child treats
  any bytes as a stop (A.2's ratified edge). Lines like `take\n` would have
  to be parsed, still on A.2's raw-fd reader. The changes would land in
  `bin/peep` `run_rec`'s key map and `cli.watch_stdin`.

**Mouse buttons, the cheap way first.**
- **What:** before any low-level mouse hook (`WH_MOUSE_LL`, which runs on a
  thread with a message loop and must answer within ~300 ms or Windows
  unhooks it), map a side button to F13/F14 in the mouse vendor's software.
  Bind `take_hotkey = "F13"`.
- **Why:** this session already allows bare F13–F24, and the probe found
  F13–F15 free. It is zero code.
- **Scope hints:** if the vendor software can't do it, a hook would live
  beside `hotkeys.HotkeyListener`, feeding the same `on_hotkey`.

**`peep ls` and `peep open` for multi-segment recordings.**
- **What:** show `2 seg · 3 takes` in `ls`. Make `peep open` play every
  segment in order (a temporary `.m3u` playlist) until C1b produces the
  concatenation.
- **Why:** today a paused recording opens at segment 1 only.
- **Scope hints:** `cli.cmd_ls`/`cmd_open`, and the catalog's `segments`
  field.

**A.2's open proposal, still open:** the WASAPI child's stdin reader on its
error exits. This session did not touch it, as the brief said.
