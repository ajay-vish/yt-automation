"""
Bhojpuri Folk Song -> YouTube Shorts, fully automated (free tools only).

What it does, every run:
  1. Lists all videos on the channel (yt-dlp, no API key needed).
  2. Picks one video that hasn't been used yet (tracked in state.json).
  3. Downloads it, finds the loudest/most "sung" ~35s window (librosa) so the
     clip lands on the chorus instead of silence or talking.
  4. Cuts that window, reframes it to 9:16 with a blurred-background fill.
  5. Transcribes that window locally with faster-whisper and burns captions.
  6. Uploads the result to YouTube as PRIVATE with a placeholder title.
  7. Writes a ready-to-paste prompt (with the transcript) into
     drafts/<video_id>_prompt.txt so you can generate title/description/tags
     with Claude free tier whenever you have time, then flip the video public.
  8. Marks the source video as used in state.json so it won't repeat until
     every video has been used once.

Everything here is free: yt-dlp, ffmpeg, librosa and faster-whisper all run
locally (or in GitHub Actions' free runner minutes). The only paid step is
optional (an LLM API), and this script does NOT call one -- you do that
manually via the generated prompt file.
"""

import json
import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import librosa
import numpy as np

CHANNEL_URL = "https://www.youtube.com/@manjuvishwakarmalokgeet/videos"
STATE_FILE = Path("state.json")
WORK_DIR = Path("work")
DRAFTS_DIR = Path("drafts")
CLIP_SECONDS = 35
SHORT_WIDTH, SHORT_HEIGHT = 1080, 1920

# Asset paths (resolved relative to the repo root, one level up from scripts/)
_REPO_ROOT = Path(__file__).resolve().parent.parent
GIF_PATH = _REPO_ROOT / "assets" / "Subscribe.gif"

# Title overlay settings
TITLE_FONT = "Noto Sans"          # Fallback to a common system font; change if needed
TITLE_FONT_SIZE = 52
TITLE_COLOR = "white"
TITLE_OUTLINE_COLOR = "black"
TITLE_OUTLINE_WIDTH = 3

# Subscribe GIF timing (seconds into the clip)
GIF_START = 5
GIF_END = 9  # GIF_START + 4 seconds visible


def run(cmd, **kwargs):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=True, **kwargs)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"used_video_ids": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def cookie_args():
    """If a cookies.txt file is present (see README), pass it to yt-dlp so
    YouTube doesn't block requests coming from GitHub's shared IPs."""
    cookies_path = os.environ.get("YT_COOKIES_FILE", "cookies.txt")
    if os.path.exists(cookies_path):
        return ["--cookies", cookies_path]
    return []


def list_channel_videos():
    """Return [{"id": ..., "title": ...}, ...] for every video on the channel."""
    out = subprocess.run(
        ["yt-dlp", *cookie_args(), "--remote-components", "ejs:github", "--flat-playlist", "-J", CHANNEL_URL],
        check=True, capture_output=True, text=True,
    )
    data = json.loads(out.stdout)
    return [{"id": e["id"], "title": e.get("title", e["id"])} for e in data["entries"]]

def pick_video(state):
    videos = list_channel_videos()
    used = set(state["used_video_ids"])
    unused = [v for v in videos if v["id"] not in used]
    if not unused:
        # Every video has been used at least once -- start a new cycle.
        state["used_video_ids"] = []
        unused = videos
    choice = random.choice(unused)
    return choice, videos


