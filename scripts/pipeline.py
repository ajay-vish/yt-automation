import json, os, random, subprocess, sys, textwrap, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import librosa
import numpy as np

CHANNEL_URL = "https://www.youtube.com/@manjuvishwakarmalokgeet/videos"
STATE_FILE = Path("state.json")
WORK_DIR = Path("work")
CLIP_SECONDS_RANGE = (15, 22)
SHORT_WIDTH, SHORT_HEIGHT = 1080, 1920

# --- UPDATED STYLING FOR CONCEPT 1 ---
TITLE_FONT_FILE = WORK_DIR / "Mukta-Bold.ttf"
TITLE_FONT_SIZE = 60
TITLE_COLOR = "white"         # Clean white modern text
TITLE_OUTLINE_COLOR = "black"
TITLE_OUTLINE_WIDTH = 2         # Thinner outline for elegance
TITLE_SHADOW_COLOR = "black@0.6"
TITLE_SHADOW_X = TITLE_SHADOW_Y = 5
TITLE_Y_START = 230
TITLE_MAX_LINES = 2
TITLE_SIDE_MARGIN = 60
TITLE_LINE_SPACING = 35

_REPO_ROOT = Path(__file__).resolve().parent.parent
GIF_PATH = _REPO_ROOT / "assets" / "Subscribe.gif"
BRANDING_PATH = _REPO_ROOT / "assets" / "Branding.png"
CALLOUT_PATH = _REPO_ROOT / "assets" / "callout.png"
GIF_START, GIF_END = 5, 9

# Callout overlay settings
CALLOUT_WIDTH = 450
CALLOUT_Y = 60
CALLOUT_CORNER_RADIUS = 10

IST = ZoneInfo("Asia/Kolkata")
SLOT_TIMES_IST = [(13, 0), (19, 0), (21, 0)]
SLOT_SEARCH_DAYS = 30

DEFAULT_LANGUAGE = "hi"
DEFAULT_AUDIO_LANGUAGE = "hi"
BASE_TAGS = [
    "bhojpuri", "bhojpuri song", "bhojpuri lokgeet", "lokgeet",
    "bhojpuri folk song", "indian folk music", "bihar", "up bhojpuri",
    "purvanchal", "bhojpuri bhajan", "bhojpuri diaspora",
]


def run(cmd, **kwargs):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=True, **kwargs)


def load_state():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("used_video_ids", [])
    state.setdefault("scheduled_slots", [])
    state.setdefault("pending_comments", {})
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_next_available_slot(state) -> str:
    now = datetime.now(timezone.utc)
    state["scheduled_slots"] = [
        s for s in state["scheduled_slots"]
        if datetime.fromisoformat(s.replace("Z", "+00:00")) > now
    ]
    taken = set(state["scheduled_slots"])
    day = datetime.now(IST).date()

    for day_offset in range(SLOT_SEARCH_DAYS):
        current_day = day + timedelta(days=day_offset)
        for hour, minute in SLOT_TIMES_IST:
            candidate = datetime(current_day.year, current_day.month, current_day.day,
                                  hour, minute, tzinfo=IST).astimezone(timezone.utc)
            if candidate <= now:
                continue
            iso = candidate.isoformat().replace("+00:00", "Z")
            if iso in taken:
                continue
            state["scheduled_slots"].append(iso)
            return iso
    raise RuntimeError(f"No available slot found in the next {SLOT_SEARCH_DAYS} days.")


