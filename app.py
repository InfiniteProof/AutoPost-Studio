"""
TikTok Uploader — Review & Publish UI

An open source tool for publishing pre-made videos to TikTok using TikTok's
official Login Kit and Content Posting API. Anyone can run their own copy of
this app on their own computer and connect their own TikTok account — there
is no shared server and no built-in account.

To use it:
1. Register your own app at https://developers.tiktok.com/ with Login Kit and
   Content Posting API (Direct Post) enabled.
2. Set environment variables: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, and
   (if not using the default) TIKTOK_REDIRECT_URI matching a Redirect URI
   registered on your app, e.g. http://localhost:5000/auth/callback
3. Put a video (and optional matching .json metadata file with "title" /
   "social_caption") in a folder named review/ next to this file.
4. Run: python app.py
5. Open http://localhost:5000, click Connect TikTok Account, and log in.

What this app does before publishing, satisfying TikTok's Content Posting
API UX requirements:
- Shows the connected creator's identity (nickname/avatar)
- Shows a preview of the video, with an editable title/caption, waiting in
  review/
- Requires you to manually pick a privacy level (nothing pre-selected)
- Requires you to manually choose comment/duet/stitch permissions (off by
  default, disabled entirely if TikTok reports them off for your account)
- Lets you disclose commercial content (Your Brand / Branded Content), off
  by default, with branded content blocked from ever going out as private
- Requires an explicit consent checkbox, with wording that adapts to the
  commercial content disclosure, before the Publish button activates
- Detects and blocks on: TikTok reporting you can't post right now, and the
  video exceeding your account's allowed duration
"""

import os
import json
import glob
import time
import secrets
import hashlib
import base64
import subprocess
import requests
from flask import Flask, jsonify, request, send_file, abort, redirect, session, make_response


def _load_env():
    """Load KEY=VALUE lines from a central .env file into os.environ, so this
    app draws its keys from the same place as the rest of the pipeline
    instead of needing them exported in the shell. Checks this script's own
    folder first (for standalone use), then the parent project folder.
    Not shared with common.py on purpose — this file is designed to also run
    standalone, outside the parent project."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, ".env"), os.path.join(here, "..", ".env")):
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip().strip('"').strip("'")
                    if key and value:
                        os.environ.setdefault(key, value)
            return


_load_env()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

# ============================================================
# SETTINGS — each user fills these in with their own TikTok app credentials
# Register your own app at https://developers.tiktok.com/ to get these.
# ============================================================
REVIEW_DIR = os.environ.get("REVIEW_DIR", os.path.join(os.path.dirname(__file__), "..", "review"))
TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET")
TIKTOK_TOKEN_FILE = os.environ.get("TIKTOK_TOKEN_FILE", os.path.join(os.path.dirname(__file__), "tiktok_token.json"))
# Must exactly match a Redirect URI registered on your TikTok app.
TIKTOK_REDIRECT_URI = os.environ.get("TIKTOK_REDIRECT_URI", "https://autopost-studio.github.io/callback.html")

# Demo-recording aids only — both default off and change nothing about real
# publishing (api_publish always re-fetches real creator_info). They exist
# because the "can't post right now" and "video too long" UI states are hard
# to trigger organically with a real, healthy, short test account/video, but
# TikTok's audit still wants to see the app detect and block on them.
FORCE_CREATOR_ERROR = os.environ.get("FORCE_CREATOR_ERROR") == "1"
FORCE_MAX_DURATION_SEC = os.environ.get("FORCE_MAX_DURATION_SEC")

def get_latest_review_video():
    videos = glob.glob(os.path.join(REVIEW_DIR, "*.mp4"))
    if not videos:
        return None
    return max(videos, key=os.path.getctime)


def load_metadata(video_path):
    metadata_path = video_path.replace(".mp4", ".json")
    if os.path.exists(metadata_path):
        with open(metadata_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_video_duration_sec(video_path):
    """Used to validate against creator_info's max_video_post_duration_sec
    before publishing, per TikTok's Content Sharing Guidelines. Returns None
    (rather than raising) if ffprobe isn't available, so this app can still
    run standalone without it — duration validation is just skipped in that
    case instead of blocking publish."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=15
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def tiktok_get_token():
    """Reuses the same refresh logic as approve_and_upload.py. Returns None
    (never raises) on any failure — TikTok can respond HTTP 200 with an
    error body instead of a non-200 status, so status_code alone isn't a
    reliable success check; the response must actually contain access_token."""
    if not os.path.exists(TIKTOK_TOKEN_FILE):
        return None
    try:
        with open(TIKTOK_TOKEN_FILE, "r") as f:
            token_data = json.load(f)
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            return None

        url = "https://open.tiktokapis.com/v2/oauth/token/"
        data = {
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }
        response = requests.post(url, headers={"Content-Type": "application/x-www-form-urlencoded"}, data=data)
        new_data = response.json()
        if response.status_code != 200 or "access_token" not in new_data:
            return None
        with open(TIKTOK_TOKEN_FILE, "w") as f:
            json.dump(new_data, f)
        return new_data["access_token"]
    except Exception:
        return None


