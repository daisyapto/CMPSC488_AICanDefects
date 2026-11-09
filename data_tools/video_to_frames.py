#!/usr/bin/env python3
"""Extract frames from a video (.mp4) and save them as images in a folder.

Usage:
    python video_to_frames.py input.mp4
    python video_to_frames.py input.mp4 -o frames --every 5 --format png

Requires: pip install opencv-python
"""

"""
To run, use terminal with: python video_to_frames.py my_video.mp4 -o my_frames --every 5 --format png
"""

import argparse
import sys
from pathlib import Path

import cv2


def extract_frames(video_path, output_dir, every=1, fmt="jpg", quality=95):
    video_path = Path(video_path)
    if not video_path.is_file():
        sys.exit(f"Error: file not found: {video_path}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        sys.exit(f"Error: could not open video: {video_path}")

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    print(f"Video: {video_path.name} | {total} frames | {fps:.2f} fps")

    # Zero-padded filenames keep frames sorted correctly
    pad = max(6, len(str(total)))

    params = []
    if fmt == "jpg":
        params = [cv2.IMWRITE_JPEG_QUALITY, quality]

    frame_idx = 0
    saved = 0
    while True:
        ok, frame = cap.read()
        if not ok:  # end of video (or read error)
            break

        if frame_idx % every == 0:
            out_path = output_dir / f"frame_{frame_idx:0{pad}d}.{fmt}"
            if not cv2.imwrite(str(out_path), frame, params):
                print(f"Warning: failed to write {out_path}")
            else:
                saved += 1

        frame_idx += 1
        if frame_idx % 100 == 0:
            print(f"  processed {frame_idx}/{total} frames...", end="\r")

    cap.release()
    print(f"\nDone. Saved {saved} frames to '{output_dir}'.")


def main():
    parser = argparse.ArgumentParser(description="Convert a video into individual frames.")
    parser.add_argument("video", help="Path to the input video (e.g. clip.mp4)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output folder (default: <video name>_frames)")
    parser.add_argument("-n", "--every", type=int, default=1,
                        help="Save every Nth frame (default: 1 = all frames)")
    parser.add_argument("-f", "--format", choices=["jpg", "png"], default="jpg",
                        help="Image format (default: jpg)")
    parser.add_argument("-q", "--quality", type=int, default=95,
                        help="JPEG quality 0-100 (default: 95)")
    args = parser.parse_args()

    if args.every < 1:
        sys.exit("Error: --every must be >= 1")

    output = args.output or f"{Path(args.video).stem}_frames"
    extract_frames(args.video, output, args.every, args.format, args.quality)


if __name__ == "__main__":
    main()
