"""Render a reviewed local edit manifest. No network or media generation."""
import argparse
import hashlib
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path


def run(args, cwd=None):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=cwd)
    if result.returncode:
        raise ValueError(result.stderr[-4000:] or "FFmpeg failed")
    return result


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: expected a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}: expected {low}..{high}")
    return value


def inspect(ffmpeg, path):
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match or "Video:" not in result.stderr:
        raise ValueError(f"Cannot inspect video: {path}")
    duration = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
    return duration, "Audio:" in result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = args.manifest.resolve()
    output = args.output.resolve()
    sidecar = output.with_suffix(".edit.json")
    if output.suffix.lower() != ".mp4":
        raise ValueError("Output must be .mp4")
    if output.exists() or sidecar.exists():
        raise ValueError("Output or edit record already exists; choose a new version")
    data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != "1.0":
        raise ValueError("Unsupported schema_version")
    width = number(data.get("width", 1280), "width", 2, 7680)
    height = number(data.get("height", 720), "height", 2, 4320)
    if width % 2 or height % 2:
        raise ValueError("Width and height must be even integers")
    width, height = int(width), int(height)
    fps = number(data.get("fps", 24), "fps", 1, 60)
    if int(fps) != fps:
        raise ValueError("This renderer supports integer fps only")
    fps = int(fps)
    sample_rate = number(data.get("sample_rate", 48000), "sample_rate", 8000, 96000)
    if sample_rate not in (32000, 44100, 48000):
        raise ValueError("sample_rate must be 32000, 44100 or 48000")
    fade_in = number(data.get("fade_in", 0), "fade_in", 0, 5)
    fade_out = number(data.get("fade_out", 0), "fade_out", 0, 5)
    loudness = number(data.get("loudness_lufs", -18), "loudness_lufs", -36, -10)
    clips = data.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ValueError("clips must contain at least one reviewed cut")
    prepared = []
    cursor = 0
    for clip in clips:
        path = (manifest.parent / clip["file"]).resolve()
        if not path.is_file() or path == output:
            raise ValueError(f"Missing or invalid input: {path}")
        source_duration, has_audio = inspect(args.ffmpeg, path)
        start = number(clip["in"], "in", 0, source_duration)
        end = number(clip["out"], "out", 0, source_duration)
        # Quantize once; the sidecar records the exact frame boundaries used.
        start_frame = math.floor(start * fps + 0.5)
        end_frame = math.floor(end * fps + 0.5)
        frames = end_frame - start_frame
        if frames <= 0 or end_frame / fps > source_duration + 1 / fps:
            raise ValueError("Cut must have positive duration within source")
        crop = clip.get("crop")
        if crop is not None:
            if not isinstance(crop, list) or len(crop) != 4:
                raise ValueError("crop must be [width,height,x,y] or null")
            for index, value in enumerate(crop):
                number(value, "crop", 1 if index < 2 else 0, 32768)
                if int(value) != value:
                    raise ValueError("crop values must be integers")
        prepared.append({"shot_id": clip["shot_id"], "file": str(path),
                         "source_in": start_frame / fps, "source_out": end_frame / fps,
                         "timeline_in": cursor / fps, "frames": frames,
                         "duration": frames / fps, "crop": crop,
                         "has_audio": has_audio, "reason": clip.get("reason", "")})
        cursor += frames
    if fade_in > prepared[0]["duration"] or fade_out > prepared[-1]["duration"]:
        raise ValueError("Fade exceeds first or last clip duration")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="director-edit-", dir=output.parent) as temporary:
        folder = Path(temporary)
        segments = []
        for index, clip in enumerate(prepared):
            segment = folder / f"{index:04d}.mp4"
            duration = clip["duration"]
            filters = []
            if clip["crop"]:
                filters.append("crop=" + ":".join(str(n) for n in clip["crop"]))
            filters += [f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2", "setsar=1",
                        f"fps={fps}", f"trim=end_frame={clip['frames']}", "setpts=PTS-STARTPTS"]
            if index == 0 and fade_in:
                filters.append(f"fade=t=in:st=0:d={fade_in}")
            if index == len(prepared) - 1 and fade_out:
                filters.append(f"fade=t=out:st={duration-fade_out}:d={fade_out}")
            audio_fade = min(0.04, duration / 4)
            af = (f"aresample={sample_rate},aformat=channel_layouts=stereo,apad,"
                  f"atrim=duration={duration},asetpts=PTS-STARTPTS,"
                  f"afade=t=in:d={audio_fade},afade=t=out:st={duration-audio_fade}:d={audio_fade}")
            command = [args.ffmpeg, "-nostdin", "-n", "-hide_banner", "-loglevel", "error",
                       "-ss", str(clip["source_in"]), "-i", clip["file"]]
            if not clip["has_audio"]:
                command += ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo"]
            command += ["-map", "0:v:0", "-map", "0:a:0" if clip["has_audio"] else "1:a:0",
                        "-t", str(duration), "-vf", ",".join(filters), "-af", af,
                        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", segment]
            run(command)
            segments.append(f"file '{segment.name}'")
        concat = folder / "concat.txt"
        concat.write_text("\n".join(segments) + "\n", encoding="utf-8")
        staged = folder / "final.mp4"
        run([args.ffmpeg, "-nostdin", "-n", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", concat.name,
             "-vf", f"fps={fps}", "-frames:v", str(cursor), "-c:v", "libx264", "-crf", "19",
             "-preset", "medium", "-pix_fmt", "yuv420p", "-af",
             f"loudnorm=I={loudness}:TP=-1.5:LRA=11,aresample={sample_rate},aformat=channel_layouts=stereo",
             "-t", str(cursor / fps), "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", staged], cwd=folder)
        run([args.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror",
             "-i", staged, "-f", "null", "-"])
        actual_duration, _ = inspect(args.ffmpeg, staged)
        if abs(actual_duration - cursor / fps) > 0.15:
            raise ValueError("Export duration differs from edit manifest")
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        record = {"schema_version": "1.0", "manifest": str(manifest), "output": str(output),
                  "width": width, "height": height, "fps": fps, "total_frames": cursor,
                  "duration": actual_duration, "sha256": digest, "decode_check": "pass",
                  "playback_check": "pending", "creative_review": "pending", "clips": prepared}
        # Exclusive creation prevents accidental replacement of an earlier delivery.
        with output.open("xb") as target, staged.open("rb") as source:
            while block := source.read(1024 * 1024):
                target.write(block)
        with sidecar.open("x", encoding="utf-8") as target:
            json.dump(record, target, ensure_ascii=False, indent=2)
        print(json.dumps({"output": str(output), "record": str(sidecar),
                          "duration": actual_duration, "decode_check": "pass"}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, TypeError) as error:
        raise SystemExit(str(error))
