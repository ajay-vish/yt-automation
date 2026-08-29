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
MATURITY_THRESHOLD_DAYS = 3

# If uploads on the same day are less than this many minutes apart, flag them
# as a burst -- YouTube tends to suppress distribution for rapid-fire same-channel uploads.
BURST_GAP_MINUTES = 45

# How many of a video's top traffic sources to keep in the readable "top_sources" column.
TOP_N_TRAFFIC_SOURCES = 3

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

# --- PER-VIDEO METRICS (BATCHED QUERY FIX) ---
def fetch_per_video_metrics(youtube_analytics, channel_id, video_ids):
    print("Retrieving per-video metrics from YouTube Analytics API v2...")
    analytics_data = {}
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # Query in batches of 50 video IDs using explicit video filter
    batch_size = 50
    for i in range(0, len(video_ids), batch_size):
        batch = video_ids[i:i + batch_size]
        filter_str = f"video=={','.join(batch)}"
        try:
            response = youtube_analytics.reports().query(
                ids=f"channel=={channel_id}",
                startDate="2005-01-01",
                endDate=end_date,
                metrics="views,likes,shares,comments,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained",
                dimensions="video",
                filters=filter_str,
                maxResults=500,
            ).execute()
            
            rows = response.get("rows", [])
            for row in rows:
                vid_id = row[0]
                analytics_data[vid_id] = {
                    "views_analytics": int(row[1] or 0),
                    "likes_analytics": int(row[2] or 0),
                    "shares": int(row[3] or 0),
                    "comments_analytics": int(row[4] or 0),
                    "estimated_minutes_watched": float(row[5] or 0.0),
                    "average_view_duration_sec": int(row[6] or 0),
                    "average_view_percentage": float(row[7] or 0.0),
                    "subscribers_gained": int(row[8] or 0),
                }
        except Exception:
            print(f"\n[Warning] Metrics query failed for batch starting at index {i}:")
            traceback.print_exc()
            
    print(f"Retrieved analytics performance rows for {len(analytics_data)} videos.")
    return analytics_data