def download_video(video_id, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    out_template = str(dest / f"{video_id}.%(ext)s")
    run([
        "yt-dlp", *cookie_args(),
        "--remote-components", "ejs:github",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
        "--merge-output-format", "mp4",
        "-o", out_template,
        f"https://www.youtube.com/watch?v={video_id}",
    ])
    matches = list(dest.glob(f"{video_id}.mp4"))
    if not matches:
        raise RuntimeError(f"Download failed for {video_id}")
    return matches[0]


def extract_audio(video_path: Path, wav_path: Path):
    run(["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "22050", str(wav_path)])


def find_best_window(wav_path: Path, clip_seconds: int) -> float:
    """Return the start time (seconds) of the loudest clip_seconds window,
    ignoring the first and last 5% of the track (usually intro/outro)."""
    y, sr = librosa.load(str(wav_path), sr=None)
    duration = librosa.get_duration(y=y, sr=sr)
    if duration <= clip_seconds:
        return 0.0

    hop = sr  # 1-second resolution
    rms = librosa.feature.rms(y=y, frame_length=sr, hop_length=hop)[0]  # 1 value/sec
    margin = max(1, int(duration * 0.05))
    window = clip_seconds

    best_start, best_score = margin, -1
    for start in range(margin, max(margin + 1, int(duration) - window - margin)):
        score = np.mean(rms[start:start + window])
        if score > best_score:
            best_score, best_start = score, start
    return float(best_start)


def cut_and_reframe(video_path: Path, start: float, clip_seconds: int, out_path: Path, title: str = ""):
    """Trim to the window and convert to 9:16 with a blurred, filled background.

    Overlays:
    - Title text at the top (first segment of title split on '|').
    - Subscribe GIF from GIF_START to GIF_END seconds, centered at the bottom.
    """
    # --- Derive display title ---
    display_title = title.split("|")[0].strip() if title else ""
    # Escape characters that break FFmpeg drawtext syntax
    display_title = display_title.replace("'", "\\'").replace(":", "\\:")

    # --- Base: blur background + foreground composite ---
    base_vf = (
        f"[0:v]trim=start={start}:duration={clip_seconds},setpts=PTS-STARTPTS,"
        f"scale={SHORT_WIDTH}:{SHORT_HEIGHT}:force_original_aspect_ratio=increase,crop={SHORT_WIDTH}:{SHORT_HEIGHT},"
        f"boxblur=20:5[bg];"
        f"[0:v]trim=start={start}:duration={clip_seconds},setpts=PTS-STARTPTS,"
        f"scale={SHORT_WIDTH}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
    )

    # --- Title text overlay at the top ---
    title_vf = ""
    if display_title:
        title_vf = (
            f"[base]drawtext="
            f"text='{display_title}':"
            f"fontfile=/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf:"
            f"fontsize={TITLE_FONT_SIZE}:"
            f"fontcolor={TITLE_COLOR}:"
            f"borderw={TITLE_OUTLINE_WIDTH}:"
            f"bordercolor={TITLE_OUTLINE_COLOR}:"
            f"x=(w-text_w)/2:"
            f"y=80:"
            f"line_spacing=10"
            f"[titled]"
        )
        last_label = "titled"
    else:
        last_label = "base"

    # --- Subscribe GIF overlay at the bottom (GIF_START to GIF_END seconds) ---
    # The GIF is passed as input[1]; we delay it by GIF_START seconds via setpts,
    # then enable the overlay only during [GIF_START, GIF_END].
    gif_vf = (
        f"[1:v]setpts=PTS-STARTPTS+{GIF_START}/TB[gif];"
        f"[{last_label}][gif]overlay="
        f"x=(W-w)/2:"
        f"y=H-h-160:"
        f"enable='between(t,{GIF_START},{GIF_END})':"
        f"eof_action=pass"
        f"[vout]"
    )

    # Assemble full filter_complex
    filter_parts = [base_vf]
    if title_vf:
        filter_parts.append(title_vf)
    filter_parts.append(gif_vf)
    filter_complex = ";".join(filter_parts)

    af = f"[0:a]atrim=start={start}:duration={clip_seconds},asetpts=PTS-STARTPTS[aout]"
    filter_complex = f"{filter_complex};{af}"

    run([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-ignore_loop", "0",   # Let FFmpeg read all GIF frames (we control timing via enable/setpts)
        "-i", str(GIF_PATH),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out_path),
    ])


def transcribe_to_srt(clip_video_path: Path, srt_path: Path) -> str:
    """Uses faster-whisper (local, free) to transcribe the clip and write an SRT.
    Returns the plain transcript text too (used in the Claude prompt)."""
    from faster_whisper import WhisperModel

    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(clip_video_path), language="hi")

    def fmt_ts(t):
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        ms = int((s - int(s)) * 1000)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"

    lines, transcript_parts = [], []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{fmt_ts(seg.start)} --> {fmt_ts(seg.end)}")
        lines.append(seg.text.strip())
        lines.append("")
        transcript_parts.append(seg.text.strip())
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return " ".join(transcript_parts)


def burn_captions(clip_video_path: Path, srt_path: Path, out_path: Path):
    # Force a readable style: white text, black outline, bottom-centered.
    # Use Noto Sans Devanagari for proper Hindi/Bhojpuri script rendering.
    style = "FontName=Noto Sans Devanagari,FontSize=20,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=3,Alignment=2,MarginV=80"
    run([
        "ffmpeg", "-y", "-i", str(clip_video_path),
        "-vf", f"subtitles={srt_path}:force_style='{style}'",
        "-c:a", "copy", str(out_path),
    ])


def upload_private(video_path: Path, placeholder_title: str, source_id: str, source_title: str) -> str:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": placeholder_title[:100],
            "description": f"DRAFT auto-clip. Source video: https://www.youtube.com/watch?v={source_id} ({source_title})\n"
                            f"Fill in real title/description/tags with the Claude prompt in drafts/{source_id}_prompt.txt, then set this to Public.",
            "tags": [],
            "categoryId": "10",  # Music
        },
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    return response["id"]


