# notes — 2026-10-07-correction-and-pill-003 (peep-peep session C1c)

Everything in the brief's scope list is built, with tests. The suite has
710 tests: the 645 from before plus 65 new ones. It passes on Python 3.12.3
(your WSL version) and on 3.13. `validation.sh` passes in a staged copy
under a pty in 2 min 11 s.

**Before the smoke steps:** run `peep agent restart` (it syncs and loads the
new code), then follow README steps 70–79.

## Old sidecars: nothing to migrate

Scope item 1 asked which way this goes. **The event record already stores
resolved boundaries**:
- each take's `open` / `close` position and `discarded_by`;
- the summary's `kept` intervals.

The render reads only those, plus each event's fiducial. It never re-reads
press semantics, so a sidecar written under C1a's retake rule renders
exactly as recorded.

**How this is proven.** `tests/render_fixtures.py` carries a frozen copy of
C1a's model (`C1aRetakeModel`, `rule="c1a"`). Thirty random C1a sidecars
render their recorded `summary.kept` with no fiducial frame, and one
validation assertion checks the same.

**Telling the rules apart.** New sidecars say `take_rule: "correction/1"`. A
missing key means C1a's rule.

**Existing cuts.** Their `source_digest` is unchanged, so a cut that was
current stays current.

## Decisions for you to ratify

Mechanism calls under AGENT.md §4. All earlier ratified decisions stand
except C1a's retake semantics.

1. **The chart lives in one function, `events.next_effect(state, kind)`.**
   - The model's Take and Correction handlers call it to decide each press.
   - The pill calls it on the take state the recorder publishes to
     active.json, to say what each key does next.
   - So the hint and the press cannot disagree. The tests hold the hint's
     prediction against the actual next press for every chart row and over
     300 random sequences (more than 1,000 presses checked).
