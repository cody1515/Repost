"""The worker: at each account's posting times, picks a random creator and reposts a random video of theirs not yet posted there."""
import hashlib
import json
import random
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import os

import requests

from store import now

TT_API = "https://open.tiktokapis.com/v2"

CAPTION_LIMIT = 2200
MAX_ATTEMPTS = 3
CATCH_UP = timedelta(hours=2)   # if the server was down at a posting time, still post within this window
MAX_CREATORS_PER_SLOT = 4       # how many creators to try before giving up on a slot


def check(resp):
    if not resp.ok:
        try:
            msg = resp.json().get("error", {}).get("message") or resp.text
        except Exception:
            msg = resp.text
        raise RuntimeError(f"HTTP {resp.status_code}: {str(msg)[:300]}")
    return resp


def short(e):
    s = str(e).strip()
    return s.splitlines()[-1][:300] if s else e.__class__.__name__


def get_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        from datetime import timezone
        return timezone.utc


def valid_time(t):
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t or ""))


def next_post_time(tz_name, times):
    """Next upcoming posting time (ISO string) for a list of 'HH:MM' times."""
    tz = get_tz(tz_name)
    local = datetime.now(tz)
    best = None
    for t in times:
        if not valid_time(t):
            continue
        h, m = map(int, t.split(":"))
        dt = local.replace(hour=h, minute=m, second=0, microsecond=0)
        if dt <= local:
            dt += timedelta(days=1)
        best = dt if best is None or dt < best else best
    return best.isoformat() if best else None


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
def parse_link(url):
    url = url.strip()
    host = (re.match(r"https?://([^/]+)", url) or [None, ""])[1].lower()
    if "tiktok.com" in host:
        m = re.search(r"/video/(\d+)", url)
        pid = m.group(1) if m else "link-" + hashlib.sha1(url.encode()).hexdigest()[:12]
        return "tiktok", pid, url.split("?")[0] if m else url
    if "instagram.com" in host:
        m = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
        if not m:
            raise ValueError("That Instagram link doesn't point to a post or reel")
        return "instagram", m.group(1), f"https://www.instagram.com/reel/{m.group(1)}/"
    raise ValueError("Paste a TikTok or Instagram link")


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def fetch_tiktok(handle, limit):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "playlistend": limit, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.tiktok.com/@{handle}", download=False)
    for e in (info.get("entries") or [])[:limit]:
        vid = str(e["id"])
        yield {"source": "tiktok", "post_id": vid, "url": f"https://www.tiktok.com/@{handle}/video/{vid}"}


def download_tiktok(url, dest):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "outtmpl": str(dest / "video.%(ext)s"),
            "format": "best[vcodec^=h264][ext=mp4]/best[ext=mp4]/best"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    found = sorted(dest.glob("video.*"))
    if not found:
        raise RuntimeError("download finished but the file is missing")
    return found[0], info.get("description") or ""


def make_ig_loader(s):
    import instaloader
    L = instaloader.Instaloader(
        download_pictures=False, download_videos=False, download_video_thumbnails=False,
        download_geotags=False, download_comments=False, save_metadata=False, quiet=True)
    if s.get("ig_sessionid") and s.get("ig_reader_username"):
        L.load_session(s["ig_reader_username"], {"sessionid": s["ig_sessionid"]})
    return L


def fetch_instagram(L, handle, limit):
    import instaloader
    profile = instaloader.Profile.from_username(L.context, handle)
    for i, post in enumerate(profile.get_posts()):
        if i >= limit:
            break
        if post.is_video:
            yield {"source": "instagram", "post_id": post.shortcode,
                   "url": f"https://www.instagram.com/reel/{post.shortcode}/",
                   "pinned": bool(getattr(post, "is_pinned", False)), "date": post.date_utc}


