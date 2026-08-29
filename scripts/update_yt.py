import argparse
import csv
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

WORKSPACE_DIR = Path(__file__).resolve().parent.parent
WORK_DIR = WORKSPACE_DIR / "work"
STATE_FILE = WORKSPACE_DIR / "state.json"
ANALYTICS_CSV = WORK_DIR / "youtube_analytics_data.csv"
CLEANUP_LOG_CSV = WORK_DIR / "cleanup_log.csv"

try:
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30))
SLOT_TIMES_IST = [(13, 0), (19, 0), (21, 0)]  # 1:00 PM, 7:00 PM, 9:00 PM IST


def load_env():
    env_path = WORKSPACE_DIR / ".env"
    if not env_path.exists():
        print(f"Information: No .env file found at {env_path}")
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip().strip("'\"")
    print("Loaded credentials from .env")


def get_youtube_client():
    refresh_token = os.environ.get("YT_REFRESH_TOKEN")
    client_id = os.environ.get("YT_CLIENT_ID")
    client_secret = os.environ.get("YT_CLIENT_SECRET")
    if not (refresh_token and client_id and client_secret):
        print("\n[Error] Missing YouTube API credentials in environment or .env (YT_REFRESH_TOKEN, YT_CLIENT_ID, YT_CLIENT_SECRET)")
        sys.exit(1)
    creds = Credentials(
        None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("youtube", "v3", credentials=creds)


def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    else:
        state = {}
    state.setdefault("used_video_ids", [])
    state.setdefault("scheduled_slots", [])
    state.setdefault("pending_comments", {})
    state.setdefault("commented_video_ids", [])
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def get_candidate_video_ids_from_csv(csv_path: Path) -> list[dict]:
    """Reads videos marked private in youtube_analytics_data.csv."""
    if not csv_path.exists():
        return []
    candidates = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("privacy_status", "").lower() == "private":
                candidates.append({
                    "id": row["video_id"],
                    "title": row.get("title", ""),
                })
    return candidates


def get_all_channel_upload_ids(youtube) -> list[dict]:
    """Paginates the channel's uploads playlist to retrieve all uploaded video IDs."""
    print("Fetching channel uploads playlist...")
    channel_response = youtube.channels().list(mine=True, part="contentDetails,id").execute()
    items = channel_response.get("items", [])
    if not items:
        raise RuntimeError("No channel found for the authorized account.")

    uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    playlist_items = []
    next_page_token = None
    while True:
        res = youtube.playlistItems().list(
            playlistId=uploads_playlist_id,
            part="snippet,contentDetails",
            maxResults=50,
            pageToken=next_page_token,
        ).execute()
        for item in res.get("items", []):
            vid_id = item["contentDetails"]["videoId"]
            title = item.get("snippet", {}).get("title", vid_id)
            playlist_items.append({"id": vid_id, "title": title})
        next_page_token = res.get("nextPageToken")
        if not next_page_token:
            break
        print(f"  Retrieved {len(playlist_items)} videos so far...")
    return playlist_items


def inspect_videos_live(youtube, candidate_ids: list[str]) -> tuple[list[dict], set[str]]:
    """
    Fetches snippet & status for candidates in chunks of 50.
    Returns:
      - stuck_private: list of dicts with id, title, status for videos with privacyStatus=private and no publishAt.
      - active_scheduled_slots: set of UTC ISO slot strings currently scheduled on YouTube.
    """
    stuck_private = []
    active_scheduled_slots = set()

    total = len(candidate_ids)
    print(f"Verifying live status for {total} candidate video(s) in batches of 50...")

    for i in range(0, total, 50):
        batch = candidate_ids[i:i + 50]
        try:
            res = youtube.videos().list(
                id=",".join(batch),
                part="snippet,status",
            ).execute()
            for item in res.get("items", []):
                vid_id = item["id"]
                status = item.get("status", {})
                snippet = item.get("snippet", {})
                privacy = status.get("privacyStatus")
                publish_at = status.get("publishAt")

                if publish_at:
                    active_scheduled_slots.add(publish_at)

                if privacy == "private" and not publish_at:
                    stuck_private.append({
                        "id": vid_id,
                        "title": snippet.get("title", ""),
                        "tags": snippet.get("tags", []),
                        "categoryId": snippet.get("categoryId", "10"),
                        "published_at_raw": snippet.get("publishedAt", ""),
                    })
        except Exception as e:
            print(f"[Warning] Failed to verify batch starting at index {i}: {e}")

    return stuck_private, active_scheduled_slots


def generate_available_slots(taken_slots: set[str], count: int) -> list[str]:
    """Generates future open slot timestamps in UTC adhering to SLOT_TIMES_IST."""
    now_utc = datetime.now(timezone.utc)
    slots = []
    day = datetime.now(IST).date()

    for day_offset in range(365):  # search up to 1 year ahead
        current_day = day + timedelta(days=day_offset)
        for hour, minute in SLOT_TIMES_IST:
            candidate = datetime(
                current_day.year, current_day.month, current_day.day,
                hour, minute, tzinfo=IST
            ).astimezone(timezone.utc)
            if candidate <= now_utc + timedelta(minutes=15):
                continue
            iso = candidate.isoformat().replace("+00:00", "Z")
            if iso in taken_slots:
                continue
            taken_slots.add(iso)
            slots.append(iso)
            if len(slots) >= count:
                return slots
    return slots


def main():
    parser = argparse.ArgumentParser(
        description="Cleanup backstop: find stuck private YouTube videos without a publish schedule, and either schedule or publish them."
    )
    parser.add_argument(
        "--action", choices=["schedule", "publish"], default="schedule",
        help="Action to perform on stuck private videos: 'schedule' (default) into open future slots, or 'publish' immediately."
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Execute updates live on YouTube. Without this flag, runs in DRY RUN mode."
    )
    parser.add_argument(
        "--limit", type=int, default=100,
        help="Maximum number of videos to update in this run (default: 100). YouTube API allows ~200 updates/day (50 quota units each)."
    )
    parser.add_argument(
        "--scan-live", action="store_true",
        help="Scan live YouTube channel uploads instead of reading candidates from youtube_analytics_data.csv."
    )
    parser.add_argument(
        "--csv", type=str, default=str(ANALYTICS_CSV),
        help=f"Path to CSV file with video data (default: {ANALYTICS_CSV})."
    )

    args = parser.parse_args()
    dry_run = not args.confirm

    load_env()
    state = load_state()
    youtube = get_youtube_client()

    csv_path = Path(args.csv)
    candidate_videos = []

    if not args.scan_live and csv_path.exists():
        print(f"Reading candidate private videos from: {csv_path}")
        candidate_videos = get_candidate_video_ids_from_csv(csv_path)
        print(f"Found {len(candidate_videos)} private video candidate(s) in CSV.")

    if not candidate_videos:
        print("No candidates found in CSV or --scan-live requested. Scanning channel uploads via YouTube API...")
        candidate_videos = get_all_channel_upload_ids(youtube)
        print(f"Total channel videos found: {len(candidate_videos)}")

    candidate_ids = [v["id"] for v in candidate_videos]
    stuck_videos, live_scheduled = inspect_videos_live(youtube, candidate_ids)

    print(f"\nDiscovered {len(stuck_videos)} stuck private video(s) (privacyStatus=private, publishAt=None).")
    print(f"Discovered {len(live_scheduled)} video(s) already scheduled on YouTube.")

    if not stuck_videos:
        print("No stuck private videos found! Channel is in a clean state.")
        return

    # Merge taken slots from state.json + live scheduled videos on YouTube
    now_utc = datetime.now(timezone.utc)
    cleaned_state_slots = {
        s for s in state.get("scheduled_slots", [])
        if datetime.fromisoformat(s.replace("Z", "+00:00")) > now_utc
    }
    all_taken_slots = cleaned_state_slots.union(live_scheduled)

    limit = args.limit if args.limit > 0 else len(stuck_videos)
    to_process = stuck_videos[:limit]
    print(f"Targeting {len(to_process)} video(s) for action '{args.action}' (limit: {args.limit}).")

    if args.action == "schedule":
        assigned_slots = generate_available_slots(all_taken_slots, len(to_process))
        for vid, slot in zip(to_process, assigned_slots):
            vid["target_slot"] = slot
    else:
        for vid in to_process:
            vid["target_slot"] = "IMMEDIATE_PUBLIC"

    if dry_run:
        print("\n" + "=" * 80)
        print("DRY RUN MODE -- No changes will be applied to YouTube.")
        print(f"Action: {args.action.upper()}")
        print(f"Run with --confirm to execute live on YouTube.")
        print("=" * 80)
        for i, vid in enumerate(to_process[:20], 1):
            target_str = vid.get("target_slot")
            if target_str != "IMMEDIATE_PUBLIC":
                # Convert to IST for readable output
                dt_utc = datetime.fromisoformat(target_str.replace("Z", "+00:00"))
                dt_ist = dt_utc.astimezone(IST).strftime("%Y-%m-%d %I:%M %p IST")
                target_str = f"{target_str} ({dt_ist})"
            print(f"[{i:03d}/{len(to_process)}] {vid['id']} -> {target_str}")
            print(f"      Title: {vid['title'][:70]}")
        if len(to_process) > 20:
            print(f"... and {len(to_process) - 20} more videos.")
        print("=" * 80)
        print(f"Total to recover: {len(to_process)} video(s).")
        return

    # LIVE EXECUTION
    print("\n" + "=" * 80)
    print(f"EXECUTING LIVE UPDATES ({args.action.upper()}) on {len(to_process)} video(s)...")
    print("=" * 80)

    log_entries = []
    quota_exhausted = False

    for i, vid in enumerate(to_process, 1):
        vid_id = vid["id"]
        target_slot = vid.get("target_slot")

        if args.action == "schedule":
            dt_utc = datetime.fromisoformat(target_slot.replace("Z", "+00:00"))
            dt_ist = dt_utc.astimezone(IST).strftime("%Y-%m-%d %I:%M %p IST")
            print(f"[{i}/{len(to_process)}] Scheduling {vid_id} for {target_slot} ({dt_ist})...", end=" ", flush=True)
            body = {
                "id": vid_id,
                "status": {
                    "privacyStatus": "private",
                    "publishAt": target_slot,
                    "selfDeclaredMadeForKids": False,
                },
            }
        else:
            print(f"[{i}/{len(to_process)}] Publishing {vid_id} immediately as public...", end=" ", flush=True)
            body = {
                "id": vid_id,
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": False,
                },
            }

        try:
            youtube.videos().update(part="status", body=body).execute()
            print("[OK]")
            log_entries.append({
                "video_id": vid_id,
                "title": vid["title"],
                "action": args.action,
                "target_slot": target_slot,
                "status": "success",
                "error": "",
            })

            if args.action == "schedule":
                if target_slot not in state["scheduled_slots"]:
                    state["scheduled_slots"].append(target_slot)
                if vid_id not in state["pending_comments"] and vid_id not in state["commented_video_ids"]:
                    state["pending_comments"][vid_id] = {
                        "source_id": vid_id,
                        "title": vid["title"],
                    }

        except HttpError as exc:
            error_str = str(exc)
            print(f"[FAILED: {exc.resp.status}]")
            log_entries.append({
                "video_id": vid_id,
                "title": vid["title"],
                "action": args.action,
                "target_slot": target_slot,
                "status": "failed",
                "error": error_str,
            })
            if "quotaExceeded" in error_str:
                print("\n[WARNING] YouTube Data API daily quota exceeded (10,000 unit limit reached).")
                print("Stopping execution to preserve quota. You can re-run this script tomorrow to continue.")
                quota_exhausted = True
                break
        except Exception as exc:
            print(f"[FAILED: {exc}]")
            log_entries.append({
                "video_id": vid_id,
                "title": vid["title"],
                "action": args.action,
                "target_slot": target_slot,
                "status": "failed",
                "error": str(exc),
            })

        # Save state periodically after every 10 updates
        if i % 10 == 0:
            save_state(state)
        time.sleep(0.5)

    # Final state and log save
    save_state(state)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with open(CLEANUP_LOG_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["video_id", "title", "action", "target_slot", "status", "error"])
        writer.writeheader()
        writer.writerows(log_entries)

    print("\n" + "=" * 80)
    success_count = sum(1 for e in log_entries if e["status"] == "success")
    fail_count = sum(1 for e in log_entries if e["status"] == "failed")
    print(f"Cleanup Run Complete: {success_count} succeeded, {fail_count} failed.")
    print(f"State saved to: {STATE_FILE}")
    print(f"Log written to: {CLEANUP_LOG_CSV}")
    if quota_exhausted:
        print("Note: Daily quota was reached. Please run again after quota resets at midnight Pacific Time.")
    print("=" * 80)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except KeyboardInterrupt:
        print("\nProcess interrupted by user.")
        sys.exit(0)
    except Exception:
        print("\n[Fatal Error]")
        traceback.print_exc()
        sys.exit(1)