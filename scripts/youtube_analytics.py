import os
import re
import csv
import sys
import time
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- DIRS CONFIG ---
WORKSPACE_DIR = Path(__file__).resolve().parent.parent
WORK_DIR = WORKSPACE_DIR / "work"
CSV_PATH = WORK_DIR / "youtube_analytics_data.csv"
REPORT_PATH = WORK_DIR / "youtube_analysis_report.md"

# Videos newer than this are excluded from "golden day/hour" and other
# performance rankings, since they haven't had time to accumulate views yet.
# They're still fetched and shown separately so you can track them.
MATURITY_THRESHOLD_DAYS = 3

# If uploads on the same day are less than this many minutes apart, flag them
# as a burst -- YouTube tends to suppress distribution for rapid-fire same-channel uploads.
BURST_GAP_MINUTES = 45

# --- ENV LOADING ---
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

# --- AUTH SETUP ---
def get_credentials():
    refresh_token = os.environ.get("YT_REFRESH_TOKEN")
    client_id = os.environ.get("YT_CLIENT_ID")
    client_secret = os.environ.get("YT_CLIENT_SECRET")

    if not (refresh_token and client_id and client_secret):
        print("\n[Error] Missing YouTube API credentials!")
        print("Please check that your .env file in the workspace root directory contains:")
        print("YT_CLIENT_ID=...")
        print("YT_CLIENT_SECRET=...")
        print("YT_REFRESH_TOKEN=...")
        sys.exit(1)

    return Credentials(
        None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        # NOTE: scopes are baked into the refresh token at consent time, not set here.
        # If the Analytics API keeps failing (see check_analytics_access below),
        # the refresh token itself was likely issued without the analytics scope
        # and needs to be re-generated via a fresh OAuth consent that includes:
        #   https://www.googleapis.com/auth/yt-analytics.readonly
        #   https://www.googleapis.com/auth/youtube.readonly
    )

