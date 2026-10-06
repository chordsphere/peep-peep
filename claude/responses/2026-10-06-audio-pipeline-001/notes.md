# notes — 2026-10-06-audio-pipeline-001 (peep-peep session A.1)

## Re-attempt: why the first apply hung, and what changed

The first tarball's `validation.sh` sat for over an hour until it was
interrupted. A read-only probe (2026-10-06 20:28 UTC) found it inside B's
`tests/test_marks.py::test_mark_pressed_just_before_stop_is_kept`, blocked
in `t.join()`.

**The chain:**

1. Bale runs `validation.sh` in a network-off namespace. There, binding
   127.0.0.1 works but connecting to it fails at once with ENETUNREACH. The
   probe reproduced that on your machine with `unshare -rn`.
2. My audio tests used real localhost TCP between `fake_ffmpeg` and the fake
   capture child, so under bale every test recording failed to start.
3. B's driver thread in that test waits for "recording" status with no
   deadline. It never came, so the join never returned.
4. My `validation.sh` had no cap on the test run.

The suite passed outside the sandbox (three runs, 3.12 and 3.13), which is
why I didn't see it. I never ran it under a network-off namespace.

**Changed in this re-attempt:**

- **The tests use abstract Unix sockets.** The capture server's listener is
  injectable (`CaptureServer(listen=...)`, default `wasapi.tcp_listener`);
  `CaptureReady` carries the stream URL; `fake_ffmpeg` and the fake child
  speak `unix:@name`.
  - Production is unchanged: loopback TCP, exactly what the probes measured.
  - One test exercises the real TCP listener and skips inside the sandbox,
    with the reason printed (`tests.loopback_tcp_available()`).
  - The listener failing to open is now an `error` event and exit 4, where
    before it was an exception that left a dead thread.
- **Every wait in the suite is bounded.** B's `test_marks` driver and
  `test_cli`'s fake recorder get deadlines, and their joins get timeouts.
  These are the only two edits to B's test files, and B's modules stay
  byte-identical.
- **`validation.sh` caps the test run** at 900 s (it takes ~45 s) and runs
  unittest `-v`, so a failure names the test. The real-ffmpeg check skips
  where loopback is unavailable and is capped at 120 s.
- **How I checked:** the whole suite, under `unshare -rn` (the closest I can
  build to bale's sandbox), passes 350 tests with 1 skip. Outside, it passes
  350.

Two paths beyond the first attempt's change set, both inside the forecast:
`tests/__init__.py` (the loopback check) and `tests/test_marks.py` (the
bounded wait).

**The interrupted apply.** It left `bale/2026-10-06-audio-pipeline-001`
created at master's tip and no verdict recorded. That is why this is sent
for `bale retry` (or `bale apply`, if bale prefers); see the chat for the
exact steps.

Everything in the brief's scope list is built, including the optional clap.
The one exception is the dialog's `audio:` status line (deferred; see
Proposals).

