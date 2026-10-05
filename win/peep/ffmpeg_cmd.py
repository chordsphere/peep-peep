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

Sections:
  1. Spec                       (~line 35)
  2. Filter graph + encoder     (~line 82)
  3. Capture / remux / probe argv (~line 120)
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config, parse_scale

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
    audio_device: str | None = None      # None = no audio track
    audio_bitrate: str = "160k"
    audio_buffer_ms: int = 50
    audio_offset_ms: int = 0
    output_format: str = "matroska"      # "null" for doctor's capture test
    duration_s: float | None = None      # -t; None = until 'q'
    loglevel: str = "info"


def spec_from_config(cfg: Config, *, ffmpeg: str, output: str, pipeline: str | None = None,
                     scale: str | None = None, fps: int | None = None, audio: bool | None = None,
                     **overrides) -> CaptureSpec:
    """The CaptureSpec a `rec` would use, with CLI overrides applied."""
    v, a = cfg.video, cfg.audio
    use_audio = a.enabled if audio is None else audio
    return CaptureSpec(
        ffmpeg=ffmpeg, output=output, pipeline=pipeline or v.pipeline, fps=fps or v.fps,
        scale=parse_scale(v.scale if scale is None else scale), output_idx=v.output_idx,
        draw_mouse=v.draw_mouse, qsv_preset=v.qsv_preset, qsv_global_quality=v.qsv_global_quality,
        x264_preset=v.x264_preset, x264_crf=v.x264_crf, gop_seconds=v.gop_seconds,
        audio_device=a.device if use_audio else None, audio_bitrate=a.bitrate,
        audio_buffer_ms=a.buffer_ms, audio_offset_ms=a.offset_ms, **overrides)


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
# 3. Capture / remux / probe argv
# ---------------------------------------------------------------------------


def build_capture_argv(spec: CaptureSpec) -> list[str]:
    """The full capture command. Input 0 is the screen; input 1, when
    present, is the microphone. `-n` because the recorder allocates a
    fresh path and must never overwrite; `-fps_mode cfr` keeps a constant
    frame rate (duplicating on idle desktops) so editors and session C's
    frame arithmetic see regular timestamps."""
    argv = [spec.ffmpeg, "-hide_banner", "-nostats", "-loglevel", spec.loglevel, "-n",
            "-f", "lavfi", "-i", ddagrab_source(spec)]
    if spec.audio_device:
        argv += ["-thread_queue_size", "1024"]
        if spec.audio_offset_ms:
            argv += ["-itsoffset", f"{spec.audio_offset_ms / 1000:.3f}"]
        argv += ["-f", "dshow", "-audio_buffer_size", str(spec.audio_buffer_ms),
                 "-i", f"audio={spec.audio_device}"]
        argv += ["-map", "0:v:0", "-map", "1:a:0"]
    argv += ["-vf", filter_graph(spec.pipeline, spec.scale)]
    argv += video_codec_args(spec)
    argv += ["-fps_mode", "cfr"] + COLOR_TAGS
    if spec.audio_device:
        argv += ["-c:a", "aac", "-b:a", spec.audio_bitrate]
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
