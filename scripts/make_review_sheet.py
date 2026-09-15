"""Extract timestamped review sheets; visual judgements remain pending."""
import argparse
import hashlib
import json
import math
import shutil
from pathlib import Path

from PIL import Image, ImageDraw
from render_edit import inspect, run, number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--shot-id", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--end", type=float)
    parser.add_argument("--step", type=float, default=1)
    parser.add_argument("--width", type=int, default=320)
    args = parser.parse_args()
    video = args.video.resolve()
    if not video.is_file():
        raise ValueError("Video file does not exist")
    ffmpeg = shutil.which(args.ffmpeg)
    if not ffmpeg:
        raise ValueError("FFmpeg executable not found")
    duration, has_audio = inspect(ffmpeg, video)
    start = number(args.start, "start", 0, duration)
    end = number(args.end if args.end is not None else duration, "end", 0, duration)
    step = number(args.step, "step", 0.01, 3600)
    width = number(args.width, "width", 160, 1280)
    if start >= end:
        raise ValueError("start must precede end")
    # Stay away from exact EOF; requested times are not exact frame PTS.
    last = max(start, end - min(0.05, (end-start)/2))
    count = math.floor((last-start)/step) + 1
    if count > 600:
        raise ValueError("More than 600 samples; split into smaller review intervals")
    times = [round(start+i*step, 6) for i in range(count)]
    if last-times[-1] > 0.001:
        times.append(round(last, 6))
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory is not empty; choose another review version")
    output.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, timestamp in enumerate(times):
        frame = output / f"frame-{index:04d}.png"
        original_timestamp = timestamp
        candidates = [timestamp]
        # Container/audio duration may extend beyond the final video frame.
        # Only recover the terminal sample, never conceal a missing middle frame.
        if index == len(times)-1:
            candidates += [round(max(start, timestamp-offset), 6) for offset in (0.1, 0.25)]
        for timestamp in dict.fromkeys(candidates):
            run([ffmpeg, "-nostdin", "-n", "-hide_banner", "-loglevel", "error",
                 "-ss", str(timestamp), "-i", video, "-frames:v", "1", frame])
            if frame.is_file():
                break
        if not frame.is_file():
            raise ValueError(f"No frame decoded at {timestamp}; inspect source interval")
        if frames and timestamp <= frames[-1]["requested_source_seconds"]:
            raise ValueError("Recovered tail overlaps previous sample; use an explicit --end")
        frames.append({"requested_source_seconds": timestamp, "file": frame.name,
                       "original_requested_source_seconds": original_timestamp,
                       "tail_seek_adjusted": timestamp != original_timestamp})
    pages = []
    for first in range(0, len(frames), 12):
        group = frames[first:first+12]
        cell_height = round(width*9/16)
        rows = math.ceil(len(group)/4)
        sheet = Image.new("RGB", (width*4, (cell_height+30)*rows), "#20242a")
        draw = ImageDraw.Draw(sheet)
        for index, frame in enumerate(group):
            x, y = (index%4)*width, (index//4)*(cell_height+30)
            with Image.open(output/frame["file"]) as original:
                preview = original.convert("RGB")
                preview.thumbnail((width, cell_height))
                sheet.paste(preview, (x+(width-preview.width)//2, y+(cell_height-preview.height)//2))
            label = f"{args.shot_id} | t={frame['requested_source_seconds']:.3f}s"
            draw.text((x+6, y+cell_height+6), label, fill="white")
        name = f"sheet-{first//12+1:03d}.jpg"
        sheet.save(output/name, quality=92)
        pages.append(name)
    digest = hashlib.sha256()
    with video.open("rb") as source:
        for block in iter(lambda: source.read(1024*1024), b""):
            digest.update(block)
    record = {"shot_id": args.shot_id, "source_file": str(video), "sha256": digest.hexdigest(),
              "duration": duration, "has_audio": has_audio, "sample_interval": step,
              "timestamp_kind": "requested_source_seek_not_exact_pts", "frames": frames,
              "sheets": pages, "visual_review": "pending", "dialogue_review": "pending",
              "lip_sync_review": "pending", "findings": []}
    (output/"review.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"review_dir": str(output), "frames": len(frames), "sheets": pages,
                      "visual_review": "pending"}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, TypeError) as error:
        raise SystemExit(str(error))
