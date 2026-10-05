# peep-peep

A screen recorder built for one machine: this Windows 11 laptop, driven from
WSL. The goal is the least possible friction between "I want to record this"
and a named file in a predictable place. `start.md` has the original intent.

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
interop**. **Session A** (this code) is that engine plus the naming and
catalog storage core and a terminal-driven `peep` shim, so the engine is
proven before any UX sits on it. **Session B** adds a resident Windows
agent: launched at login, a global hotkey, a REC pill hidden from capture,
and a stop dialog that prefills the name. **Session C** is post-processing:
finding the clapper flashes, trimming to them, mark-based cuts, GIF/WebM
presets and per-collection crop. B and C import A's modules unchanged.

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
| `peep rec [slug] [-c NAME]` | Record the screen and microphone. Stop with **q**, **Enter** or **Ctrl-C** in that terminal, or `peep stop` from another. With no slug the name comes from the foreground window (from a terminal at `~/peep-peep` that is `terminal-peep-peep`). Options: `--no-flash`, `--no-mic`, `--scale 1920x1200`, `--encoder qsv\|qsv-download\|x264`, `--fps N`. |
| `peep stop` | Ask the running recording to stop and wait until its file is finalized; prints the path. `--no-wait` returns immediately. |
| `peep ls [-c NAME] [--all] [--json]` | List recordings from the catalog (newest last). `--all` includes failed captures. |
| `peep open [last\|STEM] [--folder]` | Open with the Windows default player, or show it in Explorer. |
| `peep rename last NAME [-c NAME]` | Rename (the date prefix stays), optionally moving it to another collection. The sidecar moves with it. |
| `peep doctor [--capture]` | Check Python, ffmpeg, ddagrab/dshow, encoders, the mic, the storage root and tkinter. It never installs anything. |
| `peep config [--init]` | Show the effective config, or write a commented template. |
| `peep paths` | Where config, logs, state and recordings live. |

Every command except `install` runs on the Windows side, and the same
commands work directly from a Windows console:
`python %LOCALAPPDATA%\peep\app\peepw.py ls`.

## Where files land

```
C:\Users\chord\Videos\peep\            storage root (config: root)
  catalog.jsonl                        append-only index: recorded / failed / renamed events
  inbox\                               one folder per collection (default: inbox)
    2026-10-05-terminal-peep-peep.mp4
    2026-10-05-terminal-peep-peep.json sidecar
    2026-10-05-terminal-peep-peep-2.mp4   same name, same day: counter suffix

%LOCALAPPDATA%\peep\                   peep's own files
  config.toml                          optional; defaults apply without it
  logs\peep.log                        rotating, structured; every ffmpeg argv logged verbatim
  state\active.json, stop-request      control files between `rec` and `stop`
  app\                                 the installed Windows package
```

Recordings never go under `\\wsl.localhost`; config validation refuses such
a root.

**The sidecar** (`peep.sidecar/1`) records the title, slug, collection,
creation time, duration, the foreground window and process at start, the
video pipeline, encoder, capture and output size, the audio device, the
flash colours with their agent-side timestamps (wall clock and seconds
since ffmpeg launched), stop reason, ffmpeg's argv and exit code, and
`trim: null`. `crop` and `marks` are reserved for session C.

**The catalog** lets `ls`, `last` and `rename` work without walking folders.
Each recording has a `uid` that survives renames.

## How a recording works

1. The name is taken from the foreground window (or your slug) and a
   collision-free stem is picked. The sidecar is written straight away, which
   reserves the stem.
2. ffmpeg starts into `<stem>.recording.mkv`. If the pipeline fails during
   start-up, the next one in `video.fallback` is tried, and you are told.
3. Once ffmpeg reports its output open, a 150 ms magenta full-screen flash
   is shown. It is captured on purpose, as a fiducial for session C.
4. On stop: a 150 ms green flash, a short settle, then `q` on ffmpeg's stdin.
5. The Matroska capture is remuxed to MP4 (streams copied, index at the
   front). The capture container is Matroska because a capture cut short by
   a crash is still playable. If ffmpeg dies mid-recording, the partial
   `.mkv` is kept and catalogued as failed.

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

## Configuration

`peep config --init` writes `%LOCALAPPDATA%\peep\config.toml` with every key
commented out at its default. Unknown keys and wrong types are errors, so a
typo can't silently fall back to a default. The ones you are most likely to
touch:

```toml
default_collection = "inbox"
[video]
scale = "1920x1200"        # default "" = native 2560x1600
pipeline = "qsv"
[audio]
device = "Microphone Array on SoundWire Device (6- Realtek XU)"
offset_ms = 0              # set from the A/V sync smoke test below
[flash]
duration_ms = 150
```

If Windows renumbers the mic (the `(6- ...)` part), `peep doctor` lists the
current names and the stable `@device_cm_{...}` alternative name, and either
works as `audio.device`.

## Tests

```
python3 -m pytest                                   # if pytest is installed (sudo apt install python3-pytest)
python3 -m unittest discover -s tests -t .          # standard library only, same tests
```

The suite runs on the WSL side with no ffmpeg.exe, no display and no
interop. The recorder tests cross a real process boundary to
`tests/fake_ffmpeg.py`, a stand-in that speaks ffmpeg's stderr markers and
honours `q`. That covers the happy path, fallback, crash mid-recording, a
hung ffmpeg, remux failure, and `peep stop`. The real capture path is
covered by the checklist below.

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
   - your voice is audible.
5. Step frame by frame at the start and end (mpv: `.`/`,`). You should find
   4–5 magenta frames near the start and 4–5 green frames near the end. ★
   Is 150 ms enough with QSV's encoder? If a flash shows only 1–2 frames,
   raise `flash.duration_ms`.
6. ★ A/V sync: start a recording, then click something that changes the
   screen while saying "now", or tap the mic in view of a visible change.
   If speech leads or lags the picture, set `audio.offset_ms` (positive
   delays the mic) and re-check.
7. Check the stream properties:
   `ffprobe.exe -v error -show_entries stream=codec_name,pix_fmt,width,height,color_space,color_range,r_frame_rate -of compact "<file>"`
   should show `h264|yuv420p|2560|1600|bt709|tv|30/1` plus an `aac` stream.
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
15. `peep rec --no-flash --no-mic` gives no flashes and no audio stream.
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

## Troubleshooting

- **`python.exe` opens the Microsoft Store, or isn't found:** run
  `winget install Python.Python.3.12`, then `wsl --shutdown`.
- **The qsv pipeline fails:** peep falls back on its own and says so. To
  make it permanent, set `[video] pipeline = "qsv-download"` or `"x264"`.
- **Microphone not found:** `peep doctor` lists the dshow devices. Copy a
  name (or its `@device_cm_...` alternative) into `[audio] device`.
- **Anything else:** `peep paths` shows the log location, and every error
  is in the log with context.
