# Notes — 2026-10-08-auto-takes-001 (peep-peep C1d, auto-takes)

## Compaction

My context was compacted once, late in the session. The build, the tests,
both review passes and validation.sh were finished; I was filling in the
response manifest. I said so in chat. Following AGENT.md §11.6, I then:

- re-read the request manifest, AGENT.md §11.6, the brief, and TARBALL.md's
  core plus §8–§10;
- checked the `files/` mirror byte for byte against the working tree, and
  diffed the tree against the shipped `context/` (16 modified and 7 created
  files, nothing else);
- re-ran validation.sh against the files as packed, in a fresh staging copy
  built as a git repository (default mode 0 in 2m05s, `--slow` 0 in 3m44s,
  no `__pycache__` left behind), and the whole suite once more under
  Python 3.12.3 (813 tests, OK);
- recomputed the mechanical feedback with the lint.

Hashes and claims come from those runs, not from memory. The re-grounding
turned up two corrections:

- I had planned to report `compaction_occurred: false`. It is `true`.
- The README's example `auto_takes` ref id, `start-2`, predated the pid fix.
  It now reads `start-4120-2`, the real `<kind>-<pid>-<n>` shape. This is a
  documentation-only change.

## What the probe established

There was one paste-back probe, before building. Its output was 238 lines
and its integrity line was correct. The response relies on these facts:

- **Environment.** WSL2, repository at HEAD 5717899. Windows Python 3.12.10
  on Windows 11 with per-monitor DPI awareness. A single 2560x1600 monitor.
  ffmpeg 9.0.2.
- **GDI honours `WDA_EXCLUDEFROMCAPTURE`.** It behaves exactly like ddagrab,
  with or without CAPTUREBLT. An excluded window, and the pill's recipe
  (alpha 0.92, click-through, excluded), sampled as the background in all
  three captures. A layered window that is not excluded was visible in all
  three. So there is no pill-rectangle mask.
- **Cost.** A 64x40 HALFTONE sample costs about 33 ms wall and about 11.5 ms
  CPU per grab. Over 10 s at 4 Hz: 36 samples and 484 ms of process CPU,
  about 13 ms per sample. System busy was 12.8% while sampling against
  14.1% idle on 8 cores, so no measurable load. COLORONCOLOR is cheaper
  (6.8 ms) but coarser. I kept HALFTONE, which the brief named.
- **No cursor flicker** while sampling, with or without CAPTUREBLT.
- **Distances on real pages** (64x40 grey MAD; share of pixels changed by
  more than 24 levels):

  | Comparison | MAD | Changed |
  |---|---|---|
  | Same page, mouse hover effects | up to 4.9 | 3% |
  | Same page, mouse still | 0.0 | 0% |
  | Page scrolled three lines | 10.5–22.3 | 17–37% |
  | A different page | 166.5 | 86% |
  | A playing video tab against a page | about 90 | 71% |
  | Frames within the playing video | up to 2.2 | — |
  | GDI against ddagrab on the same page | 3.3–3.5 | 1% |

  These numbers set the defaults: a match at ≤ 8 and ≤ 8%, a clear miss
  above 12 or 15%, and a band between them that counts as neither. A
  three-line scroll is therefore a miss. The 3.5 cross-pipeline difference
  is why the render allows `screen_slack_mad/pct` (6/6) on top.
- **Video.** GDI and ddagrab agreed on a hardware-decoded browser video.
  The video was not DRM-protected ("video tab kind: no"), so the protected
  case is untested (smoke ★88).
- **Chords.** Ctrl+Alt+S and Ctrl+Alt+W are taken. Home, End, the brackets
  and Space were all free. Pressing Ctrl+Alt+Home and Ctrl+Alt+End needed
  no Fn key, but you reported them "comfortable: no".

## Decisions to ratify