def business_discovery(s, reader, handle, limit):
    """Read a public business/creator account's recent videos through Instagram's official API,
    using one of your own connected accounts. Works from servers, unlike scraping."""
    graph = f"https://graph.facebook.com/{s['ig_graph_version']}"
    fields = (f"business_discovery.username({handle}){{username,media.limit({min(max(limit * 2, 10), 50)})"
              "{id,media_type,media_url,permalink,caption,timestamp}}")
    r = requests.get(f"{graph}/{reader['ig_user_id']}", params={"fields": fields,
                     "access_token": reader["access_token"]}, timeout=30)
    d = r.json() if r.content else {}
    if not r.ok or "business_discovery" not in d:
        msg = (d.get("error") or {}).get("message") or f"HTTP {r.status_code}"
        if "business" in msg.lower() or "#110" in msg or "username" in msg.lower():
            msg = f"@{handle} isn't a public business or creator account, or the username is wrong ({msg})"
        raise RuntimeError(msg)
    out = []
    for m in (d["business_discovery"].get("media") or {}).get("data", []):
        if m.get("media_type") != "VIDEO" or not m.get("media_url"):
            continue  # skip photos, carousels and videos Instagram won't share
        permalink = m.get("permalink") or ""
        sc = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", permalink)
        out.append({"source": "instagram", "post_id": sc.group(1) if sc else m["id"],
                    "url": permalink or f"https://www.instagram.com/p/{m['id']}/",
                    "video_url": m["media_url"], "caption": m.get("caption") or ""})
    return out[:limit]


def download_url(url, dest):
    path = dest / "video.mp4"
    with requests.get(url, stream=True, timeout=180) as r:
        check(r)
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path


def download_instagram(L, shortcode, dest):
    import instaloader
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    if not post.is_video:
        raise RuntimeError("that Instagram post isn't a video")
    path = dest / "video.mp4"
    with requests.get(post.video_url, stream=True, timeout=120) as r:
        check(r)
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path, post.caption or ""


# --------------------------------------------------------------------------- #
# Instagram (Graph API)
# --------------------------------------------------------------------------- #
def has_video(path):
    """False for TikTok photo slideshows, which download as audio (sometimes with a cover image)."""
    if not shutil.which("ffprobe"):
        return True
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                              "-show_entries", "stream=codec_name:stream_disposition=attached_pic",
                              "-of", "json", str(path)], capture_output=True, text=True, timeout=60).stdout
        streams = json.loads(out or "{}").get("streams", [])
    except Exception:
        return True
    return any(st.get("codec_name") not in ("mjpeg", "png", "bmp", "gif", "webp")
               and not (st.get("disposition") or {}).get("attached_pic") for st in streams)


def prepare_video(src, work):
    """Re-encode to the format Instagram Reels expects (H.264 + AAC MP4, even dimensions, 30fps max).
    Falls back to the original file if ffmpeg isn't available or fails."""
    if not shutil.which("ffmpeg"):
        return src
    out = work / "ig.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
           "-map", "0:v:0", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-profile:v", "high", "-level", "4.1",
           "-pix_fmt", "yuv420p", "-vf", "scale='min(1080,trunc(iw/2)*2)':-2,fps=30", "-r", "30",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
           "-movflags", "+faststart", str(out)]
    try:
        subprocess.run(cmd, check=True, timeout=600, capture_output=True)
        if out.exists() and out.stat().st_size > 0:
            return out
    except subprocess.CalledProcessError as e:
        print("ffmpeg failed:", (e.stderr or b"")[-500:].decode(errors="ignore"), flush=True)
    except Exception as e:
        print("ffmpeg failed:", e, flush=True)
    return src


def _wait_and_publish(graph, uid, tok, container):
    for _ in range(90):
        st = check(requests.get(f"{graph}/{container}", params={
            "fields": "status_code,status", "access_token": tok}, timeout=30)).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video: {st.get('status')}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram processing timed out")
    return check(requests.post(f"{graph}/{uid}/media_publish", data={
        "creation_id": container, "access_token": tok}, timeout=60)).json()["id"]


def collab_list(acc):
    try:
        return [u for u in json.loads(acc.get("collab") or "[]") if u][:3]
    except Exception:
        return []


def accept_collab(s, acc, media_id):
    """Accept a collab invite on one of your own accounts so the Reel shows on its profile too."""
    graph = f"https://graph.facebook.com/{s['ig_graph_version']}"
    r = requests.post(f"{graph}/{acc['ig_user_id']}/collaboration_invites", data={
        "media_id": media_id, "accept": "true", "access_token": acc["access_token"]}, timeout=30)
    d = r.json() if r.content else {}
    if not r.ok or not d.get("success"):
        raise RuntimeError((d.get("error") or {}).get("message") or f"HTTP {r.status_code}")
    return True