2. **The debounce, read literally.**
   - **The rule.** A hotkey correction within `debounce_ms` of the previous
     accepted press is ignored. That press can be of any kind (take,
     correct, mark, pause, resume) and from any source (hotkey or WSL).
   - **What stays as C1a had it.** Take and pause keep their per-chord
     rule, and `peep correct` from WSL is never debounced itself.
   - **The consequence.** A *quick* double-tap of ⌫ does ① only, and the
     second tap shows `⌫ ignored — too fast`. Your "press ctrl alt backspace
     twice" works with a second between the taps.
   - **How I got here.** I first built it narrower (take and correct
     presses only); the review pointed out the chart says "the previous
     accepted press".
   - **If you'd rather have per-chord:** set `DEBOUNCE_AGAINST["correct"] =
     ("correct",)`. It is one line.
3. **"No take yet … or everything since the last take is already cut."**
   - Under the chart, every drop opens a fresh take. So after the first
     Take press there is always a current take, and the "already cut"
     clause has no reachable state.
   - The model treats "no current take" generically: the press is ignored,
     with nothing to correct.
   - **"Dropping every take means no cut"** therefore happens only when the
     fresh take ends up empty:
     - ⌫ while paused, then stop: `summary.kept` is `[]`, so no cut is
       rendered (C1b's path).
     - ①② then T at once: a near-empty take, which the render drops with
       C1b's "nothing left to render" message (see Proposals).
4. **Take numbers are ids, so dropped takes keep theirs.** The ratified
   feedback says `take 2 dropped · take 3 started`, so after a drop the pill
   can read `◉ take 3` while `2 takes` are kept.
5. **The sidecar** stays `peep.sidecar/2`, additively:
   - **A correction event** has `kind: "correct"`, plus `requested_as:
     "retake"` when the old name was used. It also has `action`
     (`moved-close` or `dropped-take`) and a `correction` block: the action,
     the take, the resulting `boundary` (its side and position), a moved
     close's `from` and `moved_s`, and the `undo` stack after it. An ignored
     correction has `{"action": "ignored", "reason"}`.
   - **Takes** gain `undo` and `superseded_closes`.
   - **Each press's fiducial** gains `segment`: where the patch was shown.
6. **active.json** gains `take_state` (with an `at` timestamp) and
   `feedback` (with `seq` and `at`).
   - An open take's time grows from `at` on the agent's own clock.
   - It does not grow from captured seconds: `recording_since` is stamped
     after the start flash, about 1 s off the model's frame clock, so the
     two would drift apart.
7. **The hint line follows the mockups, state by state:**

   | State | Hint shows |
   |---|---|
   | No take yet | `P pause` and `M mark` |
   | Take open | `P pause` |
   | Take closed | Take and ⌫ only |
   | Paused | `P resume · nothing is recording` |

   - While paused, T and ⌫ still work at the boundary (C1a decision 7); the
     hint just does not offer them.
   - **Fitting it in.** The hint is capped at 44 characters: trailing keys
     go first, then the text is cut with `…`.
   - **Labels.** They come from the configured chords. Two chords on the
     same key are shown in full.
8. **Feedback, for every press, accepted or ignored.**
   - Take, correct and mark presses read as in the mockups.
   - Pause and resume read `P pausing…` / `P resuming…`, and only while that
     transition lasts.
   - Take boundaries pressed while paused get `· at the resume` or `· at the
     pause`.
   - With `pill_hints = false`, the feedback appears as a second line.
   - I kept B's `◆ mark` toast (B's ratified behaviour), so a hotkey mark is
     announced twice (see Proposals).
9. **Concealment now covers every stray patch.** This extends C1b decision
   6. A patch that is not the fiducial of a kept edge, in the segment where
   it was *shown*, is hidden the way mark patches are. The recorder now
   records that segment. For older sidecars the render derives it from
   `shown_qpc`.
10. **The rename.**
    - **Config keys.** They are `correct_hotkey` and `correct_color`.
      `retake_hotkey` and `retake_color` are still accepted, and setting both
      spellings is an error.
    - **`peep config`.** `set` and `unset` accept either name, and rewrite an
      old-name line under the new name with its comment kept.
    - **Commands and requests.** `peep retake` is `peep correct`, control
      files write `correct`, and the agent maps a `retake` action.
11. **The paused line uses the mockup's spacing:** `❚❚ PAUSED 03:24`, one
    space, where C1a had two.

## Look closely at

- **`events.py`, `_correct` and `_new_take`.** "The previous one is final"
  works by clearing every earlier take's `undo` when a new take opens.
- **`render.py`, `build_plan`.** Look at `shown_in`, `event_fiducial`, and
  `stray_patches`. Edges own a patch per (event, segment); this is the
  review's fix.
- **`pill.Overlay`.** It now has a second label, packed only when there is a
  second line.
- **`agent._fresh_feedback`.**

## Uncertain (so you don't inherit false confidence)

- **The Tk overlay was not run.** My sandbox had no tkinter this time, so
  the two-label pill has never been drawn. Its geometry comes from Tk's
  requested size, exactly as before. ★ Smoke step 78 checks two things: that
  the larger pill is absent from the recording, and that it stays in its
  corner, clear of the patch. A unit test covers the arithmetic up to a
  900×200 px pill on three screen sizes.
- **The pill's width on your screen.** I estimate about 500 px at 150 %
  scaling for a 44-character hint. Tune `HINT_MAX_CHARS` after step 78 if it
  looks wide.
- **The upgrade window.** Take a terminal `peep rec` started *before* the
  sync. A `peep correct` typed *after* it writes `correct`, which that old
  recorder records as `unknown-kind` and ignores. `peep agent restart`
  covers the agent's own recordings.

## An independent review before shipping

A separate agent that had not written the code reviewed the diff, in two
passes. Every finding is fixed and pinned by tests.

1. **(high) Presses made while resuming.** These are presses after the
   resume press but before the next segment's first frame.
   - The model places them in the pause, but the recorder handles them once
     the new segment is live, and shows their patch just after its start
     flash.
   - Nothing excluded or concealed that patch, and the fixtures could not
     produce the case.
   - It predated C1c for a take opened then, but corrections made it more
     likely.
   - **Fixed:** each fiducial records the segment it was shown in; the
     render places patches by it; the fixtures model these presses; both
     fuzzers include them.
2. **(medium) Pause and resume showed no feedback.** Accepted pause and
   resume presses published none. Fixed.
3. **The debounce reading** (decision 2). I switched to the literal reading,
   and rewrote a test that could not tell the two readings apart.
4. **(low) Stale feedback after a restart.** An agent restarted mid-recording
   replayed the last press's feedback as if it were new. Feedback now
   carries `at`; anything older than 2.5 s when first seen is dropped.
5. **(high, second pass) Presses held over a pause.**
   - A press made after the pause was accepted, but before the segment
     ended, is handled after the next segment starts.
   - The model places it in segment k; its patch shows in k+1.
   - A mark's patch leaked silently. A take's did not leak, but logged a
     misleading "not found" fallback.
   - **Fixed:** edges own patches per segment, a mark shown elsewhere is a
     stray, and an edge is bare in a segment where its patch did not appear.
6. **(minor, second pass) A stale "pausing…".** The line stayed after the
   pause had happened. It now expires with the transition.

**What the reviewer confirmed.** On 120 random cases, mixing presses inside
segments, while paused, while resuming and held over pauses, under both the
new rule and C1a's, no fiducial leaked silently.

## Tests changed outside the new files

- **`test_events.py`.** C1a's `RetakeTest` became `RetakeAliasTest`: the
  alias, plus the chart's debounce. Its other tests use the `correct` kind.
- **`test_segments.py`.** `TakeRetakeTest` became `TakeCorrectTest`, now with
  ①②, nothing to correct, a correction while resuming, and pause/resume
  feedback through the real recorder.
- **`test_c1a_surface.py`.** The rename, the CLI's feedback line, and the
  paused spacing.
- **`test_render_plan.py`.** The two tests that encode C1a's rule now build
  their sidecar with `rule="c1a"`, as recordings made then were.
- **`test_hotkeys.py`** (the table) and **`test_config.py`** (a comment).
- **`render_fixtures.py`.** Extended, as described above.

C1b's fuzzers in `test_render_detect.py` are unchanged. Their `retake`
presses now mean correct, so they fuzz the chart too.

## Forecast, scope, validation

- **Forecast (`.`).** All 24 paths are inside it: 21 modified, 3 created.
- **Out of scope, untouched.**
  - There is no A/V latency compensation; `validation.sh` greps for
    `audio_shift_ms` and `mpv`.
  - Nothing from C2's queue was started.
  - Forty-one files are pinned byte-identical, including `wasapi.py`,
    `ffmpeg_cmd.py`, `winapi.py`, `lifecycle.py`, `doctor.py`, `flash.py`,
    `dialog.py`, `ui.py` and `naming.py`.
- **`validation.sh` runs:**
  - syntax checks;
  - the suite, capped at 900 s;
  - six session assertions, including the chart and the mockups compared by
    sha256;
  - two pins, the exec bit (`bin/peep` changed, so `apply.sh` restores it),
    and the claims reconciliation.

  Stdin is on /dev/null throughout, and the only writes go to
  `.validation-logs/`. On the unmodified tree, the six assertions and the
  syntax check fail (the new test files are missing there). The suite and
  the pins pass, by nature.
- **pyflakes** is clean on every file this session changed. Two older
  warnings remain, in the untouched `tests/__init__.py` and
  `tests/fake_ffmpeg.py`.
- **Shapes used:** no probe, no light question block, no clarification. The
  brief and the C1a and C1b notes carried every fact the work needed.
- **`model_identity`** is the session's configured model identifier. This
  surface does not show me the serving model's string.
- **No compaction** occurred.

## Proposals

**"Nothing left" should not look like a failure.**
- **What:** when every kept interval is shorter than `min_interval_ms`
  (①②, then Take, as the only take), report "no cut: nothing is kept" as
  information. Today it gets the render-failure toast, which says a retry
  will fix it.
- **Why:** under the chart this is a deliberate way to throw a take away.
- **Scope hints:** `render.Renderer.render`, `RenderQueue`'s notify,
  `agent.on_render_event`.

**One announcement per mark.**
- **What:** drop B's `◆ mark` toast while the pill is shown.
- **Why:** the pill's feedback (`M mark 2`) says the same thing.
- **Scope hints:** `agent._on_mark`.

**Re-render recordings that had presses while resuming or held over a
pause.**
- **What:** C1a-era cuts are still "current", but may carry a patch from
  that case. `peep render <stem> --force` applies the new concealment.
- **Why:** this is the review's first finding, which predated C1c.
- **Scope hints:** it fits C1b's backlog proposal (`peep render --all`).
