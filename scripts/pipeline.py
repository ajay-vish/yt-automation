"""
Bhojpuri Folk Song -> YouTube Shorts, fully automated.

What it does, every run:
  1. Lists all videos on the channel (yt-dlp, no API key needed).
  2. Picks one video that hasn't been used yet (tracked in state.json).
  3. Downloads it, finds the loudest/most "sung" ~35s window (librosa) so the
     clip lands on the chorus instead of silence or talking.
  4. Cuts that window, reframes it to 9:16 with a blurred-background fill.
     Overlays the song title at the top and a subscribe GIF at the bottom.
  5. Transcribes that window locally with faster-whisper and burns captions.
  6. Calls Gemini 2.0 Flash (free via Google AI Studio) to generate a
     production-ready title, description, and tags from the transcript.
  7. Uploads the result to YouTube as PRIVATE with the AI-generated metadata.
     Flip to Public in YouTube Studio once you've reviewed it.
  8. Marks the source video as used in state.json so it won't repeat until
     every video has been used once.
"""

import json
import os
import random
import subprocess
import sys
import textwrap

from pathlib import Path

import librosa
import numpy as np

CHANNEL_URL = "https://www.youtube.com/@manjuvishwakarmalokgeet/videos"
STATE_FILE = Path("state.json")
WORK_DIR = Path("work")
CLIP_SECONDS = 35
SHORT_WIDTH, SHORT_HEIGHT = 1080, 1920

# Asset paths (resolved relative to the repo root, one level up from scripts/)
_REPO_ROOT = Path(__file__).resolve().parent.parent
GIF_PATH = _REPO_ROOT / "assets" / "Subscribe.gif"

# Title overlay settings
TITLE_FONT = "Noto Sans"          # Fallback to a common system font; change if needed
TITLE_FONT_SIZE    = 58                # bigger for impact
TITLE_COLOR        = "0xFFD700"        # gold / Bollywood yellow
TITLE_OUTLINE_COLOR = "0x1A0000"       # deep dark red-black outline
TITLE_OUTLINE_WIDTH = 5               # thick outline for crispness
TITLE_SHADOW_COLOR  = "0xFF6600@0.85" # warm orange shadow
TITLE_SHADOW_X      = 4               # shadow offset x
TITLE_SHADOW_Y      = 4               # shadow offset y
TITLE_BOX_COLOR     = "0x000000@0.45" # semi-transparent dark pill behind text
TITLE_BOX_PADDING   = 18             # px padding inside the box
TITLE_Y_START = "h*0.18"              # a bit lower than before
TITLE_MAX_LINES = 2               # hard cap so a long title can't overflow the frame
TITLE_SIDE_MARGIN = 80            # px kept clear on each side when wrapping
TITLE_LINE_SPACING = 18           # extra px between wrapped lines

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


def _find_devanagari_font() -> str:
    """Ask fontconfig for the best available Devanagari font file.
    Falls back to a known path if fc-match is unavailable."""
    try:
        result = subprocess.run(
            ["fc-match", "Noto Sans Devanagari:lang=hi", "--format=%{file}"],
            capture_output=True, text=True, check=True,
        )
        font_path = result.stdout.strip()
        if font_path:
            print(f"[font] Using Devanagari font: {font_path}")
            return font_path
    except Exception as e:
        print(f"[font] fc-match failed ({e}), trying fallback paths.")

    # Fallback: try known Ubuntu paths in order
    fallbacks = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ]
    for path in fallbacks:
        if Path(path).exists():
            print(f"[font] Using fallback font: {path}")
            return path

    raise RuntimeError(
        "No Devanagari font found. Install fonts-noto: sudo apt-get install fonts-noto"
    )



def _wrap_title(text: str) -> list[str]:
    """Break a long title into up to TITLE_MAX_LINES lines that fit within
    the frame width at TITLE_FONT_SIZE. If it still doesn't fit in
    TITLE_MAX_LINES, the last line is truncated with an ellipsis so it
    never overflows the video."""
    # Rough average glyph width for Noto Sans -- good enough to size-wrap by.
    avg_char_width = TITLE_FONT_SIZE * 0.56
    max_chars_per_line = max(1, int((SHORT_WIDTH - TITLE_SIDE_MARGIN) / avg_char_width))

    lines = textwrap.wrap(text, width=max_chars_per_line) or [text]

    if len(lines) > TITLE_MAX_LINES:
        lines = lines[:TITLE_MAX_LINES]
        last = lines[-1]
        if len(last) > 3:
            last = last[: max_chars_per_line - 1].rstrip() + "\u2026"
        lines[-1] = last

    return lines