# --- PER-VIDEO TRAFFIC SOURCE BREAKDOWN ---
def fetch_traffic_source_breakdown(youtube_analytics, channel_id, video_ids):
    print("Retrieving per-video traffic source breakdown from YouTube Analytics API v2...")
    breakdown = {}
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # Smaller batch size (20) reduces API backend internal errors on 2D queries
    batch_size = 20
    
    for i in range(0, len(video_ids), batch_size):
        batch = video_ids[i:i + batch_size]
        filter_str = f"video=={','.join(batch)}"
        
        # Retry loop for transient 500/503 backend errors
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = youtube_analytics.reports().query(
                    ids=f"channel=={channel_id}",
                    startDate="2005-01-01",
                    endDate=end_date,
                    metrics="views",
                    dimensions="video,insightTrafficSourceType",
                    filters=filter_str,
                    maxResults=10000,
                ).execute()
                
                rows = response.get("rows", [])
                for row in rows:
                    vid_id, source_type, views = row[0], row[1], int(row[2] or 0)
                    breakdown.setdefault(vid_id, {})[source_type] = views
                break  # Success: exit retry loop
                
            except HttpError as e:
                # Catch 500 / 503 Internal Server Errors and retry
                if e.resp.status in (500, 503) and attempt < max_retries:
                    sleep_time = attempt * 2
                    print(f"  [Retry {attempt}/{max_retries}] Transient error on batch starting index {i}. Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                else:
                    print(f"\n[Warning] Traffic source query failed for batch starting at index {i}:")
                    traceback.print_exc()
                    break
            except Exception:
                print(f"\n[Warning] Unexpected error on batch starting at index {i}:")
                traceback.print_exc()
                break
            
    print(f"Retrieved traffic source data for {len(breakdown)} videos.")
    return breakdown

# --- DATA ACQUISITION ---
def fetch_youtube_data():
    load_env()
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
                "is_flagged_draft": bool(re.search(r"\[DRAFT\]", title, re.IGNORECASE)),
                "has_no_tags": len(tags) == 0,
            }

    analytics_data = {}
    traffic_source_data = {}
    if analytics_ok:
        analytics_data = fetch_per_video_metrics(youtube_analytics, channel_id, video_ids)
        traffic_source_data = fetch_traffic_source_breakdown(youtube_analytics, channel_id, video_ids)
    else:
        print("Skipping per-video Analytics API queries since the access probe failed above.\n")

    print("Merging metadata, analytics, and traffic source datasets...")
    combined_records = []
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist_tz)

    for vid_id, details in video_details.items():
        an = analytics_data.get(vid_id)
        analytics_available = an is not None
        if an is None:
            an = {
                "views_analytics": 0, "likes_analytics": 0, "shares": 0,
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

        sources = traffic_source_data.get(vid_id, {})
        traffic_available = len(sources) > 0
        total_source_views = sum(sources.values()) if sources else 0

        def source_pct(*names):
            if not traffic_available or total_source_views == 0:
                return None
            v = sum(sources.get(n, 0) for n in names)
            return round(v / total_source_views * 100, 2)

        pct_shorts_feed = source_pct("SHORTS")
        pct_subscriber = source_pct("SUBSCRIBER")
        pct_notification = source_pct("NOTIFICATION")
        pct_suggested = source_pct("RELATED_VIDEO", "SUGGESTED_VIDEO")
        pct_search = source_pct("YT_SEARCH", "SEARCH")
        pct_external = source_pct("EXT_URL")

        top_sources_str = ""
        if traffic_available:
            top_sorted = sorted(sources.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N_TRAFFIC_SOURCES]
            top_sources_str = "; ".join(f"{name}:{v}" for name, v in top_sorted)

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
            "traffic_available": int(traffic_available),
            "pct_traffic_shorts_feed": pct_shorts_feed,
            "pct_traffic_subscriber": pct_subscriber,
            "pct_traffic_notification": pct_notification,
            "pct_traffic_suggested": pct_suggested,
            "pct_traffic_search": pct_search,
            "pct_traffic_external": pct_external,
            "top_traffic_sources": top_sources_str,
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

    mature_data = [v for v in data if v["is_mature"] == 1]
    immature_data = [v for v in data if v["is_mature"] == 0]

    any_analytics = any(v["analytics_available"] for v in data)
    any_traffic = any(v["traffic_available"] for v in data)

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
            "shares, subscriber-gained, and traffic source figures below are omitted rather "
            "than shown as a misleading 0."
        )
    else:
        missing = sum(1 for v in data if v["analytics_available"] == 0)
        if missing:
            report.append(f"- Analytics data missing for {missing}/{total_videos} videos (partial failure).")
        else:
            report.append("- Analytics API data retrieved successfully for all videos.")
        if not any_traffic:
            report.append("- ⚠️ Traffic source breakdown unavailable this run.")

    flagged_drafts = [v for v in data if v["is_flagged_draft"] == 1]
    if flagged_drafts:
        report.append(
            f"- 🚨 **{len(flagged_drafts)} videos are live/public with a leftover `[DRAFT]` title**."
        )
    no_tag_count = sum(1 for v in data if v["has_no_tags"] == 1)
    report.append(f"- {no_tag_count}/{total_videos} videos ({no_tag_count/total_videos*100:.1f}%) have zero tags.")
    report.append(f"- {len(immature_data)} videos are younger than {MATURITY_THRESHOLD_DAYS} days and excluded from performance rankings.")
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

    # Format Performance
    report.append("## 🎥 Format Head-to-Head (Shorts vs Long-form, mature videos only)")
    report.append("| Format | Count | Average Views | Median Views | Avg Like Rate % | Avg Comment Rate % | Avg Retention % |")
    report.append("| --- | --- | --- | --- | --- | --- | --- |")
    for label, group in [("Shorts (<=60s)", [v for v in mature_data if v["is_short"] == 1]),
                          ("Long-form (>60s)", [v for v in mature_data if v["is_short"] == 0])]:
        if group:
            avg_v = mean([v["views"] for v in group])
            med_v = median([v["views"] for v in group])
            avg_lr = mean([v["likes_per_100_views"] for v in group])
            avg_cr = mean([v["comments_per_100_views"] for v in group])
            ret_vals = [v["average_view_percentage"] for v in group if v["average_view_percentage"] is not None]
            avg_ret = mean(ret_vals) if ret_vals else None
            ret_str = f"{avg_ret:.1f}%" if avg_ret is not None else "N/A"
            report.append(f"| {label} | {len(group)} | {avg_v:.1f} | {med_v:.2f} | {avg_lr:.2f}% | {avg_cr:.2f}% | {ret_str} |")
        else:
            report.append(f"| {label} | 0 | 0.0 | 0.0 | 0.00% | 0.00% | N/A |")
    report.append("")

    # Traffic Sources
    if any_traffic:
        report.append("## 🚦 Traffic Source Breakdown (Mature Shorts)")
        mature_shorts = [v for v in mature_data if v["is_short"] == 1 and v["traffic_available"] == 1]
        if mature_shorts:
            sorted_by_views = sorted(mature_shorts, key=lambda v: v["views"])
            n = len(sorted_by_views)
            bottom_third = sorted_by_views[: max(1, n // 3)]
            top_third = sorted_by_views[-max(1, n // 3):]

            def avg_pct(group, field):
                vals = [v[field] for v in group if v[field] is not None]
                return mean(vals) if vals else None

            report.append("| Group | Count | Avg Views | Avg % Shorts Feed | Avg % Subscriber | Avg % Notification | Avg % Suggested | Avg % Search |")
            report.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
            for label, group in [("Bottom 1/3 by views", bottom_third), ("Top 1/3 by views", top_third)]:
                avg_v = mean([v["views"] for v in group])
                shorts_pct = avg_pct(group, "pct_traffic_shorts_feed")
                sub_pct = avg_pct(group, "pct_traffic_subscriber")
                notif_pct = avg_pct(group, "pct_traffic_notification")
                sugg_pct = avg_pct(group, "pct_traffic_suggested")
                search_pct = avg_pct(group, "pct_traffic_search")
                fmt = lambda x: f"{x:.1f}%" if x is not None else "N/A"
                report.append(f"| {label} | {len(group)} | {avg_v:.1f} | {fmt(shorts_pct)} | {fmt(sub_pct)} | {fmt(notif_pct)} | {fmt(sugg_pct)} | {fmt(search_pct)} |")
            report.append("")

    # Day of Week Performance
    report.append("## 📅 Upload Day Performance (IST, mature videos only)")
    report.append("| Upload Day | Video Count | Total Views | Average Views |")
    report.append("| --- | --- | --- | --- |")
    day_groups = {}
    for v in mature_data:
        day_groups.setdefault(v["publish_day"], []).append(v)
    
    days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for day in days_order:
        group = day_groups.get(day, [])
        if group:
            tot_v = sum(v["views"] for v in group)
            avg_v = mean([v["views"] for v in group])
            report.append(f"| {day} | {len(group)} | {tot_v:,} | {avg_v:.1f} |")
        else:
            report.append(f"| {day} | 0 | 0 | 0.0 |")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    print(f"Successfully generated analysis report: {REPORT_PATH}")

# --- MAIN RUNNER ---
def main():
    try:
        data = fetch_youtube_data()
        save_to_csv(data)
        perform_analysis(data)
    except Exception:
        print("\n[CRITICAL ERROR] Script execution failed:")
        traceback.print_exc()

if __name__ == "__main__":
    main()