def _make_pkce_pair():
    """Generates a PKCE code_verifier + code_challenge pair, as required by TikTok Login Kit."""
    verifier = secrets.token_urlsafe(64)[:64]
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return verifier, challenge


# ============================================================
# LOGIN / OAUTH ROUTES
# Lets anyone running this app connect their own TikTok account.
# ============================================================

@app.route("/auth/status")
def auth_status():
    """Tells the frontend whether a TikTok account is currently connected."""
    connected = os.path.exists(TIKTOK_TOKEN_FILE) and tiktok_get_token() is not None
    return jsonify({
        "connected": connected,
        "client_key_configured": bool(TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET)
    })


@app.route("/auth/login")
def auth_login():
    """Starts TikTok's OAuth 2.0 + PKCE login flow."""
    if not TIKTOK_CLIENT_KEY or not TIKTOK_CLIENT_SECRET:
        return jsonify({
            "error": "Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET (from your own TikTok "
                     "Developer app) as environment variables before connecting an account."
        }), 400

    state = secrets.token_urlsafe(24)
    verifier, challenge = _make_pkce_pair()
    session["oauth_state"] = state
    session["pkce_verifier"] = verifier

    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "scope": "user.info.basic,video.publish",
        "response_type": "code",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256"
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return redirect(f"https://www.tiktok.com/v2/auth/authorize/?{query}")


@app.route("/auth/callback")
def auth_callback():
    """TikTok redirects here after the user approves (or denies) access."""
    error = request.args.get("error")
    if error:
        return f"TikTok login was not completed: {request.args.get('error_description', error)}", 400

    code = request.args.get("code")
    state = request.args.get("state")
    if not code or state != session.get("oauth_state"):
        return "Invalid or expired login attempt. Please try connecting again.", 400

    verifier = session.get("pkce_verifier")
    token_resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": TIKTOK_REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    token_data = token_resp.json()
    if "access_token" not in token_data:
        return f"Could not complete TikTok login: {token_data}", 500

    with open(TIKTOK_TOKEN_FILE, "w") as f:
        json.dump(token_data, f)

    return redirect("/")


@app.route("/auth/disconnect", methods=["POST"])
def auth_disconnect():
    """Removes the locally saved token so a different account can be connected."""
    if os.path.exists(TIKTOK_TOKEN_FILE):
        os.remove(TIKTOK_TOKEN_FILE)
    return jsonify({"disconnected": True})


# ============================================================
# API ROUTES
# ============================================================

