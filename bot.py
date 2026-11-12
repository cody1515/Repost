"""The worker: finds new videos, downloads them, posts them with credit. Runs in a background thread."""
import hashlib
import os
import re
import threading
import time
import traceback
from pathlib import Path

import requests

from store import now

TT_API = "https://open.tiktokapis.com/v2"
CAPTION_LIMIT = 2200
MAX_ATTEMPTS = 3


def check(resp):
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
    return resp


def short(e):
    return str(e).strip().splitlines()[-1][:300] if str(e).strip() else e.__class__.__name__


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
def parse_link(url):
    """Returns (source, post_id, clean_url) for a pasted TikTok/Instagram link."""
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
        yield {"post_id": vid, "url": f"https://www.tiktok.com/@{handle}/video/{vid}",
               "caption": e.get("description") or ""}


def download_tiktok(url, dest, key):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True,
            "outtmpl": str(dest / f"tt_{key}.%(ext)s"),
            "format": "best[vcodec^=h264][ext=mp4]/best[ext=mp4]/best"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if not path.exists():
        found = sorted(dest.glob(f"tt_{key}.*"))
        if not found:
            raise RuntimeError("download finished but the file is missing")
        path = found[0]
    return path, info.get("description") or ""


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
            yield {"post_id": post.shortcode,
                   "url": f"https://www.instagram.com/reel/{post.shortcode}/",
                   "caption": post.caption or ""}


def download_instagram(L, shortcode, dest):
    import instaloader
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    if not post.is_video:
        raise RuntimeError("that Instagram post isn't a video")
    path = dest / f"ig_{shortcode}.mp4"
    with requests.get(post.video_url, stream=True, timeout=120) as r:
        check(r)
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path, post.caption or ""


# --------------------------------------------------------------------------- #
# Posting: Instagram (Graph API, Reels, resumable upload)
# --------------------------------------------------------------------------- #
def post_instagram(s, video, caption):
    if not (s["ig_user_id"] and s["ig_access_token"]):
        raise RuntimeError("Instagram isn't connected (Settings → Instagram)")
    v, tok, uid = s["ig_graph_version"], s["ig_access_token"], s["ig_user_id"]
    graph = f"https://graph.facebook.com/{v}"
    container = check(requests.post(f"{graph}/{uid}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption,
        "share_to_feed": "true", "access_token": tok}, timeout=60)).json()["id"]
    size = video.stat().st_size
    with open(video, "rb") as f:
        check(requests.post(f"https://rupload.facebook.com/ig-api-upload/{v}/{container}",
                            headers={"Authorization": f"OAuth {tok}", "offset": "0",
                                     "file_size": str(size)}, data=f, timeout=900))
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


