# notes — 2026-10-07-render-002 (peep-peep session C1b, the render)

Everything in the brief's scope list is built, with tests, including the
optional clap calibration. That part ships measure-only by default; see
decision 9.

`peep render`, and the automatic render after every stop, write
`<stem>.cut.mp4` beside the original, and the original is never opened for
writing. The cut:

- joins the segments;
- trims each segment to its own flashes;
- keeps only the takes, with their patches;
- trims silence and snaps every seam;
- crossfades the audio at every join;
- turns marks into chapters.

The sidecar gets a `render` block recording every decision.

The suite has 645 tests: the 475 from before plus 170 new ones. It passes on
Python 3.12 and 3.13, under a pty inside `timeout` (the shape that held the
first attempt), and inside `unshare -rn`, where the one loopback-TCP test
skips as before.

**Before your first render:** `peep agent restart`. It syncs the new code and
starts the agent's render queue. Then follow smoke steps 60–69.

## Re-attempt after the HOLD (this response corrects 2026-10-07-render-002)

**What held.** In your validation, the suite ran past its 900 s cap inside
`RealFfmpegTest.test_the_cut_is_exact`. That test was the only one that ran
the sandbox's real ffmpeg 6.1, and the only thing that changed is in how it
starts ffmpeg; the render itself is unchanged.

**Root cause, reproduced here.**
- `timeout 900 python3 -m unittest ...` runs the suite in a background
  process group. Under a pty, my first run hung too, at exactly that test.
- The test's synthesis ffmpeg was started without `-nostdin` and with
  stdin inherited from the terminal. ffmpeg touched the terminal, and the
  kernel answered with SIGTTOU to the whole process group.
- Python, waiting on it, was stopped as well (`ps`: both in state `T`). That
  is why the call's own 120 s timeout never fired, and the run sat until
  `timeout` killed it.
- My earlier runs passed only because stdin was not a terminal, or the
  suite ran in the foreground.

**Fixes, following the planner note.**
- **Every ffmpeg says `-nostdin` and gets stdin on /dev/null.** This covers
  the render's four commands (encode, frames, PCM, volumedetect) and the
  test synthesis. New test: `NoTerminalTest`.
- **`RealFfmpegTest` runs as a bounded child.** The scenario moved to
  `tests/real_ffmpeg_scenario.py`, a new file that discovery does not pick
  up as a test. The test runs it as a child process:
  - in a session of its own, with no controlling terminal to be stopped by;
  - with stdin on /dev/null;
  - with a hard 120 s cap that kills the whole process group (checked: with
    the cap forced to 1 s it fails in 1.1 s and leaves no ffmpeg behind);
  - with a 60 s timeout on each ffmpeg call inside it, and the stall
    watchdog lowered to 30 s;
  - with `-y` to a fresh path for synthesis (the placeholder is removed
    first); the render keeps `-n` to a fresh part file.
- **Nothing is spawned at import.** The probe for ffmpeg, ffprobe,
  libx264/aac and the 19 filters the cut uses runs when the test runs. If
  anything is missing, the test skips and names it; ffmpeg 6.1 has
  everything.
- **The test stays in the default suite** and takes about 4 s. Nothing needs
  minutes, so nothing went behind `--slow`.
- **`validation.sh` runs every check with stdin on /dev/null.** This guards
  against the same failure for anything it starts.
- **Encode pipes are closed on every path.** An exception mid-encode killed
  ffmpeg but left its pipes to the garbage collector. That is the
  `ResourceWarning: unclosed file` in your log, which surfaced under the next
  test. The pipes are now closed on every path, and the suite logs no
  ResourceWarning.

**Verified, after the fix.**
- The suite exactly as `validation.sh` runs it (`timeout 900`, under a pty):
  645 tests, OK, 110 s. `RealFfmpegTest` ran and passed; it did not skip.
- The suite also passes on Python 3.13, and under `unshare -rn` with stdin
  on /dev/null.
- `validation.sh` in a fresh staging copy under a pty: exit 0, 1 m 56 s.
- On the unmodified tree: exit 1, the six change assertions failing as
  before.

**Compaction disclosure (AGENT.md §11.6).** The runtime compacted this
session's context while I was working on the re-attempt. Before shipping I
re-grounded from the durable side, not from the summary:
- I re-read the request manifest (goal, constraints, out of scope) and
  TARBALL.md §§5.1–5.4, 7, 8 and 10. I re-read AGENT.md §§11.6–11.7.
- I worked from the response already on disk.
- `changes[]` sizes and hashes were recomputed by the crafter from the bytes
  in files/, not carried over. `feedback.mechanical` was re-emitted by the
  lint.
