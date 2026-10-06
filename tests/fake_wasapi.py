"""Stand-ins for the WASAPI side, so the audio path runs on WSL with no
Win32, no audio devices and no ffmpeg.exe.

  SyntheticCapture    duck-types wasapi.EndpointCapture: 48 kHz stereo
                      float packets stamped on the QPC clock (perf_counter
                      off Windows), delivered only inside `bursts` (the way
                      loopback goes quiet when nothing plays), so the real
                      Timeline has gaps to fill
  FakeCaptureProcess  duck-types wasapi.CaptureProcess for the recorder: a
                      real CaptureServer (real sockets, real anchors, real
                      Timeline) running in a thread instead of a child
  unix_listener       an abstract AF_UNIX listener (URL `unix:@name`): works
                      inside bale's network-off validation sandbox, where a
                      loopback TCP connect fails
  __main__            `python fake_wasapi.py serve --source S ...` runs the
                      same server as a real child process, for testing
                      CaptureProcess across a process boundary
"""

import io
import itertools
import json
import os
import socket
import struct
import sys
import threading
import time

if __name__ == "__main__":     # as a child: make the package importable
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "win"))

from peep import wasapi as wa  # noqa: E402

FMT = wa.PcmFormat(48000, 2, 32, True)
_names = itertools.count()


def unix_listener():
    """(listening abstract AF_UNIX socket, 'unix:@name'). Abstract names live in
    the network namespace, need no filesystem path and no network."""
    name = f"peep-test-{os.getpid()}-{next(_names)}-{os.urandom(4).hex()}"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind("\0" + name)
    s.listen(1)
    return s, f"unix:@{name}"


def connect(url, timeout=2.0):
    """A client socket for a capture URL (unix:@name or tcp://host:port)."""
    if url.startswith("unix:@"):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(timeout)
        c.connect("\0" + url[len("unix:@"):])
        return c
    host, port = url[len("tcp://"):].rsplit(":", 1)
    return socket.create_connection((host, int(port)), timeout=timeout)
TONE = struct.pack("<ff", 0.25, -0.25) * 480          # one 10 ms packet


class SyntheticCapture:
    def __init__(self, source="system", selector="", bursts=((0.0, 1e9),), fail=None):
        if fail == "open":
            raise wa.ComError("IMMDevice::Activate", 0x88890004)
        self.source = source
        self.format = FMT
        name = "FxSound Speakers (FxSound Audio Enhancer)" if source == "system" else \
            "Microphone Array on SoundWire Device (6- Realtek XU)"
        self.endpoint = wa.Endpoint("{0.0.0.00000000}.{fake}", selector or name, "render" if source == "system"
                                    else "capture", "active", not selector)
        self.period_s, self.buffer_frames = 0.01, 48000
        self.bursts = bursts
        self.t0, self.n = None, 0

    def start(self):
        self.t0, self.n = wa.qpc_now(), 0

    def packets(self):
        now, out = wa.qpc_now(), []
        while self.t0 + self.n * 0.01 < now - 0.01:
            t = self.t0 + self.n * 0.01
            rel = t - self.t0
            if any(a <= rel < b for a, b in self.bursts):
                out.append((480, TONE, t, 0))
            self.n += 1
        return out

    def stop(self):
        pass

    def close(self):
        pass


class _NoCom:
    def close(self):
        pass


class _LineSink(io.TextIOBase):
    """A text stream that hands each complete line to `on_line`."""

    def __init__(self, on_line):
        self.on_line, self.buf, self.lock = on_line, "", threading.Lock()

    def write(self, s):
        with self.lock:
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                self.on_line(line)
        return len(s)

    def flush(self):
        pass


class _Pipe:
    """stdin for the server: readline blocks until a line or close()."""

    def __init__(self):
        self.lines, self.cond, self.closed = [], threading.Condition(), False

    def readline(self):
        with self.cond:
            while not self.lines and not self.closed:
                self.cond.wait()
            return self.lines.pop(0) if self.lines else ""

    def put(self, line):
        with self.cond:
            self.lines.append(line)
            self.cond.notify_all()

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()