def write_claude_prompt(source_id: str, source_title: str, transcript: str, uploaded_video_id: str):
    dated_dir = DRAFTS_DIR / date.today().isoformat()  # e.g. drafts/2026-07-03/
    dated_dir.mkdir(parents=True, exist_ok=True)
    prompt = f"""I run a YouTube channel of Bhojpuri folk songs (lokgeet). I've made a Short from this song: "{source_title}" (full video: https://www.youtube.com/watch?v={source_id}).

Here is a rough transcript of the clip used in the Short (Hindi/Bhojpuri, may contain transcription errors):
"{transcript.strip()}"

Please give me:

A YouTube Shorts TITLE (under 100 characters) in the format: Song Name – Singer | Bhojpuri Folk Song #Shorts (fix/guess the singer and song name from context if needed)
A YouTube DESCRIPTION (3-5 lines) that includes the song name, a short evocative line about the song, the actual full video link I provided above (not a placeholder), and relevant hashtags.
15-20 YouTube TAGS (comma-separated) mixing broad terms (bhojpuri, folk song, lokgeet, indian folk music) and specific terms (song name, singer name, region).
"""
    (dated_dir / f"{source_id}_prompt.txt").write_text(prompt, encoding="utf-8")


def main():
    state = load_state()
    video, _all_videos = pick_video(state)
    video_id, title = video["id"], video["title"]
    print(f"Selected video: {title} ({video_id})")

    WORK_DIR.mkdir(exist_ok=True)
    raw_path = download_video(video_id, WORK_DIR)

    wav_path = WORK_DIR / f"{video_id}.wav"
    extract_audio(raw_path, wav_path)
    start = find_best_window(wav_path, CLIP_SECONDS)
    print(f"Best window starts at {start:.0f}s")

    clip_path = WORK_DIR / f"{video_id}_clip.mp4"
    cut_and_reframe(raw_path, start, CLIP_SECONDS, clip_path, title=title)

    srt_path = WORK_DIR / f"{video_id}.srt"
    transcript = transcribe_to_srt(clip_path, srt_path)

    final_path = WORK_DIR / f"{video_id}_final.mp4"
    burn_captions(clip_path, srt_path, final_path)

    placeholder_title = f"[DRAFT] {title[:80]}"
    uploaded_id = upload_private(final_path, placeholder_title, video_id, title)
    print(f"Uploaded as private: https://studio.youtube.com/video/{uploaded_id}/edit")

    write_claude_prompt(video_id, title, transcript, uploaded_id)

    state["used_video_ids"].append(video_id)
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