- `claims` keys were re-checked against `validation_will_run`.
- The staging runs above are this session's own, after the compaction.

## What the probe established (relied on)

One paste-back probe ran: 89 lines, and the integrity trailer matched.

- **Environment.** HEAD `1b05db1`. The tree is clean apart from two untracked
  telemetry JSONs. ffmpeg is 9.0.2 (Gyan full build), and every filter the
  render uses is present. WSL Python is 3.12.3; Windows Python is 3.12.10.
  `config.toml` sets only `[flash] duration_ms = 200`.
- **No C1a recording exists yet.** All 12 recent sidecars are
  `peep.sidecar/1`, so there was nothing with corner patches to measure.
  **The patch colours are still unmeasured**; smoke step 62 measures them.
- **Flashes as encoded.** The probe used the 29-minute `brave-…-free2`
  recording.
  - **Magenta:** exactly 6 frames at (253,0,252), first frame at 0.800 s,
    38 ms after its stamp.
  - **Green:** 6 frames at (0,254,0), 48 ms after its stamp.
  - The frames either side of each flash are clean, with no blended frame.
  - Video and audio both start at 0.000, so frames sit exactly on k/30.
- **Seeking.** `-ss` with `-copyts` keeps the original timestamps. Every
  detection window relies on this.
- **The clap.** The 1 kHz onset came 5 ms before the first magenta frame.
- **Levels.** The 10 ms RMS percentiles of that recording (a film, not a
  voice) were p5 −62, p50 −42 and p95 −24 dBFS.
- **Speed** (60 s of 2560×1600, to the null muxer):

  | Path | Speed |
  |---|---|
  | software decode + `h264_qsv` | 6.0× real time |
  | the cut + concat + `acrossfade` graph through QSV | 6.3× |
  | the same graph through x264 | 2.7× |
  | decode only | 19.7× |
  | **QSV decode** (`-hwaccel qsv`) | **failed** (−22) |

  So the render decodes in software and encodes with QSV. A 10-minute
  recording renders in about 1.5 minutes.
- **`harness3.py` is gone.** Neither `~`, the repo nor the Windows home has
  it. The onset detector is new, built on the same shape A.1 described (a
  band-pass at 1 kHz with a 60 ms hold).

## Decisions for you to ratify

Mechanism calls under AGENT.md §4. All earlier ratified decisions stand.

1. **Full re-encode, in one filtergraph per render.**
   - The picture: each piece is trimmed on its own frame grid, the pieces are
     concatenated, and the result goes to the original's encoder (`ffmpeg_cmd`
     choosing QSV, A #1).
   - The audio: each piece is atrimmed, padded to its exact length, and the
     pieces are chained through `acrossfade`.
   - The probe ran this graph at 6.3×. In the sandbox's real ffmpeg 6.1 it
     ran with up to 9 pieces, concealed marks and filler tracks.
   - I did not stage per-interval intermediates: they would add disk churn
     and gain nothing measurable.
   - If QSV fails, the whole graph is re-encoded with libx264. It says so,
     and the sidecar records both attempts.
   - The output keeps the original's resolution, frame rate, encoder
     settings and track layout. Colour tags pass through from the decoded
     frames, so the cut carries what the original carried (the probe saw
     `iec61966-2-1` there). It adds `-movflags +faststart`.
2. **Every boundary is found in the video.**
   - **Where to look:** ±`search_s` (1 s) around the sidecar's stamp. Flashes
     use a 16×10 average of the whole frame; patches use a 4×4 median of the
     patch rect, inset 15% and scaled by `screen` to the output size.
   - **What counts:** a frame is the colour when every channel is within 70.
     A run is refused if it is:
     - longer than the fiducial could be (the corner is that colour anyway);
     - touching the edge of the search window;
     - more than 300 ms from a QPC stamp (it is another fiducial).
   - **The kept edges:** kept material starts at the frame after the last
     coloured frame, and ends at the frame before the first.
   - **Fallback:** the stamp, erring toward cutting more. A start uses the
     stamp + 40 ms + the flash's length + 100 ms; an end uses the stamp itself
     (the first frame comes later).
   - **Never silent:** the result says `N fallback(s)`, the log has
     `render.boundary_fallback`, and the sidecar records the reason.
3. **A segment's flashes are excluded from every piece**, whatever the take
   boundaries say. A take pressed during a flash, or a stamp that is wrong,
   can never carry magenta or green into the cut.