# --- ISO DURATION PARSER ---
def parse_duration(duration_str):
    pattern = re.compile(r'P?(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?')
    match = pattern.match(duration_str)
    if not match:
        return 0
    gd = match.groupdict()
    days = int(gd.get('days') or 0)
    hours = int(gd.get('hours') or 0)
    minutes = int(gd.get('minutes') or 0)
    seconds = int(gd.get('seconds') or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds

# --- DIAGNOSTIC: verify Analytics API access BEFORE trusting its data ---
def check_analytics_access(youtube_analytics, channel_id):
    """
    The old script wrapped the whole analytics query in a try/except and
    silently fell back to all-zero values on ANY failure. That meant every
    video in the last report had average_view_percentage, shares,
    subscribers_gained, and estimated_minutes_watched hard-coded to 0 --
    not because performance was zero, but because the query never succeeded.

    This runs a minimal, isolated call first so failures are loud and
    specific instead of silently poisoning the whole dataset.
    """
    try:
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        probe = youtube_analytics.reports().query(
            ids=f"channel=={channel_id}",
            startDate="2005-01-01",
            endDate=end_date,
            metrics="views",
        ).execute()
        total_views = probe.get("rows", [[0]])[0][0] if probe.get("rows") else 0
        print(f"[OK] Analytics API reachable. Channel lifetime views per Analytics API: {total_views}")
        return True
    except HttpError as e:
        print("\n[CRITICAL] YouTube Analytics API call failed.")
        print(f"HTTP status: {e.resp.status if hasattr(e, 'resp') else 'unknown'}")
        print(f"Details: {e}")
        if e.resp is not None and e.resp.status in (401, 403):
            print(
                "\nThis looks like a SCOPE/PERMISSION problem, not a transient error.\n"
                "Your refresh token was likely issued without the analytics scope.\n"
                "Fix: re-run the OAuth consent flow and request BOTH scopes:\n"
                "  https://www.googleapis.com/auth/yt-analytics.readonly\n"
                "  https://www.googleapis.com/auth/youtube.readonly\n"
                "then replace YT_REFRESH_TOKEN in your .env with the new token.\n"
            )
        return False
    except Exception:
        print("\n[CRITICAL] Unexpected error calling the Analytics API:")
        traceback.print_exc()
        return False

# --- DATA ACQUISITION ---
def fetch_youtube_data():
    creds = get_credentials()
    youtube = build("youtube", "v3", credentials=creds)
    youtube_analytics = build("youtubeAnalytics", "v2", credentials=creds)

    print("Connecting to YouTube APIs...")
    channel_response = youtube.channels().list(mine=True, part="contentDetails,id").execute()
    if not channel_response.get("items"):
        raise RuntimeError("No channel found for the authorized Google Account.")

    channel_id = channel_response["items"][0]["id"]
    uploads_playlist_id = channel_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    print(f"Connected to Channel ID: {channel_id}")
    print(f"Uploads Playlist ID: {uploads_playlist_id}")

    analytics_ok = check_analytics_access(youtube_analytics, channel_id)

    print("Paginating through all uploads items...")
    playlist_items = []
    next_page_token = None
    while True:
        res = youtube.playlistItems().list(
            playlistId=uploads_playlist_id,
            part="snippet,contentDetails",
            maxResults=50,
            pageToken=next_page_token
        ).execute()
        playlist_items.extend(res.get("items", []))
        next_page_token = res.get("nextPageToken")
        if not next_page_token:
            break

    print(f"Found {len(playlist_items)} uploaded video items.")

    video_details = {}
    video_ids = [item["contentDetails"]["videoId"] for item in playlist_items]

    print("Retrieving metadata, statistics, and privacy status from Data API v3...")
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i:i+50]
        # Added "status" part -> gives us privacyStatus so we can tell what's
        # ACTUALLY public vs. private/unlisted, instead of guessing from titles.
        res = youtube.videos().list(
            id=",".join(batch_ids),
            part="snippet,contentDetails,statistics,status"
        ).execute()

        for video in res.get("items", []):
            vid_id = video["id"]
            snippet = video.get("snippet", {})
            content_details = video.get("contentDetails", {})
            statistics = video.get("statistics", {})
            status = video.get("status", {})

            title = snippet.get("title", "")
            tags = snippet.get("tags", [])

            video_details[vid_id] = {
                "id": vid_id,
                "title": title,
                "description": snippet.get("description", ""),
                "published_at": snippet.get("publishedAt", ""),
                "tags": tags,
                "duration_raw": content_details.get("duration", "PT0S"),
                "views": int(statistics.get("viewCount", 0)),
                "likes": int(statistics.get("likeCount", 0)),
                "comments": int(statistics.get("commentCount", 0)),
                "privacy_status": status.get("privacyStatus", "unknown"),
                # Flags a title still carrying pipeline placeholder text --
                # this should never reach a public upload.
                "is_flagged_draft": bool(re.search(r"\[DRAFT\]", title, re.IGNORECASE)),
                "has_no_tags": len(tags) == 0,
            }

    analytics_data = {}
    if analytics_ok:
        print("Retrieving per-video metrics from YouTube Analytics API v2...")
        try:
            end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            analytics_response = youtube_analytics.reports().query(
                ids=f"channel=={channel_id}",
                startDate="2005-01-01",
                endDate=end_date,
                metrics="views,likes,dislikes,shares,comments,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained",
                dimensions="video",
                maxResults=10000,  # explicit, generous cap so large channels don't get silently truncated
            ).execute()

            rows = analytics_response.get("rows", [])
            print(f"Retrieved analytics performance rows for {len(rows)} videos.")

            if len(rows) == 0 and len(video_details) > 0:
                print(
                    "[Warning] Analytics API call succeeded but returned 0 rows for "
                    f"{len(video_details)} known videos. Per-video retention/watch-time "
                    "data will be unavailable this run -- check date range and channel activity."
                )

            for row in rows:
                vid_id = row[0]
                analytics_data[vid_id] = {
                    "views_analytics": int(row[1] or 0),
                    "likes_analytics": int(row[2] or 0),
                    "dislikes": int(row[3] or 0),
                    "shares": int(row[4] or 0),
                    "comments_analytics": int(row[5] or 0),
                    "estimated_minutes_watched": float(row[6] or 0.0),
                    "average_view_duration_sec": int(row[7] or 0),
                    "average_view_percentage": float(row[8] or 0.0),
                    "subscribers_gained": int(row[9] or 0),
                }
        except Exception:
            print("\n[Warning] Analytics API query for per-video metrics failed after the initial probe succeeded:")
            traceback.print_exc()
            print("Falling back to Data API stats only for this run.\n")
    else:
        print("Skipping per-video Analytics API query since the access probe failed above.")
        print("Retention %, watch time, shares, and subscriber-gained fields will be recorded as 'unavailable', not silently zeroed.\n")

    print("Merging metadata and analytics datasets...")
    combined_records = []
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist_tz)

    for vid_id, details in video_details.items():
        an = analytics_data.get(vid_id)
        analytics_available = an is not None
        if an is None:
            an = {
                "views_analytics": 0, "likes_analytics": 0, "dislikes": 0, "shares": 0,
                "comments_analytics": 0, "estimated_minutes_watched": 0.0,
                "average_view_duration_sec": 0, "average_view_percentage": 0.0,
                "subscribers_gained": 0,
            }

        duration_sec = parse_duration(details["duration_raw"])
        pub_dt = datetime.fromisoformat(details["published_at"].replace("Z", "+00:00"))
        pub_ist = pub_dt.astimezone(ist_tz)
        days_since_publish = (now_ist - pub_ist).total_seconds() / 86400.0

        views = details["views"]
        likes = details["likes"]
        comments = details["comments"]

        likes_per_100_views = (likes / views * 100) if views > 0 else 0.0
        comments_per_100_views = (comments / views * 100) if views > 0 else 0.0
        shares_per_100_views = (an["shares"] / views * 100) if views > 0 and analytics_available else 0.0
        subs_per_100_views = (an["subscribers_gained"] / views * 100) if views > 0 and analytics_available else 0.0

        record = {
            "video_id": vid_id,
            "title": details["title"],
            "url": f"https://www.youtube.com/watch?v={vid_id}",
            "privacy_status": details["privacy_status"],
            "is_flagged_draft": int(details["is_flagged_draft"]),
            "has_no_tags": int(details["has_no_tags"]),
            "publish_time_ist": pub_ist.strftime("%Y-%m-%d %H:%M:%S"),
            "publish_date": pub_ist.strftime("%Y-%m-%d"),
            "publish_hour": pub_ist.hour,
            "publish_day": pub_ist.strftime("%A"),
            "days_since_publish": round(days_since_publish, 2),
            "is_mature": int(days_since_publish >= MATURITY_THRESHOLD_DAYS),
            "duration_sec": duration_sec,
            "is_short": int(duration_sec <= 60),
            "tags_count": len(details["tags"]),
            "tags": "; ".join(details["tags"]),
            "views": views,
            "likes": likes,
            "comments": comments,
            "analytics_available": int(analytics_available),
            "shares": an["shares"] if analytics_available else None,
            "subscribers_gained": an["subscribers_gained"] if analytics_available else None,
            "estimated_minutes_watched": round(an["estimated_minutes_watched"], 2) if analytics_available else None,
            "average_view_duration_sec": an["average_view_duration_sec"] if analytics_available else None,
            "average_view_percentage": round(an["average_view_percentage"], 2) if analytics_available else None,
            "likes_per_100_views": round(likes_per_100_views, 2),
            "comments_per_100_views": round(comments_per_100_views, 2),
            "shares_per_100_views": round(shares_per_100_views, 2) if analytics_available else None,
            "subscribers_per_100_views": round(subs_per_100_views, 2) if analytics_available else None,
        }
        combined_records.append(record)

    return combined_records

