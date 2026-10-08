"""Recordings for the render tests (session C1b), built the way the recorder
builds them: the real `events.EventModel` decides every press, and the
sidecar carries what the recorder writes (segments with their epochs, the
flashes' QPC stamps, each fiducial's colour and rect, the audio tracks and
the clap). `scene_for` turns a sidecar into what fake_ffmpeg synthesises
for the analysis decodes: the flashes and patches where a real capture puts
them (the render probe: first frame ~40 ms after the shown stamp), the
clap's tone ~40 ms after its request, and any "speech" the test asks for.

Not a test module itself (no test_ prefix); the render tests import it."""

from __future__ import annotations

import json
from pathlib import Path

from peep import catalog
from peep.events import CORRECT, MARK, TAKE, TAKE_RULE, EventModel, Press

FPS = 30
RECT = {"x": 0, "y": 1400, "w": 200, "h": 200}
SCREEN = {"w": 2560, "h": 1600}
MAGENTA, GREEN = "#FF00FF", "#00FF00"
BLUE, RED, YELLOW, CYAN = "#0000FF", "#FF0000", "#FFFF00", "#00FFFF"
START_FLASH_AT = 0.76      # shown, seconds after the segment's first frame (the probe: 0.762)
STOP_FLASH_BEFORE = 0.70   # shown this long before the segment's last frame (flash + settle 400 ms + q)
LAG = 0.04                 # shown -> first captured frame


def _flash(color: str, shown_qpc: float, launch: float) -> dict:
    return {"color": color, "duration_ms": 200, "shown": True, "shown_at": "2026-10-07T10:00:00.000-04:00",
            "since_ffmpeg_start_s": round(shown_qpc - launch, 3), "actual_ms": 203.0,
            "shown_qpc": round(shown_qpc, 6)}


class C1aRetakeModel(EventModel):
    """C1a's event model exactly as it shipped (2026-10-07-session-events-001),
    frozen here so the render can be tested on the sidecars that rule wrote:
    a retake discards the most recent take, open or closed, and opens a fresh
    one at the press (undo depth one). Its records have kind "retake", action
    "open", no `correction`, no `undo`, and no `take_rule` in the sidecar."""

    def _correct(self, rec: dict) -> None:
        last = self.takes[-1] if self.takes else None
        if last is not None and last["discarded_by"] is None:
            last["discarded_by"] = rec["id"]
            last["status"] = "discarded"
            rec["discarded_take"] = last["id"]
        take = {"id": len(self.takes) + 1, "open": self._position(rec), "close": None, "discarded_by": None,
                "status": "open"}
        self.takes.append(take)
        rec.update(accepted=True, action="open", take=take["id"], kind="retake")
        rec.pop("requested_as", None)

    def _take(self, rec: dict) -> None:
        open_ = self.open_take()
        if open_ is None:
            take = {"id": len(self.takes) + 1, "open": self._position(rec), "close": None, "discarded_by": None,
                    "status": "open"}
            self.takes.append(take)
            rec.update(accepted=True, action="open", take=take["id"])
        else:
            open_["close"] = self._position(rec)
            open_["status"] = "closed"
            rec.update(accepted=True, action="close", take=open_["id"])


