# Notes — 2026-10-06-stdin-shutdown-fix-002 (A.2)

One defect, one function changed (`cli.watch_stdin`), tests around it.
Nothing on the recorder, shim, audio or agent side moved; validation
checks those files byte-for-byte against the request.

## What it was (verified, not only hypothesized)

The planner's reading was right about the mechanism, with one correction
about why it showed up when it did.

`cmd_rec` starts `watch_stdin`, a daemon thread that sat in
`sys.stdin.buffer.readline()`. A `BufferedReader` holds its internal lock
for the whole of a blocking read. The shim (`bin/peep` `run_rec`) keeps
the child's stdin pipe open until the child has exited; it only writes
to it on q/Enter/Ctrl-C/EOF. So:

- **q / Enter / Ctrl-C:** the shim writes `stop\n`, the thread's
  readline returns and the thread ends. No lock is held at exit, so it
  exits clean. That matches "q doesn't do it".
- **`peep stop`, or the agent's hotkey on a terminal recording** (B #3:
  the same control-file stop): the recorder stops and `cmd_rec` returns
  while the thread is still blocked in readline, holding the lock. The
  finalizer has to take that lock to close `sys.stdin`. During
  finalization CPython waits only a 1 s grace for it, then calls
  `Py_FatalError("_enter_buffered_busy: could not acquire lock for
  <_io.BufferedReader name='<stdin>'> at interpreter shutdown, possibly
  due to daemon threads")`, which is `abort()`.

I reproduced it here on Linux CPython 3.13 with the shipped `cli.py`:
the same message, `Current thread ... <no Python frame>`, and SIGABRT.
The new regression tests fail on the shipped code with exactly that
stderr. Timing doesn't matter: the pipe stays open until the child
exits, so the 1 s grace never helps. In the code as shipped, the crash
needs only an external stop plus a stdin that's still open, so A.1's
stop reorder isn't part of it. I can't tell from here whether A's smoke
test ever stopped a terminal recording from outside; my guess is that
it didn't.

**One thing you may not have noticed:** `abort()` on Windows exits with
code 3, and the shim passes the child's code through. So every
externally stopped `peep rec` has also been *exiting 3*, not 0, even
though the file was saved. Anything chained on `peep rec && …` would
have seen a failure. After this fix those stops exit 0.

## The fix and why this mechanism

`watch_stdin` now reads the raw descriptor, `os.read(fd, 4096)`. It
never touches `sys.stdin` or `sys.stdin.buffer`. `os.read` holds no
Python-level lock, so finalization closes `sys.stdin` without waiting.
A daemon thread still blocked in the read just ends with the process.
`sys.stdin`'s FileIO wraps fd 0 with `closefd=False`, so shutdown
doesn't touch the descriptor the thread is reading.

I weighed the brief's other candidates and turned them down:

- **The shim closes the child's stdin when the recording finishes:**
  the shim only learns that from the child exiting, which is too late.
  It would also need a new signal on the shim↔child channel. That's more
  surface than this defect warrants.
- **Join the reader with a timeout:** the read never returns, so the
  join only delays the same crash.
- **`os._exit` after the catalog write:** it skips logging shutdown and
  atexit, and it would also mask any future finalizer problem instead
  of fixing this one.

Small behavioural edge, flagged for ratification: any *bytes* on stdin
are now a stop, where before it took a full line. In practice nothing
changes. The shim always sends `stop\n` in one write, a Windows console
read returns on Enter, and EOF and read errors are stops as before.

`stdin_fd()` is new. When `sys.stdin` is `None` (pythonw) it returns
None quietly. When the stream has no descriptor it logs `stdin.unwatchable`
and returns None. Before, an fd-less stdin would have killed the
watcher thread with a traceback, which was a silent failure. `cmd_rec`
dropped its own `sys.stdin is not None` guard, since `watch_stdin` now
handles that case.

## The guarantees, path by path

- **A #11, the terminal can never kill a recording:** unchanged. The
  shim's own-session launch and key translation are untouched (hash
  check in validation), and reading stdin can only *set* the stop event.