def post_instagram(s, acc, video, caption, link_for=None):
    """Upload the file directly; if Instagram's upload server rejects it, give Instagram a
    temporary private link to the file instead (link_for(path) -> public URL)."""
    if not (acc["ig_user_id"] and acc["access_token"]):
        raise RuntimeError("this account isn't connected")
    v, tok, uid = s["ig_graph_version"], acc["access_token"], acc["ig_user_id"]
    graph = f"https://graph.facebook.com/{v}"
    base = {"media_type": "REELS", "caption": caption, "share_to_feed": "true", "access_token": tok}
    collabs = collab_list(acc)
    if collabs:  # up to 3 usernames; each gets a collab invite
        base["collaborators"] = ",".join(collabs)

    try:
        container = check(requests.post(f"{graph}/{uid}/media", data={**base, "upload_type": "resumable"},
                                        timeout=60)).json()["id"]
        size = video.stat().st_size
        with open(video, "rb") as f:
            check(requests.post(f"https://rupload.facebook.com/ig-api-upload/{v}/{container}",
                                headers={"Authorization": f"OAuth {tok}", "offset": "0",
                                         "file_size": str(size)}, data=f, timeout=900))
        return _wait_and_publish(graph, uid, tok, container)
    except Exception as first:
        url = link_for(video) if link_for else None
        if not url:
            raise
        try:
            container = check(requests.post(f"{graph}/{uid}/media", data={**base, "video_url": url},
                                            timeout=60)).json()["id"]
            return _wait_and_publish(graph, uid, tok, container)
        except Exception as second:
            raise RuntimeError(f"direct upload: {short(first)} / link upload: {short(second)}")