def build_sidecar(stem: str, durations: list, presses: list = (), *, collection: str = "inbox",
                  tracks: tuple = ("system",), flash: bool = True, base: float = 1000.0, gap: float = 20.0,
                  output_size: str = "2560x1600", pipeline: str = "qsv", clap: bool = True,
                  status: str = "ok", serial_fiducials: bool = True, rule: str = "correction",
                  source: str = "cli") -> dict:
    """A peep.sidecar/2 for a recording of len(durations) segments.

    presses: (kind, segment, media_s[, label]) for a press while segment k
    records, (kind, "paused", k[, label]) for one during the pause after
    segment k, or (kind, "resuming", k[, label]) for one made after the resume
    press but before segment k+1's first frame: the model places it in the
    pause, but the recorder handles it once k+1 is live and shows its patch
    there, after the start flash; (kind, "held", k[, label]) for one made in
    segment k after the pause press was accepted (0.3 s before its end): the
    model places it in segment k, the recorder holds it over the pause and
    shows its patch in k+1 too. Kinds: take, correct (or its alias retake),
    mark. Pauses happen
    between segments (a pause press 0.5 s before each segment's end, a resume
    1 s before the next one's first frame), exactly as the recorder feeds the
    model. rule "c1a": the sidecar C1a's retake rule wrote for the same
    presses (C1aRetakeModel). source "hotkey" applies the debounce.

    serial_fiducials: as the recorder shows them, one after another. A patch
    is shown on the recorder's thread and holds it for 200 ms, and nothing is
    handled during the start flash, so a patch goes up 50 ms after its press
    or as soon as the previous fiducial is down, whichever is later. False
    draws every patch 50 ms after its press, overlapping (a stamp a render
    must not trust blindly)."""
    model = C1aRetakeModel(1.0) if rule == "c1a" else EventModel(1.0)
    epochs, t = {}, base
    for k, d in enumerate(durations, 1):
        epochs[k] = t
        t += d + gap
    sc = catalog.new_sidecar(uid=catalog.new_uid(), slug=stem[11:], collection=collection, file=f"{stem}.mp4",
                             created="2026-10-07T10:00:00.000-04:00")
    marks, segments = [], []
    busy = {"until": 0.0}

    def press(kind, qpc, label="", shown_in=None):
        src = source if kind not in ("pause", "resume") else "cli"
        rec = model.press(Press(kind=kind, qpc=qpc, source=src, label=label), qpc + 0.05)
        seg_shown = shown_in if shown_in is not None else rec.get("segment")
        k = CORRECT if rec["kind"] == "retake" else rec["kind"]
        if rec["accepted"] and k in (TAKE, CORRECT, MARK) and seg_shown is not None and flash:
            color = {("take", "open"): BLUE, ("take", "close"): RED}.get((k, rec["action"]))
            color = YELLOW if k == CORRECT else CYAN if k == MARK else color
            launch = epochs[seg_shown] - 0.1
            shown = max(qpc + 0.05, busy["until"]) if serial_fiducials else qpc + 0.05
            busy["until"] = shown + 0.21
            rec["fiducial"] = {**_flash(color, shown, launch), "style": "patch", "rect": dict(RECT),
                               "screen": dict(SCREEN),
                               "kind": f"{rec['kind']}-{rec['action']}" if rec["kind"] in (TAKE, CORRECT)
                               else rec["kind"]}
            if rule != "c1a":                        # C1c's recorder says where it showed the patch
                rec["fiducial"]["segment"] = seg_shown
        if kind == MARK and rec["accepted"]:
            m = {"t": None, "label": label, "source": "cli", "flash": rec["fiducial"], "segment": rec.get("segment"),
                 "media_s": rec.get("media_s"), "event": rec["id"]}
            if rec.get("segment") is None:
                m["after_segment"] = rec.get("after_segment")
            marks.append(m)
        return rec

    for k, d in enumerate(durations, 1):
        e = epochs[k]
        busy["until"] = e + START_FLASH_AT + 0.21 if flash else e
        model.segment_started(k, e, e - 0.1)
        model.set_paused(False)
        if k > 1:
            model.note_pause_capture(k - 1, epochs[k - 1] + durations[k - 2], e)
            prev_end = epochs[k - 1] + durations[k - 2]
            for n, p in enumerate(p for p in presses if p[1] == "held" and p[2] == k - 1):
                press(p[0], prev_end - 0.3 + n * 0.05, p[3] if len(p) > 3 else "", shown_in=k)
            for n, p in enumerate(p for p in presses if p[1] == "resuming" and p[2] == k - 1):
                press(p[0], e - 0.4 + n * 0.08, p[3] if len(p) > 3 else "", shown_in=k)
        for p in sorted((p for p in presses if p[1] == k), key=lambda p: p[2]):
            press(p[0], e + p[2], p[3] if len(p) > 3 else "")
        last = k == len(durations)
        if not last:
            press("pause", e + d - 0.5)
        model.segment_ended(k, e + d, d)
        if not last:
            model.set_paused(True)
            for n, p in enumerate(p for p in presses if p[1] == "paused" and p[2] == k):
                press(p[0], e + d + 1.0 + n * 2.0, p[3] if len(p) > 3 else "")
            press("resume", epochs[k + 1] - 1.0)
        launch = e - 0.1
        audio = None
        if tracks:
            audio = {"device": None, "codec": "aac", "bitrate": "160k", "offset_ms": 0, "sources": "system",
                     "mix": "separate" if len(tracks) > 1 else None,
                     "tracks": [{"index": i, "content": c} for i, c in enumerate(tracks)],
                     "clap": ({"played": True, "requested_qpc": round(e + 0.755, 6), "freq_hz": 1000, "ms": 120,
                               "since_ffmpeg_start_s": round(0.855, 3)} if (clap and flash) else None)}
        segsc = {"video": {"pipeline": pipeline, "encoder": "h264_qsv" if pipeline != "x264" else "libx264",
                           "fps": FPS, "output_idx": 0, "capture_size": "2560x1600", "output_size": output_size,
                           "container": "mp4"},
                 "audio": audio,
                 "flash": {"enabled": flash,
                           "start": _flash(MAGENTA, e + START_FLASH_AT, launch) if flash else None,
                           "stop": _flash(GREEN, e + d - STOP_FLASH_BEFORE, launch) if flash else None},
                 "timeline": {"ffmpeg_started_qpc": launch, "input0_qpc": e + 0.058,
                              "stop_reason": "pause" if not last else "terminal"},
                 "ffmpeg": {"argv": ["ffmpeg"], "exit_code": 0, "stderr_tail": None, "remux_argv": None}}
        file = f"{stem}.mp4" if k == 1 else f"{stem}.seg{k}.mp4"
        segments.append({"index": k, "capture_file": file.replace(".mp4", ".recording.mkv"), "file": file,
                         "status": status if k == len(durations) else "ok", "duration_s": d,
                         "started_at": "2026-10-07T10:00:00.000-04:00", "ffmpeg_started_qpc": launch,
                         "input0_qpc": e + 0.058, "video_epoch_qpc_est": round(e, 6), "exit_code": 0,
                         "stop_reason": segsc["timeline"]["stop_reason"], **segsc})
        if k == 1:
            sc.update(video=segsc["video"], audio=audio, flash=segsc["flash"], timeline=segsc["timeline"],
                      ffmpeg=segsc["ffmpeg"])
    summary = model.finish()
    sc.update(status="ok", duration_s=round(sum(durations), 3), segments=segments, events=model.events,
              takes=model.takes, pauses=model.pauses, summary=summary, marks=marks)
    if rule == "c1a":
        sc.pop("take_rule", None)
        for t in sc["takes"]:
            t.pop("undo", None)
    else:
        sc["take_rule"] = TAKE_RULE
    return sc