4. **Seams.**
   - **Where they apply:** at every take edge and every join (pause). The
     outer edges of a zero-take recording are trimmed to its flashes only, as
     the brief asks.
   - **What moves them:**
     - silence is trimmed, keeping 250 ms of it, at most 1 s;
     - with no silence, the seam snaps to the quietest 10 ms within ±300 ms;
     - a take that never rises above the threshold (a silent walkthrough) is
       never trimmed.
   - **Never backward:** a seam only moves into the kept side and never past
     a piece's middle, so trimming can never re-admit a fiducial or discarded
     material. The brief's "±300 ms" is therefore one-sided at a take edge;
     the other side is the patch.
   - **Picture and audio:** the picture cuts hard on a frame. The audio
     crossfades over 40 ms, centred on the cut: each side is read 20 ms
     further and the overlap removes them. Every piece is padded to its exact
     length, so the cut's audio is exactly as long as its picture. Tested for
     that invariant, and frame-counted on real ffmpeg.
5. **The frame grid is measured, not assumed.** The recorder's own files have
   their first frame at 0, but an MP4 remuxed by another tool can start at
   0.021 s. The independent review showed that at 60 fps such a file would
   gain or lose a frame at the edges. Each segment's grid phase is read from
   the frames detection decodes, and frame times come from showinfo's integer
   `pts` and time base, which stay exact past 1000 s.
6. **Marks.**
   - **Chapters.** Marks become chapters, using ffmetadata and
     `-map_chapters`, titled with the label or `mark N`. A mark pressed while
     paused starts its chapter at the resume. One pressed less than 1.5 s
     before a piece starts that piece's chapter (a mark pressed with the take
     press). Marks at the same instant share one chapter, and marks in cut
     material are listed in `marks_excluded`.
   - **Their patches are concealed.** This goes beyond the brief: it asks to
     exclude take patches, but a cyan square blinking in the corner of a
     finished tutorial is exactly a patch reaching the cut. For the patch's
     frames, the corner shows what it showed the frame before, using
     `freezeframes` + `crop` + `overlay` with `enable`. With
     `mark_style = "full"`, or for a session-B `/1` mark, the whole frame
     holds.
   - **At a piece's edge,** a patch is cut off that edge, or the piece is
     dropped if almost nothing else is left. Both are recorded.
7. **When is a cut current.** The `render` block says ok, its
   `source_digest` matches, and the file has the recorded size.
   - The digest covers the segments' timing and flashes, the events, takes,
     kept intervals and marks. It leaves out file names, so a rename keeps the
     cut current.
   - **`peep open`:**
     - it plays a current cut;
     - otherwise it plays the original, and says why the cut was not used;
     - `--raw` always plays the original;
     - an unrendered recording of several segments plays every segment in
       order, as a playlist written to `state\playlists\<stem>.m3u` (not
       into the Videos folder).