The suite has 350 tests (B's 250 plus 100). It passes under unittest on
Python 3.12.3, your WSL version, and on 3.13, with no Win32, no ffmpeg.exe
and no audio devices.

The WASAPI COM code has run on the laptop, inside the probes: device
enumeration, loopback on both output devices, plain mic capture, and twelve
recordings through the TCP transport. The recorder integration has not.
README smoke-test steps 41–49 are the acceptance test for that.

**Before your first recording after applying this:**

```
peep config unset audio.offset_ms
```

Your config still has the `offset_ms = 500` from the sync tests. Under A.1
it would delay the audio by half a second. `peep doctor` flags it.

## What the probes established (relied on throughout)

The manifest expected a probe. I ran three, all file-based (TARBALL.md §4.4)
because they had to record:

- `./probe-output/` (6 recordings plus 2 stall tests);
- read-only re-analysis of those recordings (no new output);
- `./probe-output-3/` (4 recordings).

**Both folders are untracked in the repo root, about 20 MB. Delete them.**

### The sync5 forensics

- **The argv.** `-itsoffset 0.500` *did* land on the dshow input in the
  `sync5` argv: `... -thread_queue_size 1024 -itsoffset 0.500 -f dshow -audio_buffer_size 50 -i "audio=Microphone Array ..."`.
  The sidecar says `offset_ms: 500`. The earlier runs (`sync`–`sync4`) had
  no offset, as the brief said.
- **In the `.mp4`.** `sync5.mp4`'s audio track starts at 0.477 s, carried
  only as an MP4 **edit list**: an empty edit `(477, -1)`, then the media.
  The probe's own `-itsoffset 0.5` run showed the same thing: start_time
  0.500 in the `.mkv`, and after A's remux, start 0.477 via `elst [(477,
  -1), (7251, 0)]`.
- **The player.** Your `.mp4` and `.mkv` default is a Store app (ProgId
  `AppXqj98qxeaynz6dv4459ayz6bnqxbyaqcs`, presumably Media Player). A player
  that ignores a leading empty edit plays the audio from 0 and the +500
  vanishes, which fits "no perceptible change" exactly. I can't prove what
  that player does from here. The fix no longer depends on it either way:
  offsets now go into the samples or the anchor, never into timestamps.

### The lead itself, measured

Three of the probe-3 recordings ran the screen, A's dshow mic, our WASAPI
system audio and our WASAPI mic together, with a magenta flash and a 1 kHz
beep at the same instant. The WASAPI mic stamps (QPC) the moment each beep
reached the microphone, so the dshow track's lead is measured against
ground truth, not inferred.

| run | dshow mic lead (+ = voice early), per beep |
|---|---|
| q1 | +240.1, +431.1 ms |
| q2 | +247.7, +348.7 ms |
| q3 | +345.4, +974.4 ms (the second may be a misdetection) |

**The cause:** the dshow microphone track starts about 250–350 ms ahead of
the picture and loses ground while it records. It is short, not shifted:

- the mic tracks of the A-era `sync` files are 0.41–0.64 s shorter than
  their video;
- probe-1's dshow-only run is 0.36 s short.

So a constant `offset_ms` could never have fixed it; it would have matched
one moment of the recording at best. Of the brief's two hypotheses, the
"different epochs" one is closest. Each input's start is normalized
separately, and dshow's first sample comes ~300 ms after ddagrab's first
frame. On top of that, dshow's stream drifts. I don't know whether dshow
drops buffers or its clock disagrees with QPC. With WASAPI available, I
did not chase it further.

### The video epoch (what the alignment is built on)

ddagrab's first frame is captured **before** ffmpeg prints `Input #0`. The
first-frame time was recovered from the flash edges, both sides of each of
three flashes.

| | values (ms) | spread |
|---|---|---|
| `Input #0` line − first frame, probe 1 | 56.8, 62.4, 62.7, 57.5, 64.5, 59.3 | |
| same, probe 3 | 44.7, 57.2, 45.9, 67.5 | |
| pooled | mean 57.9 | σ 7 |
| ffmpeg launch → first frame | | σ 20, so not used |

ffmpeg prints `Input #0` before it opens the next input (so before it
connects to our port), which is what makes the anchor race-free.

### The production anchoring, measured end to end

Probe 3 used the anchor exactly as shipped (`Input #0` − 60.5 ms then;
58 ms now). The residual is the system track against the picture, and the
WASAPI mic got the same numbers:

| run | residual |
|---|---|
| q1 | −15.9 ms |
| q2 | −3.3 ms |
| q3 | −14.6 ms |
| q4 (mixed `both`) | +7.0 ms |

Under half a frame at 30 fps, and slightly audio-late, which is the benign
direction.

### The audio environment

- **Devices.** Default output: `FxSound Speakers (FxSound Audio Enhancer)`.
  Also active: `Speakers (Realtek XU)`. Default recording device:
  `Microphone Array on SoundWire Device (6- Realtek XU)`. All three report
  48 kHz, 2 ch, 32-bit float (WAVE_FORMAT_EXTENSIBLE).
- **Loopback during silence.** It delivered **zero packets** on both output
  devices: the classic defect, confirmed. FxSound later emitted
  silent-flagged packets. The gap filler handled both.
- **Playback latency.** PlaySound to loopback was 45–110 ms on FxSound's
  device and 223 ms on Realtek's, so FxSound adds ~150 ms before the real
  speakers. The speaker-to-mic acoustic path is 102–126 ms.
- **Clocks.** `time.perf_counter()` and a direct `QueryPerformanceCounter`
  differ by 0.007 ms, so they are the same clock. dshow's reference clock
  sat ~23.35 s behind QPC, with ±40 ms jitter: not usable for alignment.
