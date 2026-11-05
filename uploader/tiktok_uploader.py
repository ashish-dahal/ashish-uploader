#!/usr/bin/env python3
"""
Ashish Uploader: post your own videos to TikTok via the official Content Posting API.

Commands
  auth                 One-time sign-in (opens TikTok consent, you paste back the redirect URL)
  whoami               Show the connected account and its current creator settings
  post FILE [opts]     Post one video (caption from --caption or FILE.txt next to it)
  queue [opts]         Post the oldest video in the queue folder, then move it to posted/
  status PUBLISH_ID    Check the processing status of an earlier post

Config comes from uploader/.env (see .env.example). Tokens are stored in
~/.config/ashish-uploader/token.json with 0600 permissions.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

import requests

API = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.publish,video.upload"

HERE = Path(__file__).resolve().parent
TOKEN_FILE = Path(os.environ.get("TT_TOKEN_FILE", Path.home() / ".config/ashish-uploader/token.json"))

MIN_CHUNK = 5 * 1024 * 1024        # TikTok: chunks must be >= 5 MB (except a single-chunk upload)
CHUNK = 10 * 1024 * 1024           # we use 10 MB chunks; the last chunk absorbs the remainder
VIDEO_EXTS = {".mp4", ".mov", ".webm"}


# ---------------------------------------------------------------- config

def load_env() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cfg(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing {name}. Set it in {HERE / '.env'} (copy .env.example).")
    return val


# ---------------------------------------------------------------- tokens

def save_token(tok: dict) -> None:
    tok["obtained_at"] = int(time.time())
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tok, indent=2))
    os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)


def load_token() -> dict:
    if not TOKEN_FILE.exists():
        sys.exit("Not signed in yet. Run:  python tiktok_uploader.py auth")
    return json.loads(TOKEN_FILE.read_text())


def token_request(data: dict) -> dict:
    r = requests.post(f"{API}/v2/oauth/token/", data=data,
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    body = r.json()
    if r.status_code != 200 or "access_token" not in body:
        sys.exit(f"Token request failed ({r.status_code}): {body}")
    return body


def access_token() -> str:
    """Return a valid access token, refreshing it if it is about to expire."""
    tok = load_token()
    age = time.time() - tok.get("obtained_at", 0)
    if age < tok.get("expires_in", 0) - 300:
        return tok["access_token"]
    if age > tok.get("refresh_expires_in", 0) - 60:
        sys.exit("Refresh token expired. Run auth again.")
    new = token_request({
        "client_key": cfg("TIKTOK_CLIENT_KEY"),
        "client_secret": cfg("TIKTOK_CLIENT_SECRET"),
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
    })
    save_token(new)
    return new["access_token"]


# ---------------------------------------------------------------- API helpers

def api_post(path: str, payload: dict | None = None) -> dict:
    r = requests.post(f"{API}{path}", json=payload or {}, timeout=60, headers={
        "Authorization": f"Bearer {access_token()}",
        "Content-Type": "application/json; charset=UTF-8",
    })
    body = r.json()
    err = body.get("error", {})
    if r.status_code != 200 or err.get("code") not in (None, "ok"):
        sys.exit(f"API error on {path} ({r.status_code}): {err.get('code')}: {err.get('message')}"
                 f"  [log_id={err.get('log_id')}]")
    return body.get("data", {})


def creator_info() -> dict:
    return api_post("/v2/post/publish/creator_info/query/")


# ---------------------------------------------------------------- commands

def cmd_auth(_args) -> None:
    client_key = cfg("TIKTOK_CLIENT_KEY")
    redirect = cfg("TIKTOK_REDIRECT_URI")
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": client_key, "scope": SCOPES, "response_type": "code",
        "redirect_uri": redirect, "state": state,
    })
    print("Opening TikTok sign-in. If it doesn't open, visit:\n\n  " + url + "\n")
    webbrowser.open(url)
    pasted = input("After approving, paste the full URL shown on the callback page:\n> ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if q.get("state", [None])[0] != state:
        sys.exit("State mismatch. Start auth again and paste the URL from this attempt.")
    if "code" not in q:
        sys.exit(f"No code in that URL: {q.get('error', ['?'])[0]}")
    tok = token_request({
        "client_key": client_key,
        "client_secret": cfg("TIKTOK_CLIENT_SECRET"),
        "code": q["code"][0],
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
    })
    if not set(SCOPES.split(",")) <= set(tok.get("scope", "").split(",")):
        print(f"Warning: granted scopes are {tok.get('scope')}; posting may fail.")
    save_token(tok)
    print(f"Signed in. Token saved to {TOKEN_FILE}")
    cmd_whoami(None)


def cmd_whoami(_args) -> None:
    info = creator_info()
    print(json.dumps(info, indent=2, ensure_ascii=False))


def find_caption(video: Path, explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    side = video.with_suffix(".txt")
    return side.read_text(encoding="utf-8").strip() if side.exists() else ""


def upload_chunks(upload_url: str, video: Path, size: int, chunk: int, count: int) -> None:
    mime = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}[video.suffix.lower()]
    with video.open("rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            data = f.read(end - start + 1)
            for attempt in range(3):
                r = requests.put(upload_url, data=data, timeout=300, headers={
                    "Content-Type": mime,
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                })
                if r.status_code in (200, 201, 206):
                    break
                time.sleep(2 * (attempt + 1))
            else:
                sys.exit(f"Chunk {i + 1}/{count} failed: {r.status_code} {r.text[:300]}")
            print(f"  uploaded chunk {i + 1}/{count}")


def wait_status(publish_id: str, timeout_s: int = 600) -> dict:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        st = api_post("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
        if st.get("status") != last:
            last = st.get("status")
            print(f"  status: {last}")
        if last in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"):
            return st
        time.sleep(5)
    return {"status": "TIMEOUT"}


def post_video(video: Path, caption: str, mode: str, privacy: str | None,
               no_comment: bool, no_duet: bool, no_stitch: bool, aigc: bool, yes: bool) -> dict:
    if video.suffix.lower() not in VIDEO_EXTS:
        sys.exit(f"Unsupported file type: {video.suffix}")
    size = video.stat().st_size
    if size < MIN_CHUNK:
        chunk, count = size, 1
    else:
        chunk = CHUNK
        count = max(1, size // chunk)

    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}

    if mode == "inbox":
        print(f"\nSending to your TikTok inbox as a draft:\n  file: {video.name} ({size / 1e6:.1f} MB)")
        if not yes and input("Proceed? [y/N] ").lower() != "y":
            sys.exit("Cancelled.")
        data = api_post("/v2/post/publish/inbox/video/init/", {"source_info": source})
    else:
        info = creator_info()
        options = info.get("privacy_level_options", [])
        privacy = privacy or ("PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else "SELF_ONLY")
        if privacy not in options:
            sys.exit(f"Privacy {privacy} not allowed for this account now. Options: {options}")
        post_info = {
            "title": caption[:2200],
            "privacy_level": privacy,
            # The creator's own TikTok settings win: if they have turned something off, keep it off.
            "disable_comment": no_comment or info.get("comment_disabled", False),
            "disable_duet": no_duet or info.get("duet_disabled", False),
            "disable_stitch": no_stitch or info.get("stitch_disabled", False),
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "is_aigc": aigc,
        }
        print(f"\nPosting to @{info.get('creator_username')} ({info.get('creator_nickname')}):")
        print(f"  file:     {video.name} ({size / 1e6:.1f} MB)")
        print(f"  caption:  {caption or '(none)'}")
        print(f"  privacy:  {privacy}")
        print(f"  comments: {'off' if post_info['disable_comment'] else 'on'}, "
              f"duet: {'off' if post_info['disable_duet'] else 'on'}, "
              f"stitch: {'off' if post_info['disable_stitch'] else 'on'}")
        print(f"  max length for this account: {info.get('max_video_post_duration_sec')} s")
        print("  By posting, you agree to TikTok's Music Usage Confirmation.")
        if not yes and input("Post now? [y/N] ").lower() != "y":
            sys.exit("Cancelled.")
        data = api_post("/v2/post/publish/video/init/", {"post_info": post_info, "source_info": source})

    publish_id = data["publish_id"]
    print(f"  publish_id: {publish_id}")
    upload_chunks(data["upload_url"], video, size, chunk, count)
    result = wait_status(publish_id)
    if result.get("status") == "FAILED":
        print(f"  failed: {result.get('fail_reason')}")
    elif result.get("status") == "SEND_TO_USER_INBOX":
        print("  Draft is in your TikTok inbox. Open the app to finish posting (within 24 h).")
    elif result.get("status") == "PUBLISH_COMPLETE":
        ids = result.get("publicaly_available_post_id") or result.get("publicly_available_post_id")
        print(f"  Posted. {('Post id: ' + str(ids)) if ids else ''}")
    result["publish_id"] = publish_id
    return result


def cmd_post(a) -> None:
    video = Path(a.file).expanduser().resolve()
    if not video.exists():
        sys.exit(f"No such file: {video}")
    post_video(video, find_caption(video, a.caption), a.mode, a.privacy,
               a.no_comment, a.no_duet, a.no_stitch, a.aigc, a.yes)


def cmd_queue(a) -> None:
    qdir = Path(a.dir or os.environ.get("TT_QUEUE_DIR", HERE / "queue")).expanduser()
    done = qdir / "posted"
    failed = qdir / "failed"
    videos = sorted((p for p in qdir.iterdir() if p.suffix.lower() in VIDEO_EXTS),
                    key=lambda p: p.stat().st_mtime) if qdir.exists() else []
    if not videos:
        print(f"Queue empty: {qdir}")
        return
    video = videos[0]
    result = post_video(video, find_caption(video, None), a.mode, a.privacy,
                        False, False, False, a.aigc, a.yes)
    target = failed if result.get("status") in ("FAILED", "TIMEOUT") else done
    target.mkdir(parents=True, exist_ok=True)
    for f in (video, video.with_suffix(".txt")):
        if f.exists():
            shutil.move(str(f), target / f.name)
    with (qdir / "log.jsonl").open("a") as log:
        log.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                              "file": video.name, **result}) + "\n")
    print(f"  moved to {target}. {len(videos) - 1} left in queue.")


def cmd_status(a) -> None:
    print(json.dumps(api_post("/v2/post/publish/status/fetch/", {"publish_id": a.publish_id}), indent=2))


def main() -> None:
    load_env()
    p = argparse.ArgumentParser(description="Post your own videos to TikTok via the Content Posting API.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth").set_defaults(fn=cmd_auth)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)

    def common(sp):
        sp.add_argument("--mode", choices=["direct", "inbox"], default=os.environ.get("TT_MODE", "direct"),
                        help="direct = publish to profile, inbox = draft you finish in the app")
        sp.add_argument("--privacy", choices=["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS",
                                              "FOLLOWER_OF_CREATOR", "SELF_ONLY"],
                        default=os.environ.get("TT_PRIVACY") or None,
                        help="default: public if allowed, else SELF_ONLY (unaudited apps)")
        sp.add_argument("--aigc", action="store_true", help="label the video as AI-generated")
        sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt (for cron)")

    sp = sub.add_parser("post")
    sp.add_argument("file")
    sp.add_argument("--caption")
    sp.add_argument("--no-comment", action="store_true")
    sp.add_argument("--no-duet", action="store_true")
    sp.add_argument("--no-stitch", action="store_true")
    common(sp)
    sp.set_defaults(fn=cmd_post)

    sq = sub.add_parser("queue")
    sq.add_argument("--dir", help="queue folder (default: TT_QUEUE_DIR or uploader/queue)")
    common(sq)
    sq.set_defaults(fn=cmd_queue)

    ss = sub.add_parser("status")
    ss.add_argument("publish_id")
    ss.set_defaults(fn=cmd_status)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
