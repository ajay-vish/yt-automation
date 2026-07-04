import json, os
from pathlib import Path

STATE_FILE = Path("state.json")


def load_state():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("pending_comments", {})
    state.setdefault("commented_video_ids", [])
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_youtube_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("youtube", "v3", credentials=creds)


def is_public(youtube, video_id: str) -> bool:
    resp = youtube.videos().list(part="status", id=video_id).execute()
    items = resp.get("items", [])
    return bool(items) and items[0]["status"]["privacyStatus"] == "public"


def post_comment(youtube, video_id: str, source_id: str):
    text = (
        f"🎶 Full song here: https://www.youtube.com/watch?v={source_id}\n"
        f"Which song should I post next? Comment below 👇"
    )
    youtube.commentThreads().insert(
        part="snippet",
        body={
            "snippet": {
                "videoId": video_id,
                "topLevelComment": {"snippet": {"textOriginal": text}},
            }
        },
    ).execute()


def main():
    state = load_state()
    pending = dict(state["pending_comments"])
    if not pending:
        print("No pending comments to check.")
        return

    youtube = get_youtube_client()
    for video_id, info in pending.items():
        try:
            if not is_public(youtube, video_id):
                print(f"{video_id}: not public yet, skipping.")
                continue
            post_comment(youtube, video_id, info["source_id"])
            print(f"{video_id}: comment posted.")
            state["commented_video_ids"].append(video_id)
            del state["pending_comments"][video_id]
        except Exception as exc:
            print(f"{video_id}: failed to comment ({exc}). Will retry next run.")

    save_state(state)


if __name__ == "__main__":
    main()