- **Transport.**
  - ffmpeg 9.0.2 reads raw PCM from `tcp://` (and from `\\.\pipe\`).
  - Stalled writer, socket open, then `q`: without `-rw_timeout` ffmpeg was
    still running 8 s later and had to be killed. With `-rw_timeout
    3000000` it exited 2.8 s after `q`. The sandbox's ffmpeg 6.1 behaved
    the same; `validation.sh` re-runs it if WSL has an ffmpeg.
- **Filters.** `amix` (normalize, weights), `adelay` (all), `atrim` and
  `alimiter` are all present.
- **Firewall.** No prompt was reported across the 14 localhost listeners
  the probes opened (smoke step 48 still asks).

## Decisions for you to ratify

Mechanism calls under AGENT.md §4. A's thirteen and B's sixteen stand.
Where one is extended, it says so.

1. **The microphone moves to our WASAPI capture. This is a flagged
   deviation from the brief.** The brief keeps the mic path and makes it
   correct; the measurement says the dshow path cannot be made correct with
   an offset. So `audio.mic_backend = "wasapi"` is the default, and the mic
   goes through the same child, anchor and timeline as system audio. dshow
   remains as `mic_backend = "dshow"`, with a fixed sample-domain delay
   (`dshow_align_ms = 300`, the measured start) and an honest README note
   that it drifts. If you'd rather keep dshow as the default, it's one
   config key.
2. **One child process per source**, `python -m peep.wasapi serve --source
   system|mic`, rather than a thread in the recorder.
   - Hand-declared COM vtables are the riskiest code in the package. A
     mistake kills the child (exit code and stderr tail reported), not the
     resident agent.
   - The children go in kill-on-close jobs like ffmpeg. The recorder logs
     their argv and their stderr (`wasapi|` lines at DEBUG, warnings and
     errors at WARNING).
   - Their stdout is a JSON-lines protocol (`ready`, `connected`,
     `disconnected`, `stats`, `error`). Their stdin takes `anchor`, `stop`,
     and `stall`/`resume`; the last two are diagnostics used by the tests
     and the probe.
3. **TCP on 127.0.0.1, ephemeral port, `-rw_timeout 3000000`** (brief
   item 2's transport choice).
   - Trade-off: a socket where A #6 chose files ("nothing to firewall").
     It's loopback-only, refuses non-loopback peers, and no firewall prompt
     was reported in the probes.
   - What it buys: a read timeout in ffmpeg (named pipes have none), and a
     transport the WSL suite runs for real.
   - The stop order is part of the contract. `q` goes to ffmpeg while the
     children keep feeding it, and the children stop only after ffmpeg has
     exited. The sender never blocks past a stop: socket timeout plus a stop
     check on every wait.
4. **The anchor.**
   - Normal case: QPC of ffmpeg's `Input #0` line minus `audio.epoch_lead_ms
     = 58`, sent to every child from ffmpeg's stderr thread, before ffmpeg
     connects.
   - No anchor (never seen): the child starts at connect − 430 ms, the
     probes' median connect delay.
   - On a pipeline fallback, each attempt gets its own anchor and
     connection; the children persist.
5. **`audio.offset_ms` changes meaning.** It was "-itsoffset on the mic";
   it is now "manual trim for every source, positive delays". For WASAPI
   sources it moves the anchor; for dshow it moves the samples. The default
   needs none. This is the migration note at the top.
6. **`audio.device` defaults to `""`, meaning the Windows default recording
   device.** It used to be A's friendly name, which embeds the renumberable
   `(6- ...)`. A name, a unique substring, or an endpoint id all work.
   `audio.system_device` works the same way for the output.
7. **A source that cannot start is loud and the recording goes on**, the
   same as A's flash-unavailable path:
   - a `!` status line, which the agent shows as a toast;
   - an `audio.source_failed` ERROR;
   - the sidecar's `audio.<source>.error`.

   With no source at all, the recording is video only and says so. A child
   that dies mid-recording is reported at stop.
8. **`both` mixes with `amix … normalize=0`.** Each source keeps its own
   level, so a lone voice isn't halved. `system_gain` and `mic_gain` are
   the knobs; I didn't add a limiter. `mix = "separate"` writes two titled
   tracks, system first, which the MP4 remux keeps.
9. **The clap is shipped (item 5).**
   - What it is: a 120 ms 1 kHz tone at amplitude 0.4, generated into
     `%LOCALAPPDATA%\peep\clap.wav`.
   - When it plays: started just before the magenta flash when system audio
     is recorded and flashes are on; `audio.clap = false` turns it off.
   - What's stamped: the request instant in QPC. The measured render
     latency is 45–110 ms on FxSound. C should find the tone in the system
     track rather than trust the request stamp.
   - Not measured: the clap inside a real `peep rec` (the probe's beep was
     the same mechanism). That's smoke step 42.
10. **The sidecar stays `peep.sidecar/1`; the A.1 fields are additive.**
    - A's four `audio` keys keep their meaning; `device` is the mic name,
      or null.
    - New in `audio`: `sources`, `mix`, `mic_backend`, `tracks`, `epoch`,
      `system`/`mic` (endpoint, format, each connection's
      `first_sample_qpc`, timeline stats, exit code, error) and `clap`.
    - `timeline` gains `ffmpeg_started_qpc` and `input0_qpc`.
    - Every flash gains `shown_qpc`. `time.monotonic` is GetTickCount64 on
      Python 3.12 (15.6 ms steps), too coarse to relate to audio.
11. **`peep config` behaviour.**
    - `set` on a missing file writes the template first, then edits it, so
      the comments exist from the start.
    - `unset` restores the current template's commented line, or removes
      the line if the template has none.
    - `config set/unset/path` still run when the file is invalid, so a
      broken file can be fixed without an editor. The new value is still
      fully validated.
    - `get KEY` prints just the TOML value, so it's scriptable.
12. **`peep doctor` plays a short tone** whenever system audio is a source:
    the loopback check the brief asked for. A silent capture is a warning,
    not a failure; it could just be the mute key. The doctor's
    `--capture` test is now video-only. The audio paths have their own
    checks, and a 2 s null capture can't host the children cleanly.
13. **`--no-mic` stays, meaning "take the mic out".** `both` becomes
    `system`, `mic` becomes `none`, and `system` is unchanged.
    `RecordRequest.audio = False` still means no audio at all, for any
    caller that sets it.
14. **The `rec` status line names the audio**, e.g. `audio: system (FxSound
    Speakers …)`. This is cheap, and it's the friction point: you see before
    talking whether your voice is being recorded.

## Look closely at

- **`win/peep/wasapi.py` §3, the vtable indices and signatures.** The
  probes exercised every call used: enumeration, GetDefaultAudioEndpoint,
  GetDevice by id, the property store, Activate, GetMixFormat,
  GetDevicePeriod, Initialize with and without the loopback flag,
  GetService, GetNextPacketSize, GetBuffer and ReleaseBuffer. Any change
  there needs a re-probe.
- **`recorder.Recorder._anchor`** runs on ffmpeg's stderr-drain thread.
  It's guarded so an exception can't kill the drain.
- **The `Timeline` correction policy.** A packet more than 20 ms off its
  expected place is realigned (silence in, or the overlap dropped);
  anything smaller is jitter and left alone. Card-vs-QPC drift is therefore
  corrected in ≤20 ms steps, which could be audible as a tiny click on very
  long recordings. While nothing plays, the stream is padded up to `now −
  120 ms`.
- **★ The mic child's gaps.** In probe 3 it recorded up to five gap fills,
  each under 60 ms, per ~10 s run (max drift 57–59 ms), while dshow held
  the same microphone. I don't know whether that happens without dshow.
  Smoke step 43 asks you to listen and to read `audio.mic.stats`.
- **★ Under the agent**, `sys.executable` is `pythonw.exe`, so the children
  are `pythonw.exe -m peep.wasapi serve`. Pipes work under pythonw, but the
  probes only ran `python.exe`.

## Uncertain (so you don't inherit false confidence)

- **Other pipelines.** The 58 ms epoch lead was measured on the `qsv`
  pipeline. The fallbacks open the same ddagrab input first, so I expect
  the same number, but I didn't measure it.
- **Device changes mid-recording** (headphones, default switch). The child
  reports the error and that input ends (EOF to ffmpeg). The recording
  continues and the sidecar says why. Not re-opened: deferred, and smoke
  step 47.
- **FxSound.** Loopback on `FxSound Speakers` captures FxSound's *input*
  (unprocessed). Loopback on `Speakers (Realtek XU)` captures the processed
  output, ~150 ms later. The alignment holds either way because it uses
  capture timestamps. Which one sounds right in a tutorial is your call;
  `audio.system_device` picks.
- **Long recordings.** No recording here was longer than ~11 s. Drift
  handling is unit-tested with a simulated 0.1 %-slow card; real-world
  long-run behaviour is smoke step 19 plus the sidecar's stats.

## Deferred (also in the manifest)

- **The dialog status line** (`audio: system`). It needs a change in B's
  `agent.py`, where the dialog's status text is built. The brief made it
  optional, the `rec` terminal and the sidecar already name the audio, and
  I kept B's modules byte-identical (`validation.sh` asserts it).
- **Re-opening an invalidated endpoint.** See above.

## Forecast, scope, validation

- **Forecast.** All 22 paths sit inside the write forecast (`.`): 17
  modified, 5 created (`win/peep/wasapi.py`, `win/peep/configedit.py`,
  `tests/fake_wasapi.py`, `tests/test_wasapi.py`, `tests/test_configedit.py`). Nothing in `out_of_scope` was
  touched: no post-processing, no webcam or region capture, no pip package
  or third-party tool. `validation.sh` asserts the import surface of the
  two new modules.
- **Unchanged files.** B's `agent.py`, `dialog.py`, `pill.py`, `hotkeys.py`,
  `ui.py`, `agentcli.py`, `lifecycle.py`, `winapi.py` and `control.py` are
  byte-identical. Like B's own pins, that check and the schema and exec-bit
  checks pass on the unmodified tree too, by nature.
- **Assertions tested both ways.** The other five session assertions fail
  on the unmodified tree and pass with the change; I ran both in a staging
  copy.
- **The real-ffmpeg stall check** runs only if WSL has an `ffmpeg` on PATH.
  Otherwise it prints `[SKIP]` with the reason.
- **pyflakes** is clean on every file this session created or changed,
  apart from B's two known pre-existing nits (`recorder.py`'s unused
  `FlashRecord` import, an f-string in `fake_ffmpeg.py`). I ran it in my
  sandbox; it isn't shipped.
- **Shapes used.** No light question blocks and no clarification. Three
  probes, as above.

## Proposals

**C: calibrate each recording from the clap, not just the flash.**

- **Why:** the sidecar now has everything for an exact per-recording A/V
  check. `flash.start.shown_qpc` is when the magenta window was up;
  `audio.clap.requested_qpc` is when the tone was requested;
  `audio.system.connections[-1].first_sample_qpc` is audio t=0; and the
  tone itself is in the system track 45–110 ms after the request (FxSound).
  Find the magenta frame (A's proposal) and the 1 kHz onset (a narrow
  band-pass at 1 kHz plus a 60 ms hold rejected clicks in the probe). Their
  difference against the stamps is this recording's residual; correct it
  if it exceeds a frame.
- **Scope hints:** session C; read-only on the sidecar. The probe's
  `harness3.py` onset detector is a working starting point.

**Re-open a capture endpoint after `AUDCLNT_E_DEVICE_INVALIDATED`.**

- **Why:** plugging in headphones mid-tutorial currently ends that audio
  input. The child could re-open the (new) default endpoint, and keep
  feeding the same stream if the mix format matches (it was 48 kHz float
  stereo on every device here), filling the switch gap with silence.
- **Scope hints:** `wasapi.CaptureServer._capture_main`, after a smoke test
  of what Windows actually reports (step 47).

**The dialog's `audio:` status line, and an audio meter in the pill.**

- **Why:** you'll want to see "audio: system + mic" at the moment you
  start talking. The pill could show a level bar from the children's peak
  levels (they'd emit a `level` line every ~200 ms).
- **Scope hints:** `agent.py` and `pill.py` (B's), plus a `level` event in
  the wasapi protocol. Only if the status line proves not enough.

**Retire the dshow backend after a week of WASAPI-mic use.**

- **Why:** it's kept as an escape hatch, but it's the measured source of
  "the first bug". If the WASAPI mic holds up in the smoke test, removing
  dshow simplifies the argv, doctor and config.
- **Scope hints:** `ffmpeg_cmd.AudioInput` (dshow branch), `doctor.check_mic`,
  `config` (`mic_backend`, `dshow_align_ms`, `buffer_ms`).

**Separate-track export.**

- **Why:** with `mix = "separate"` the `.mp4` has system and mic as two
  tracks, which is good for editing, but most players only play the first.
  A C preset could export a mixed copy, or a voice-only one, from the two.
- **Scope hints:** session C's preset list.