def write_recording(root: Path, sc: dict, *, collection: str | None = None) -> str:
    """The recording on disk as the recorder leaves it: every segment file (a few
    bytes; fake_ffmpeg never reads them), the sidecar, and the catalog event."""
    collection = collection or sc["collection"]
    folder = Path(root) / collection
    folder.mkdir(parents=True, exist_ok=True)
    for s in sc["segments"]:
        (folder / s["file"]).write_bytes(b"\0\0\0\x18ftypisom" + b"x" * 512)
    catalog.write_sidecar(folder / (sc["file"].split(".", 1)[0] + ".json"), sc)
    rec = {"event": "recorded", "uid": sc["uid"], "collection": collection, "file": f"{collection}/{sc['file']}",
           "title": sc["title"], "created": sc["created"], "duration_s": sc["duration_s"]}
    if len(sc["segments"]) > 1:
        rec["segments"] = len(sc["segments"])
    catalog.Catalog(Path(root)).append(rec)
    return sc["uid"]


def _shown_segment(fid: dict, segments: list) -> int | None:
    """Where a patch was on screen, from its own QPC stamp: the segment whose
    first frame came before it and whose last came after (the fixture's own
    reckoning, independent of the render's)."""
    q = fid.get("shown_qpc")
    for s in segments:
        e = s.get("video_epoch_qpc_est")
        if isinstance(q, (int, float)) and isinstance(e, (int, float)) and e <= q <= e + float(s["duration_s"]):
            return s["index"]
    return None