# --------------------------------------------------------------------------- #
# TikTok (Content Posting API)
# --------------------------------------------------------------------------- #
def tiktok_exchange(data):
    r = requests.post(f"{TT_API}/oauth/token/", data={
        "client_key": os.environ.get("TIKTOK_CLIENT_KEY", ""),
        "client_secret": os.environ.get("TIKTOK_CLIENT_SECRET", ""), **data},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    d = r.json() if r.content else {}
    if "access_token" not in d:
        raise RuntimeError(f"TikTok login failed: {d.get('error_description') or d.get('error') or r.status_code}")
    return d


def tiktok_display_name(token):
    r = requests.get(f"{TT_API}/user/info/", params={"fields": "open_id,display_name"},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    try:
        return r.json()["data"]["user"].get("display_name") or ""
    except Exception:
        return ""


def tiktok_token(store, acc):
    """Current access token for a TikTok account, refreshing it when it's about to expire."""
    if not acc.get("refresh_token"):
        raise RuntimeError("TikTok isn't connected. Tap Reconnect on this account.")
    if time.time() < (acc.get("token_expires") or 0) - 300:
        return acc["access_token"]
    try:
        d = tiktok_exchange({"grant_type": "refresh_token", "refresh_token": acc["refresh_token"]})
    except RuntimeError as e:
        raise RuntimeError(f"{e}. Tap Reconnect on this account.")
    store.x("UPDATE accounts SET access_token=?, refresh_token=?, token_expires=? WHERE id=?",
            (d["access_token"], d.get("refresh_token") or acc["refresh_token"],
             time.time() + d.get("expires_in", 86400), acc["id"]))
    return d["access_token"]


def _tt_error(d):
    err = d.get("error") or {}
    if err.get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok ({err.get('code')}): {err.get('message') or 'request failed'}")


def tiktok_creator_info(store, acc):
    """Latest posting info for the connected TikTok account (TikTok asks apps to fetch this each time)."""
    h = {"Authorization": f"Bearer {tiktok_token(store, acc)}", "Content-Type": "application/json; charset=UTF-8"}
    d = requests.post(f"{TT_API}/post/publish/creator_info/query/", headers=h, timeout=30).json()
    _tt_error(d)
    return d.get("data", {})


def _tt_upload(store, acc, video, init_url, extra):
    h = {"Authorization": f"Bearer {tiktok_token(store, acc)}", "Content-Type": "application/json; charset=UTF-8"}
    size = video.stat().st_size
    if size <= 64 * 1024 * 1024:
        chunk, count = size, 1
    else:  # TikTok rule: 10MB chunks, the last one absorbs the remainder
        chunk = 10 * 1024 * 1024
        count = size // chunk
    body = {"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk, "total_chunk_count": count}, **extra}
    d = requests.post(init_url, headers=h, json=body, timeout=60).json()
    _tt_error(d)
    publish_id, upload_url = d["data"]["publish_id"], d["data"]["upload_url"]

    with open(video, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            data = f.read(end - start + 1)
            check(requests.put(upload_url, data=data, timeout=900, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{size}"}))

    for _ in range(90):
        st = requests.post(f"{TT_API}/post/publish/status/fetch/", headers=h,
                           json={"publish_id": publish_id}, timeout=30).json().get("data", {})
        if st.get("status") in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return publish_id
        if st.get("status") == "FAILED":
            raise RuntimeError(f"TikTok rejected it: {st.get('fail_reason')}")
        time.sleep(10)
    return publish_id  # still processing on TikTok's side; it finishes on its own


def post_tiktok_draft(store, acc, video):
    """Sends the video to the account's TikTok inbox; the owner finishes posting in the TikTok app."""
    return _tt_upload(store, acc, video, f"{TT_API}/post/publish/inbox/video/init/", {})


def post_tiktok_direct(store, acc, video, caption, opts):
    """Publishes with the settings the owner picked on the approval screen."""
    return _tt_upload(store, acc, video, f"{TT_API}/post/publish/video/init/", {"post_info": {
        "title": caption,
        "privacy_level": opts["privacy"],
        "disable_comment": not opts.get("comment"),
        "disable_duet": not opts.get("duet"),
        "disable_stitch": not opts.get("stitch"),
        "brand_organic_toggle": bool(opts.get("your_brand")),
        "brand_content_toggle": bool(opts.get("branded")),
    }})


def video_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(path)], capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# TikTok through Zernio (an already-approved posting service)
# --------------------------------------------------------------------------- #
ZERNIO_API = "https://zernio.com/api/v1"
TT_DEFAULTS = {"privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": True, "allow_duet": True,
               "allow_stitch": True, "draft": False, "auto_consent": False}


def tt_settings(acc):
    try:
        return {**TT_DEFAULTS, **json.loads(acc.get("tt_settings") or "{}")}
    except Exception:
        return dict(TT_DEFAULTS)


def _zernio(method, path, **kw):
    key = os.environ.get("ZERNIO_API_KEY", "")
    if not key:
        raise RuntimeError("Add ZERNIO_API_KEY in Railway Variables")
    r = requests.request(method, ZERNIO_API + path, timeout=kw.pop("timeout", 60),
                         headers={"Authorization": f"Bearer {key}"}, **kw)
    try:
        d = r.json()
    except Exception:
        d = {}
    if not r.ok:
        msg = d.get("error") or d.get("message") or r.text[:300]
        raise RuntimeError(f"Zernio {r.status_code}: {msg}")
    return d


def zernio_tiktok_accounts():
    d = _zernio("GET", "/accounts")
    out = []
    for a in d.get("accounts", d if isinstance(d, list) else []):
        if a.get("platform") == "tiktok":
            out.append({"zernio_id": a.get("_id") or a.get("id") or a.get("accountId"),
                        "username": a.get("username") or "", "display_name": a.get("displayName") or "",
                        "active": a.get("isActive", True)})
    return out


def post_tiktok_zernio(acc, video, caption):
    """Upload the file to Zernio, then ask Zernio to publish it to TikTok."""
    st = tt_settings(acc)
    if not st["auto_consent"]:
        raise RuntimeError("Auto-posting isn't turned on for this TikTok account (Accounts → Edit)")
    pre = _zernio("POST", "/media/presign", json={"filename": "video.mp4", "contentType": "video/mp4",
                                                   "size": video.stat().st_size})
    with open(video, "rb") as f:
        check(requests.put(pre["uploadUrl"], data=f, headers={"Content-Type": "video/mp4"}, timeout=900))
    settings = {"privacy_level": st["privacy_level"], "allow_comment": bool(st["allow_comment"]),
                "allow_duet": bool(st["allow_duet"]), "allow_stitch": bool(st["allow_stitch"]),
                "content_preview_confirmed": True, "express_consent_given": True}
    if st["draft"]:
        settings["draft"] = True
    d = _zernio("POST", "/posts", timeout=300, json={
        "content": caption,
        "mediaItems": [{"type": "video", "url": pre["publicUrl"]}],
        "platforms": [{"platform": "tiktok", "accountId": acc["zernio_id"]}],
        "tiktokSettings": settings,
        "publishNow": True})
    post = d.get("post") or d
    post_id = post.get("_id") or post.get("id") or ""

    # Wait briefly for the result; Zernio keeps retrying on its own if TikTok is slow
    for attempt in range(30):
        plats = post.get("platforms") or []
        pstat = (plats[0].get("status") if plats else None) or post.get("status")
        if pstat in ("published", "posted", "completed", "success"):
            return post_id
        if pstat in ("failed", "error"):
            err = (plats[0].get("error") or plats[0].get("errorMessage") if plats else None) or post.get("error")
            raise RuntimeError(f"TikTok via Zernio failed: {err or 'no reason given'}")
        if not post_id:
            return ""
        time.sleep(10)
        try:
            got = _zernio("GET", f"/posts/{post_id}")
            post = got.get("post") or got
        except RuntimeError:
            pass
    return post_id  # still processing; Zernio finishes it


def discover_accounts(version, user_token):
    """List the Instagram accounts reachable with a (long-lived) user token."""
    r = check(requests.get(f"https://graph.facebook.com/{version}/me/accounts", params={
        "fields": "name,access_token,instagram_business_account{id,username}",
        "limit": 100, "access_token": user_token}, timeout=30)).json()
    out = []
    for page in r.get("data", []):
        ig = page.get("instagram_business_account")
        if ig and page.get("access_token"):
            out.append({"page": page.get("name"), "ig_user_id": ig["id"],
                        "username": ig.get("username") or "", "access_token": page["access_token"]})
    return out


def account_username(version, acc):
    r = check(requests.get(f"https://graph.facebook.com/{version}/{acc['ig_user_id']}", params={
        "fields": "username", "access_token": acc["access_token"]}, timeout=30)).json()
    return r.get("username", "")


# --------------------------------------------------------------------------- #
# Captions
# --------------------------------------------------------------------------- #
def build_caption(s, creator, original, url, platform="instagram"):
    """Credit uses the creator's handle on the app you're posting to, if you have it."""
    handle = (creator.get(platform) or creator.get(creator["pull_from"]) or "").lstrip("@").strip()
    credit = s["credit_format"].replace("{handle}", handle)
    tmpl = s["caption_template"]

    def fill(cap):
        return (tmpl.replace("{caption}", cap).replace("{credit}", credit)
                .replace("{name}", creator.get("name") or handle).replace("{url}", url).strip())

    original = (original or "").strip()
    text = fill(original)
    if len(text) > CAPTION_LIMIT:  # trim their caption, never the credit
        over = len(text) - CAPTION_LIMIT + 1
        text = fill(original[:max(0, len(original) - over)] + "…")
    return text[:CAPTION_LIMIT]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, store, data_dir):
        self.store = store
        self.tmp = Path(data_dir) / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.pending_dir = Path(data_dir) / "pending"   # TikTok videos waiting for your approval
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.wake = threading.Event()
        self.pending = []          # account ids queued by "Post now"
        self.public = {}           # token -> file, served briefly at /media/<token>.mp4 for Instagram to fetch
        self.public_base = ""      # set by app.py to the app's https address
        self.status = "Starting"

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def post_now(self, account_id):
        if account_id not in self.pending:
            self.pending.append(account_id)
        self.wake.set()

    def link_for(self, path):
        if not self.public_base.startswith("https://"):
            return None
        token = secrets.token_urlsafe(24)
        self.public[token] = Path(path)
        return f"{self.public_base}/media/{token}.mp4"

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                traceback.print_exc()
                self.store.log("error", f"Worker error: {short(e)}")
            self.status = "Paused" if self.store.settings()["paused"] else "Waiting for next posting time"
            self.wake.wait(30)
            self.wake.clear()

    def tick(self):
        s = self.store.settings()
        while self.pending:  # "Post now" works even while paused
            acc = self.store.account(self.pending.pop(0))
            if acc:
                self.fire(s, acc, "Post now")
        if s["paused"]:
            return
        local = datetime.now(get_tz(s["timezone"]))
        for acc in self.store.accounts(enabled_only=True):
            for t in acc["times"]:
                if not valid_time(t):
                    continue
                h, m = map(int, t.split(":"))
                slot = local.replace(hour=h, minute=m, second=0, microsecond=0)
                if not (slot <= local < slot + CATCH_UP):
                    continue
                _, new = self.store.x("INSERT OR IGNORE INTO slots VALUES (?, ?)",
                                      (acc["id"], f"{slot.date()} {t}"))
                if new:
                    self.fire(s, acc, f"{t} post")

    # ---- one posting slot ----
    def fire(self, s, acc, why):
        self.status = f"Posting to {acc['name']}"
        if acc.get("zernio_id") and not tt_settings(acc)["auto_consent"]:
            self.store.log("warn", f"{acc['name']}: auto-posting is off, so nothing was posted (Accounts → Edit)")
            return False
        if acc.get("platform") == "tiktok" and not acc.get("zernio_id") and s["tiktok_mode"] == "direct":
            waiting = self.store.q("SELECT COUNT(*) n FROM reposts WHERE account_id=? AND status='awaiting'",
                                   (acc["id"],))[0]["n"]
            if waiting >= 5:
                self.store.log("warn", f"{acc['name']}: 5 videos are already waiting for approval, so none was added")
                return False
        # 1) links you added by hand go first
        for row in self.store.q("SELECT * FROM reposts WHERE account_id=? AND status='queued' ORDER BY id",
                                (acc["id"],)):
            cr = self.store.q("SELECT * FROM creators WHERE id=?", (row["creator_id"],))
            if not cr:
                self.store.x("UPDATE reposts SET status='skipped', error='creator deleted' WHERE id=?", (row["id"],))
                continue
            if self.post_video(s, acc, cr[0], row) is True:
                return True

        # 2) a random creator, and a random video of theirs that this account hasn't posted yet
        creators = self.store.q("SELECT c.* FROM creators c JOIN creator_accounts ca ON ca.creator_id=c.id "
                                "WHERE ca.account_id=? AND c.enabled=1", (acc["id"],))
        if not creators:
            self.store.log("warn", f"{acc['name']}: no creators to pick from ({why} skipped)")
            return False
        random.shuffle(creators)
        tried = 0
        for cr in creators:
            result = None
            for _ in range(5):  # photo posts get skipped, so try a few of this creator's videos
                cand = self.random_unposted(s, acc, cr)
                if not cand:
                    break
                result = self.post_video(s, acc, cr, cand)
                if result != "skip":
                    break
            if result is True:
                return True
            if result is False:
                tried += 1
                if tried >= MAX_CREATORS_PER_SLOT:
                    break
        self.store.log("warn", f"{acc['name']}: nothing could be posted for the {why}")
        return False

    # ---- Instagram collabs ----
    def accept_collabs_later(self, s, acc, cr, p, media_id):
        """Invites take a moment to appear, so accept them on your own accounts in the background."""
        own = {(a["username"] or "").lower(): a for a in self.store.accounts()
               if a.get("platform", "instagram") == "instagram" and a["id"] != acc["id"] and a["username"]}
        names = collab_list(acc)
        outside = [u for u in names if u.lower() not in own]
        if outside:
            self.store.log("info", f"{acc['name']}: collab invite sent to " + ", ".join("@" + u for u in outside)
                           + " (they need to accept it in Instagram)")

        def run():
            for u in names:
                other = own.get(u.lower())
                if not other:
                    continue
                for attempt in range(8):
                    time.sleep(20 if attempt else 15)
                    try:
                        accept_collab(s, other, media_id)
                        # it's on that profile now, so that account won't post the same video again
                        self.store.x("INSERT OR IGNORE INTO reposts(account_id, creator_id, source, post_id, url, status,"
                                     " created_at, posted_at, ig_media_id, error) VALUES (?,?,?,?,?,'posted',?,?,?,?)",
                                     (other["id"], cr["id"], p["source"], p["post_id"], p["url"], now(), now(),
                                      media_id, f"collab with {acc['name']}"))
                        self.store.log("info", f"{other['name']}: accepted collab with {acc['name']}")
                        break
                    except Exception as e:
                        last = short(e)
                else:
                    self.store.log("warn", f"{other['name']}: couldn't accept the collab from {acc['name']} ({last}). "
                                           "Accept it in the Instagram app.")
        threading.Thread(target=run, daemon=True).start()

    # ---- TikTok approval ----
    def publish_approved(self, repost_id, caption, opts):
        """Runs in the background after you tap Post to TikTok on the approval screen."""
        def run():
            row = self.store.q("SELECT * FROM reposts WHERE id=?", (repost_id,))[0]
            acc = self.store.account(row["account_id"])
            try:
                pid = post_tiktok_direct(self.store, acc, Path(row["file"]), caption, opts)
                self.store.x("UPDATE reposts SET status='posted', posted_at=?, ig_media_id=?, caption_out=?, error=NULL,"
                             " file=NULL WHERE id=?", (now(), pid, caption, repost_id))
                Path(row["file"]).unlink(missing_ok=True)
                self.store.log("info", f"{acc['name']}: posted to TikTok")
            except Exception as e:
                self.store.x("UPDATE reposts SET status='awaiting', error=? WHERE id=?", (short(e), repost_id))
                self.store.log("error", f"{acc['name']}: TikTok post failed ({short(e)})")
        self.store.x("UPDATE reposts SET status='publishing', error=NULL WHERE id=?", (repost_id,))
        threading.Thread(target=run, daemon=True).start()

    def discard_pending(self, repost_id):
        row = self.store.q("SELECT file FROM reposts WHERE id=?", (repost_id,))
        if row and row[0]["file"]:
            Path(row[0]["file"]).unlink(missing_ok=True)
        self.store.x("UPDATE reposts SET status='skipped', file=NULL WHERE id=?", (repost_id,))

    def download_ig(self, s, acc, cr, p, work):
        if p.get("video_url"):
            return download_url(p["video_url"], work), p.get("caption", "")
        # a link you pasted: find it in the creator's recent videos via the official API
        handle = (cr.get("instagram") or "").lstrip("@").strip()
        reader = self.ig_reader(acc)
        if handle and reader:
            try:
                for m in business_discovery(s, reader, handle, 50):
                    if m["post_id"] == p["post_id"]:
                        return download_url(m["video_url"], work), m["caption"]
            except Exception:
                pass
        return download_instagram(make_ig_loader(s), p["post_id"], work)

    def ig_reader(self, acc):
        """One of your connected Instagram accounts to read other accounts with."""
        if acc.get("platform", "instagram") == "instagram" and acc.get("ig_user_id") and acc.get("access_token"):
            return acc
        for a in self.store.accounts():
            if a.get("platform", "instagram") == "instagram" and a.get("ig_user_id") and a.get("access_token"):
                return a
        return None

    def load_instagram(self, s, acc, handle):
        reader = self.ig_reader(acc)
        first = None
        if reader:
            try:
                return business_discovery(s, reader, handle, s["scan_depth"])
            except Exception as e:
                first = e
        try:  # fallback: old method (often blocked on servers)
            return list(fetch_instagram(make_ig_loader(s), handle, s["scan_depth"]))
        except Exception:
            raise first or RuntimeError("couldn't read that Instagram account")

    def random_unposted(self, s, acc, cr):
        src = cr["pull_from"]
        handle = (cr.get(src) or "").lstrip("@").strip()
        if not handle:
            return None
        try:
            if src == "tiktok":
                posts = list(fetch_tiktok(handle, s["scan_depth"]))
            else:
                posts = self.load_instagram(s, acc, handle)
        except Exception as e:
            self.store.log("warn", f"Couldn't load @{handle}'s {src}: {short(e)}")
            return None
        done = {(r["source"], r["post_id"]): r for r in self.store.q(
            "SELECT source, post_id, status, attempts FROM reposts WHERE account_id=?", (acc["id"],))}
        options = [p for p in posts
                   if (r := done.get((p["source"], p["post_id"]))) is None
                   or (r["status"] == "failed" and r["attempts"] < MAX_ATTEMPTS)]
        return random.choice(options) if options else None

    def post_video(self, s, acc, cr, p):
        """p is a fetched post dict or an existing reposts row.
        Returns True if it posted, "skip" if it isn't a video, False if posting failed."""
        self.store.x("INSERT OR IGNORE INTO reposts(account_id, creator_id, source, post_id, url, status, created_at)"
                     " VALUES (?,?,?,?,?,?,?)",
                     (acc["id"], cr["id"], p["source"], p["post_id"], p["url"], "posting", now()))
        row = self.store.q("SELECT * FROM reposts WHERE account_id=? AND source=? AND post_id=?",
                           (acc["id"], p["source"], p["post_id"]))[0]
        work = Path(tempfile.mkdtemp(dir=self.tmp))
        try:
            if p["source"] == "tiktok":
                path, caption = download_tiktok(p["url"], work)
            else:
                path, caption = self.download_ig(s, acc, cr, p, work)
            if not has_video(path):
                self.store.x("UPDATE reposts SET status='skipped', error='photo post, not a video' WHERE id=?",
                             (row["id"],))
                self.store.log("info", f"Skipped a photo post from {cr['name']} (not a video)")
                return "skip"
            video = prepare_video(path, work)
            platform = acc.get("platform") or "instagram"
            text = build_caption(s, cr, caption, p["url"], platform)
            if platform == "tiktok" and acc.get("zernio_id"):
                media_id = post_tiktok_zernio(acc, video, text)
                self.store.x("UPDATE reposts SET status='posted', posted_at=?, error=NULL, ig_media_id=?, caption_out=? "
                             "WHERE id=?", (now(), media_id, text, row["id"]))
                drafts = " (in TikTok drafts)" if tt_settings(acc)["draft"] else ""
                self.store.log("info", f"{acc['name']}: posted {cr['name']}'s video via Zernio{drafts}")
                return True
            if platform == "tiktok" and s["tiktok_mode"] == "direct":
                dest = self.pending_dir / f"{row['id']}.mp4"
                shutil.move(str(video), dest)
                self.store.x("UPDATE reposts SET status='awaiting', file=?, duration=?, caption_out=?, error=NULL "
                             "WHERE id=?", (str(dest), video_duration(dest), text, row["id"]))
                self.store.log("info", f"{acc['name']}: {cr['name']}'s video is ready for your approval")
                return True
            if platform == "tiktok":
                media_id = post_tiktok_draft(self.store, acc, video)
            else:
                media_id = post_instagram(s, acc, video, text, self.link_for)
                if collab_list(acc):
                    self.accept_collabs_later(s, acc, cr, p, media_id)
            self.store.x("UPDATE reposts SET status='posted', posted_at=?, error=NULL, ig_media_id=?, caption_out=? "
                         "WHERE id=?", (now(), media_id, text, row["id"]))
            where = " (in TikTok drafts)" if platform == "tiktok" and s["tiktok_mode"] == "inbox" else ""
            self.store.log("info", f"{acc['name']}: posted {cr['name']}'s video{where}")
            return True
        except Exception as e:
            attempts = (row["attempts"] or 0) + 1
            self.store.x("UPDATE reposts SET status='failed', attempts=?, error=? WHERE id=?",
                         (attempts, short(e), row["id"]))
            self.store.log("error", f"{acc['name']}: couldn't post {cr['name']}'s video ({short(e)})")
            return False
        finally:
            for t in [t for t, f in self.public.items() if f.parent == work]:
                self.public.pop(t, None)
            for f in work.glob("*"):
                f.unlink(missing_ok=True)
            work.rmdir()