1. **Learn chords `Ctrl+Alt+[` (start) and `Ctrl+Alt+]` (end):** an opening
   and a closing bracket. They are free per the probe, and Home/End were not
   comfortable. The README keeps the RDP note for anyone who rebinds to
   Ctrl+Alt+End. Ctrl+Alt+Space is refused by config for any key.
2. **Thresholds and timing.** The defaults are the numbers above, with
   hysteresis 2, 4 Hz and a 64-pixel-wide thumbnail (64x40 here). All are
   configurable in `[auto_takes]` and recorded with every reference in the
   sidecar. `learn_wait_ms` is 1000, with a range of 100..5000: a learn
   waits for the screen to hold still, and is flagged `stable: false` if it
   doesn't.
3. **Reading of the implicit take.** With no takes yet, nothing is open, so
   a start screen counts: learning or seeing one first cuts what came before
   it. The first end screen closes the implicit take, which becomes take 1
   with `implicit: true`. A recording with neither is unchanged.
4. **Guards beyond the table**, from the order events reach the model. All
   are documented in the README.
   - A press wins over a screen that reaches the model after it
     (`— a press came first`).
   - A start screen giving way straight to the end screen opens no take
     (`— the end screen is up`). Same-sample changes are ordered so the end
     screen's arrival comes first.
   - An automatic open that an end screen learned on that same page
     overtakes closes empty.
5. **Learning is refused while paused**, since there is nothing to capture.
   A learn of a screen that matches the other kind's reference is refused by
   the agent (`⇥ not learned: that is the end screen`).
6. **Learn presses and screen events are not take-key presses.** They do not
   open C1c's debounce or too-fast windows, so ⌫ right after an automatic
   close works at once.
7. **The learned-on appearance is followed back to where it began.** This
   goes past the brief's "±1 s of the event", which fits live sampler
   events, not a screen that was already up when learned. The walk is
   bounded by the take's own start and by `render.screen_lookback_s`
   (120 s).
8. **Refinement fallbacks.**
   - If the live reference matches no decoded frame (another colour
     pipeline), the render self-anchors on the video at the point the screen
     is known to be up, corrected for capture lag. A plausibility bound
     follows from the sample period.
   - If even that fails, it falls back to the sample time, erring toward
     cutting more. The fallback is recorded in `render.fallbacks` like every
     C1b fallback. For a learned-on screen the line also says that its
     earlier frames may remain.
9. **Sidecar stays `peep.sidecar/2`, additively.** The `auto_takes` block,
   the learn/forget/screen events and the `implicit` flag appear only when
   something was learned. That is also what keeps never-learned recordings'
   source digests and cuts byte-identical.
10. **Reference PNGs live only while the recording does.** They are written
    to `<state>/auto-takes/<uid>.<ref>.png`, cleared at claim and at the end.
    Saved references are out of scope. The sidecar carries the thumbnail as
    hex.
11. **Plumbing.** Learn and screen requests travel through the existing
    control files, and the agent confirms a learn through active.json
    (5 s timeout). Reference ids carry the agent's pid
    (`end-<pid>-<n>`), so a restarted agent cannot reuse one.
12. **Control request file names gained a sequence number.** This was not on
    the brief's list but sits directly in the path. Two requests within
    Windows Python's ~15.6 ms clock tick could share a name; a learn
    followed at once by a screen event made that real. Old names still
    parse.
13. **validation.sh runs the C1d modules and their nearest suites by default**
    (about 2 min). The whole suite runs behind `--slow` (about 3.5 min).

## Deviation from a ratified decision

C1c's ratified hint-line mockups gain learn-key tails, such as
`] end screen`, or `] end` when space is short. Scope item 5 asks for this
("the hint line names the two learn keys where they fit"). I updated C1c's
mockup expectations in `tests/test_c1c_pill.py` and the README mockups.
Nothing else on the pill changed.

## Look closely at

- `render.py` `_resolve_screen` / `screen_edge`: the anchor, the run walk,
  the self-anchor plausibility bound and the fallbacks. It is the most
  intricate part, and the two review passes found real bugs there (below).
