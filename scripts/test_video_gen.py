#!/usr/bin/env python3
"""
Test script to generate a short 5-second sample video using the YouTube pipeline's styling,
layout, crop, title, logo, subscribe GIF overlay, and subtitles.
This uses a local video file (or automatically generates a dummy video if none exists)
to let you check if elements (captions, title, overlays, branding) are positioned correctly.
"""

import sys
import os
import argparse
import shutil
from pathlib import Path

# Set up paths
SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
WORK_DIR = REPO_ROOT / "work"

# Add scripts directory to path to import pipeline
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

# Mock zoneinfo before importing pipeline to avoid ZoneInfoNotFoundError on Windows machines lacking tzdata
import zoneinfo
from datetime import timedelta, timezone

def mock_zoneinfo(key):
    try:
        from zoneinfo import ZoneInfo as OriginalZoneInfo
        return OriginalZoneInfo(key)
    except Exception:
        if key == "Asia/Kolkata":
            return timezone(timedelta(hours=5, minutes=30), name="IST")
        return timezone.utc

zoneinfo.ZoneInfo = mock_zoneinfo

try:
    import pipeline
    from pipeline import (
        cut_and_reframe,
        burn_captions,
        download_font,
        TITLE_FONT_FILE,
        GIF_PATH,
        BRANDING_PATH,
        CALLOUT_PATH,
        run as pipeline_run
    )
except ImportError as e:
    print(f"Error: Could not import pipeline.py. Details: {e}")
    sys.exit(1)

# Modify default GIF timing to fit in a 5-second test video
pipeline.GIF_START = 1
pipeline.GIF_END = 4

def resolve_ffmpeg_path() -> Path | None:
    """Finds ffmpeg path on Windows looking at PATH, Env variables, and common install directories."""
    # 1. Check in PATH
    ffmpeg_in_path = shutil.which("ffmpeg")
    if ffmpeg_in_path:
        return Path(ffmpeg_in_path)

    # 2. Check FFMPEG_PATH env var
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    # 3. Check workspace directory
    for loc in [REPO_ROOT, WORK_DIR, SCRIPTS_DIR]:
        p = loc / "ffmpeg.exe"
        if p.exists():
            return p

    # 4. Standard WinGet, Scoop, and Chocolatey directories
    user_home = Path.home()
    common_paths = [
        user_home / "scoop/shims/ffmpeg.exe",
        user_home / "scoop/apps/ffmpeg/current/bin/ffmpeg.exe",
        Path("C:/ProgramData/chocolatey/bin/ffmpeg.exe"),
        user_home / "AppData/Local/Microsoft/WinGet/Links/ffmpeg.exe",
    ]
    for p in common_paths:
        if p.exists():
            return p

    return None

# Resolve ffmpeg executable
FFMPEG_EXE = resolve_ffmpeg_path()

if not FFMPEG_EXE:
    print("=" * 80)
    print("[Error] FFmpeg executable could not be found.")
    print("To test style and layout rendering, please install FFmpeg on your computer.")
    print("\nHow to install:")
    print("  Run this command in a new PowerShell window:")
    print("      winget install Gyan.FFmpeg")
    print("\nAlternative options:")
    print("  - Place 'ffmpeg.exe' in this directory.")
    print("  - Set the environment variable FFMPEG_PATH to point to your ffmpeg.exe.")
    print("=" * 80)
    sys.exit(1)

# Monkeypatch pipeline.run to use the resolved ffmpeg executable path
def patched_run(cmd, **kwargs):
    if len(cmd) > 0 and cmd[0] == "ffmpeg":
        cmd[0] = str(FFMPEG_EXE)
    return pipeline_run(cmd, **kwargs)

pipeline.run = patched_run

def generate_dummy_video(output_path: Path):
    """Generates a 5-second dummy color test video with audio using ffmpeg."""
    print(f"Generating 5-second dummy video at {output_path}...")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050",
        "-t", "5",
        "-pix_fmt", "yuv420p",
        str(output_path)
    ]
    patched_run(cmd)

