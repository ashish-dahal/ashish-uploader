# Ashish Uploader

A personal tool for posting my own videos to TikTok through the official Login Kit and Content Posting API. No third-party services.

Site (used for the TikTok app's website, Terms, Privacy and OAuth callback): https://ashish-dahal.github.io/ashish-uploader/

## Setup

```bash
cd uploader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in client key and secret from the developer portal
python tiktok_uploader.py auth
```

`auth` opens TikTok's consent screen. After approving, the callback page shows a URL; paste it back into the terminal. Tokens are saved to `~/.config/ashish-uploader/token.json` and refresh automatically (the refresh token lasts about a year).

## Posting

```bash
python tiktok_uploader.py whoami                       # account + creator settings
python tiktok_uploader.py post clip.mp4 --caption "my caption #tag"
python tiktok_uploader.py post clip.mp4 --mode inbox   # send as a draft to finish in the app
```

## Automating 2 to 3 posts a day

Drop videos into the queue folder, each with an optional caption file of the same name (`clip.mp4` + `clip.txt`). Each run posts the oldest one and moves it to `queue/posted/` (or `queue/failed/`), logging to `queue/log.jsonl`.

```cron
# 09:07, 14:07, 19:07 Helsinki time
CRON_TZ=Europe/Helsinki
7 9,14,19 * * * cd ~/ashish-uploader/uploader && .venv/bin/python tiktok_uploader.py queue -y >> queue/cron.log 2>&1
```

## Audit status

Until the app passes TikTok's Content Posting API audit, direct posts are forced to `SELF_ONLY` and the account must be private when posting. Use `--mode inbox` meanwhile if you want posts to go public: the draft lands in your TikTok inbox and you tap publish.