class FakeCaptureProcess:
    """In-process CaptureProcess. `fail`: None | "start" (ready never comes) |
    "open" (the device cannot be opened: the child reports an error)."""

    instances: list = []

    def __init__(self, job=None, fail=None, bursts=((0.0, 1e9),), exit_code=0, listen=unix_listener):
        self.job, self.fail, self.bursts, self.forced_exit = job, fail, bursts, exit_code
        self.events, self.tail, self.anchors_sent = [], [], []
        self.ready_info = None
        self.exit_code = None
        self.stdin = _Pipe()
        self.thread = None
        self.source = None
        self.listen = listen
        FakeCaptureProcess.instances.append(self)

    def _on_out(self, line):
        msg = json.loads(line)
        self.events.append(msg)
        if msg.get("event") == "ready":
            self.ready_info = wa.CaptureReady(msg["port"], wa.PcmFormat.from_dict(msg["format"]), msg["endpoint"],
                                              msg["epoch_qpc"], msg, msg.get("url") or "")

    def start(self, source, selector="", ready_timeout_s=10.0, fallback_lead_ms=0):
        self.source = source
        if self.fail == "start":
            self.exit_code = 3
            self.tail.append("ERROR wasapi.capture_failed error=\"fake: no device\"")
            raise wa.CaptureStartError("system audio capture failed to start: fake: no device")
        em = wa.Emitter(_LineSink(self._on_out), _LineSink(self.tail.append))
        factory = (lambda so, se: (_NoCom(), SyntheticCapture(so, se, self.bursts, self.fail)))
        self.server = wa.CaptureServer(source, selector, fallback_lead_s=fallback_lead_ms / 1000, emitter=em,
                                       capture_factory=factory, stdin=self.stdin, listen=self.listen)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline and self.ready_info is None:
            if any(e.get("event") == "error" for e in self.events):
                self.thread.join(2)
                raise wa.CaptureStartError(f"{source} audio capture failed to start: "
                                           f"{next(e['error'] for e in self.events if e.get('event') == 'error')}")
            time.sleep(0.01)
        if self.ready_info is None:
            raise wa.CaptureStartError("no ready line")
        return self.ready_info

    def _run(self):
        rc = self.server.run()
        self.exit_code = self.forced_exit if rc == 0 else rc

    def send_anchor(self, qpc):
        self.anchors_sent.append(qpc)
        self.stdin.put(f"anchor {qpc:.7f}\n")
        return True

    def send(self, line):
        self.stdin.put(line + "\n")
        return True

    def poll(self):
        if self.thread is None:
            return self.exit_code
        return None if self.thread.is_alive() else self.exit_code

    def stop(self, timeout_s=3.0):
        self.stdin.put("stop\n")
        self.stdin.close()
        if self.thread is not None:
            self.thread.join(timeout_s + 2)
        return self.exit_code

    def last(self, name):
        for e in reversed(self.events):
            if e.get("event") == name:
                return e
        return None

    def all(self, name):
        return [e for e in self.events if e.get("event") == name]

    def stderr_tail(self, n=15):
        return self.tail[-n:]


def main(argv):
    """Child mode: the real CaptureServer over real stdin/stdout, synthetic audio."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd")
    p.add_argument("--source", default="system")
    p.add_argument("--device", default="")
    p.add_argument("--fallback-lead-ms", type=int, default=0)
    p.add_argument("--mode", default="ok", help="ok | open-fails | crash-after-ready | hang")
    p.add_argument("--unix", action="store_true", help="listen on an abstract AF_UNIX socket instead of TCP")
    a = p.parse_args(argv)
    if a.mode == "hang":
        time.sleep(60)
        return 0
    if a.mode == "crash-after-ready":
        print(json.dumps({"event": "ready", "port": 1, "format": FMT.to_dict(), "endpoint": {"name": "x"},
                          "epoch_qpc": 0.0}), flush=True)
        sys.stderr.write("ERROR fake crash\n")
        sys.stderr.flush()
        os._exit(7)
    fail = "open" if a.mode == "open-fails" else None
    srv = wa.CaptureServer(a.source, a.device, fallback_lead_s=a.fallback_lead_ms / 1000,
                           capture_factory=lambda so, se: (_NoCom(), SyntheticCapture(so, se, fail=fail)),
                           listen=unix_listener if a.unix else None)
    return srv.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
