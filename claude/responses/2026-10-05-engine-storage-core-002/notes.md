# notes — 2026-10-05-engine-storage-core-002 (peep-peep session A)

Everything in the brief's scope list is built. The suite has 135 tests and
passes under pytest and plain unittest, on Python 3.12.3 (the laptop's WSL
version) and 3.13. Nothing here has touched a real Windows desktop except
through the two probes below. The README's smoke-test checklist is the real
acceptance test, and its ★ items are the questions I could not answer from
here.

## What the probes established (relied on throughout)

I ran two paste-back probes before building. These are the facts the code
depends on.

**Probe 1: the installs, devices and the stop path.**

- **ffmpeg:** 9.0.2 Gyan full build, at
  `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_...\ffmpeg-9.0.2-full_build\bin`.
  It's on PATH from both interop and Windows Python. The `WinGet\Links`
  directory is empty, so don't hard-code that path.
- **ffmpeg features:** `ddagrab`, `gfxcapture`, `dshow`, `h264_qsv`,
  `libx264`, `vpp_qsv`, `scale_qsv`, `scale_d3d11`, `hwmap` and
  `hwdownload` are all present. `ffprobe.exe` sits beside ffmpeg.
- **Python:** 3.12.10 (64-bit, python.org) at
  `%LOCALAPPDATA%\Programs\Python\Python312`. It comes first on PATH, ahead
  of the Store stub. tomllib and tkinter (Tk 8.6) both work, and pip 25.0.1
  is present.
- **Microphone:** dshow lists exactly one audio device,
  `Microphone Array on SoundWire Device (6- Realtek XU)`, with the
  alternative name
  `@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{85A9C569-9688-43B6-833E-478DF7BBFDDC}`.
  Both open (44.1 kHz stereo s16). The friendly name is the config default.
- **Capture:** ddagrab delivers D3D11 frames at 2560x1600. Every pipeline
  ran: zero-copy QSV, the download path, x264, and scaling to 1920x1200
  through both `scale_qsv` and `vpp_qsv`.
- **Stopping:** Windows Python writing `q` to ffmpeg's stdin stops a
  screen+mic capture cleanly, exit 0, 0.69 s after the `q`. Writing to an
  already-closed pipe raises `EINVAL` on Windows, not `EPIPE`, and the code
  handles that.
- **Tk and DPI:** Tk reports 1707x1067 until
  `SetProcessDpiAwareness(2)` is called, then 2560x1600.
- **Foreground window:** the read works. From the terminal it returns
  Windows Terminal (`WindowsTerminal.exe`, title
  `chordsphere@chordsphere: ~/peep-peep`).
- **Repo access:** Windows Python can read the repo over
  `\\wsl.localhost\Ubuntu\home\chordsphere\peep-peep`.
- **Videos folder:** `C:\Users\chord\Videos` is not redirected and is
  writable.
- **WSL side:** no pytest. `python3.12-venv` and pip are installed.
  `~/.local/bin` exists and is on PATH. bale 0.4.45.

**Probe 2: colour, timestamps and the flash.** Captured frames were decoded
back to RGB.

- **The plain zero-copy chain has wrong colours.** It hands the encoder
  BGRA tagged `pc, gbr`, so black decodes as 16/16/16 (`scale_qsv` does the
  same).
- **The `vpp_qsv` chain is exact.** Adding
  `vpp_qsv=format=nv12:out_color_matrix=bt709:out_range=tv` gives black 0,
  grey 126, magenta 253/0/252 and green 0/254/0, tagged `tv/bt709`. The
  1920x1200 variant is exact too, and so is the CPU-download route through
  swscale. `scale_d3d11` failed with "Could not create the texture".
- **The timestamp warnings were a null-output artifact.** Probe 1's
  non-monotonic DTS warnings don't occur with Matroska output.
- **The 150 ms flash works.** It covered 4–5 frames at 30 fps and filled
  the screen to both sampled corners.
- **Defender:** controlled folder access is off (`0`), so ffmpeg can write
  to Videos.

## Decisions for you to ratify

None of these touch the four ratified decisions. They are mechanism calls
made under AGENT.md §4.

1. **Default video chain:**
   `hwmap=derive_device=qsv,format=qsv,vpp_qsv=format=nv12:out_color_matrix=bt709:out_range=tv`
   then `h264_qsv -preset medium -global_quality 25`. Frames stay on the
   GPU the whole way, and the colours were measured exact. The brief's
   "libx264 fallback" became an automatic fallback list:
   `qsv-download`, then `x264`. These are tried only if ffmpeg dies during
   start-up. The attempt is printed, logged and kept in the sidecar's
   `ffmpeg.attempts`. To pin one chain, set `video.fallback = []`.
