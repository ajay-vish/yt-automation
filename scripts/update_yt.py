import os
import sys
import csv
import time
import traceback
import difflib
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

WORKSPACE_DIR = Path(__file__).resolve().parent.parent
WORK_DIR = WORKSPACE_DIR / "work"
SUGGESTIONS_CSV = WORK_DIR / "metadata_suggestions_review.csv"
LOG_CSV = WORK_DIR / "metadata_apply_log.csv"

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

def get_credentials():
    refresh_token = os.environ.get("YT_REFRESH_TOKEN")
    client_id = os.environ.get("YT_CLIENT_ID")
    client_secret = os.environ.get("YT_CLIENT_SECRET")
    if not (refresh_token and client_id and client_secret):
        print("\n[Error] Missing YouTube API credentials in .env")
        sys.exit(1)
    return Credentials(
        None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        # videos().update() needs the full 'youtube' scope, same as the delete script.
    )

def load_approved_rows(csv_path):
    if not csv_path.exists():
        print(f"[Error] Suggestions CSV not found at {csv_path}. Run generate_metadata_suggestions.py first.")
        sys.exit(1)

    approved, skipped_unapproved, skipped_incomplete = [], 0, 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("needs_manual_review") == "1":
                skipped_incomplete += 1
                continue
            if row.get("approved") != "1":
                skipped_unapproved += 1
                continue
            if not row.get("suggested_title") or not row.get("suggested_tags"):
                skipped_incomplete += 1
                continue
            approved.append(row)

    print(f"{len(approved)} row(s) marked approved and ready to apply.")
    if skipped_unapproved:
        print(f"{skipped_unapproved} row(s) skipped (not marked approved=1).")
    if skipped_incomplete:
        print(f"{skipped_incomplete} row(s) skipped (missing suggested title/tags, likely a generation failure).")
    return approved

def fetch_current_metadata(youtube, video_ids):
    metadata_map = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i+50]
        ids_str = ",".join(chunk)
        try:
            res = youtube.videos().list(id=ids_str, part="snippet").execute()
            for item in res.get("items", []):
                metadata_map[item["id"]] = item["snippet"]
        except Exception as e:
            print(f"[Warning] Failed to fetch metadata for chunk: {e}")
    return metadata_map

def apply_update(youtube, row, category_id_cache):
    vid_id = row["video_id"]

    # videos().update with part="snippet" requires the FULL snippet object,
    # including categoryId -- omitting it can wipe the existing category.
    current = youtube.videos().list(id=vid_id, part="snippet").execute()
    items = current.get("items", [])
    if not items:
        raise RuntimeError(f"Video {vid_id} not found (deleted or no longer accessible)")

    snippet = items[0]["snippet"]
    tags = [t.strip() for t in row["suggested_tags"].split(",") if t.strip()]

    snippet["title"] = row["suggested_title"][:100]
    snippet["description"] = row["suggested_description"]
    snippet["tags"] = tags

    youtube.videos().update(
        part="snippet",
        body={"id": vid_id, "snippet": snippet},
    ).execute()

def main():
    dry_run = "--confirm" not in sys.argv

    load_env()
    approved = load_approved_rows(SUGGESTIONS_CSV)
    if not approved:
        print("Nothing approved to apply. Set approved=1 on rows in metadata_suggestions_review.csv first.")
        return

    if dry_run:
        print("\nRunning in DRY RUN mode -- no metadata will actually be updated.")
        print("Re-run with --confirm to push changes live, e.g.:")
        print("    python apply_metadata_updates.py --confirm\n")
        
        creds = get_credentials()
        youtube = build("youtube", "v3", credentials=creds)
        
        approved_ids = [row["video_id"] for row in approved]
        print(f"Fetching current live metadata for {len(approved_ids)} approved video(s)...")
        current_metadata = fetch_current_metadata(youtube, approved_ids)
        
        diff_count = 0
        for row in approved:
            vid_id = row["video_id"]
            if vid_id not in current_metadata:
                print(f"Warning: Video {vid_id} not found on YouTube (might be deleted, private, or inaccessible).")
                continue
            
            snippet = current_metadata[vid_id]
            current_title = snippet.get("title", "")
            current_description = snippet.get("description", "")
            current_tags = snippet.get("tags", [])
            
            # Suggested fields
            suggested_title = (row.get("suggested_title") or "")[:100]
            suggested_description = row.get("suggested_description") or ""
            suggested_tags = [t.strip() for t in (row.get("suggested_tags") or "").split(",") if t.strip()]
            
            title_changed = (current_title != suggested_title)
            desc_changed = (current_description != suggested_description)
            tags_changed = (current_tags != suggested_tags)
            
            if title_changed or desc_changed or tags_changed:
                diff_count += 1
                print("=" * 80)
                print(f"VIDEO ID: {vid_id}")
                print(f"URL: {row.get('url', f'https://www.youtube.com/watch?v={vid_id}')}")
                print("-" * 80)
                
                if title_changed:
                    print("Title:")
                    print(f"  [-] {current_title}")
                    print(f"  [+] {suggested_title}")
                    print()
                
                if desc_changed:
                    print("Description Diff:")
                    diff_lines = list(difflib.unified_diff(
                        current_description.splitlines(),
                        suggested_description.splitlines(),
                        lineterm=""
                    ))
                    for line in diff_lines[2:]:
                        print(f"  {line}")
                    print()
                
                if tags_changed:
                    print("Tags:")
                    added_tags = [t for t in suggested_tags if t not in current_tags]
                    removed_tags = [t for t in current_tags if t not in suggested_tags]
                    
                    if added_tags:
                        print(f"  [+] Added: {', '.join(added_tags)}")
                    if removed_tags:
                        print(f"  [-] Removed: {', '.join(removed_tags)}")
                    
                    if not added_tags and not removed_tags:
                        print("  [*] Tag order changed.")
                    print()
        
        print("=" * 80)
        if diff_count == 0:
            print("No differences found between current metadata and suggestions for the approved video(s).")
        else:
            print(f"Dry run complete. Shown diffs for {diff_count} out of {len(approved)} approved video(s).")
        return

    creds = get_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    results = []
    for i, row in enumerate(approved, 1):
        vid_id = row["video_id"]
        print(f"[{i}/{len(approved)}] Updating {vid_id}...")
        try:
            apply_update(youtube, row, {})
            print("    [OK]")
            results.append({"video_id": vid_id, "status": "updated", "error": ""})
        except HttpError as e:
            print(f"    [Failed] {e}")
            results.append({"video_id": vid_id, "status": "failed", "error": str(e)})
        except Exception as e:
            print(f"    [Failed] {e}")
            results.append({"video_id": vid_id, "status": "failed", "error": str(e)})
        time.sleep(0.5)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["video_id", "status", "error"])
        writer.writeheader()
        writer.writerows(results)
    print(f"\nLog written to: {LOG_CSV}")

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception:
        print("\n[Fatal Error]")
        traceback.print_exc()
        sys.exit(1)