- **A #11, a vanished terminal is a stop:** the shim dying gives EOF,
  which reads `b""` and stops (test: `stdin closing`). An EIO/EBADF read
  error logs `stdin.read_failed` and stops (test: directory-fd read
  error). A closed tab where the shim survives SIGHUP still goes through
  the shim's `stop` line (existing shim tests, unchanged).
- **A.1's stop order (q to ffmpeg, capture children after):**
  `recorder.py` is untouched and byte-checked.
- **Every stop path exits 0 with empty stderr.** Each case is tested
  with a real `rec` child process (`cli.main`, real fd 0, real
  interpreter shutdown, a fake recorder standing in for ffmpeg). The
  external stop is a flag file standing in for the control file. Three
  cases: external stop with stdin held open (the regression), a stop
  line, and stdin closing. The external case also runs end to end
  through the shim's `run_rec`, where the shim sends nothing and closes
  the child's stdin only after the child exits. A fourth test asserts
  the harness still reproduces the old crash, so the clean-exit tests
  can't go vacuous. On an interpreter that doesn't reproduce it, that
  test skips and says why.

## The agent-owned path (brief item 3)

Confirmed by reading: it has no equivalent reader. The agent runs under
`pythonw.exe` (`sys.stdin` is `None`) and is spawned with
`stdin=DEVNULL` (`lifecycle.spawn_agent`). It runs A's recorder on a
worker thread with its own `stop_event`, and only `cli.cmd_rec` calls
`watch_stdin`. Its other daemon threads (hotkeys, workers) hold no
buffered-stream locks. Nothing to fix there.

## No probe: the laptop check rides in validation.sh instead

The brief offered a probe to confirm the mechanism on the laptop. The
Linux reproduction is exact, and this is CPython's finalizer, not
anything Windows-specific. What's left to confirm is that Windows'
python.exe treats a thread blocked in `os.read` on an interop pipe the
same way. So `validation.sh` carries that check: through interop it
runs both the old pattern and the staged `watch_stdin` on your
`python.exe` (or `$PEEP_PYTHON`), with stdin held open the way the shim
holds it. It expects the field error from the old pattern and a clean
exit from the fix. It also fails if the process hangs at exit, which
was the Windows-specific risk I could think of (a CRT lock on fd 0). It
imports the staged package over `\\wsl.localhost` with `-B`, so it
writes nothing. Where interop or `wslpath` is unreachable (possibly
bale's sandbox) it prints `[SKIP]` with the reason, and then the
smoke steps below are the confirmation. I tested the check's driver
here with Linux python standing in for python.exe. That caught a bug in
my first draft (`communicate()` closes stdin, which hid the very
ordering under test), and it now passes on the fix and fails on the
shipped code.

The "stop order and audio/agent files untouched" assertion passes on the
unmodified tree too, by design. It guards an outcome the brief pins
rather than testing the change.

## Smoke check worth two minutes

Each of these should end at `✓ saved …` with no `Fatal Python error`
line, and `echo $?` should print `0`:
`peep rec`, then `peep stop` from a second tab (not tried last round);
`peep rec`, then the record hotkey; `peep rec`, then `q`; and `peep rec`
with the tab closed mid-recording, where the file should still be
cataloged. Autosync copies the fix into the installed app on the next
`peep` command, so nothing needs reinstalling.

## README

Nothing added. After the fix there's nothing the architect can hit,
and the brief asked for a Troubleshooting line only if there was.

## Proposals

- **What:** give the WASAPI capture child's stdin reader
  (`wasapi.CaptureServer._stdin_main`, which reads `sys.stdin.readline`
  on a daemon thread) the same raw-fd treatment, or have its error
  exits not leave the reader blocked.
  **Why:** `run()` returns 3 or 4 on its error paths (capture didn't
  start, listen failed, idle exit) while the recorder still holds the
  child's stdin open, which is the same ordering as this defect. I
  haven't reproduced it, but by the same mechanism the child would
  likely abort at shutdown on those paths. Its exit code becomes 3 and
  Fatal lines land on its stderr, after it has already emitted its
  `error` event, so the impact is a muddier failure report on paths
  that are already failing, not lost audio. It's audio code, so it's
  out of scope here.
  **Scope hints:** `win/peep/wasapi.py` `_stdin_main`/`run`, with
  `tests/test_wasapi.py`'s child mode for a regression test in this
  session's shape.
