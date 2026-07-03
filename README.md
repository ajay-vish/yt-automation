# Bhojpuri Folk Song -> Daily YouTube Short (free, automated)

Every day, GitHub Actions runs a script that:
1. Picks a random video from your channel that hasn't been used yet.
2. Cuts the most energetic/musical ~35s window (usually the chorus).
3. Reframes it to 9:16 with a blurred-background fill (no stretching).
4. Burns in captions (auto-transcribed locally, free).
5. Uploads it to your channel as **Private**.
6. Writes a ready-made prompt into `drafts/<video_id>_prompt.txt` containing
   the transcript, so you can paste it into Claude free tier chat whenever
   you have time and get back a title, description and tags.
7. You paste those into YouTube Studio and flip the video to Public.

Nothing here costs money: yt-dlp, ffmpeg, librosa and faster-whisper all run
locally on GitHub's free Actions runners (2,000 free minutes/month on
private repos, unlimited on public repos -- one run takes roughly 5-15
minutes depending on video length).

## One-time setup (~15 minutes)

### 1. Create the repo
Push this folder to a new GitHub repo (public repo = unlimited free Actions
minutes; private repo = 2,000 free minutes/month, still plenty for 1 run/day).

### 2. Get YouTube upload credentials
1. Go to console.cloud.google.com -> create a project.
2. Enable "YouTube Data API v3" (APIs & Services -> Library).
3. Configure the OAuth consent screen (External, add your own Google
   account as a test user -- you don't need to publish the app).
4. Credentials -> Create Credentials -> OAuth client ID -> Desktop app.
   Download the JSON as `scripts/client_secret.json`.
5. On your own computer (not in Actions), run:
   ```
   pip install google-auth-oauthlib
   python scripts/setup_oauth.py
   ```
   Log in with the Google account that owns the
   manjuvishwakarmalokgeet channel. It will print a client ID, client
   secret, and refresh token.
6. In your GitHub repo: Settings -> Secrets and variables -> Actions ->
   New repository secret. Add all three:
   - `YT_CLIENT_ID`
   - `YT_CLIENT_SECRET`
   - `YT_REFRESH_TOKEN`

### 3. Done
The workflow in `.github/workflows/daily_short.yml` runs automatically once
a day. You can also trigger it manually anytime from the repo's "Actions"
tab -> "Daily Bhojpuri Short" -> "Run workflow".

## Your daily 2-minute task
1. Open `drafts/` in the repo, find the newest `*_prompt.txt`.
2. Paste its contents into a fresh Claude free tier chat.
3. Copy the title/description/tags it gives you into YouTube Studio for
   that (private) video.
4. Set the video to Public.

## Notes
- The quota cost of one upload is 1,600 units/day against a 10,000/day
  free quota -- one short a day is nowhere close to the limit.
- `state.json` tracks which source videos have been used so it won't
  repeat a video until every video on the channel has had a turn.
- If a clip ever looks wrong (e.g. lands on silence/talking), it's still
  private -- just delete it from YouTube Studio, no harm done.
- Caption quality depends on faster-whisper's Hindi/Bhojpuri transcription,
  which won't be perfect -- it's there to make the Short watchable muted,
  not to be publication-perfect lyrics.