def create_sample_srt(output_path: Path):
    """Creates a sample SRT file with Hindi and English text to test subtitle alignment."""
    srt_content = (
        "1\n"
        "00:00:00,500 --> 00:00:02,500\n"
        "मनु भाई लोकगीत परीक्षण (Hindi)\n\n"
        "2\n"
        "00:00:02,800 --> 00:00:04,800\n"
        "Testing Subtitle Overlay (English)\n"
    )
    output_path.write_text(srt_content, encoding="utf-8")
    print(f"Created sample SRT template at {output_path}")

def find_local_mp4():
    """Searches workspace directories for any MP4 to use as input."""
    # Prioritise assets/test_vid.mp4 if present
    assets_test_vid = REPO_ROOT / "assets" / "test_vid.mp4"
    if assets_test_vid.exists():
        return assets_test_vid

    search_dirs = [REPO_ROOT, WORK_DIR]
    for d in search_dirs:
        if d.exists():
            for f in d.glob("*.mp4"):
                if "test_" not in f.name and "dummy_" not in f.name:
                    return f
    return None

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Generate a 5-second test video using the pipeline's layout/styling."
    )
    parser.add_argument(
        "input_video",
        nargs="?",
        help="Path to a local MP4 video file. If not provided, search for local MP4s or generate a dummy."
    )
    parser.add_argument(
        "--title",
        default="परीक्षण गाना - शिल्पी राज | Bhojpuri Folk Song #Shorts",
        help="Title text to burn on top."
    )
    parser.add_argument(
        "--output",
        default="work/test_layout_final.mp4",
        help="Path to save the generated output video."
    )
    
    args = parser.parse_args()
    
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    
    if args.input_video:
        input_path = Path(args.input_video).resolve()
        if not input_path.exists():
            print(f"Error: Input video '{args.input_video}' not found.")
            sys.exit(1)
    else:
        found = find_local_mp4()
        if found:
            input_path = found
            print(f"Found local video file to use: {input_path}")
        else:
            input_path = WORK_DIR / "dummy_input.mp4"
            try:
                generate_dummy_video(input_path)
            except Exception as e:
                print(f"Failed to generate dummy video: {e}")
                sys.exit(1)

    output_path = Path(args.output).resolve()
    
    if not BRANDING_PATH.exists():
        print(f"Error: Branding asset not found at {BRANDING_PATH}")
        sys.exit(1)
    if not GIF_PATH.exists():
        print(f"Error: Subscribe GIF not found at {GIF_PATH}")
        sys.exit(1)
    if not CALLOUT_PATH.exists():
        print(f"Error: Callout asset not found at {CALLOUT_PATH}")
        sys.exit(1)
        
    if not TITLE_FONT_FILE.exists():
        print(f"Font file '{TITLE_FONT_FILE}' not found. Downloading...")
        download_font(TITLE_FONT_FILE)

    temp_clip = WORK_DIR / "test_layout_clip.mp4"
    temp_srt = WORK_DIR / "test_layout_captions.srt"
    
    clip_seconds = 5

    print("\n--- Phase 1: Re-framing & Overlaying Title / Logos ---")
    try:
        cut_and_reframe(
            video_path=input_path,
            start=0,
            clip_seconds=clip_seconds,
            out_path=temp_clip,
            title=args.title
        )
    except Exception as e:
        print(f"Error during re-framing: {e}")
        sys.exit(1)
        
    print("\n--- Phase 2: Burning Captions ---")
    create_sample_srt(temp_srt)
    try:
        burn_captions(
            clip_video_path=temp_clip,
            srt_path=temp_srt,
            out_path=output_path
        )
    except Exception as e:
        print(f"Error during caption burn/transcription: {e}")
        if temp_clip.exists():
            temp_clip.unlink()
        if temp_srt.exists():
            temp_srt.unlink()
        sys.exit(1)

    if temp_clip.exists():
        temp_clip.unlink()
    if temp_srt.exists():
        temp_srt.unlink()
        
    print(f"\n[Success] Test video generated successfully!")
    print(f"Output saved to: {output_path}")

if __name__ == "__main__":
    main()