8. **The render lock, `<stem>.cut.lock`** (C1a's namespace).
   - **While it is held,** rename and discard refuse with "is being
     rendered", because ffmpeg is still writing into the recording's family.
   - **A stale lock** (its process is dead) is taken over loudly, and any
     leftover part file is removed.
   - **Rename** moves the cut with the recording and updates `render.file`.
   - **A `/1` sidecar** gets a `render` block and stays `/1`.
9. **Clap calibration: measured always, applied only with
   `render.av_calibration = "apply"`. This is a flagged deviation.**
   - **What is measured.** The residual is the 1 kHz onset relative to the
     first magenta frame, minus what the stamps predicted.
   - **Why not apply it.** The tone reaches the loopback 45–110 ms after it
     is requested (A.1's measurement on FxSound). That jitter is larger than
     the half-frame error the correction targets, so applying every measured
     residual could make sync worse.
   - **Where it goes.** The residual is recorded per segment in
     `render.calibration`. Smoke step 69 asks for a few values. If they sit
     consistently past ±33 ms, one config key turns correction on, as a
     per-segment shift of the audio window.
10. **Auto-render UX** (`render.auto = always` by default, as briefed).
    - **Agent recordings.**
      - The render starts after the dialog's Enter or Esc, or straight after
        the stop when `dialog = false`. A discard renders nothing.
      - It runs on the queue's own worker thread, one render at a time, in
        order, at below-normal process priority.
      - Toasts mark the start, the finish (or `N fallback(s)`) and a failure
        ("the original is kept; `peep render` retries"). Progress shows in
        `peep agent status`; I did not add it to the pill.
      - Stopping the agent cancels the render in progress and drops the queue
        (logged). `peep render` picks those up.
    - **Terminal recordings** render in the `rec` process after `✓ saved`,
      printing every 10%. `--no-render` skips that. If only the render fails,
      the exit code stays 0, because the recording saved; the message says
      `peep render` retries.
    - **When nothing renders:**
      - `render.auto = takes` renders only recordings with takes or pauses;
      - a recording whose every take was discarded never renders, whatever
        the setting (there is nothing to cut).
11. **Silence defaults: −45 dBFS, keep 250 ms, trim at most 1000 ms.** These
    are reasoned, not calibrated. The probe only had a film to measure, not
    your voice. Smoke step 63 is the check, and the troubleshooting section
    says which key to turn.

## Look closely at

- **`render.resolve()` and `_conceal_marks()`.** This is where the edge
  cases live:
  - the flash clamp;
  - the edge trim and drop for marks, which rebuilds the piece list and
    remaps the concealment entries to the new piece numbers;
  - seams and records kept in step with the final pieces.
- **`Renderer._encode`.**
  - It kills ffmpeg in a `finally` when leaving on an exception or a cancel.
  - The CLI's renders run in a kill-on-close job, so a killed `peep render`
    never leaves an encode running. The agent's renders do too.
  - Any progress line counts as a sign of life for the 300 s stall watchdog,
    because ffmpeg reports `out_time_us=N/A` while trim decodes up to a late
    first piece.
- **`os.replace(part, cut)` on Windows.** It refuses when the old cut is open
  in a player, and the render then fails with that reason (smoke step 66).
- **The agent's threads.** Queue notifications reach the UI thread only
  through `ui.post`. `maybe_render` runs on the UI thread, inside the dialog's
  result handler.

## Uncertain (so you don't inherit false confidence)

- **The patch colours through the pipeline.** Detection tolerates 70 per
  channel, and falls back loudly when a patch is not found. ★ Smoke step 62
  records the `measured_rgb`.
- **A recording and a render at the same time.** That is two QSV sessions, or
  QSV and x264 under load, on the Arc 140V. I expect it to be fine, since the
  render's priority is lowered. ★ Smoke step 65.
- **`freezeframes` (the mark concealment) in ffmpeg 9.0.2.** It is a
  standard built-in filter (in 6.1 here). If it were missing, recordings with
  marks would fail to render and say why. ★ Smoke step 64.
- **Chapters with `+faststart`.** They worked in 6.1 here (a QuickTime
  chapter track). I have not seen whether Windows' Media Player shows them;
  mpv does. ★ Smoke steps 64 and 68.
- **Segments of different sizes** (the display resolution changed during a
  pause). They are scaled to segment 1's size, with `setsar=1`, which
  stretches a different aspect rather than letterboxing it.

## An independent review before shipping

A separate agent that had not written the code reviewed the diff. It found 8
defects and reproduced 7 of them, the most serious on real ffmpeg 6.1. All
are fixed and pinned by tests.

1. **(high)** A take pressed during a segment's start flash, or closed after
   its stop flash, carried magenta or green into the cut. Fixed by decision 3.
2. Frame quantisation assumed frames at k/30. Fixed by decision 5 (the
   reviewer's 60 fps remuxed file now cuts exactly).
3. Session-B `/1` marks lost their chapters, and their full-screen cyan
   stayed.
4. Segments of different aspect ratios failed in concat. Fixed with
   `setsar=1`.
5. The stall watchdog could kill a healthy encode while ffmpeg decoded up to a
   late first piece.
6. A mark patch was looked for in the wrong piece when two pieces were close.
7. `peep render --dry-run` crashed when every piece had been dropped.
8. An exception mid-encode left ffmpeg running, and the CLI renders had no
   kill-on-close job.
9. **(minor)** A recording whose every take was discarded produced a render
   failure toast that a retry could never fix.

I then ran the reviewer's fuzzers. They turned up three more cases, now fixed:

- **A take pressed before a segment's first frame.** The model clamps it to
  0, but the recorder still shows its patch.
- **A mark patch at a piece's edge.**
- **Marks pressed together.** They make one long run.

The fuzzing also showed that my test fixture drew fiducials overlapping. The
recorder shows them one after another, and the fixture now does the same.

A second review pass of the fixes confirmed all of them, found no
regressions, and found four smaller issues, now fixed:

- a far same-coloured run could be taken for a hidden fiducial (the 300 ms
  rule in decision 2);
- the seam and dropped records went stale after a mark-patch edge trim;
- a mark pressed with the take press got no chapter (decision 6);
- showinfo printed `pts_time` with 6 significant digits, so detection lost
  precision past 1000 s.

**Final state.** About 2,000 fuzzed press sequences (takes, retakes and marks
anywhere, including at and before the flashes, across 1–3 segments) put no
fiducial frame in any cut. With deliberately overlapping fiducials, any that
remain are always reported. On real ffmpeg, every reproduction is clean:

- the cuts hold no fiducial colour;
- the frame counts are exact;
- the audio duration equals the video duration.

## Tests changed outside the new files

- **`tests/test_config.py`** (B's): one line. The template test lists the
  config sections, and `render` is a new one.
- **`tests/fake_ffmpeg.py`** (A's helper): extended, not changed. It now
  handles the analysis decodes, synthesised from a scene file the fixtures
  write, and the render itself, with its failure modes. Its capture and remux
  paths are untouched.

## Forecast, scope, validation

- **Forecast.** All 17 paths sit inside the write forecast (`.`): 9 modified,
  8 created (`win/peep/render.py`, `tests/render_fixtures.py`,
  `tests/real_ffmpeg_scenario.py`, and the five `tests/test_render_*.py`).
- **Out of scope, untouched.** No GIF/WebM/presets, crop, dialog toggles,
  Recycle-Bin discard, dshow changes, `rec` take keys, mouse hooks or WASAPI
  stdin reader. Smart-cut is a proposal only, and no pip package was added.
  `recorder.py`, `events.py`, `ffmpeg_cmd.py`, `wasapi.py`, `winapi.py`,
  `flash.py`, `lifecycle.py`, `doctor.py` and 9 more are byte-identical, and
  `validation.sh` asserts it.
- **`validation.sh`** runs:
  - syntax checks;
  - the suite, capped at 900 s (it takes ~110 s, real-ffmpeg check included);
  - seven session assertions;
  - the exec bit (`bin/peep` changed, so `apply.sh` restores it);
  - the claims reconciliation.

  Every check runs with stdin on /dev/null. It writes only
  `.validation-logs/<stamp>/`. I ran it on a staging copy with
  the change applied (all pass) and on the unmodified tree. On the
  unmodified tree, the six assertions about the change fail and the two pins
  (byte identity, exec bit) pass, by nature.
- **pyflakes** (run here, not shipped) is clean on every file this session
  changed.
- **Shapes used:** one paste-back probe; no light question block; no
  clarification.
- **`model_identity`** is the session's configured model identifier. This
  surface does not show me the serving model's string.

## Proposals

**Smart-cut: re-encode only the boundary GOPs.**
- **What:** stream-copy the middle of each piece, and re-encode only from
  each cut to the next keyframe. `-g 60` makes that at most 2 s per edge.
- **Why:** the full re-encode is ~6× real time, which is fine in the
  background but keeps the GPU busy. Smart-cut would turn a 30-minute
  render into seconds. The catch is that concat by stream copy needs the
  re-encoded boundary pieces to match the original's encoder parameters
  exactly (QSV's SPS/PPS), so it needs a probe of `h264_qsv`'s output.
- **Scope hints:** `render.build_render_argv` becomes several commands plus a
  concat-demuxer list. The plan and resolve stages are unchanged.

**Render the backlog, and resume renders the agent dropped.**
- **What:** `peep render --all [--since DATE]`, and an agent start-up scan for
  recordings without a current cut.
- **Why:** your 28 existing recordings are `/1` and have no cut. And a render
  dropped by `peep agent stop` is only logged.
- **Scope hints:** `cli.cmd_render`, and `Agent.start` using
  `render.cut_status`.

**Render progress in the pill.**
- **What:** a small `✂ 42%` while nothing is recording.
- **Why:** toasts mark the start and the end; a long render is quiet in
  between.
- **Scope hints:** `pill.pill_text` and `Agent._refresh_pill`, from
  `render_state`.

**For C2.**
- **Presets.** GIF/WebM presets should take the cut as their source when it
  is current (`render.cut_status`), and the original only on request.
- **Crop.** Per-collection crop can be one more filter after concat (crop
  before scale), recorded in the sidecar's reserved `crop`.
- **Dialog toggles.** The toggle row could carry "render: on/off" for this
  one recording.
- **Recycle-Bin discard.** It must take the whole `.cut.*` family, as
  `catalog.discard` does now.

**Letterbox instead of stretch** for a segment of a different aspect (see
Uncertain): `scale=…:force_original_aspect_ratio=decrease,pad=…`. This only
matters if you change resolution mid-recording.

**Calibration.** After a few smoke recordings, look at
`render.calibration[].residual_ms`. If they are consistently non-zero beyond
±33 ms, make `apply` the default, or move `audio.epoch_lead_ms`, which is
the cause, not the symptom.

**Housekeeping:** the probe found `probe-output` folders in `~/bale-src` and
`~/ironwood`. They are not this project's (A.1's were deleted), so I'm only
mentioning them.
