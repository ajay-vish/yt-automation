"""
Run this ONCE, on your own computer (not in GitHub Actions), to get a refresh
token that lets the pipeline upload videos without you logging in again.

Steps before running:
  1. Go to https://console.cloud.google.com/ -> create a project (free).
  2. Enable "YouTube Data API v3" for that project.
  3. Go to "Credentials" -> Create Credentials -> OAuth client ID
     -> Application type: Desktop app. Download the JSON, save it here as
     client_secret.json (same folder as this script).
  4. Run: python setup_oauth.py
  5. A browser window opens -- log in with the Google account that owns
     the manjuvishwakarmalokgeet YouTube channel, and approve access.
  6. This prints your CLIENT_ID, CLIENT_SECRET and REFRESH_TOKEN.
     Save all three as GitHub repo secrets:
       YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
     (Repo -> Settings -> Secrets and variables -> Actions -> New secret)

You only ever need to do this once. The refresh token doesn't expire unless
you revoke access.
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def main():
    flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
    creds = flow.run_local_server(port=0)
    print("\n--- Save these as GitHub Actions secrets ---")
    print("YT_CLIENT_ID     =", creds.client_id)
    print("YT_CLIENT_SECRET =", creds.client_secret)
    print("YT_REFRESH_TOKEN =", creds.refresh_token)

if __name__ == "__main__":
    main()