# --- EXPORT TO CSV ---
def save_to_csv(data):
    if not data:
        print("No records found to save.")
        return
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = list(data[0].keys())
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)
    print(f"Successfully exported {len(data)} video records to CSV: {CSV_PATH}")

# --- UPLOAD CADENCE / BURST DETECTION ---
def analyze_upload_cadence(data):
    """
    Detects same-day upload bursts (uploads published within BURST_GAP_MINUTES
    of each other). Dense bursts are a likely cause of suppressed reach --
    YouTube doesn't push several same-channel uploads to the same audience
    in a short window, and unusually dense publishing can read as spam-like.
    """
    sorted_data = sorted(data, key=lambda v: v["publish_time_ist"])
    bursts = []
    current_burst = []
    prev_dt = None

    for v in sorted_data:
        dt = datetime.strptime(v["publish_time_ist"], "%Y-%m-%d %H:%M:%S")
        if prev_dt is not None:
            gap_minutes = (dt - prev_dt).total_seconds() / 60.0
            if gap_minutes <= BURST_GAP_MINUTES:
                if not current_burst:
                    current_burst.append(sorted_data[sorted_data.index(v) - 1])
                current_burst.append(v)
            else:
                if len(current_burst) >= 3:
                    bursts.append(list(current_burst))
                current_burst = []
        prev_dt = dt
    if len(current_burst) >= 3:
        bursts.append(list(current_burst))

    return bursts