# --------------------------------------------------------------------------- #
# Posting: TikTok (Content Posting API)
# --------------------------------------------------------------------------- #
def tiktok_access_token(store):
    t = store.settings()["tiktok_tokens"]
    if not t.get("refresh_token"):
        raise RuntimeError("TikTok isn't connected (Settings → Connect TikTok)")
    if time.time() > t.get("expires_at", 0) - 300:
        d = check(requests.post(f"{TT_API}/oauth/token/", data={
            "client_key": os.environ.get("TIKTOK_CLIENT_KEY", ""),
            "client_secret": os.environ.get("TIKTOK_CLIENT_SECRET", ""),
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)).json()
        if "access_token" not in d:
            raise RuntimeError(f"TikTok login expired, reconnect it in Settings ({d.get('error')})")
        t = {"access_token": d["access_token"], "refresh_token": d["refresh_token"],
             "expires_at": time.time() + d["expires_in"], "open_id": d.get("open_id")}
        store.set_settings({"tiktok_tokens": t})
    return t["access_token"]


def post_tiktok(store, s, video, caption):
    h = {"Authorization": f"Bearer {tiktok_access_token(store)}",
         "Content-Type": "application/json; charset=UTF-8"}
    size = video.stat().st_size
    if size <= 64 * 1024 * 1024:
        chunk, count = size, 1
    else:
        chunk = 10 * 1024 * 1024
        count = size // chunk
    source = {"source": "FILE_UPLOAD", "video_size": size,
              "chunk_size": chunk, "total_chunk_count": count}

    if s["tiktok_mode"] == "inbox":
        url, body = f"{TT_API}/post/publish/inbox/video/init/", {"source_info": source}
    else:
        info = check(requests.post(f"{TT_API}/post/publish/creator_info/query/",
                                   headers=h, timeout=30)).json().get("data", {})
        privacy = s["tiktok_privacy"]
        options = info.get("privacy_level_options") or []
        if options and privacy not in options:
            privacy = options[0]
        url = f"{TT_API}/post/publish/video/init/"
        body = {"source_info": source, "post_info": {
            "title": caption, "privacy_level": privacy,
            "disable_comment": False, "disable_duet": False, "disable_stitch": False}}

    d = check(requests.post(url, headers=h, json=body, timeout=60)).json()
    if (d.get("error") or {}).get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok: {d['error'].get('message') or d['error']}")
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

    for _ in range(60):
        st = check(requests.post(f"{TT_API}/post/publish/status/fetch/", headers=h,
                                 json={"publish_id": publish_id}, timeout=30)).json().get("data", {})
        if st.get("status") in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return publish_id
        if st.get("status") == "FAILED":
            raise RuntimeError(f"TikTok rejected it: {st.get('fail_reason')}")
        time.sleep(10)
    return publish_id  # still processing on TikTok's side; it finishes on its own


# --------------------------------------------------------------------------- #
# Captions
# --------------------------------------------------------------------------- #
def build_caption(s, creator, original, target, url):
    handle = (creator.get(target) or creator.get(creator["pull_from"]) or "").lstrip("@").strip()
    credit = s["credit_format"].replace("{handle}", handle)
    tmpl = s["caption_template"]

    def fill(cap):
        return (tmpl.replace("{caption}", cap).replace("{credit}", credit)
                .replace("{name}", creator.get("name") or handle).replace("{url}", url).strip())

    text = fill(original.strip())
    if len(text) > CAPTION_LIMIT:  # trim their caption, never the credit
        over = len(text) - CAPTION_LIMIT + 1
        text = fill(original.strip()[:max(0, len(original.strip()) - over)] + "…")
    return text[:CAPTION_LIMIT]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, store, data_dir):
        self.store = store
        self.dest = Path(data_dir) / "downloads"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.wake = threading.Event()
        self.force_scan = False
        self.status = "Starting"

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def check_now(self):
        self.force_scan = True
        self.wake.set()

    def poke(self):
        self.wake.set()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                traceback.print_exc()
                self.store.log("error", f"Worker error: {short(e)}")
            self.status = "Paused" if self.store.settings()["paused"] else "Idle"
            self.wake.wait(30)
            self.wake.clear()

    def tick(self):
        s = self.store.settings()
        if s["paused"] and not self.force_scan:
            return
        if self.force_scan or time.time() >= s["last_scan_at"] + s["check_every_minutes"] * 60:
            self.force_scan = False
            self.status = "Checking creators"
            self.scan(s)
            self.store.set_settings({"last_scan_at": time.time()})
        if s["paused"] or time.time() < s["next_post_at"]:
            return
        rows = self.store.q("SELECT * FROM posts WHERE status IN ('queued','partial') "
                            "ORDER BY manual DESC, found_at, rowid LIMIT 1")
        if rows:
            self.status = "Posting"
            posted = self.process(s, rows[0])
            # Space out real posts; after a failure, just wait a couple of minutes before trying the next one
            wait = s["minutes_between_posts"] * 60 if posted else 120
            self.store.set_settings({"next_post_at": time.time() + wait})

    def scan(self, s):
        loader = None
        for cr in self.store.q("SELECT * FROM creators WHERE enabled=1"):
            src = cr["pull_from"]
            handle = (cr.get(src) or "").lstrip("@").strip()
            if not handle:
                continue
            try:
                if src == "tiktok":
                    posts = list(fetch_tiktok(handle, s["scan_depth"]))
                else:
                    loader = loader or make_ig_loader(s)
                    posts = list(fetch_instagram(loader, handle, s["scan_depth"]))
            except Exception as e:
                self.store.log("warn", f"Couldn't check @{handle} on {src}: {short(e)}")
                continue
            first = not cr["seeded"]
            new = 0
            for i, p in enumerate(posts):  # newest first
                status = "queued" if (not first or i < s["first_run_backfill"]) else "skipped"
                if self.store.add_post(src, p, cr["id"], status) and status == "queued":
                    new += 1
            if first:
                self.store.x("UPDATE creators SET seeded=1 WHERE id=?", (cr["id"],))
                self.store.log("info", f"Now watching @{handle} on {src}. Only new videos will post.")
            if new:
                self.store.log("info", f"Found {new} new video(s) from @{handle}")

    def process(self, s, row):
        src, pid = row["source"], row["post_id"]
        cr = self.store.q("SELECT * FROM creators WHERE id=?", (row["creator_id"],))
        if not cr:
            self.store.update_post(src, pid, status="skipped", error="creator was deleted")
            return False
        cr = cr[0]
        targets = [t for t in ("instagram", "tiktok") if s[f"post_to_{t}"]]
        if not targets:
            self.store.log("warn", "Nothing is turned on under 'Post to' in Settings")
            return False

        try:
            path = Path(row["file"]) if row["file"] else None
            caption = row["caption"] or ""
            if not path or not path.exists():
                key = re.sub(r"[^A-Za-z0-9_-]", "", pid)
                if src == "tiktok":
                    path, caption = download_tiktok(row["url"], self.dest, key)
                else:
                    path, caption = download_instagram(make_ig_loader(s), pid, self.dest)
                self.store.update_post(src, pid, file=str(path), caption=caption)
        except Exception as e:
            self._fail(row, f"download failed: {short(e)}", partial=False)
            return False

        done = {"instagram": row["ig_status"] == "done", "tiktok": row["tt_status"] == "done"}
        errors = []
        posted_any = False
        for target in targets:
            if done[target]:
                continue
            text = build_caption(s, cr, caption, target, row["url"])
            try:
                if target == "instagram":
                    post_instagram(s, path, text)
                    self.store.update_post(src, pid, ig_status="done")
                else:
                    post_tiktok(self.store, s, path, text)
                    self.store.update_post(src, pid, tt_status="done", tt_caption=text)
                done[target] = True
                where = "TikTok inbox" if target == "tiktok" and s["tiktok_mode"] == "inbox" else target.capitalize()
                self.store.log("info", f"Posted {cr['name']}'s video to {where}")
                posted_any = True
            except Exception as e:
                errors.append(f"{target}: {short(e)}")

        if all(done[t] for t in targets):
            self.store.update_post(src, pid, status="posted", posted_at=now(), error=None, file=None)
            path.unlink(missing_ok=True)
        else:
            self._fail(row, "; ".join(errors), partial=any(done.values()))
        return posted_any

    def _fail(self, row, msg, partial):
        attempts = (row["attempts"] or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            status = "failed"
            self.store.log("error", f"Gave up on {row['url']} after {attempts} tries: {msg}")
            if row.get("file"):
                Path(row["file"]).unlink(missing_ok=True)
        else:
            status = "partial" if partial else "queued"
            self.store.log("warn", f"Will retry {row['url']} ({attempts}/{MAX_ATTEMPTS}): {msg}")
        self.store.update_post(row["source"], row["post_id"], status=status,
                               attempts=attempts, error=msg[:1000])
