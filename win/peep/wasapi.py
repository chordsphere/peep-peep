"""Our own audio capture: WASAPI through ctypes COM, standard library only,
feeding ffmpeg raw PCM over localhost TCP. Two sources share the code:

  system  loopback on a render endpoint (AUDCLNT_STREAMFLAGS_LOOPBACK): what
          the computer plays. The default output device unless one is
          named; on the laptop that is "FxSound Speakers" (probe 2026-10-06).
  mic     plain shared-mode capture on a capture endpoint: the microphone.

Why each piece is shaped the way it is (session A.1):

* **Its own process.** Each source runs as a child, `python -m peep.wasapi
  serve --source system|mic`, which the recorder starts, puts in its
  kill-on-close job, logs like ffmpeg, and stops. Hand-declared COM vtables
  are the riskiest code in the package: if one is wrong the child dies with
  an access violation, the recorder reports the exit code and stderr tail,
  and the resident agent (which runs the recorder on a worker thread)
  survives.
* **TCP, not ffmpeg's stdin, not a named pipe.** ffmpeg's stdin carries `q`
  (A #11). The child listens on 127.0.0.1 (ephemeral port) and ffmpeg
  connects as `-i tcp://127.0.0.1:PORT` with `-rw_timeout`. The probe
  measured why the timeout matters: with a writer stalled but its socket
  open, ffmpeg without it was still running 8 s after `q`; with it, ffmpeg
  exited 2.8 s after `q`. A named pipe also worked, but ffmpeg has no read
  timeout on one.
* **A continuous timeline.** Loopback delivers nothing while nothing plays
  (probed: zero packets during silence on both laptop endpoints). `Timeline`
  turns bursty packets into one gap-free PCM stream on the QPC clock:
  silence is inserted for gaps (measured from each packet's QPC stamp), and
  while nothing arrives the stream is padded up to `now - idle_lag`, so
  ffmpeg's input never stalls and the track stays wall-clock long. The same
  alignment absorbs the slow drift between the sound card clock and QPC.
* **The start is chosen, not inherited.** ffmpeg puts the first byte it reads
  at audio t=0 and its first ddagrab frame at video t=0. The child keeps a
  ring of recent audio; when ffmpeg connects it starts the stream at the
  sample captured at the *anchor* QPC time the recorder sends: its estimate
  of when the first video frame was captured (see recorder.VIDEO_EPOCH_*).
  Both t=0s are then the same instant by construction.

Sections:
  1. PCM formats (pure)                          (~line 66)
  2. Timeline (pure: gaps, drift, ring, anchors) (~line 155)
  3. WASAPI via ctypes COM (Windows only)        (~line 339)
  4. The server process (`... serve`)            (~line 719)
  5. Parent side: CaptureProcess                 (~line 981)
  6. Capture test (doctor) + CLI entry           (~line 1202)
"""

from __future__ import annotations

import collections
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .logsetup import event, kv

log = logging.getLogger("peep.wasapi")

# ---------------------------------------------------------------------------
# 1. PCM formats
# ---------------------------------------------------------------------------

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
# KSDATAFORMAT_SUBTYPE_PCM / _IEEE_FLOAT, as uuid strings (lower case)
SUBTYPE_PCM = "00000001-0000-0010-8000-00aa00389b71"
SUBTYPE_IEEE_FLOAT = "00000003-0000-0010-8000-00aa00389b71"


class UnsupportedFormat(ValueError):
    """A mix format peep cannot hand to ffmpeg as raw PCM."""


@dataclass(frozen=True)
class PcmFormat:
    rate: int
    channels: int
    sample_bits: int            # container bits per sample (what is in the buffer)
    is_float: bool
    valid_bits: int = 0         # WAVEFORMATEXTENSIBLE wValidBitsPerSample (0 = same as sample_bits)
    channel_mask: int = 0

    @property
    def block_align(self) -> int:
        return self.channels * self.sample_bits // 8

    @property
    def bytes_per_second(self) -> int:
        return self.block_align * self.rate

    @property
    def ffmpeg_format(self) -> str:
        """The `-f` raw demuxer that reads these bytes exactly."""
        if self.is_float:
            return {32: "f32le", 64: "f64le"}[self.sample_bits]
        return {16: "s16le", 24: "s24le", 32: "s32le"}[self.sample_bits]

    def describe(self) -> str:
        kind = "float" if self.is_float else "int"
        return f"{self.rate} Hz, {self.channels} ch, {self.sample_bits}-bit {kind} ({self.ffmpeg_format})"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(block_align=self.block_align, ffmpeg_format=self.ffmpeg_format)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PcmFormat":
        return cls(rate=int(d["rate"]), channels=int(d["channels"]), sample_bits=int(d["sample_bits"]),
                   is_float=bool(d["is_float"]), valid_bits=int(d.get("valid_bits") or 0),
                   channel_mask=int(d.get("channel_mask") or 0))


def format_from_waveformat(tag: int, channels: int, rate: int, bits: int, block_align: int,
                           subformat: str | None = None, valid_bits: int = 0,
                           channel_mask: int = 0) -> PcmFormat:
    """WAVEFORMATEX(TENSIBLE) fields -> PcmFormat, or UnsupportedFormat saying why.
    Pure, so every branch is tested on Linux."""
    if tag == WAVE_FORMAT_EXTENSIBLE:
        sub = (subformat or "").strip("{}").lower()
        if sub == SUBTYPE_IEEE_FLOAT:
            is_float = True
        elif sub == SUBTYPE_PCM:
            is_float = False
        else:
            raise UnsupportedFormat(f"WAVE_FORMAT_EXTENSIBLE with sub-format {subformat!r}")
    elif tag == WAVE_FORMAT_IEEE_FLOAT:
        is_float = True
    elif tag == WAVE_FORMAT_PCM:
        is_float = False
    else:
        raise UnsupportedFormat(f"wFormatTag 0x{tag:04x}")
    fmt = PcmFormat(rate=rate, channels=channels, sample_bits=bits, is_float=is_float,
                    valid_bits=valid_bits if valid_bits and valid_bits != bits else 0,
                    channel_mask=channel_mask)
    if channels < 1 or rate < 8000:
        raise UnsupportedFormat(f"{channels} channel(s) at {rate} Hz")
    try:
        fmt.ffmpeg_format
    except KeyError:
        raise UnsupportedFormat(f"{bits}-bit {'float' if is_float else 'integer'} samples") from None
    if block_align != fmt.block_align:
        raise UnsupportedFormat(f"nBlockAlign {block_align} != {channels} ch x {bits} bit")
    return fmt


# ---------------------------------------------------------------------------
# 2. Timeline
# ---------------------------------------------------------------------------

AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4