def _escape_drawtext(text: str) -> str:
    """Escape characters that break FFmpeg drawtext's text='...' syntax."""
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def cut_and_reframe(video_path: Path, start: float, clip_seconds: int, out_path: Path, title: str = ""):
    """Trim to the window and convert to 9:16 with a blurred, filled background.

    Overlays:
    - Title text at the top (first segment of title split on '|').
    - Subscribe GIF from GIF_START to GIF_END seconds, centered at the bottom.
    """
    # --- Derive display title ---
    display_title = title.split("|")[0].strip() if title else ""

    # --- Base: blur background + foreground composite ---
    base_vf = (
        f"[0:v]trim=start={start}:duration={clip_seconds},setpts=PTS-STARTPTS,"
        f"scale={SHORT_WIDTH}:{SHORT_HEIGHT}:force_original_aspect_ratio=increase,crop={SHORT_WIDTH}:{SHORT_HEIGHT},"
        f"boxblur=20:5[bg];"
        f"[0:v]trim=start={start}:duration={clip_seconds},setpts=PTS-STARTPTS,"
        f"scale={SHORT_WIDTH}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
    )

    # --- Title text overlay at the top, wrapped so it never overflows ---
    title_vf_parts = []
    if display_title:
        font_path = _find_devanagari_font()
        lines = _wrap_title(display_title)
        line_height = TITLE_FONT_SIZE + TITLE_LINE_SPACING
        prev_label = "base"
        for i, line in enumerate(lines):
            out_label = f"title{i}"
            y_expr = f"{TITLE_Y_START}+{i * line_height}" if i else TITLE_Y_START
            title_vf_parts.append(
                f"[{prev_label}]drawtext="
                f"text='{_escape_drawtext(line)}':"
                f"fontfile={font_path}:"
                f"fontsize={TITLE_FONT_SIZE}:"
                f"fontcolor={TITLE_COLOR}:"
                f"borderw={TITLE_OUTLINE_WIDTH}:"
                f"bordercolor={TITLE_OUTLINE_COLOR}:"
                f"shadowx={TITLE_SHADOW_X}:shadowy={TITLE_SHADOW_Y}:"
                f"shadowcolor={TITLE_SHADOW_COLOR}:"
                f"box=1:boxcolor={TITLE_BOX_COLOR}:boxborderw={TITLE_BOX_PADDING}:"
                f"x=(w-text_w)/2:"
                f"y={y_expr}"
                f"[{out_label}]"
            )
            prev_label = out_label
        last_label = prev_label
    else:
        last_label = "base"
    title_vf = ";".join(title_vf_parts)

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
    style = "FontName=Noto Sans Devanagari,FontSize=11,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=1.5,Alignment=2,MarginV=80"
    run([
        "ffmpeg", "-y", "-i", str(clip_video_path),
        "-vf", f"subtitles={srt_path}:force_style='{style}'",
        "-c:a", "copy", str(out_path),
    ])