def scene_for(sc: dict, speech: dict | None = None, *, noise: float = 0.0005, hide: tuple = (),
              recolor: dict | None = None, phase: float = 0.0) -> dict:
    """What fake_ffmpeg should 'decode' for this recording's files.

    speech: {segment: [(t0, t1), ...]}: a 300 Hz tone at -14 dBFS standing in
    for a voice. hide: fiducials not drawn (("segment-start", k), ("event", id)),
    to exercise the fallbacks. recolor: {("event", id): "#123456"} draws one in
    another colour. phase: the files' frames sit at phase + k/fps instead of
    k/fps (a video stream that starts late). Session-B marks (a /1 sidecar,
    through catalog.as_v2) draw their full-screen cyan flash."""
    from peep import render
    recolor = recolor or {}
    files = {}
    sc = catalog.as_v2(sc)
    for s in sc["segments"]:
        k, e = s["index"], s["video_epoch_qpc_est"]
        video, audio = [], []
        fl = s.get("flash") or {}
        for which, rec in (("segment-start", fl.get("start")), ("segment-end", fl.get("stop"))):
            if rec and (which, k) not in hide:
                t0 = rec["shown_qpc"] - e + LAG
                video.append({"t0": t0, "t1": t0 + 0.2, "color": rec["color"], "where": "full"})
        for ev in sc["events"]:
            fid = ev.get("fiducial")
            if fid and _shown_segment(fid, sc["segments"]) == k and ("event", ev["id"]) not in hide:
                t0 = fid["shown_qpc"] - e + LAG
                video.append({"t0": t0, "t1": t0 + 0.2, "color": recolor.get(("event", ev["id"]), fid["color"]),
                              "where": "patch"})
        for m in sc.get("marks") or []:
            f = m.get("flash")
            if m.get("event") is None and isinstance(f, dict) and m.get("segment", 1) == k:
                t, _ = render.media_time(f.get("shown_qpc"), f.get("since_ffmpeg_start_s"), e,
                                         s.get("ffmpeg_started_qpc"))
                if t is not None:
                    video.append({"t0": t + LAG, "t1": t + LAG + 0.2, "color": f["color"], "where": "full"})
        clap = (s.get("audio") or {}).get("clap")
        if clap and clap.get("played"):
            t0 = clap["requested_qpc"] - e + LAG
            audio.append({"t0": t0, "t1": t0 + 0.12, "freq": 1000, "amp": 0.4})
        for t0, t1 in (speech or {}).get(k, []):
            audio.append({"t0": t0, "t1": t1, "freq": 300, "amp": 0.2})
        files[s["file"]] = {"duration": s["duration_s"], "video": video, "audio": audio, "noise": noise,
                            "phase": phase}
    return {"fps": FPS, "files": files}


def leaked_frames(scene: dict, plan, res) -> list:
    """Every frame of the cut that shows a fiducial and is not concealed:
    [(segment, frame time, colour)]. The render's promise is that this is []."""
    out = []
    for j, p in enumerate(res.pieces):
        f = scene["files"][plan.seg(p.segment).file]
        hidden = [(c["first"], c["last"]) for c in res.concealed if c["piece"] == j and not c.get("trimmed")]
        for k in range(p.k1, p.k2):
            if any(a <= k - p.k1 <= b for a, b in hidden):
                continue
            t = p.phase + k / p.fps
            for span in f["video"]:
                if span["t0"] <= t < span["t1"]:
                    out.append((p.segment, round(t, 4), span["color"]))
    return out


def write_scene(path: Path, scene: dict) -> Path:
    Path(path).write_text(json.dumps(scene), encoding="utf-8")
    return Path(path)