@dataclass
class TimelineStats:
    packets: int = 0
    silent_packets: int = 0             # AUDCLNT_BUFFERFLAGS_SILENT
    discontinuities: int = 0            # AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY (glitch reported by WASAPI)
    timestamp_errors: int = 0
    gap_fills: int = 0                  # times silence was inserted because a packet arrived late in time
    gap_fill_frames: int = 0
    idle_fill_frames: int = 0           # silence padded while no packets arrived at all
    overlap_drops: int = 0              # packets whose head overlapped audio already in the stream
    dropped_frames: int = 0
    captured_frames: int = 0            # frames that came from WASAPI (incl. silent-flag packets)
    max_abs_drift_ms: float = 0.0       # largest |packet stamp - expected| seen, before correction

    def to_dict(self) -> dict:
        return asdict(self)


class Timeline:
    """One continuous PCM stream on the QPC clock, built from WASAPI packets.

    Frame k of the stream is the sound that was playing at QPC time
    `epoch_s + k / rate`. The writer side (`add_packet`, `fill_idle`) is the
    capture loop; the reader side (`read`) is the socket sender, and it can
    start from any frame still in the ring (`frame_at(anchor)`), including
    frames before the epoch or already evicted, which read as silence.

    Correction policy: a packet whose QPC stamp differs from where the stream
    expects it by more than `tolerance_s` is re-aligned (silence inserted
    for a late stamp, the overlapping head dropped for an early one).
    Smaller differences are jitter and are left alone, so steady playback is
    never touched; the slow card-vs-QPC drift is corrected in steps of at
    most `tolerance_s`.
    """

    def __init__(self, fmt: PcmFormat, epoch_s: float, *, tolerance_s: float = 0.02,
                 idle_lag_s: float = 0.12, ring_s: float = 15.0):
        self.fmt = fmt
        self.epoch_s = epoch_s
        self.tolerance_s = tolerance_s
        self.idle_lag_s = idle_lag_s
        self.ring_frames = max(1, int(ring_s * fmt.rate))
        self.stats = TimelineStats()
        self._chunks: collections.deque[tuple[int, bytes]] = collections.deque()   # (first frame, data)
        self._ring_start = 0          # first frame still held
        self._total = 0               # frames in the stream so far (next frame index to append)
        self._cond = threading.Condition()
        self._closed = False
        self.first_audio_qpc: float | None = None    # stamp of the first packet WASAPI delivered

    # -- writer side ------------------------------------------------------------------

    @property
    def total_frames(self) -> int:
        with self._cond:
            return self._total

    def expected_time(self) -> float:
        """QPC time of the next frame to be appended."""
        return self.epoch_s + self._total / self.fmt.rate

    def time_of(self, frame: int) -> float:
        return self.epoch_s + frame / self.fmt.rate

    def frame_at(self, t_s: float) -> int:
        return int(round((t_s - self.epoch_s) * self.fmt.rate))

    def silence(self, frames: int) -> bytes:
        return bytes(frames * self.fmt.block_align)      # all-zero is silence for int and float PCM

    def _append(self, data: bytes) -> None:
        frames = len(data) // self.fmt.block_align
        if frames <= 0:
            return
        self._chunks.append((self._total, data))
        self._total += frames
        while self._chunks and self._total - (self._chunks[0][0] + len(self._chunks[0][1]) // self.fmt.block_align) \
                >= self.ring_frames:
            first, old = self._chunks.popleft()
            self._ring_start = first + len(old) // self.fmt.block_align
        self._cond.notify_all()

    def add_packet(self, frames: int, data: bytes | None, qpc_s: float | None, flags: int = 0) -> None:
        """One GetBuffer packet. `data` None (or the SILENT flag) means `frames` of silence."""
        if frames <= 0:
            return
        ba = self.fmt.block_align
        with self._cond:
            st = self.stats
            st.packets += 1
            st.captured_frames += frames
            if flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
                st.discontinuities += 1
            if flags & AUDCLNT_BUFFERFLAGS_SILENT or data is None:
                st.silent_packets += 1 if flags & AUDCLNT_BUFFERFLAGS_SILENT else 0
                payload = self.silence(frames)
            else:
                payload = data[:frames * ba]
                if len(payload) < frames * ba:      # never trust a short copy: pad, and say so via drops=0
                    payload += bytes(frames * ba - len(payload))
            if self.first_audio_qpc is None and qpc_s is not None:
                self.first_audio_qpc = qpc_s
            if flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR or qpc_s is None:
                st.timestamp_errors += 1 if flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR else 0
                self._append(payload)
                return
            drift = qpc_s - self.expected_time()
            st.max_abs_drift_ms = max(st.max_abs_drift_ms, round(abs(drift) * 1000, 3))
            if drift > self.tolerance_s:
                gap = int(round(drift * self.fmt.rate))
                st.gap_fills += 1
                st.gap_fill_frames += gap
                self._append(self.silence(gap))
            elif drift < -self.tolerance_s:
                drop = int(round(-drift * self.fmt.rate))
                st.overlap_drops += 1
                if drop >= frames:
                    st.dropped_frames += frames
                    return
                st.dropped_frames += drop
                payload = payload[drop * ba:]
            self._append(payload)

    def fill_idle(self, now_s: float) -> int:
        """Pad silence up to `now_s - idle_lag_s` when no packets have come.
        Returns the frames added (0 while audio is flowing normally)."""
        with self._cond:
            target = self.frame_at(now_s - self.idle_lag_s)
            missing = target - self._total
            if missing <= 0:
                return 0
            self.stats.idle_fill_frames += missing
            self._append(self.silence(missing))
            return missing

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # -- reader side ------------------------------------------------------------------

    def read(self, start: int, max_frames: int, timeout_s: float = 0.1) -> bytes:
        """Up to `max_frames` frames starting at `start`; waits up to `timeout_s`
        for them to exist. Frames before the epoch or already evicted from
        the ring read as silence. Returns b"" when nothing is available yet
        (or the timeline is closed and drained)."""
        ba = self.fmt.block_align
        with self._cond:
            if start >= self._total and not self._closed:
                self._cond.wait(timeout_s)
            if start >= self._total:
                return b""
            end = min(self._total, start + max_frames)
            out = bytearray()
            pos = start
            if pos < self._ring_start:
                pad = min(end, self._ring_start) - pos
                out += bytes(pad * ba)
                pos += pad
            for first, data in self._chunks:
                n = len(data) // ba
                if first + n <= pos:
                    continue
                if first >= end:
                    break
                lo = max(pos, first) - first
                hi = min(end, first + n) - first
                out += data[lo * ba:hi * ba]
                pos = first + hi
                if pos >= end:
                    break
            return bytes(out)


# ---------------------------------------------------------------------------
# 3. WASAPI via ctypes COM (Windows only)
# ---------------------------------------------------------------------------

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioClient = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"
PKEY_Device_FriendlyName = ("{A45C254E-DF1C-4EFD-8020-67D146A850E0}", 14)
E_RENDER, E_CAPTURE = 0, 1
E_CONSOLE, E_MULTIMEDIA = 0, 1
DEVICE_STATE_ACTIVE = 0x1
DEVICE_STATEMASK_ALL = 0xF
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_E_DEVICE_INVALIDATED = 0x88890004
REFTIMES_PER_SEC = 10_000_000
STATE_NAMES = {1: "active", 2: "disabled", 4: "not present", 8: "unplugged"}


class ComError(OSError):
    def __init__(self, what: str, hresult: int):
        self.hresult = hresult & 0xFFFFFFFF
        super().__init__(f"{what} failed: HRESULT 0x{self.hresult:08X}")


def qpc_now() -> float:
    """The QueryPerformanceCounter clock in seconds: the clock WASAPI stamps
    packets with (u64QPCPosition is the same counter in 100 ns units).
    time.perf_counter() is the same clock in CPython on Windows; this reads
    it directly so the equality is not an assumption. Off Windows,
    perf_counter (tests)."""
    if _QPC is None:
        return time.perf_counter()
    return _QPC()


def _make_qpc():
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        freq = ctypes.c_longlong()
        if not k32.QueryPerformanceFrequency(ctypes.byref(freq)) or freq.value <= 0:
            return None
        f = float(freq.value)
        qpc = k32.QueryPerformanceCounter

        def now() -> float:
            counter = ctypes.c_longlong()       # per call: several threads stamp concurrently
            qpc(ctypes.byref(counter))
            return counter.value / f
        return now
    except Exception:       # fall back to perf_counter; recorded via qpc_source()
        return None


_QPC = _make_qpc()


def qpc_source() -> str:
    return "QueryPerformanceCounter" if _QPC is not None else "time.perf_counter"


@dataclass(frozen=True)
class Endpoint:
    id: str
    name: str
    flow: str                   # "render" | "capture"
    state: str
    is_default: bool = False


class _Com:
    """The ctypes plumbing: GUIDs, vtable calls, PROPVARIANT, CoTaskMemFree.
    Built lazily (and only on Windows) so importing this module is free."""

    def __init__(self):
        import ctypes
        import uuid
        from ctypes import wintypes
        self.ct, self.wt, self.uuid = ctypes, wintypes, uuid
        self.ole32 = ctypes.WinDLL("ole32")
        self.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole32.CoTaskMemFree.restype = None

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                        ("Data4", ctypes.c_ubyte * 8)]

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

        class PROPVARIANT(ctypes.Structure):       # 24 bytes on x64: vt + 3 reserved + 16-byte union
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                        ("r3", ctypes.c_ushort), ("val", ctypes.c_void_p), ("pad", ctypes.c_void_p)]

        class WAVEFORMATEX(ctypes.Structure):
            _pack_ = 1
            _fields_ = [("wFormatTag", ctypes.c_uint16), ("nChannels", ctypes.c_uint16),
                        ("nSamplesPerSec", ctypes.c_uint32), ("nAvgBytesPerSec", ctypes.c_uint32),
                        ("nBlockAlign", ctypes.c_uint16), ("wBitsPerSample", ctypes.c_uint16),
                        ("cbSize", ctypes.c_uint16)]

        class WAVEFORMATEXTENSIBLE(ctypes.Structure):
            _pack_ = 1
            _fields_ = [("Format", WAVEFORMATEX), ("wValidBitsPerSample", ctypes.c_uint16),
                        ("dwChannelMask", ctypes.c_uint32), ("SubFormat", GUID)]

        self.GUID, self.PROPERTYKEY, self.PROPVARIANT = GUID, PROPERTYKEY, PROPVARIANT
        self.WAVEFORMATEX, self.WAVEFORMATEXTENSIBLE = WAVEFORMATEX, WAVEFORMATEXTENSIBLE
        if ctypes.sizeof(WAVEFORMATEX) != 18 or ctypes.sizeof(WAVEFORMATEXTENSIBLE) != 40:
            raise RuntimeError("WAVEFORMATEX layout mismatch")      # a packing bug, caught before any call

    def guid(self, text: str):
        return self.GUID.from_buffer_copy(self.uuid.UUID(text).bytes_le)

    def guid_str(self, g) -> str:
        return str(self.uuid.UUID(bytes_le=bytes(self.ct.string_at(self.ct.addressof(g), 16))))

    def call(self, obj, index: int, what: str, argtypes: list, *args) -> int:
        """Call vtable slot `index` of COM object `obj` (a c_void_p); raise ComError on failure."""
        ct = self.ct
        vtbl = ct.cast(obj, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
        proto = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, *argtypes)
        hr = proto(vtbl[index])(obj, *args)
        if hr < 0:
            raise ComError(what, hr)
        return hr

    def release(self, obj) -> None:
        if obj:
            ct = self.ct
            vtbl = ct.cast(obj, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
            ct.WINFUNCTYPE(ct.c_ulong, ct.c_void_p)(vtbl[2])(obj)

    def free(self, ptr) -> None:
        if ptr:
            self.ole32.CoTaskMemFree(ptr)


class Wasapi:
    """Device enumeration and loopback capture. Every method runs on the
    thread that constructed it (COM is initialized multithreaded there)."""

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("WASAPI loopback needs Windows (run through the peep shim)")
        self.c = _Com()
        ct = self.c.ct
        hr = self.c.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        # S_OK, S_FALSE (already initialized), RPC_E_CHANGED_MODE (STA here already): all usable for MTA-agnostic calls
        if hr < 0 and (hr & 0xFFFFFFFF) != 0x80010106:
            raise ComError("CoInitializeEx", hr)
        self.enum = ct.c_void_p()
        hr = self.c.ole32.CoCreateInstance(ct.byref(self.c.guid(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
                                           ct.byref(self.c.guid(IID_IMMDeviceEnumerator)), ct.byref(self.enum))
        if hr < 0:
            raise ComError("CoCreateInstance(MMDeviceEnumerator)", hr)

    def close(self) -> None:
        self.c.release(self.enum)
        self.enum = None

    # -- devices --------------------------------------------------------------------

    def _device_id(self, dev) -> str:
        ct = self.c.ct
        p = ct.c_void_p()
        self.c.call(dev, 5, "IMMDevice::GetId", [ct.POINTER(ct.c_void_p)], ct.byref(p))
        try:
            return ct.wstring_at(p.value)
        finally:
            self.c.free(p.value)

    def _device_state(self, dev) -> int:
        ct = self.c.ct
        s = ct.c_uint32()
        self.c.call(dev, 6, "IMMDevice::GetState", [ct.POINTER(ct.c_uint32)], ct.byref(s))
        return s.value

    def _friendly_name(self, dev) -> str:
        ct, c = self.c.ct, self.c
        store = ct.c_void_p()
        c.call(dev, 4, "IMMDevice::OpenPropertyStore", [ct.c_uint32, ct.POINTER(ct.c_void_p)], 0, ct.byref(store))
        try:
            key = c.PROPERTYKEY(c.guid(PKEY_Device_FriendlyName[0]), PKEY_Device_FriendlyName[1])
            pv = c.PROPVARIANT()
            c.call(store, 5, "IPropertyStore::GetValue", [ct.c_void_p, ct.c_void_p], ct.byref(key), ct.byref(pv))
            try:
                return ct.wstring_at(pv.val) if pv.vt == 31 and pv.val else ""     # VT_LPWSTR
            finally:
                c.ole32.PropVariantClear(ct.byref(pv))
        finally:
            c.release(store)

    def _default(self, flow: int, role: int = E_CONSOLE):
        ct = self.c.ct
        dev = ct.c_void_p()
        self.c.call(self.enum, 4, "GetDefaultAudioEndpoint", [ct.c_int, ct.c_int, ct.POINTER(ct.c_void_p)],
                    flow, role, ct.byref(dev))
        return dev

    def default_endpoint(self, flow: int = E_RENDER, role: int = E_CONSOLE) -> Endpoint:
        dev = self._default(flow, role)
        try:
            return Endpoint(self._device_id(dev), self._friendly_name(dev), "render" if flow == E_RENDER else "capture",
                            STATE_NAMES.get(self._device_state(dev), "?"), True)
        finally:
            self.c.release(dev)

    def endpoints(self, flow: int = E_RENDER, state_mask: int = DEVICE_STATEMASK_ALL) -> list[Endpoint]:
        ct, c = self.c.ct, self.c
        try:
            default_id = self.default_endpoint(flow).id
        except ComError:
            default_id = None
        coll = ct.c_void_p()
        c.call(self.enum, 3, "EnumAudioEndpoints", [ct.c_int, ct.c_uint32, ct.POINTER(ct.c_void_p)],
               flow, state_mask, ct.byref(coll))
        out = []
        try:
            n = ct.c_uint32()
            c.call(coll, 3, "IMMDeviceCollection::GetCount", [ct.POINTER(ct.c_uint32)], ct.byref(n))
            for i in range(n.value):
                dev = ct.c_void_p()
                c.call(coll, 4, "IMMDeviceCollection::Item", [ct.c_uint32, ct.POINTER(ct.c_void_p)], i, ct.byref(dev))
                try:
                    dev_id = self._device_id(dev)
                    out.append(Endpoint(dev_id, self._friendly_name(dev), "render" if flow == E_RENDER else "capture",
                                        STATE_NAMES.get(self._device_state(dev), "?"), dev_id == default_id))
                finally:
                    c.release(dev)
        finally:
            c.release(coll)
        return out

    def open_device(self, selector: str = "", flow: int = E_RENDER):
        """'' / 'default' -> the default endpoint of `flow`; otherwise an endpoint
        id or a case-insensitive friendly-name match among active endpoints
        (exact name first, then a unique substring). Returns (device ptr, Endpoint)."""
        ct = self.c.ct
        sel = (selector or "").strip()
        if sel.lower() in ("", "default"):
            dev = self._default(flow)
            return dev, Endpoint(self._device_id(dev), self._friendly_name(dev),
                                 "render" if flow == E_RENDER else "capture", "active", True)
        active = self.endpoints(flow, DEVICE_STATE_ACTIVE)
        ep = pick_endpoint(active, sel)
        dev = ct.c_void_p()
        self.c.call(self.enum, 5, "GetDevice", [ct.c_wchar_p, ct.POINTER(ct.c_void_p)], ep.id, ct.byref(dev))
        return dev, ep


def pick_endpoint(endpoints: list[Endpoint], selector: str) -> Endpoint:
    """Endpoint by id, exact friendly name, or unique case-insensitive substring.
    Pure; raises LookupError naming the candidates."""
    sel = selector.strip()
    for ep in endpoints:
        if ep.id == sel:
            return ep
    low = sel.lower()
    exact = [ep for ep in endpoints if ep.name.lower() == low]
    if len(exact) == 1:
        return exact[0]
    partial = [ep for ep in endpoints if low in ep.name.lower()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(repr(ep.name) for ep in endpoints) or "none"
    if not partial:
        raise LookupError(f"no active audio device matches {selector!r}; active: {names}")
    raise LookupError(f"{selector!r} matches several audio devices ({', '.join(repr(p.name) for p in partial)}); "
                      f"use the full name or the id from `peep doctor`")


SOURCES = ("system", "mic")


class EndpointCapture:
    """IAudioClient in shared mode plus its IAudioCaptureClient, for one of:

      source "system"  a render endpoint with AUDCLNT_STREAMFLAGS_LOOPBACK:
                       what the computer plays (the default output unless
                       a device is named)
      source "mic"     a capture endpoint, plain capture: the microphone
                       (the default recording device unless one is named)

    Both stamp every packet with the QPC time of its first frame, which is
    what lets the two sources and the video share one clock. Polled; no
    event handle (event callbacks and loopback did not mix before Windows
    10, and polling every few ms costs nothing at this rate)."""

    def __init__(self, wasapi: Wasapi, source: str = "system", selector: str = "", buffer_s: float = 1.0):
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
        self.w, self.c, self.source = wasapi, wasapi.c, source
        ct, c = self.c.ct, self.c
        self.client = ct.c_void_p()
        self.capture = ct.c_void_p()
        self._mix = None
        flow = E_RENDER if source == "system" else E_CAPTURE
        dev, self.endpoint = wasapi.open_device(selector, flow)
        try:
            c.call(dev, 3, "IMMDevice::Activate",
                   [ct.c_void_p, ct.c_uint32, ct.c_void_p, ct.POINTER(ct.c_void_p)],
                   ct.byref(c.guid(IID_IAudioClient)), CLSCTX_ALL, None, ct.byref(self.client))
        finally:
            c.release(dev)
        mix = ct.c_void_p()
        c.call(self.client, 8, "IAudioClient::GetMixFormat", [ct.POINTER(ct.c_void_p)], ct.byref(mix))
        self._mix = mix
        self.format = self._read_format(mix)
        default_period, min_period = ct.c_longlong(), ct.c_longlong()
        c.call(self.client, 9, "IAudioClient::GetDevicePeriod", [ct.POINTER(ct.c_longlong)] * 2,
               ct.byref(default_period), ct.byref(min_period))
        self.period_s = default_period.value / REFTIMES_PER_SEC
        c.call(self.client, 3, "IAudioClient::Initialize",
               [ct.c_int, ct.c_uint32, ct.c_longlong, ct.c_longlong, ct.c_void_p, ct.c_void_p],
               AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK if source == "system" else 0,
               int(buffer_s * REFTIMES_PER_SEC), 0,
               mix, None)
        frames = ct.c_uint32()
        c.call(self.client, 4, "IAudioClient::GetBufferSize", [ct.POINTER(ct.c_uint32)], ct.byref(frames))
        self.buffer_frames = frames.value
        c.call(self.client, 14, "IAudioClient::GetService", [ct.c_void_p, ct.POINTER(ct.c_void_p)],
               ct.byref(c.guid(IID_IAudioCaptureClient)), ct.byref(self.capture))

    def _read_format(self, mix) -> PcmFormat:
        ct, c = self.c.ct, self.c
        wf = ct.cast(mix, ct.POINTER(c.WAVEFORMATEX)).contents
        sub, valid, mask = None, 0, 0
        if wf.wFormatTag == WAVE_FORMAT_EXTENSIBLE and wf.cbSize >= 22:
            ext = ct.cast(mix, ct.POINTER(c.WAVEFORMATEXTENSIBLE)).contents
            sub, valid, mask = c.guid_str(ext.SubFormat), ext.wValidBitsPerSample, ext.dwChannelMask
        return format_from_waveformat(wf.wFormatTag, wf.nChannels, wf.nSamplesPerSec, wf.wBitsPerSample,
                                      wf.nBlockAlign, sub, valid, mask)

    def start(self) -> None:
        self.c.call(self.client, 10, "IAudioClient::Start", [])

    def stop(self) -> None:
        try:
            self.c.call(self.client, 11, "IAudioClient::Stop", [])
        except ComError as exc:
            event(log, logging.WARNING, "wasapi.stop_failed", error=str(exc))

    def packets(self):
        """Yield (frames, data|None, qpc_s, flags) for every packet available now."""
        ct, c = self.c.ct, self.c
        ba = self.format.block_align
        size = ct.c_uint32()
        data, frames, flags = ct.c_void_p(), ct.c_uint32(), ct.c_uint32()
        devpos, qpcpos = ct.c_uint64(), ct.c_uint64()
        while True:
            c.call(self.capture, 5, "IAudioCaptureClient::GetNextPacketSize", [ct.POINTER(ct.c_uint32)],
                   ct.byref(size))
            if size.value == 0:
                return
            c.call(self.capture, 3, "IAudioCaptureClient::GetBuffer",
                   [ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32),
                    ct.POINTER(ct.c_uint64), ct.POINTER(ct.c_uint64)],
                   ct.byref(data), ct.byref(frames), ct.byref(flags), ct.byref(devpos), ct.byref(qpcpos))
            n, fl = frames.value, flags.value
            try:
                payload = None if (fl & AUDCLNT_BUFFERFLAGS_SILENT or not data.value) else ct.string_at(data.value, n * ba)
            finally:
                c.call(self.capture, 4, "IAudioCaptureClient::ReleaseBuffer", [ct.c_uint32], n)
            yield n, payload, qpcpos.value / REFTIMES_PER_SEC, fl

    def close(self) -> None:
        self.c.release(self.capture)
        self.c.release(self.client)
        if self._mix is not None:
            self.c.free(self._mix.value)
        self.capture = self.client = self._mix = None


# ---------------------------------------------------------------------------
# 4. The server process
# ---------------------------------------------------------------------------

POLL_S = 0.005
SEND_CHUNK_S = 0.02
ANCHOR_WAIT_S = 1.0         # after ffmpeg connects, how long to wait for the recorder's anchor


class Emitter:
    """The child's protocol: one JSON object per line on stdout (`emit`), and
    structured log lines on stderr (`log`). Thread-safe."""

    def __init__(self, out=None, err=None):
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        self._lock = threading.Lock()

    def emit(self, name: str, **fields) -> None:
        line = json.dumps({"event": name, **fields}, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.out.write(line + "\n")
                self.out.flush()
            except (OSError, ValueError):
                pass      # parent gone; the job object is about to end us anyway

    def log(self, level: str, name: str, **fields) -> None:
        with self._lock:
            try:
                self.err.write(f"{level} {kv(name, **fields)}\n")
                self.err.flush()
            except (OSError, ValueError):
                pass


class AnchorBox:
    """Anchors from the recorder (QPC seconds), newest wins, each used once."""

    def __init__(self):
        self._cond = threading.Condition()
        self._value: float | None = None
        self._fresh = False

    def put(self, value: float) -> None:
        with self._cond:
            self._value, self._fresh = value, True
            self._cond.notify_all()

    def take(self, wait_s: float) -> float | None:
        with self._cond:
            if not self._fresh:
                self._cond.wait(wait_s)
            if not self._fresh:
                return None
            self._fresh = False
            return self._value


def choose_start_frame(tl: Timeline, anchor_s: float | None, accept_s: float, fallback_lead_s: float) -> tuple[int, str]:
    """The stream frame ffmpeg's audio t=0 will be: the anchor's frame when the
    recorder sent one, else the connect time minus `fallback_lead_s`."""
    if anchor_s is not None:
        return tl.frame_at(anchor_s), "anchor"
    return tl.frame_at(accept_s - fallback_lead_s), "fallback"


def send_stream(sock, tl: Timeline, start: int, stop: threading.Event, stall: threading.Event,
                chunk_frames: int, clock: Callable[[], float] = time.monotonic) -> tuple[int, str]:
    """Write the timeline from `start` to `sock` until stop, or the peer closes.
    Never blocks for long: the socket has a timeout and every wait re-checks
    `stop`, so stopping the server can never hang on a full socket. A
    partial send keeps its frame alignment (the remainder is sent next).
    `stall` (diagnostics only) pauses sending without closing, to prove
    ffmpeg's -rw_timeout handles a hung writer. Returns (frames sent, why)."""
    ba = tl.fmt.block_align
    cursor = start                    # next frame to read from the timeline
    pending = memoryview(b"")         # read but not yet fully sent

    def sent() -> int:
        return cursor - start - len(pending) // ba

    while not stop.is_set():
        if stall.is_set():
            stop.wait(0.05)
            continue
        if not pending:
            data = tl.read(cursor, chunk_frames, timeout_s=0.1)
            if not data:
                continue
            cursor += len(data) // ba
            pending = memoryview(data)
        try:
            n = sock.send(pending)
        except socket.timeout:
            continue
        except OSError as exc:        # ConnectionReset/Aborted/BrokenPipe: ffmpeg closed its end
            return sent(), f"peer closed ({type(exc).__name__})"
        pending = pending[n:]
    return sent(), "stopped"


def tcp_listener(host: str = "127.0.0.1", port: int = 0):
    """The production listener: TCP on loopback, ephemeral port. Returns
    (listening socket, the URL ffmpeg opens)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind((host, port))
    listener.listen(1)
    return listener, f"tcp://{host}:{listener.getsockname()[1]}"


class CaptureServer:
    """The child process's whole life: capture thread (COM, WASAPI, Timeline),
    accept loop, one sender at a time, stdin commands.

    stdin commands, one per line:  anchor <qpc_seconds> | stop | stall | resume
    EOF on stdin = stop (the recorder died or closed us)."""

    def __init__(self, source: str = "system", selector: str = "", host: str = "127.0.0.1", port: int = 0,
                 fallback_lead_s: float = 0.0, idle_exit_s: float = 300.0, emitter: Emitter | None = None,
                 capture_factory: Callable | None = None, stdin=None, clock: Callable[[], float] = qpc_now,
                 listen: Callable | None = None):
        self.source, self.selector, self.host, self.port = source, selector, host, port
        self.fallback_lead_s = fallback_lead_s
        self.idle_exit_s = idle_exit_s
        self.em = emitter or Emitter()
        self.capture_factory = capture_factory or self._wasapi_capture
        # () -> (listening socket, URL for ffmpeg). Production: TCP loopback. The tests pass an
        # abstract AF_UNIX listener, because bale validates inside a network-off namespace where
        # loopback TCP connects fail (found the hard way: the first A.1 apply hung on it).
        self.listen = listen or (lambda: tcp_listener(self.host, self.port))
        self.stdin = stdin if stdin is not None else sys.stdin
        self.clock = clock
        self.stop = threading.Event()
        self.stall = threading.Event()
        self.anchors = AnchorBox()
        self.ready = threading.Event()
        self.timeline: Timeline | None = None
        self.capture_error: str | None = None
        self.capture_info: dict = {}
        self.connections = 0

    @staticmethod
    def _wasapi_capture(source: str, selector: str):
        w = Wasapi()
        return w, EndpointCapture(w, source, selector)

    # -- capture thread -----------------------------------------------------------------

    def _capture_main(self) -> None:
        wasapi = cap = None
        try:
            wasapi, cap = self.capture_factory(self.source, self.selector)
            epoch = self.clock()
            cap.start()
            self.timeline = Timeline(cap.format, epoch)
            self.capture_info = {"source": self.source, "endpoint": asdict(cap.endpoint), "format": cap.format.to_dict(),
                                 "period_ms": round(getattr(cap, "period_s", 0) * 1000, 2),
                                 "buffer_frames": getattr(cap, "buffer_frames", None), "epoch_qpc": epoch,
                                 "clock": qpc_source()}
            self.ready.set()
            while not self.stop.is_set():
                for frames, data, qpc_s, flags in cap.packets():
                    self.timeline.add_packet(frames, data, qpc_s, flags)
                self.timeline.fill_idle(self.clock())
                time.sleep(POLL_S)
        except Exception as exc:     # surfaced to the parent as an error event + exit code
            self.capture_error = f"{type(exc).__name__}: {exc}"
            self.em.log("ERROR", "wasapi.capture_failed", source=self.source, error=self.capture_error)
            self.ready.set()
            self.stop.set()
        finally:
            if self.timeline is not None:
                self.timeline.close()
            if cap is not None:
                cap.stop()
                cap.close()
            if wasapi is not None:
                wasapi.close()

    # -- stdin --------------------------------------------------------------------------

    def _stdin_main(self) -> None:
        try:
            for raw in iter(self.stdin.readline, ""):
                line = raw.strip()
                if not line:
                    continue
                cmd, _, arg = line.partition(" ")
                if cmd == "anchor":
                    try:
                        self.anchors.put(float(arg))
                        self.em.log("INFO", "wasapi.anchor", qpc=float(arg))
                    except ValueError:
                        self.em.log("WARNING", "wasapi.bad_anchor", line=line)
                elif cmd == "stop":
                    break
                elif cmd == "stall":
                    self.stall.set()
                    self.em.log("WARNING", "wasapi.stall", note="diagnostic: sending paused")
                elif cmd == "resume":
                    self.stall.clear()
                else:
                    self.em.log("WARNING", "wasapi.unknown_command", line=line)
        except (OSError, ValueError) as exc:
            self.em.log("WARNING", "wasapi.stdin_failed", error=repr(exc))
        self.stop.set()

    # -- main -----------------------------------------------------------------------------

    def run(self) -> int:
        cap_thread = threading.Thread(target=self._capture_main, name="wasapi-capture", daemon=True)
        cap_thread.start()
        threading.Thread(target=self._stdin_main, name="wasapi-stdin", daemon=True).start()
        self.ready.wait(15)
        if self.capture_error or self.timeline is None:
            err = self.capture_error or "capture did not start within 15 s"
            self.em.emit("error", error=err)
            return 3
        try:
            listener, url = self.listen()
        except OSError as exc:              # reported, never a silent dead thread
            self.stop.set()
            self.em.log("ERROR", "wasapi.listen_failed", error=repr(exc))
            self.em.emit("error", error=f"cannot listen for ffmpeg: {exc}")
            return 4
        listener.settimeout(0.2)
        self._inet = listener.family in (socket.AF_INET, socket.AF_INET6)
        port = listener.getsockname()[1] if self._inet else 0
        self.em.emit("ready", port=port, url=url, host=self.host, pid=os.getpid(), **self.capture_info)
        self.em.log("INFO", "wasapi.ready", url=url, endpoint=self.capture_info["endpoint"]["name"],
                    format=self.timeline.fmt.describe())
        idle_since = time.monotonic()
        try:
            while not self.stop.is_set():
                try:
                    sock, peer = listener.accept()
                except socket.timeout:
                    if time.monotonic() - idle_since > self.idle_exit_s:
                        self.em.log("ERROR", "wasapi.idle_exit", seconds=self.idle_exit_s)
                        self.em.emit("error", error=f"no connection for {self.idle_exit_s:g}s")
                        return 4
                    continue
                self._serve(sock, peer)
                idle_since = time.monotonic()
        finally:
            listener.close()
            self.stop.set()
            cap_thread.join(5)
            stats = self.timeline.stats.to_dict() if self.timeline else {}
            self.em.emit("stats", connections=self.connections, timeline=stats,
                         first_audio_qpc=self.timeline.first_audio_qpc if self.timeline else None,
                         capture_error=self.capture_error)
        return 3 if self.capture_error else 0

    def _serve(self, sock, peer) -> None:
        tl = self.timeline
        accept_s = self.clock()
        self.connections += 1
        n = self.connections
        if self._inet and peer[0] not in ("127.0.0.1", "::1"):
            self.em.log("WARNING", "wasapi.refused_peer", peer=str(peer))
            sock.close()
            return
        anchor = self.anchors.take(ANCHOR_WAIT_S)
        start, source = choose_start_frame(tl, anchor, accept_s, self.fallback_lead_s)
        sock.settimeout(0.25)
        if self._inet:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.em.emit("connected", n=n, accept_qpc=accept_s, anchor_qpc=anchor, start_source=source,
                     start_frame=start, first_sample_qpc=tl.time_of(start), total_frames=tl.total_frames)
        chunk = max(1, int(SEND_CHUNK_S * tl.fmt.rate))
        sent, why = send_stream(sock, tl, start, self.stop, self.stall, chunk)
        try:
            sock.close()
        except OSError:
            pass
        self.em.emit("disconnected", n=n, frames_sent=sent, seconds_sent=round(sent / tl.fmt.rate, 3), reason=why)


# ---------------------------------------------------------------------------
# 5. Parent side: CaptureProcess
# ---------------------------------------------------------------------------

CREATE_NO_WINDOW = 0x08000000


class CaptureStartError(RuntimeError):
    """The loopback child could not start; the message is user-facing."""


@dataclass
class CaptureReady:
    port: int
    format: PcmFormat
    endpoint: dict
    epoch_qpc: float
    info: dict = field(default_factory=dict)
    url: str = ""                       # what ffmpeg opens; "" = tcp://127.0.0.1:<port>

    @property
    def stream_url(self) -> str:
        return self.url or f"tcp://127.0.0.1:{self.port}"


def server_argv(python: str, source: str, selector: str, fallback_lead_ms: int = 0) -> list[str]:
    argv = [python, "-X", "utf8", "-m", "peep.wasapi", "serve", "--source", source,
            "--fallback-lead-ms", str(int(fallback_lead_ms))]
    if selector:
        argv += ["--device", selector]
    return argv


def package_root() -> str:
    """The directory holding the `peep` package (the child's PYTHONPATH)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CaptureProcess:
    """The recorder's handle on the child: start it, read its protocol
    (stdout JSON lines) and logs (stderr), send anchors, stop it. Shaped like
    recorder.FfmpegProcess: argv logged verbatim first, stderr drained into
    the log and a tail, exit code and tail surfaced on failure."""

    def __init__(self, popen: Callable = subprocess.Popen, job=None, python: str | None = None,
                 tail_lines: int = 100, argv_factory: Callable[..., list[str]] | None = None):
        self._popen = popen
        self._argv_factory = argv_factory or server_argv      # tests point this at tests/fake_wasapi.py
        self._job = job
        self.python = python or sys.executable
        self.proc = None
        self.argv: list[str] = []
        self.events: list[dict] = []
        self.tail = collections.deque(maxlen=tail_lines)
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self.ready_info: CaptureReady | None = None
        self.exit_code: int | None = None

    def start(self, source: str, selector: str = "", ready_timeout_s: float = 10.0,
              fallback_lead_ms: int = 0) -> CaptureReady:
        self.source = source
        self.argv = self._argv_factory(self.python, source, selector, fallback_lead_ms)
        env = dict(os.environ)
        env["PYTHONPATH"] = package_root() + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        event(log, logging.INFO, "wasapi.argv", argv=self.argv, cmdline=subprocess.list2cmdline(self.argv))
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        try:
            self.proc = self._popen(self.argv, **kwargs)
        except OSError as exc:
            raise CaptureStartError(f"could not start the loopback capture ({exc})") from exc
        event(log, logging.INFO, "wasapi.start", pid=getattr(self.proc, "pid", None))
        if self._job is not None:
            self._job.assign(self.proc)
        for target, name in ((self._read_stdout, "wasapi-stdout"), (self._read_stderr, "wasapi-stderr")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline:
            if self._ready.wait(0.05):
                break
            if self.proc.poll() is not None:
                self._join(1)
                break
        info = self.ready_info
        if info is None:
            err = next((e.get("error") for e in self.events if e.get("event") == "error"), None)
            if err is not None:                 # the child said why; let it exit on its own
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            rc = self.proc.poll()
            why = err or (f"exited {rc}" if rc is not None else f"no answer in {ready_timeout_s:g}s (killed)")
            if rc is None:
                self.kill("no ready line")
            self._join(1)
            self._close_pipes()
            self.exit_code = rc if rc is not None else self.proc.poll()
            event(log, logging.ERROR, "wasapi.start_failed", source=source, error=why, exit_code=self.exit_code,
                  stderr_tail=self.stderr_tail(8))
            raise CaptureStartError(f"{source} audio capture failed to start: {why}")
        return info

    def _close_pipes(self) -> None:
        for stream in (getattr(self.proc, "stdin", None), getattr(self.proc, "stdout", None),
                       getattr(self.proc, "stderr", None)):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def _read_stdout(self) -> None:
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    event(log, logging.WARNING, "wasapi.bad_line", line=line[:200])
                    continue
                with self._lock:
                    self.events.append(msg)
                name = msg.get("event")
                event(log, logging.INFO if name != "error" else logging.ERROR, f"wasapi.child_{name}",
                      **{k: v for k, v in msg.items() if k != "event"})
                if name == "ready":
                    try:
                        self.ready_info = CaptureReady(int(msg["port"]), PcmFormat.from_dict(msg["format"]),
                                                        msg.get("endpoint") or {}, float(msg["epoch_qpc"]), msg,
                                                        msg.get("url") or "")
                    except (KeyError, TypeError, ValueError) as exc:
                        event(log, logging.ERROR, "wasapi.bad_ready", error=repr(exc), line=line[:300])
                    self._ready.set()
                elif name == "error":
                    self._ready.set()
        except (OSError, ValueError) as exc:
            event(log, logging.WARNING, "wasapi.stdout_read_failed", error=repr(exc))

    def _read_stderr(self) -> None:
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                with self._lock:
                    self.tail.append(line)
                log.log(logging.WARNING if line.startswith(("WARNING", "ERROR")) else logging.DEBUG,
                        "wasapi| %s", line)
        except (OSError, ValueError) as exc:
            event(log, logging.WARNING, "wasapi.stderr_read_failed", error=repr(exc))

    def _join(self, timeout_s: float) -> None:
        for t in self._threads:
            t.join(timeout_s)

    def send(self, line: str) -> bool:
        try:
            self.proc.stdin.write((line + "\n").encode("ascii"))
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError, AttributeError) as exc:
            event(log, logging.WARNING, "wasapi.send_failed", line=line, error=repr(exc))
            return False

    def send_anchor(self, qpc_s: float) -> bool:
        ok = self.send(f"anchor {qpc_s:.7f}")
        event(log, logging.INFO, "wasapi.anchor_sent", qpc=round(qpc_s, 7), ok=ok)
        return ok

    def poll(self):
        return self.proc.poll() if self.proc else None

    def stop(self, timeout_s: float = 3.0) -> int | None:
        """Ask the child to stop (stdin 'stop' then EOF); kill it if it has not
        exited within the timeout. Never blocks longer than ~timeout_s + 2 s."""
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.send("stop")
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self.kill(f"no exit {timeout_s:g}s after stop")
        self._join(2)
        self._close_pipes()
        self.exit_code = self.proc.poll()
        event(log, logging.INFO if self.exit_code == 0 else logging.ERROR, "wasapi.exit",
              pid=getattr(self.proc, "pid", None), exit_code=self.exit_code)
        return self.exit_code

    def kill(self, reason: str) -> None:
        event(log, logging.ERROR, "wasapi.kill", pid=getattr(self.proc, "pid", None), reason=reason)
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            event(log, logging.ERROR, "wasapi.kill_failed", error=repr(exc))

    def last(self, name: str) -> dict | None:
        with self._lock:
            for e in reversed(self.events):
                if e.get("event") == name:
                    return e
        return None

    def all(self, name: str) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e.get("event") == name]

    def stderr_tail(self, n: int = 15) -> list[str]:
        with self._lock:
            return list(self.tail)[-n:]


# ---------------------------------------------------------------------------
# 6. Capture test (doctor) + CLI entry
# ---------------------------------------------------------------------------

def tone_wav(freq_hz: float = 1000.0, ms: int = 150, rate: int = 48000, amplitude: float = 0.5) -> bytes:
    """A short sine burst as WAV bytes (16-bit mono), with 3 ms ramps so it
    starts and ends without a click. Pure; the clap and the doctor's test tone."""
    import io
    import math
    import struct
    import wave
    n = int(rate * ms / 1000)
    ramp = max(1, int(rate * 0.003))
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / ramp, (n - 1 - i) / ramp)
        frames += struct.pack("<h", int(32767 * amplitude * env * math.sin(2 * math.pi * freq_hz * i / rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def peak_level(data: bytes, fmt: PcmFormat) -> float:
    """Largest absolute sample, 0..1 (float or integer PCM). Pure."""
    import array
    if not data:
        return 0.0
    if fmt.is_float and fmt.sample_bits == 32:
        a = array.array("f")
        a.frombytes(data[:len(data) - len(data) % 4])
        return max((abs(x) for x in a), default=0.0)
    if not fmt.is_float and fmt.sample_bits == 16:
        a = array.array("h")
        a.frombytes(data[:len(data) - len(data) % 2])
        return max((abs(x) for x in a), default=0) / 32768.0
    if not fmt.is_float and fmt.sample_bits == 32:
        a = array.array("i")
        a.frombytes(data[:len(data) - len(data) % 4])
        return max((abs(x) for x in a), default=0) / 2147483648.0
    if fmt.is_float and fmt.sample_bits == 64:
        a = array.array("d")
        a.frombytes(data[:len(data) - len(data) % 8])
        return max((abs(x) for x in a), default=0.0)
    if fmt.sample_bits == 24:
        best = 0
        for i in range(0, len(data) - 2, 3):
            v = int.from_bytes(data[i:i + 3], "little", signed=True)
            best = max(best, abs(v))
        return best / 8388608.0
    return 0.0


def play_wav_async(path: str) -> bool:
    """Play a WAV file through the default output without blocking (winsound)."""
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        return True
    except Exception as exc:          # no winsound off Windows; no output device
        event(log, logging.WARNING, "wasapi.tone_failed", error=repr(exc))
        return False


def capture_test(source: str = "system", selector: str = "", seconds: float = 2.0, tone_at_s: float | None = 0.6,
                 tone_path: str | None = None, threshold: float = 0.01) -> dict:
    """Open the endpoint, capture `seconds`, optionally play a test tone part
    way in, and report what arrived: the facts `peep doctor` prints, and the
    proof that loopback capture works on this endpoint. Runs in-process (call it
    from a child: see `python -m peep.wasapi test`)."""
    w = Wasapi()
    cap = None
    try:
        cap = EndpointCapture(w, source, selector)
        fmt = cap.format
        epoch = qpc_now()
        tl = Timeline(fmt, epoch)
        cap.start()
        played_at = None
        silent_window_packets = 0
        first_loud_qpc = None
        peak = 0.0
        end = epoch + seconds
        while qpc_now() < end:
            for frames, data, qpc_s, flags in cap.packets():
                tl.add_packet(frames, data, qpc_s, flags)
                if played_at is None:
                    silent_window_packets += 1
                if data:
                    p = peak_level(data, fmt)
                    peak = max(peak, p)
                    if p >= threshold and first_loud_qpc is None and played_at is not None:
                        first_loud_qpc = qpc_s
            tl.fill_idle(qpc_now())
            if tone_at_s is not None and played_at is None and qpc_now() - epoch >= tone_at_s and tone_path:
                played_at = qpc_now()
                play_wav_async(tone_path)
            time.sleep(POLL_S)
        return {"ok": True, "source": source, "endpoint": asdict(cap.endpoint), "format": fmt.to_dict(), "describe": fmt.describe(),
                "period_ms": round(cap.period_s * 1000, 2), "buffer_frames": cap.buffer_frames,
                "seconds": seconds, "packets_before_tone": silent_window_packets,
                "tone_played": played_at is not None, "peak": round(peak, 4),
                "non_silent": peak >= threshold,
                "tone_latency_ms": round((first_loud_qpc - played_at) * 1000, 1)
                if (first_loud_qpc is not None and played_at is not None) else None,
                "timeline_seconds": round(tl.total_frames / fmt.rate, 3), "stats": tl.stats.to_dict()}
    finally:
        if cap is not None:
            cap.stop()
            cap.close()
        w.close()


def list_devices() -> dict:
    w = Wasapi()
    try:
        out = {"render": [asdict(e) for e in w.endpoints(E_RENDER)],
               "capture": [asdict(e) for e in w.endpoints(E_CAPTURE)]}
        for flow, key in ((E_RENDER, "default_render"), (E_CAPTURE, "default_capture")):
            try:
                out[key] = asdict(w.default_endpoint(flow))
            except ComError as exc:
                out[key] = {"error": str(exc)}
        mix = {}
        for key, source in (("render", "system"), ("capture", "mic")):
            for e in out[key]:
                if e["state"] != "active":
                    continue
                try:
                    cap = EndpointCapture(w, source, e["id"])
                    mix[e["name"]] = cap.format.to_dict()
                    cap.close()
                except (ComError, UnsupportedFormat, LookupError) as exc:
                    mix[e["name"]] = {"error": str(exc)}
        out["mix_formats"] = mix
        return out
    finally:
        w.close()


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m peep.wasapi",
                                description="peep's WASAPI capture: system audio (loopback) and the microphone; "
                                            "normally started by the recorder")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="capture and serve raw PCM to one TCP client at a time")
    s.add_argument("--source", choices=SOURCES, default="system")
    s.add_argument("--device", default="", help="endpoint: '' = the default for the source, an id, or a name")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--fallback-lead-ms", type=int, default=0,
                   help="when no anchor arrives, start this far before the connect time")
    s.add_argument("--idle-exit-s", type=float, default=300.0)
    t = sub.add_parser("test", help="capture a few seconds (optionally with a test tone); print JSON")
    t.add_argument("--source", choices=SOURCES, default="system")
    t.add_argument("--device", default="")
    t.add_argument("--seconds", type=float, default=2.0)
    t.add_argument("--tone", default=None, help="WAV file to play part way in")
    sub.add_parser("list", help="list endpoints, defaults and mix formats as JSON")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.cmd == "serve":
        return CaptureServer(args.source, args.device, args.host, args.port, args.fallback_lead_ms / 1000,
                              args.idle_exit_s).run()
    try:
        result = list_devices() if args.cmd == "list" else capture_test(args.source, args.device, args.seconds,
                                                                          0.6 if args.tone else None, args.tone)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