# --- ANALYSE DATA ---
def perform_analysis(data):
    def mean(vals):
        return sum(vals) / len(vals) if vals else 0.0

    def median(vals):
        if not vals:
            return 0.0
        sorted_vals = sorted(vals)
        n = len(sorted_vals)
        if n % 2 == 1:
            return sorted_vals[n // 2]
        return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0

    total_videos = len(data)
    shorts = [v for v in data if v["is_short"] == 1]
    longs = [v for v in data if v["is_short"] == 0]

    # Only use "mature" videos (past the maturity threshold) for performance
    # rankings/day/hour analysis, so brand-new uploads with 0 views by
    # necessity don't distort the picture.
    mature_data = [v for v in data if v["is_mature"] == 1]
    immature_data = [v for v in data if v["is_mature"] == 0]

    any_analytics = any(v["analytics_available"] for v in data)

    total_views = sum(v["views"] for v in data)
    total_likes = sum(v["likes"] for v in data)
    total_comments = sum(v["comments"] for v in data)

    report = []
    report.append("# YouTube Performance & Content Analysis Report")
    report.append(f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (IST/Kolkata Context)\n")

    report.append("## ⚠️ Data Health Check")
    if not any_analytics:
        report.append(
            "- **Analytics API data is UNAVAILABLE for this run.** Retention %, watch-time, "
            "shares, and subscriber-gained figures below are omitted rather than shown as a "
            "misleading 0. See console output for the specific auth/scope error."
        )
    else:
        missing = sum(1 for v in data if v["analytics_available"] == 0)
        if missing:
            report.append(f"- Analytics data missing for {missing}/{total_videos} videos (partial failure).")
        else:
            report.append("- Analytics API data retrieved successfully for all videos.")

    flagged_drafts = [v for v in data if v["is_flagged_draft"] == 1]
    if flagged_drafts:
        report.append(
            f"- 🚨 **{len(flagged_drafts)} videos are live/public with a leftover `[DRAFT]` title** "
            "(pipeline bug: these were meant to stay private). See 'Flagged Videos' section below."
        )
    no_tag_count = sum(1 for v in data if v["has_no_tags"] == 1)
    report.append(f"- {no_tag_count}/{total_videos} videos ({no_tag_count/total_videos*100:.1f}%) have zero tags.")
    report.append(f"- {len(immature_data)} videos are younger than {MATURITY_THRESHOLD_DAYS} days and excluded from performance rankings below (still too new to judge).")
    report.append("")

    report.append("## 📊 Channel Overview Stats")
    report.append(f"- **Total Videos Uploaded:** {total_videos}")
    report.append(f"  - **Shorts (<= 60s):** {len(shorts)} ({len(shorts)/total_videos*100:.1f}%)")
    report.append(f"  - **Long-form (> 60s):** {len(longs)} ({len(longs)/total_videos*100:.1f}%)")
    report.append(f"- **Total Views Accumulated:** {total_views:,}")
    report.append(f"- **Total Likes:** {total_likes:,}")
    report.append(f"- **Total Comments:** {total_comments:,}")
    if any_analytics:
        total_watch = sum(v["estimated_minutes_watched"] or 0 for v in data if v["analytics_available"])
        total_subs = sum(v["subscribers_gained"] or 0 for v in data if v["analytics_available"])
        report.append(f"- **Total Subscribers Gained:** {total_subs:,}")
        report.append(f"- **Total Estimated Minutes Watched:** {total_watch:,.2f} mins ({total_watch/60:,.1f} hours)")
    report.append("")

    # Shorts vs Long-form (mature only)
    report.append("## 🎥 Format Head-to-Head (Shorts vs Long-form, mature videos only)")
    report.append("| Format | Count | Average Views | Median Views | Avg Like Rate % | Avg Comment Rate % |")
    report.append("| --- | --- | --- | --- | --- | --- |")
    for label, group in [("Shorts (<=60s)", [v for v in mature_data if v["is_short"] == 1]),
                          ("Long-form (>60s)", [v for v in mature_data if v["is_short"] == 0])]:
        if group:
            avg_v = mean([v["views"] for v in group])
            med_v = median([v["views"] for v in group])
            avg_lr = mean([v["likes_per_100_views"] for v in group])
            avg_cr = mean([v["comments_per_100_views"] for v in group])
            report.append(f"| {label} | {len(group)} | {avg_v:.1f} | {med_v:.2f} | {avg_lr:.2f}% | {avg_cr:.2f}% |")
        else:
            report.append(f"| {label} | 0 | 0.0 | 0.0 | 0.00% | 0.00% |")
    report.append("")

    # Tag impact
    report.append("## 🏷️ Tag Presence Impact (mature videos only)")
    report.append("| Group | Count | Avg Views |")
    report.append("| --- | --- | --- |")
    tagged = [v for v in mature_data if v["has_no_tags"] == 0]
    untagged = [v for v in mature_data if v["has_no_tags"] == 1]
    if tagged:
        report.append(f"| Has tags | {len(tagged)} | {mean([v['views'] for v in tagged]):.1f} |")
    if untagged:
        report.append(f"| No tags | {len(untagged)} | {mean([v['views'] for v in untagged]):.1f} |")
    report.append("")

    # Upload Day
    report.append("## 📅 Upload Day Performance (IST, mature videos only)")
    report.append("| Upload Day | Video Count | Total Views | Average Views |")
    report.append("| --- | --- | --- | --- |")
    day_groups = {}
    for v in mature_data:
        day_groups.setdefault(v["publish_day"], []).append(v)
    days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_metrics = []
    for day in days_order:
        vids = day_groups.get(day, [])
        if vids:
            day_views = sum(v["views"] for v in vids)
            avg_views = day_views / len(vids)
            day_metrics.append((day, len(vids), day_views, avg_views))
            report.append(f"| {day} | {len(vids)} | {day_views:,} | {avg_views:.1f} |")
    if day_metrics:
        best_day = max(day_metrics, key=lambda x: x[3])
        report.append(f"\n💡 **Golden Day Insight:** **{best_day[0]}** leads with an average of **{best_day[3]:.1f}** views/video.\n")

    # Upload Hour
    report.append("## ⏰ Upload Hour Analysis (IST, mature videos only)")
    report.append("| Hour Slot (IST) | Count | Average Views |")
    report.append("| --- | --- | --- |")
    hour_groups = {}
    for v in mature_data:
        hour_groups.setdefault(v["publish_hour"], []).append(v)
    hour_metrics = []
    for hour in range(24):
        vids = hour_groups.get(hour, [])
        if vids:
            avg_views = sum(v["views"] for v in vids) / len(vids)
            hour_metrics.append((hour, len(vids), avg_views))
            report.append(f"| {hour:02d}:00 | {len(vids)} | {avg_views:.1f} |")
    if hour_metrics:
        # require a minimum sample size so a single lucky video doesn't crown an hour
        reliable = [h for h in hour_metrics if h[1] >= 5] or hour_metrics
        best_hour = max(reliable, key=lambda x: x[2])
        report.append(f"\n💡 **Golden Hour Insight:** **{best_hour[0]:02d}:00 IST** (from slots with >=5 uploads) averages **{best_hour[2]:.1f}** views.\n")

    # Upload cadence / burst detection
    report.append("## 🚨 Upload Cadence & Burst Risk")
    bursts = analyze_upload_cadence(data)
    if bursts:
        report.append(
            f"Detected {len(bursts)} burst window(s) where 3+ videos were published within "
            f"{BURST_GAP_MINUTES} minutes of each other. Rapid same-channel bursts are a likely "
            "cause of suppressed algorithmic reach -- consider spacing daily uploads out."
        )
        for b in bursts[:10]:
            start = b[0]["publish_time_ist"]
            end = b[-1]["publish_time_ist"]
            report.append(f"- {start} → {end}: {len(b)} videos published back-to-back")
    else:
        report.append("No burst windows detected.")
    report.append("")

    # Flagged videos
    if flagged_drafts:
        report.append("## 🏴 Flagged Videos (leftover [DRAFT] titles, action needed)")
        report.append("| Title | Privacy Status | Tags | Views |")
        report.append("| --- | --- | --- | --- |")
        for v in flagged_drafts:
            report.append(f"| {v['title']} | {v['privacy_status']} | {v['tags_count']} | {v['views']} |")
        report.append("")

    # Leaderboard (mature only)
    report.append("## 🏆 Top 10 Videos by View Count (mature videos only)")
    report.append("| Video Title | Type | Views | Like Ratio |")
    report.append("| --- | --- | --- | --- |")
    views_sorted = sorted(mature_data, key=lambda x: x["views"], reverse=True)[:10]
    for v in views_sorted:
        vtype = "Short" if v["is_short"] == 1 else "Long"
        report.append(f"| [{v['title']}]({v['url']}) | {vtype} | {v['views']:,} | {v['likes_per_100_views']:.2f}% |")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    print(f"Successfully generated analysis report: {REPORT_PATH}")

# --- MAIN ---
def main():
    load_env()
    try:
        data = fetch_youtube_data()
        save_to_csv(data)
        perform_analysis(data)
    except Exception:
        print("\n[Fatal Error] Application failed:")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()