- `events.py` `_screen_effect` and the model's `_appearance` /
  `_opened_by_a_screen`: the guards and the empty close.
- The never-learned proof, which has two parts.
  - A golden digest of 120 random never-learned recordings' cut decisions,
    computed with the pre-C1d modules and pinned in
    `tests/test_auto_render.py`.
  - When git is reachable in staging, validation.sh also runs the same cases
    through HEAD's modules and compares.

## Independent review

Two review passes were run against the diff, each by an agent that hadn't
built it. Every finding was fixed, with a test.

- **Pass 1**
  - Reference ids collided across agent restarts.
  - Control file names collided (decision 12).
  - The refinement could leak a reference frame when the reference didn't
    match.
  - A start screen giving way to the end screen opened a take.
  - Thumbnail size mismatches went unseen. They are now counted in the
    sampler stats, with one toast.
  - Quiet ignored events wiped the pill's feedback line.
  - `learn_wait_ms = 0` always reported "moving".
- **Pass 2**
  - The self-anchor ignored capture lag. The new test fails on the old code.
  - A learn arriving after the start screen's "gone" was mishandled.
  - A same-sample switch could make an empty take.

Beyond the 150-case in-suite fuzz, the extended fuzzer ran 1,500 cases, then
5 seeds × 400 cases (about 3,500 screen edges). No reference or fiducial
frame reached any cut.

## Uncertain (needs the laptop)

- ★86: the sampler's cost alongside a QSV capture and a background render.
  The probe measured it idle.
- ★87: tolerance on your own pages (a clock, a caret, two similar pages of
  one site).
- ★88: DRM-protected video.
- The Tk pill itself wasn't run here. Its lines are tested as strings, as in
  C1c.

## Tests outside the new files

Four existing test files changed:

- `tests/test_c1c_pill.py`: the learn-key tails and labels.
- `tests/test_config.py`: `[auto_takes]` and the render settings.
- `tests/test_hotkeys.py`: the learn chords and the Ctrl+Alt+Space refusal.
- `tests/render_fixtures.py` and `tests/fake_ffmpeg.py`: screens in scenes,
  and thumbnail decodes.

No existing assertion was loosened. The suite is 813 tests, OK on Python
3.12.3 and 3.13 under WSL.

All 23 paths are inside the write forecast (`.`). There were no forecast
departures.

## Exchanges

One paste-back probe, before building. No light question block and no
clarification.

`model_identity` is `anthropic:unknown`: this surface doesn't show me the
serving model string. The session was configured for claude-opus-5-5.

## Proposals

- **Saved named references across recordings.**
  - What: learn an outro card once and reuse it in every recording.
  - Why: every recording now relearns its screens, and the PNG and hex
    thumbnail already exist to be kept.
  - Scope hints: `screens.py` (PNG), a references folder under the state
    dir, the agent's adopt path. C1e's expanded pill is the natural place
    to pick one.
- **A per-reference tolerance, or a mask.**
  - What: let a reference carry its own thresholds, or ignore a rectangle
    (a clock, a live counter).
  - Why: the probe's pages separate cleanly, but a page with a large
    changing region would need looser global thresholds that risk matching
    similar pages.
  - Scope hints: `Thresholds` is already stored per reference, so this is
    mostly UI (C1e) and config.
- **DRM handling, if ★88 shows black.** If protected video samples black in
  both pipelines, a black end screen would match any black frame.
  - What: refuse learning a near-uniform thumbnail, with a pill line.
  - Why: such a learn would match any black frame.
  - Scope hints: `stable_capture` already computes the thumbnail.
- **For C1e.** active.json's `auto_takes` block (README, "For the expanded
  pill") has everything the expanded pill should need per kind: learned,
  ref, thumb path, seen, last seen, last effect, present, pending. The
  sampler's own state (running, rate, CPU per sample) is in
  `peep agent status --json`, not in active.json.

  The thumbnail PNG is grey and 64 px wide, so C1e will want to scale it up
  with nearest-neighbour.