@app.route("/api/review-video")
def api_review_video():
    """Returns info about the latest video waiting for review, plus creator info from TikTok."""
    access_token = tiktok_get_token()
    if not access_token:
        return jsonify({"video": None, "connected": False})

    video_path = get_latest_review_video()
    if not video_path:
        return jsonify({"video": None, "connected": True})

    metadata = load_metadata(video_path)
    caption = metadata.get("social_caption", metadata.get("title", ""))

    creator_info = None
    creator_error = None
    if FORCE_CREATOR_ERROR:
        print("[demo] FORCE_CREATOR_ERROR active — simulating 'can't post right now', unset for real use.")
        creator_error = "Please try again later."
    else:
        try:
            resp = requests.post(
                "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            )
            data = resp.json()
            if "data" in data:
                creator_info = data["data"]
            else:
                creator_error = data.get("error", {}).get("message", "Unknown error fetching creator info")
        except Exception as e:
            creator_error = str(e)

        if creator_info and FORCE_MAX_DURATION_SEC:
            creator_info["max_video_post_duration_sec"] = float(FORCE_MAX_DURATION_SEC)

    return jsonify({
        "connected": True,
        "video": {
            "filename": os.path.basename(video_path),
            "caption": caption,
            "title": metadata.get("title", ""),
            "duration_sec": get_video_duration_sec(video_path)
        },
        "creator_info": creator_info,
        "creator_error": creator_error
    })


@app.route("/api/video-file")
def api_video_file():
    """Serves the actual video file for preview in the browser."""
    video_path = get_latest_review_video()
    if not video_path:
        abort(404)
    return send_file(video_path, mimetype="video/mp4")


@app.route("/api/publish", methods=["POST"])
def api_publish():
    """Publishes the reviewed video to TikTok with the user's chosen settings.

    Per TikTok's Content Sharing Guidelines, the client-submitted settings
    are never trusted on their own — this re-fetches creator_info and
    re-derives what's actually allowed server-side:
      - privacy_level must be one of the account's current privacy_level_options
      - duet/stitch are never allowed when privacy_level is SELF_ONLY (you
        can't duet/stitch a private video — this is the "full interaction
        between privacy settings and interaction settings" TikTok's audit
        checks for), regardless of what the client sent
      - comment/duet/stitch are forced off if the account has them disabled
      - video duration is checked against max_video_post_duration_sec
      - branded content can't be posted as SELF_ONLY either, same reasoning
      - if commercial content disclosure is on, at least one of "your brand"
        / "branded content" must be selected
    """
    body = request.get_json()

    privacy_level = body.get("privacy_level")
    title = (body.get("title") or "").strip()
    allow_comment = body.get("allow_comment", False)
    allow_duet = body.get("allow_duet", False)
    allow_stitch = body.get("allow_stitch", False)
    consent_given = body.get("consent_given", False)
    content_disclosure = body.get("content_disclosure", False)
    your_brand = body.get("your_brand", False) if content_disclosure else False
    branded_content = body.get("branded_content", False) if content_disclosure else False

    if not consent_given:
        return jsonify({"error": "Consent is required before publishing."}), 400
    if not privacy_level:
        return jsonify({"error": "You must select a privacy level."}), 400
    if content_disclosure and not (your_brand or branded_content):
        return jsonify({"error": "Indicate whether this content promotes yourself, a third party, or both."}), 400
    if branded_content and privacy_level == "SELF_ONLY":
        return jsonify({"error": "Branded content visibility can't be set to private."}), 400

    video_path = get_latest_review_video()
    if not video_path:
        return jsonify({"error": "No video found to publish."}), 404

    metadata = load_metadata(video_path)
    caption = title or metadata.get("social_caption", metadata.get("title", ""))

    access_token = tiktok_get_token()
    if not access_token:
        return jsonify({"error": "Could not get TikTok access token."}), 500

    creator_resp = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    creator_data = creator_resp.json().get("data", {})

    privacy_options = creator_data.get("privacy_level_options", [])
    if privacy_options and privacy_level not in privacy_options:
        return jsonify({"error": f"'{privacy_level}' is not a privacy level this account currently allows."}), 400

    if privacy_level == "SELF_ONLY":
        allow_duet = False
        allow_stitch = False
    if creator_data.get("comment_disabled"):
        allow_comment = False
    if creator_data.get("duet_disabled"):
        allow_duet = False
    if creator_data.get("stitch_disabled"):
        allow_stitch = False

    max_duration = creator_data.get("max_video_post_duration_sec")
    video_duration = get_video_duration_sec(video_path)
    if max_duration and video_duration and video_duration > max_duration:
        return jsonify({
            "error": f"Video is {video_duration:.0f}s, but this account's limit is {max_duration:.0f}s."
        }), 400

    file_size = os.path.getsize(video_path)
    init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    init_headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    init_body = {
        "post_info": {
            "title": caption,
            "privacy_level": privacy_level,
            "disable_duet": not allow_duet,
            "disable_comment": not allow_comment,
            "disable_stitch": not allow_stitch,
            "brand_organic_toggle": your_brand,
            "brand_content_toggle": branded_content,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": file_size,
            "total_chunk_count": 1
        }
    }

    init_resp = requests.post(init_url, headers=init_headers, json=init_body)
    init_data = init_resp.json()
    if "data" not in init_data:
        return jsonify({"error": f"TikTok init failed: {init_data}"}), 500

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]

    with open(video_path, "rb") as f:
        video_data = f.read()
    upload_headers = {"Content-Type": "video/mp4", "Content-Range": f"bytes 0-{file_size - 1}/{file_size}"}
    upload_resp = requests.put(upload_url, headers=upload_headers, data=video_data)
    if not upload_resp.ok:
        return jsonify({"error": f"TikTok video upload failed: {upload_resp.status_code} {upload_resp.text}"}), 500

    time.sleep(3)
    status_resp = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
        headers=init_headers,
        json={"publish_id": publish_id}
    )
    status_data = status_resp.json().get("data", {})
    status = status_data.get("status")
    if status == "FAILED":
        return jsonify({"error": f"TikTok publish failed: {status_data.get('fail_reason', 'unknown reason')}"}), 500

    return jsonify({"success": True, "publish_id": publish_id, "status": status})


@app.route("/")
def index():
    return send_file("templates/index.html")


if __name__ == "__main__":
    import threading, webbrowser
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5000")).start()
    # host is loopback-only on purpose: this app holds your TikTok token and
    # can publish videos, and debug=True runs Flask's Werkzeug debugger,
    # which allows arbitrary code execution to anyone who can reach it.
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)