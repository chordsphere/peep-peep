"""ffmpeg argv assembly — pure functions from a CaptureSpec to a list of
strings. Nothing here runs a process; `recorder` does, after logging the
argv verbatim.

The three video pipelines, each verified on the laptop by the session-A
probes (2026-10-05, ffmpeg 9.0.2 Gyan full build, Intel Arc 140V):

  qsv           ddagrab (D3D11 frames) -> hwmap to QSV -> vpp_qsv converts
                BGRA to NV12 with BT.709 limited range on the GPU -> h264_qsv.
                No frame ever touches system memory. Measured colours exact
                (black 0, grey 126, magenta 253/0/252, green 0/254/0).
  qsv-download  ddagrab -> hwdownload -> swscale BT.709 -> NV12 -> h264_qsv.
                Same output, costs a CPU copy; the first fallback.
  x264          ddagrab -> hwdownload -> swscale BT.709 -> yuv420p -> libx264.
                The documented software fallback.

Without the explicit conversion the GPU path hands the encoder BGRA
tagged `gbr`/full range and players lift black to 16 — that is why
`filter_graph` always converts. The graph is one function so the smoke
test's findings can be applied in one place.

Audio (session A.1) arrives as `AudioInput`s: raw PCM from our WASAPI
children over localhost TCP (system audio and, by default, the mic), or A's
dshow microphone. WASAPI streams are aligned before ffmpeg sees them (the
child starts each stream at the video's first-frame instant), so they need
no timestamp tricks; the dshow mic is shifted in the *samples* (adelay /
atrim), never with -itsoffset: the A.1 probe found -itsoffset reaching the
.mp4 only as an edit list, which a player is free to ignore.

Sections:
  1. Spec                       (~line 43)
  2. Filter graph + encoder     (~line 90)
  3. Audio inputs and graph     (~line 128)
  4. Capture / remux / probe argv (~line 220)
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import DEFAULT_MIC, Config, parse_scale

# ---------------------------------------------------------------------------
# 1. Spec
# ---------------------------------------------------------------------------

ENCODERS = {"qsv": "h264_qsv", "qsv-download": "h264_qsv", "x264": "libx264"}
COLOR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
              "-color_range", "tv"]


@dataclass(frozen=True)
class CaptureSpec:
    ffmpeg: str
    output: str                          # capture file (Matroska), or "-" with output_format "null"
    pipeline: str = "qsv"
    fps: int = 30
    scale: tuple[int, int] | None = None
    output_idx: int = 0
    draw_mouse: bool = True
    qsv_preset: str = "medium"
    qsv_global_quality: int = 25
    x264_preset: str = "veryfast"
    x264_crf: int = 20
    gop_seconds: int = 2
    audio_inputs: tuple = ()             # AudioInput, in ffmpeg input order after the screen; () = no audio
    audio_mix: str = "mix"               # two inputs: "mix" (one amix track) | "separate" (one track each)
    audio_bitrate: str = "160k"
    rw_timeout_s: float = 3.0            # TCP inputs: a stalled writer costs ffmpeg at most this on its read
    output_format: str = "matroska"      # "null" for doctor's capture test
    duration_s: float | None = None      # -t; None = until 'q'
    loglevel: str = "info"


def spec_from_config(cfg: Config, *, ffmpeg: str, output: str, pipeline: str | None = None,
                     scale: str | None = None, fps: int | None = None, audio_inputs=(),
                     **overrides) -> CaptureSpec:
    """The CaptureSpec a `rec` would use, with CLI overrides applied. Audio
    inputs come from `audio_inputs_for` once the capture children are up
    (their ports and formats are only known then)."""
    v, a = cfg.video, cfg.audio
    return CaptureSpec(
        ffmpeg=ffmpeg, output=output, pipeline=pipeline or v.pipeline, fps=fps or v.fps,
        scale=parse_scale(v.scale if scale is None else scale), output_idx=v.output_idx,
        draw_mouse=v.draw_mouse, qsv_preset=v.qsv_preset, qsv_global_quality=v.qsv_global_quality,
        x264_preset=v.x264_preset, x264_crf=v.x264_crf, gop_seconds=v.gop_seconds,
        audio_inputs=tuple(audio_inputs), audio_mix=a.mix, audio_bitrate=a.bitrate, **overrides)


# ---------------------------------------------------------------------------
# 2. Filter graph + encoder
# ---------------------------------------------------------------------------


def ddagrab_source(spec: CaptureSpec) -> str:
    return (f"ddagrab=output_idx={spec.output_idx}:framerate={spec.fps}"
            f":draw_mouse={1 if spec.draw_mouse else 0}")


def filter_graph(pipeline: str, scale: tuple[int, int] | None = None) -> str:
    """The -vf chain for a pipeline (see module docstring). The only place
    colour conversion and scaling are decided."""
    if pipeline == "qsv":
        size = f"w={scale[0]}:h={scale[1]}:" if scale else ""
        return ("hwmap=derive_device=qsv,format=qsv,"
                f"vpp_qsv={size}format=nv12:out_color_matrix=bt709:out_range=tv")
    if pipeline in ("qsv-download", "x264"):
        size = f"w={scale[0]}:h={scale[1]}:flags=lanczos:" if scale else ""
        pix = "nv12" if pipeline == "qsv-download" else "yuv420p"
        return f"hwdownload,format=bgra,scale={size}out_color_matrix=bt709:out_range=tv,format={pix}"
    raise ValueError(f"unknown pipeline {pipeline!r}")


def video_codec_args(spec: CaptureSpec) -> list[str]:
    gop = str(spec.fps * spec.gop_seconds)
    if spec.pipeline in ("qsv", "qsv-download"):
        return ["-c:v", "h264_qsv", "-preset", spec.qsv_preset,
                "-global_quality", str(spec.qsv_global_quality), "-g", gop]
    if spec.pipeline == "x264":
        return ["-c:v", "libx264", "-preset", spec.x264_preset, "-crf", str(spec.x264_crf), "-g", gop]
    raise ValueError(f"unknown pipeline {spec.pipeline!r}")


def output_size(spec: CaptureSpec, capture_size: tuple[int, int] | None) -> tuple[int, int] | None:
    return spec.scale or capture_size


# ---------------------------------------------------------------------------
# 3. Audio inputs and graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioInput:
    label: str                           # "system" | "mic" (also the track title with mix = separate)
    kind: str                            # "stream" (a WASAPI child's socket) | "dshow"
    url: str = ""                        # tcp://127.0.0.1:PORT (tests: unix:@name)
    sample_format: str = ""              # raw demuxer: f32le | s16le | s24le | s32le | f64le
    rate: int = 0
    channels: int = 0
    device: str = ""                     # dshow device name
    buffer_ms: int = 50                  # dshow audio_buffer_size
    shift_ms: int = 0                    # sample-domain shift: + pads silence (adelay), - drops the start (atrim)
    gain: float = 1.0

    def input_args(self, rw_timeout_s: float) -> list[str]:
        if self.kind == "stream":
            return ["-thread_queue_size", "1024", "-rw_timeout", str(int(rw_timeout_s * 1_000_000)),
                    "-f", self.sample_format, "-ar", str(self.rate), "-ac", str(self.channels), "-i", self.url]
        if self.kind == "dshow":
            return ["-thread_queue_size", "1024", "-f", "dshow", "-audio_buffer_size", str(self.buffer_ms),
                    "-i", f"audio={self.device}"]
        raise ValueError(f"unknown audio input kind {self.kind!r}")

    def filters(self) -> list[str]:
        out = []
        if self.shift_ms > 0:
            out.append(f"adelay=delays={self.shift_ms}:all=1")
        elif self.shift_ms < 0:
            out.append(f"atrim=start={-self.shift_ms / 1000:.3f},asetpts=PTS-STARTPTS")
        if self.gain != 1.0:
            out.append(f"volume={self.gain:g}")
        return out


def audio_inputs_for(cfg: Config, sources: tuple[str, ...], ready: dict) -> list[AudioInput]:
    """The ffmpeg audio inputs for the sources that are running. `ready`
    maps a WASAPI-backed source to its child's CaptureReady (port, format);
    a source missing from it (its child failed) is simply absent. The mic
    on the dshow backend needs no child. System first, always."""
    a = cfg.audio
    out = []
    for src in sources:
        gain = a.system_gain if src == "system" else a.mic_gain
        if src == "mic" and a.mic_backend == "dshow":
            out.append(AudioInput("mic", "dshow", device=a.device or DEFAULT_MIC, buffer_ms=a.buffer_ms,
                                  shift_ms=a.dshow_align_ms + a.offset_ms, gain=gain))
            continue
        r = ready.get(src)
        if r is None:
            continue
        f = r.format
        out.append(AudioInput(src, "stream", url=r.stream_url, sample_format=f.ffmpeg_format,
                              rate=f.rate, channels=f.channels, gain=gain))
    return out


def audio_args(inputs, mix: str, first_index: int = 1) -> tuple[list[str], list[str]]:
    """(-filter_complex args, -map args for the audio) for `inputs`, whose
    ffmpeg input indexes start at `first_index`. One input with nothing to
    filter maps straight through; anything else goes through one
    filter_complex. Two inputs with mix = "mix" become one track via
    amix (normalize=0: each source keeps its level, so a lone voice is not
    halved; the gains are the knob)."""
    if not inputs:
        return [], []
    chains = [inp.filters() for inp in inputs]
    mixing = len(inputs) > 1 and mix == "mix"
    if not mixing and not any(chains):
        return [], [x for i in range(len(inputs)) for x in ("-map", f"{first_index + i}:a:0")]
    parts = [f"[{first_index + i}:a]{','.join(ch) or 'anull'}[a{i}]" for i, ch in enumerate(chains)]
    if mixing:
        parts.append("".join(f"[a{i}]" for i in range(len(inputs)))
                     + f"amix=inputs={len(inputs)}:duration=longest:normalize=0[aout]")
        maps = ["-map", "[aout]"]
    else:
        maps = [x for i in range(len(inputs)) for x in ("-map", f"[a{i}]")]
    return ["-filter_complex", ";".join(parts)], maps


def audio_track_layout(inputs, mix: str) -> list[dict]:
    """What each audio track in the file holds, for the sidecar."""
    if not inputs:
        return []
    if len(inputs) > 1 and mix == "mix":
        return [{"index": 0, "content": "+".join(i.label for i in inputs)}]
    return [{"index": n, "content": i.label} for n, i in enumerate(inputs)]


# ---------------------------------------------------------------------------
# 4. Capture / remux / probe argv
# ---------------------------------------------------------------------------


def build_capture_argv(spec: CaptureSpec) -> list[str]:
    """The full capture command. Input 0 is the screen; inputs 1.. are the
    audio inputs in order (system first). `-n` because the recorder
    allocates a fresh path and must never overwrite; `-fps_mode cfr` keeps a
    constant frame rate (duplicating on idle desktops) so editors and
    session C's frame arithmetic see regular timestamps."""
    argv = [spec.ffmpeg, "-hide_banner", "-nostats", "-loglevel", spec.loglevel, "-n",
            "-f", "lavfi", "-i", ddagrab_source(spec)]
    for inp in spec.audio_inputs:
        argv += inp.input_args(spec.rw_timeout_s)
    fc, amaps = audio_args(spec.audio_inputs, spec.audio_mix)
    argv += fc
    if amaps:
        argv += ["-map", "0:v:0"] + amaps
    argv += ["-vf", filter_graph(spec.pipeline, spec.scale)]
    argv += video_codec_args(spec)
    argv += ["-fps_mode", "cfr"] + COLOR_TAGS
    if spec.audio_inputs:
        argv += ["-c:a", "aac", "-b:a", spec.audio_bitrate]
        if len(spec.audio_inputs) > 1 and spec.audio_mix == "separate":
            for n, inp in enumerate(spec.audio_inputs):
                argv += [f"-metadata:s:a:{n}", f"title={inp.label}"]
    if spec.duration_s is not None:
        argv += ["-t", f"{spec.duration_s:g}"]
    argv += ["-f", spec.output_format, spec.output]
    return argv


def build_remux_argv(ffmpeg: str, src: str, dst: str) -> list[str]:
    """Matroska capture -> MP4 with the index up front, streams copied."""
    return [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning", "-n", "-i", src,
            "-map", "0", "-c", "copy", "-movflags", "+faststart", dst]


def build_list_devices_argv(ffmpeg: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