def generate_metadata(source_id: str, source_title: str, transcript: str) -> tuple[str, str, list[str]]:
    """Call Gemini 2.0 Flash to produce a production-ready title, description,
    and tags. Returns (title, description, tags). Raises on any failure so the
    caller can decide the fallback strategy."""
    import json as _json
    import traceback
    from google import genai
    from google.genai import types

    # --- 1. Confirm the API key is present ---
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY env var is not set.")
    print(f"[Gemini] API key present (length={len(api_key)}, prefix={api_key[:6]}...)")

    client = genai.Client(api_key=api_key)

    prompt = (
        f'You are a YouTube channel manager for a Bhojpuri folk songs (lokgeet) channel.\n\n'
        f'Source song: "{source_title}"\n'
        f'Full video: https://www.youtube.com/watch?v={source_id}\n\n'
        f'Transcript of the 35-second Short clip (Hindi/Bhojpuri, may have errors):\n'
        f'"{transcript.strip()}"\n\n'
        f'Return a JSON object with exactly these three keys:\n'
        f'  "title"       \u2013 YouTube Shorts title, under 100 characters.\n'
        f'                   Format: Song Name \u2013 Singer | Bhojpuri Folk Song #Shorts\n'
        f'                   Guess / fix song name and singer from context if needed.\n'
        f'  "description" \u2013 3-5 line YouTube description. Include the song name, one\n'
        f'                   evocative line about the song, the full video link\n'
        f'                   (https://www.youtube.com/watch?v={source_id}), and hashtags.\n'
        f'  "tags"        \u2013 JSON array of 15-20 tag strings mixing broad terms\n'
        f'                   (bhojpuri, folk song, lokgeet, indian folk music) and\n'
        f'                   specific terms (song name, singer name, region).\n\n'
        f'Return only valid JSON \u2014 no markdown fences, no explanation.'
    )

    # --- 2. Log the prompt being sent ---
    print(f"[Gemini] Sending prompt ({len(prompt)} chars):\n{prompt[:300]}{'...' if len(prompt) > 300 else ''}")
    print("[Gemini] Calling gemini-2.0-flash ...")

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    # --- 3. Log the raw response ---
    print(f"[Gemini] Raw response:\n{response.text}")

    # --- 4. Parse and log each field ---
    data = _json.loads(response.text)
    ai_title       = str(data.get("title", source_title))[:100]
    ai_description = str(data.get("description", ""))
    ai_tags        = [str(t) for t in data.get("tags", [])][:20]

    print(f"[Gemini] title       : {ai_title}")
    print(f"[Gemini] description : {ai_description[:120]}{'...' if len(ai_description) > 120 else ''}")
    print(f"[Gemini] tags ({len(ai_tags)})   : {ai_tags}")

    return ai_title, ai_description, ai_tags


def build_claude_prompt(source_id: str, source_title: str, transcript: str) -> str:
    """Build the prompt stored in the uploaded video's description.
    Open the video in YouTube Studio, copy the description into Claude / ChatGPT,
    get title/description/tags, then flip the video to Public."""
    return (
        f'I run a YouTube channel of Bhojpuri folk songs (lokgeet). I\'ve made a Short from this song: "{source_title}" '
        f"(full video: https://www.youtube.com/watch?v={source_id}).\n\n"
        f"Here is a rough transcript of the clip used in the Short (Hindi/Bhojpuri, may contain transcription errors):\n"
        f'"{transcript.strip()}"\n\n'
        f"Please give me:\n\n"
        f"A YouTube Shorts TITLE (under 100 characters) in the format: Song Name \u2013 Singer | Bhojpuri Folk Song #Shorts "
        f"(fix/guess the singer and song name from context if needed)\n"
        f"A YouTube DESCRIPTION (3-5 lines) that includes the song name, a short evocative line about the song, "
        f"the actual full video link I provided above (not a placeholder), and relevant hashtags.\n"
        f"15-20 YouTube TAGS (comma-separated) mixing broad terms (bhojpuri, folk song, lokgeet, indian folk music) "
        f"and specific terms (song name, singer name, region)."
    )


def upload_private(video_path: Path, title: str, description: str, tags: list[str]) -> str:
    """Upload the video as PRIVATE with the supplied metadata."""
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
            "title": title[:100],
            "description": description[:5000],  # YouTube description hard limit
            "tags": tags,
            "categoryId": "10",  # Music
        },
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    return response["id"]


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

    # --- Generate metadata: try Gemini, fall back to prompt-in-description ---
    try:
        print("Generating metadata with Gemini...")
        ai_title, ai_description, ai_tags = generate_metadata(video_id, title, transcript)
        print(f"Gemini metadata ready: {ai_title}")
    except Exception as exc:
        print(f"WARNING: Gemini failed ({exc}). Falling back to prompt-in-description.")
        ai_title       = f"[DRAFT] {title[:80]}"
        ai_description = build_claude_prompt(video_id, title, transcript)
        ai_tags        = []

    uploaded_id = upload_private(final_path, ai_title, ai_description, ai_tags)
    print(f"Uploaded as private: https://studio.youtube.com/video/{uploaded_id}/edit")

    state["used_video_ids"].append(video_id)
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
