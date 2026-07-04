"""
Run this ONCE, on your own computer (not in GitHub Actions), to get a refresh
token that lets the pipeline upload, read analytics, and update existing
videos -- without you logging in again.

Steps before running:
  1. Reuse the same client_secret.json you already have from last time
     (same folder as this script). No need to create a new project/client.
  2. Make sure "YouTube Analytics API" is also enabled for that project
     at https://console.cloud.google.com/ -> APIs & Services -> Library
     (YouTube Data API v3 should already be enabled from last time).
  3. Run: python setup_oauth.py
  4. A browser window opens -- log in with the Google account that owns
     the manjuvishwakarmalokgeet YouTube channel, and approve access.
  5. This prints your CLIENT_ID, CLIENT_SECRET and REFRESH_TOKEN.
     Replace the existing GitHub repo secrets with these new values:
       YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
     (Repo -> Settings -> Secrets and variables -> Actions)

This new token fully replaces the old one -- it still covers uploading
(the "youtube" scope includes upload), plus analytics and updates.
You only need to do this once. The refresh token doesn't expire unless
you revoke access.
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube",               # upload, read, update videos
    "https://www.googleapis.com/auth/yt-analytics.readonly",  # views, watch time, country, traffic source
]

def main():
    flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
    creds = flow.run_local_server(port=0)
    print("\n--- Save these as GitHub Actions secrets ---")
    print("YT_CLIENT_ID     =", creds.client_id)
    print("YT_CLIENT_SECRET =", creds.client_secret)
    print("YT_REFRESH_TOKEN =", creds.refresh_token)

if __name__ == "__main__":
    main()