2. **30 fps,** as the brief proposed. Tutorials are mostly static text, and
   QSV at 2560x1600 stays cheap at 30. Session C's frame arithmetic is
   simpler at a constant 30 (`-fps_mode cfr` is set), and a 150 ms flash is
   still 4–5 frames. `--fps 60` exists for motion-heavy recordings.
3. **Containers:** the capture is written as `<stem>.recording.mkv`, then
   remuxed (stream copy) to `<stem>.mp4` on a clean stop. A crash
   mid-recording leaves a playable Matroska file; a crash with MP4 leaves
   nothing usable. If the remux fails, the `.mkv` is kept and you are told.
   `output.container = "mkv"` skips the remux.
4. **Flash colours:** magenta at start, as briefed, and green (#00FF00) at
   stop. Green is magenta's complement, so the two can't be confused, and
   both survived the pipeline exactly.
5. **Flash timing:**
   - The start flash waits until ffmpeg prints `Output #0` (its output file
     is open), then a further `lead_ms` of 300 ms, so the flash can't fall
     before the first frame.
   - The stop flash is followed by `settle_ms` of 400 ms before `q`, so the
     flash's frames are written before the file closes.
   - All four values are configurable.
6. **IPC is control files, not a socket.** They live in
   `%LOCALAPPDATA%\peep\state`:
   - `active.json` carries the recorder's pid, so a stale file from a crash
     is detected rather than trusted.
   - `stop-request` is polled every 100 ms.

   That means no port, no listener thread and nothing to firewall, and you
   can inspect it with `type`.
7. **Install story:** the repo's `win/` is copied into
   `%LOCALAPPDATA%\peep\app`. The shim re-copies changed files before every
   command, using a hash compare against `app\INSTALLED.json`, and prints
   one line when it does. The trade-off against the alternatives:
   - **UNC execution** works (probed), but needs WSL running and goes
     through 9P on every import. It can't work at Windows login, which is
     when session B starts.
   - **`pip install --user -e` from UNC** has the same login problem,
     because the editable install points back at the UNC path.
   - **The sync** costs a copy step, but the shim does it for you, it's
     invisible after `peep install`, and the repo stays the only source.
     B's startup entry is just `pythonw.exe %LOCALAPPDATA%\peep\app\peepw.py <agent-command>`.
8. **Config location:** `%LOCALAPPDATA%\peep\config.toml`. That's inside
   the user profile, as the brief asked, and next to the logs and state, so
   one folder holds everything of peep's. `PEEP_CONFIG` and `PEEP_HOME`
   override it.
9. **Default collection is `inbox`.**
10. **A kill-on-close Job Object** wraps ffmpeg. If the Windows Python
    process dies (killed, crashed, the interop proxy torn down), Windows
    kills ffmpeg instead of leaving it recording until the disk fills. The
    `.mkv` written so far survives. If creating the job fails, that's a
    logged warning and recording goes ahead without it.
11. **The terminal can never kill the recording.**
    - Ctrl-C, q or Enter in the `rec` terminal all become one `stop` line on
      the Windows child's stdin.
    - The child runs in its own session, so the terminal's SIGINT never
      reaches the interop proxy.
    - If the shim or its terminal disappears, the child gets EOF, which the
      Windows side treats as stop.
    - Status prints to a vanished terminal are logged, never raised, so
      closing the tab can't abort the remux.
12. **Tests are `unittest.TestCase` classes.** pytest runs them, and so does
    `python3 -m unittest discover -s tests -t .` on a WSL install without
    pytest, which is the laptop's current state. `validation.sh` uses
    whichever is available.
13. **A start where every pipeline failed** deletes its placeholder sidecar
    rather than leave an orphan JSON in Videos. The exit codes and stderr
    tails are printed and logged.

## Look closely at

- **`win/peep/recorder.py`, `Recorder.record()` and `_finalize()`:** the
  ordering of flash, settle, `q`, wait, terminate, remux, sidecar and
  catalog.
- **`win/peep/winapi.py`, `KillOnCloseJob`:** hand-declared
  `JOBOBJECT_EXTENDED_LIMIT_INFORMATION` structs. They are 64-bit layouts
  and untested on Windows. A mistake shows up as a `job.create_failed` or
  `job.assign_failed` warning, never as a broken recording.
- **`bin/peep`, `run_rec()`:** whether interop still relays the child's
  stdout to the terminal when the child has no controlling terminal
  (`start_new_session=True`). I believe so, but I couldn't run it. If
  `peep rec` prints nothing, try `PEEP_REC_NO_SETSID=1`. That keeps the
  output, but Ctrl-C then also hits the proxy, so use q instead.

## Uncertain (so you don't inherit false confidence)

- **A/V sync.** ffmpeg normalises each input's start time separately. If
  dshow delivers its first audio later than ddagrab's first frame, the
  audio sits slightly early or late. `-audio_buffer_size 50` shrinks
  dshow's default 500 ms buffer, which is the usual culprit. Smoke-test
  step 6 measures what's left, and `audio.offset_ms` corrects it. I
  couldn't predict the number.
- **The microphone name.** The `(6- ...)` in it is a Windows endpoint index
  and can change when audio devices come and go. `peep doctor` flags it and
  lists the stable `@device_cm_` form, which also works as `audio.device`.
- **ICQ or CQP.** I don't know whether `-global_quality 25` with no bitrate
  puts h264_qsv into ICQ (quality-targeted) or CQP mode on this driver.
  Either looks fine for screen content. File size in smoke step 19 will
  show whether 25 is too generous.
- **The transfer tag.** The probe's ffprobe showed
  `color_transfer=iec61966-2-1` (sRGB) even with `-color_trc bt709` passed,
  so the frame property wins over the option. sRGB is the correct transfer
  for desktop pixels and players handle it, so I left it.
- **The secure desktop.** I don't know how ddagrab behaves across a UAC
  prompt or Win+L. If it errors, peep keeps the partial `.mkv` and
  catalogs it as failed (smoke step 20).
- **The slug from `peep rec`.** Run from the terminal it is always
  `terminal-<cwd>`, because the terminal is the foreground window. That is
  correct behaviour, and it only becomes useful when B's hotkey records
  whatever app you are in.

## Deferred (also in the manifest)

- **Automated tests for the Win32 and Tk paths:**
  - `TkFlasher`;
  - `winapi.foreground_window`, `set_dpi_aware` and `KillOnCloseJob`;
  - `pid_alive` and `open_with_default` on Windows.

  They need a Windows desktop session, and the brief requires a WSL-only
  suite. Their behaviour was exercised by the probes (foreground read, DPI,
  the Tk flash's geometry and timing), and smoke-test steps 2–5, 10, 11
  and 17 cover them for real.
- **A/V sync calibration:** see above.

No paths outside the write forecast (`.`). Nothing in `out_of_scope` was
touched.

## Proposals

**B: run the recorder on a worker thread and show the flash on B's UI thread.**

- **Why:** Tk must live on one thread. B will own a tkinter dialog on its
  main thread, and A's `TkFlasher` creates its own `Tk()` on whichever
  thread calls `record()`. Two Tk interpreters on different threads is
  asking for trouble.
- **Scope hints:** `Recorder(flasher_factory=...)` is already injectable.
  B supplies a flasher that marshals `flash()` onto its UI thread and
  blocks until the window has been shown. `stop_event` is already a
  `threading.Event`, so the dialog can set it. Nothing in A needs to change.

**B: capture the foreground window at hotkey time, not when `record()` runs.**

- **Why:** by the time the agent reacts, the foreground may already be the
  REC pill or the agent itself.
- **Scope hints:** add an optional `foreground` field to `RecordRequest`
  (one line in `recorder.py`), or keep passing `foreground=` into
  `Recorder`. `naming.suggest_slug(image, title)` is the dialog's prefill.

**B: extend the control files for the agent's other commands.**

- **Why:** the `state/` directory pattern already carries stop. A `mark`
  request file (for C's marks) and a `status` field in `active.json` would
  give B and the WSL shim the same view without adding a socket.
- **Scope hints:** `control.py`. Only after B decides whether marks are a
  hotkey.

**C: use the sidecar as the search window for the flashes.**

- **Why:** `flash.start.since_ffmpeg_start_s` and
  `flash.stop.since_ffmpeg_start_s` place each flash to within roughly a
  second of media time. Detection only needs to scan about ±1 s around
  them.
- **Detection thresholds:** the measured decoded values are magenta
  ≈ (253, 0, 252) and green ≈ (0, 254, 0), full-frame. Use the frame
  median, because ddagrab draws the cursor over the flash.
- **Scope hints:** the `peep.sidecar/1` fields are stable. `crop` and
  `marks` are reserved and `trim` is null.

**C: put per-collection crop in a collection-level file.**

- **Why:** the crop belongs to the collection, not to each recording.
  Something like `<collection>/collection.json` would hold the default
  rectangle, with each sidecar's `crop` recording what was actually
  applied.
- **Scope hints:** a new file and module in C. The catalog fold is
  unaffected.

**C: consider an audible clap alongside the visual flash.**

- **Why:** the flash fixes video time but says nothing about audio. A short
  tone played at the start flash would let C measure, and correct, the A/V
  offset per recording instead of relying on a global `audio.offset_ms`.
- **Scope hints:** this is system audio playing into the mic, so it needs
  the mic to hear the speakers. It only makes sense if the smoke test shows
  sync drifting between recordings.