def synth_media_argv(ffmpeg: str, spec: dict, out: Path, size: str = "640x400") -> list:
    """Real media for one file of a scene (the optional real-ffmpeg test): a
    grey picture with the scene's flashes (full frame) and patches (the
    bottom-left corner, scaled from 2560x1600), and its tones. -nostdin and -y:
    run it with stdin on /dev/null, a timeout, and a fresh `out`."""
    w, h = (int(x) for x in size.split("x"))
    pw, ph = round(RECT["w"] * w / SCREEN["w"]), round(RECT["h"] * h / SCREEN["h"])
    py = round(RECT["y"] * h / SCREEN["h"])
    boxes = []
    for v in spec["video"]:
        geo = "x=0:y=0:w=iw:h=ih" if v["where"] == "full" else f"x=0:y={py}:w={pw}:h={ph}"
        boxes.append(f"drawbox={geo}:color=0x{v['color'].lstrip('#')}:t=fill:"
                     f"enable='between(t,{v['t0']:.4f},{v['t1'] - 0.001:.4f})'")
    tones = "+".join(f"{a['amp']}*sin(2*PI*{a['freq']}*t)*between(t,{a['t0']:.4f},{a['t1']:.4f})"
                     for a in spec["audio"]) or "0"
    expr = f"{tones}+{spec['noise']}*(random(0)-0.5)"
    return [ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
            f"color=c=0x3c3c3c:s={size}:r={FPS}:d={spec['duration']}", "-f", "lavfi", "-i",
            f"aevalsrc='{expr}|{expr}':s=48000:d={spec['duration']}", "-vf", ",".join(boxes) or "null",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "60", "-c:a", "aac",
            "-shortest", str(out)]


class SceneAnalyzer:
    """render.Analyzer over a scene, in-process: the same synthesis fake_ffmpeg
    does, for testing resolve() without any process. `calls` records every
    request; `fail` names what to refuse ("frames", "pcm", "max_db")."""

    def __init__(self, scene: dict, fail: tuple = ()):
        self.scene, self.fail, self.calls = scene, set(fail), []

    def _file(self, seg):
        return self.scene["files"][seg.file]

    def frames(self, seg, start_s, dur_s, crop, grid):
        import math
        from peep import render
        self.calls.append(("frames", seg.index, round(start_s, 3), round(dur_s, 3), crop is not None))
        if "frames" in self.fail:
            raise render.AnalysisError("ffmpeg exit 1: (scene) refused")
        f, fps = self._file(seg), self.scene["fps"]
        ph = f.get("phase", 0.0)
        end = min(start_s + dur_s, f["duration"])
        out, i = [], math.ceil((start_s - ph) * fps - 1e-6)
        while ph + i / fps < end - 1e-9:
            t = ph + i / fps
            color = tuple(f.get("background", [60, 60, 60]))
            for span in f["video"]:
                if span["t0"] <= t < span["t1"] and (span["where"] == "full" or crop is not None):
                    c = span["color"].lstrip("#")
                    color = (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
                    break
            out.append((round(t, 6), color))
            i += 1
        return out

    def _sample(self, f, t, n):
        import math
        v = sum(a["amp"] * math.sin(2 * math.pi * a["freq"] * t) for a in f["audio"] if a["t0"] <= t < a["t1"])
        v += f.get("noise", 0.0005) * (((n * 1103515245 + 12345) >> 8) % 2001 / 1000.0 - 1.0)
        return max(-32768, min(32767, int(v * 32767)))

    def pcm(self, seg, start_s, dur_s, track=None):
        import math
        from peep import render
        self.calls.append(("pcm", seg.index, round(start_s, 3), round(dur_s, 3), track))
        if "pcm" in self.fail:
            raise render.AnalysisError("ffmpeg exit 1: (scene) refused")
        f, rate = self._file(seg), render.PCM_RATE
        end = min(start_s + dur_s, f["duration"])
        return [self._sample(f, k / rate, k) for k in range(math.ceil(start_s * rate - 1e-9), int(end * rate))]

    def max_db(self, seg, start_s, dur_s):
        import math
        from peep import render
        self.calls.append(("max_db", seg.index, round(start_s, 3), round(dur_s, 3)))
        if "max_db" in self.fail:
            raise render.AnalysisError("volumedetect reported no max_volume")
        f = self._file(seg)
        peak = max([a["amp"] for a in f["audio"] if a["t0"] < start_s + dur_s and a["t1"] > start_s]
                   + [f.get("noise", 0.0005)])
        return 20 * math.log10(peak)