def cookie_args():
    cookies_path = os.environ.get("YT_COOKIES_FILE", "cookies.txt")
    if not os.path.exists(cookies_path):
        return []
    try:
        content = Path(cookies_path).read_text(encoding="utf-8").strip()
        if content and not content.startswith("#") and "\t" not in content:
            lines = ["# Netscape HTTP Cookie File"]
            for part in content.split(";"):
                part = part.strip()
                if part and "=" in part:
                    name, val = part.split("=", 1)
                    lines.append(f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{name.strip()}\t{val.strip()}")
            Path(cookies_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[cookies] Warning: {e}")
    return ["--cookies", cookies_path]


def list_channel_videos():
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
        state["used_video_ids"] = []
        unused = videos
    return random.choice(unused)


def download_video(video_id, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    run([
        "yt-dlp", *cookie_args(), "--remote-components", "ejs:github",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
        "--merge-output-format", "mp4",
        "-o", str(dest / f"{video_id}.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ])
    matches = list(dest.glob(f"{video_id}.mp4"))
    if not matches:
        raise RuntimeError(f"Download failed for {video_id}")
    return matches[0]


def extract_audio(video_path: Path, wav_path: Path):
    run(["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "22050", str(wav_path)])


def find_best_window(wav_path: Path, clip_seconds: int) -> float:
    y, sr = librosa.load(str(wav_path), sr=None)
    duration = librosa.get_duration(y=y, sr=sr)
    if duration <= clip_seconds:
        return 0.0

    rms = librosa.feature.rms(y=y, frame_length=sr, hop_length=sr)[0]
    margin = max(1, int(duration * 0.05))

    onset_frames = librosa.onset.onset_detect(
        y=y, sr=sr, units="time", backtrack=True,
    )
    onset_times = [t for t in onset_frames if margin <= t <= duration - clip_seconds - margin]

    def window_score(start: float) -> float:
        start_i = int(start)
        return float(np.mean(rms[start_i:start_i + clip_seconds]))

    if onset_times:
        best_start = max(onset_times, key=window_score)
        return float(best_start)

    best_start, best_score = margin, -1
    for start in range(margin, max(margin + 1, int(duration) - clip_seconds - margin)):
        score = window_score(start)
        if score > best_score:
            best_score, best_start = score, start
    return float(best_start)


def download_font(dest_path: Path):
    # Swapped to Mukta: beautiful sans-serif that supports both Hindi (Devanagari) & English
    url = "https://github.com/google/fonts/raw/main/ofl/mukta/Mukta-Bold.ttf"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as response, open(dest_path, "wb") as f:
        f.write(response.read())


def _wrap_title(text: str) -> list[str]:
    avg_char_width = TITLE_FONT_SIZE * 0.56
    max_chars = max(1, int((SHORT_WIDTH - TITLE_SIDE_MARGIN) / avg_char_width))
    lines = textwrap.wrap(text, width=max_chars) or [text]
    if len(lines) > TITLE_MAX_LINES:
        lines = lines[:TITLE_MAX_LINES]
        last = lines[-1]
        if len(last) > 3:
            last = last[:max_chars - 1].rstrip() + "\u2026"
        lines[-1] = last
    return lines


def _escape_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def cut_and_reframe(video_path: Path, start: float, clip_seconds: int, out_path: Path, title: str = ""):
    display_title = title.split("#")[0].strip() if title else ""

    fg_width = SHORT_WIDTH - 25
    fg_height = SHORT_WIDTH - 25  # 1:1 square
    radius = 30  # Rounded corner radius

    # 1. Background: Blurred + 5% Black overlay (Frosted Glass)
    bg_vf = (
        f"[0:v]trim=start={start}:duration={clip_seconds},setpts=PTS-STARTPTS,"
        f"scale={SHORT_WIDTH}:{SHORT_HEIGHT}:force_original_aspect_ratio=increase,crop={SHORT_WIDTH}:{SHORT_HEIGHT},"
        f"boxblur=20:5,drawbox=x=0:y=0:w=iw:h=ih:color=black@0.05:t=fill[bg_dark]"
    )

    # 2. Foreground: Square cropped center video with rounded corners via Alpha Mask
    fg_vf = (
        f"[0:v]trim=start={start}:duration={clip_seconds},setpts=PTS-STARTPTS,"
        f"scale={fg_width}:{fg_height}:force_original_aspect_ratio=increase,crop={fg_width}:{fg_height},"
        f"format=yuva420p,"
        f"geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':"
        f"a='if(gt(abs(W/2-X),W/2-{radius})*gt(abs(H/2-Y),H/2-{radius}),"
        f"if(lte(hypot({radius}-(W/2-abs(W/2-X)),{radius}-(H/2-abs(H/2-Y))),{radius}),255,0),255)'[fg_round]"
    )

    # 3. No Drop Shadow: Overlay FG directly onto bg_dark
    shadow_vf = (
        f"[bg_dark][fg_round]overlay=(W-w)/2:(H-h)/2[base]"
    )

    base_vf = f"{bg_vf};{fg_vf};{shadow_vf}"

    title_vf_parts = []
    last_label = "base"
    if display_title:
        safe_font_path = str(TITLE_FONT_FILE.resolve()).replace('\\', '/').replace(':', '\\:')
        lines = _wrap_title(display_title)
        line_height = TITLE_FONT_SIZE + TITLE_LINE_SPACING
        prev_label = "base"
        for i, line in enumerate(lines):
            out_label = f"title{i}"
            y_expr = f"{TITLE_Y_START}+{i * line_height}" if i else str(TITLE_Y_START)
            title_vf_parts.append(
                f"[{prev_label}]drawtext=text='{_escape_drawtext(line)}':"
                f"fontfile='{safe_font_path}':fontsize={TITLE_FONT_SIZE}:fontcolor={TITLE_COLOR}:"
                f"borderw={TITLE_OUTLINE_WIDTH}:bordercolor={TITLE_OUTLINE_COLOR}:"
                f"shadowx={TITLE_SHADOW_X}:shadowy={TITLE_SHADOW_Y}:shadowcolor={TITLE_SHADOW_COLOR}:"
                f"x=(w-text_w)/2:y={y_expr}[{out_label}]"
            )
            prev_label = out_label
        last_label = prev_label
    title_vf = ";".join(title_vf_parts)

    scale_vf = (
        f"[1:v]scale=350:-2[gif_scaled];"
        f"[2:v]scale=546:-2,format=yuva420p,"
        f"geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='if(gt(abs(W/2-X),W/2-(H*0.1))*gt(abs(H/2-Y),H/2-(H*0.1)),"
        f"if(lte(hypot((H*0.1)-(W/2-abs(W/2-X)),(H*0.1)-(H/2-abs(H/2-Y))),(H*0.1)),p(X,Y),0),p(X,Y))'[brand_scaled];"
        f"[3:v]scale={CALLOUT_WIDTH}:-2,format=yuva420p,"
        f"geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='if(gt(abs(W/2-X),W/2-{CALLOUT_CORNER_RADIUS})*gt(abs(H/2-Y),H/2-{CALLOUT_CORNER_RADIUS}),"
        f"if(lte(hypot({CALLOUT_CORNER_RADIUS}-(W/2-abs(W/2-X)),{CALLOUT_CORNER_RADIUS}-(H/2-abs(H/2-Y))),{CALLOUT_CORNER_RADIUS}),p(X,Y),0),p(X,Y))'[callout_scaled]"
    )
    callout_vf = f"[{last_label}][callout_scaled]overlay=x=(W-w)/2:y={CALLOUT_Y}:eof_action=repeat[with_callout]"
    brand_vf = f"[with_callout][brand_scaled]overlay=x=(W-w)/2:y=H-h-120:eof_action=repeat[branded]"
    gif_vf = (
        f"[gif_scaled]setpts=PTS-STARTPTS+{GIF_START}/TB[gif_timed];"
        f"[branded][gif_timed]overlay=x=(W-w)/2:y=H-h-100:"
        f"enable='between(t,{GIF_START},{GIF_END})':eof_action=pass[vout]"
    )

    filter_parts = [base_vf]
    if title_vf:
        filter_parts.append(title_vf)
    filter_parts += [scale_vf, callout_vf, brand_vf, gif_vf]
    filter_complex = ";".join(filter_parts)
    filter_complex += f";[0:a]atrim=start={start}:duration={clip_seconds},asetpts=PTS-STARTPTS[aout]"

    run([
        "ffmpeg", "-y", "-i", str(video_path), "-ignore_loop", "0",
        "-i", str(GIF_PATH), "-i", str(BRANDING_PATH), "-i", str(CALLOUT_PATH),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out_path),
    ])


def transcribe_to_srt(clip_video_path: Path, srt_path: Path) -> str:
    from faster_whisper import WhisperModel

    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(clip_video_path), language="hi")

    def fmt_ts(t):
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s - int(s)) * 1000):03d}"

    lines, parts = [], []
    for i, seg in enumerate(segments, start=1):
        lines += [str(i), f"{fmt_ts(seg.start)} --> {fmt_ts(seg.end)}", seg.text.strip(), ""]
        parts.append(seg.text.strip())

    if not lines:
        lines = ["1", "00:00:00,000 --> 00:00:01,000", "", ""]

    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return " ".join(parts)


def burn_captions(clip_video_path: Path, srt_path: Path, out_path: Path):
    style = (
        "FontName=Noto Sans Devanagari,FontSize=8,PrimaryColour=&HFFFFFF&,"
        "OutlineColour=&H000000&,BorderStyle=1,Outline=1.2,Alignment=2,MarginV=50"
    )
    safe_srt_path = str(srt_path.resolve()).replace('\\', '/').replace(':', '\\:')
    run(["ffmpeg", "-y", "-i", str(clip_video_path),
         "-vf", f"subtitles='{safe_srt_path}':force_style='{style}'",
         "-c:a", "copy", str(out_path)])


def generate_metadata(source_id: str, source_title: str, transcript: str) -> tuple[str, str, list[str]]:
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY env var is not set.")

    client = genai.Client(api_key=api_key)
    prompt = (
        f'You are an expert YouTube Shorts strategist for a Bhojpuri folk songs (lokgeet) channel, '
        f'skilled at writing titles that match what currently trends and ranks well in this niche.\n\n'
        f'Source song: "{source_title}"\n'
        f'Full video: https://www.youtube.com/watch?v={source_id}\n\n'
        f'Transcript of the Short clip (Hindi/Bhojpuri, may have errors):\n"{transcript.strip()}"\n\n'
        f'Before writing, think about how top-performing Bhojpuri/Indian folk music Shorts titles are '
        f'usually written: they front-load the song or singer name (high search volume terms), use '
        f'emotional or curiosity-driving phrases (e.g. "dil dhoom", "sabse hit", "bewafa", "viral"), '
        f'keep it short enough to not get cut off on mobile, and match common search phrasing '
        f'("bhojpuri new song", "bhojpuri lokgeet 2025", singer name + song name) rather than generic wording.\n\n'
        f'Return a JSON object with exactly these three keys:\n'
        f'  "title" - under 100 characters. Lead with the strongest hook (song name, singer, or '
        f'emotional phrase) in the first few words since that is what shows in search/suggested feeds. '
        f'Format: Song Name - Singer | Bhojpuri Folk Song #Shorts (adapt wording for higher CTR while '
        f'keeping it truthful to the clip)\n'
        f'  "description" - 3-5 lines: song name, one evocative/emotional line that encourages watching '
        f'the full video, the full video link (https://www.youtube.com/watch?v={source_id}), then a '
        f'final line with 8-10 hashtags for maximum discovery: always include #Shorts, #Bhojpuri, '
        f'#Lokgeet, and #BhojpuriSong, plus a region hashtag (#Bihar, #UP, or #Purvanchal), plus 3-4 '
        f'more specific ones from the song itself (singer name, occasion/festival if mentioned e.g. '
        f'#Vivah #Bhakti #Shaadi, or genre like #FolkMusic #DesiMusic)\n'
        f'  "tags" - JSON array of 15-20 strings optimized for YouTube search ranking: mix broad '
        f'high-volume terms (bhojpuri, bhojpuri song, folk song, lokgeet, indian folk music, bhojpuri '
        f'new song), regional terms (bihar, up bhojpuri, purvanchal), and specific terms (song name, '
        f'singer name in full and common misspellings, occasion). Order tags roughly by expected search '
        f'volume, highest first.\n\n'
        f'Return only valid JSON - no markdown fences, no explanation.'
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash", contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = json.loads(response.text)
    return (
        str(data.get("title", source_title))[:100],
        str(data.get("description", "")),
        [str(t) for t in data.get("tags", [])][:20],
    )


def build_fallback_prompt(source_id: str, source_title: str, transcript: str) -> str:
    return (
        f'I run a YouTube channel of Bhojpuri folk songs (lokgeet). I\'ve made a Short from this song: '
        f'"{source_title}" (full video: https://www.youtube.com/watch?v={source_id}).\n\n'
        f'Transcript of the clip (Hindi/Bhojpuri, may contain errors):\n"{transcript.strip()}"\n\n'
        f'Please give me a TITLE (under 100 chars, format: Song Name - Singer | Bhojpuri Folk Song #Shorts), '
        f'a DESCRIPTION (3-5 lines with song name, an evocative line, the video link, then a final '
        f'line of 8-10 hashtags - always #Shorts #Bhojpuri #Lokgeet #BhojpuriSong, a region hashtag '
        f'like #Bihar/#UP/#Purvanchal, and 3-4 more specific ones from the singer, occasion, or genre), '
        f'and 15-20 comma-separated TAGS mixing broad and specific terms.'
    )


def merge_tags(ai_tags: list[str]) -> list[str]:
    seen, merged = set(), []
    for t in ai_tags + BASE_TAGS:
        key = t.strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(t.strip())
    return merged[:20]


def upload_private(video_path: Path, title: str, description: str, tags: list[str],
                    publish_at: str | None = None) -> str:
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

    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    if publish_at:
        status["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": "10",
            "defaultLanguage": DEFAULT_LANGUAGE,
            "defaultAudioLanguage": DEFAULT_AUDIO_LANGUAGE,
        },
        "status": status,
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    response = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    return response["id"]


def main():
    state = load_state()
    video = pick_video(state)
    video_id, title = video["id"], video["title"]
    print(f"Selected video: {title} ({video_id})")

    WORK_DIR.mkdir(exist_ok=True)
    raw_path = download_video(video_id, WORK_DIR)

    wav_path = WORK_DIR / f"{video_id}.wav"
    extract_audio(raw_path, wav_path)

    clip_seconds = random.randint(*CLIP_SECONDS_RANGE)
    start = find_best_window(wav_path, clip_seconds)
    print(f"Clip length {clip_seconds}s, best window starts at {start:.0f}s")

    clip_path = WORK_DIR / f"{video_id}_clip.mp4"
    download_font(TITLE_FONT_FILE)
    cut_and_reframe(raw_path, start, clip_seconds, clip_path, title=title)

    srt_path = WORK_DIR / f"{video_id}.srt"
    transcript = transcribe_to_srt(clip_path, srt_path)

    final_path = WORK_DIR / f"{video_id}_final.mp4"
    burn_captions(clip_path, srt_path, final_path)

    # --- Get AI metadata (independent of scheduling) ---
    try:
        ai_title, ai_description, ai_tags = generate_metadata(video_id, title, transcript)
    except Exception as exc:
        print(f"WARNING: Gemini failed ({exc}). Falling back to draft.")
        ai_title = f"[DRAFT] {title[:80]}"
        ai_description = build_fallback_prompt(video_id, title, transcript)
        ai_tags = []

    # --- Try to find a publish slot (independent of metadata generation) ---
    publish_at = None
    try:
        publish_at = get_next_available_slot(state)
        print(f"Scheduled to auto-publish at {publish_at} (UTC)")
    except Exception as exc:
        print(f"WARNING: No available slot in next {SLOT_SEARCH_DAYS} days ({exc}). "
              f"Uploading as private, unscheduled.")

    ai_tags = merge_tags(ai_tags)
    uploaded_id = upload_private(final_path, ai_title, ai_description, ai_tags, publish_at=publish_at)
    print(f"Uploaded: https://studio.youtube.com/video/{uploaded_id}/edit "
          f"({'scheduled ' + publish_at if publish_at else 'unscheduled draft'})")

    state["used_video_ids"].append(video_id)
    state["pending_comments"][uploaded_id] = {"source_id": video_id, "title": title}
    save_state(state)


if __name__ == "__main__":
    sys.exit(main())
