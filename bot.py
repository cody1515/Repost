"""The worker: at each account's posting times, picks a random creator and reposts their newest unposted video."""
import hashlib
import random
import re
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import requests

from store import now

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
def post_instagram(s, acc, video, caption):
    if not (acc["ig_user_id"] and acc["access_token"]):
        raise RuntimeError("this account isn't connected")
    v, tok, uid = s["ig_graph_version"], acc["access_token"], acc["ig_user_id"]
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
def build_caption(s, creator, original, url):
    handle = (creator.get("instagram") or creator.get(creator["pull_from"]) or "").lstrip("@").strip()
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
        self.wake = threading.Event()
        self.pending = []          # account ids queued by "Post now"
        self.status = "Starting"

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def post_now(self, account_id):
        if account_id not in self.pending:
            self.pending.append(account_id)
        self.wake.set()

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
        # 1) links you added by hand go first
        for row in self.store.q("SELECT * FROM reposts WHERE account_id=? AND status='queued' ORDER BY id",
                                (acc["id"],)):
            cr = self.store.q("SELECT * FROM creators WHERE id=?", (row["creator_id"],))
            if not cr:
                self.store.x("UPDATE reposts SET status='skipped', error='creator deleted' WHERE id=?", (row["id"],))
                continue
            if self.post_video(s, acc, cr[0], row):
                return True

        # 2) a random creator's newest video that hasn't been reposted yet
        creators = self.store.q("SELECT * FROM creators WHERE account_id=? AND enabled=1", (acc["id"],))
        if not creators:
            self.store.log("warn", f"{acc['name']}: no creators to pick from ({why} skipped)")
            return False
        random.shuffle(creators)
        tried = 0
        for cr in creators:
            cand = self.newest_unposted(s, acc, cr)
            if not cand:
                continue
            if self.post_video(s, acc, cr, cand):
                return True
            tried += 1
            if tried >= MAX_CREATORS_PER_SLOT:
                break
        self.store.log("warn", f"{acc['name']}: nothing could be posted for the {why}")
        return False

    def newest_unposted(self, s, acc, cr):
        src = cr["pull_from"]
        handle = (cr.get(src) or "").lstrip("@").strip()
        if not handle:
            return None
        try:
            if src == "tiktok":
                posts = list(fetch_tiktok(handle, s["scan_depth"]))
            else:
                posts = list(fetch_instagram(make_ig_loader(s), handle, s["scan_depth"]))
                # pinned posts come first on Instagram; sort by date so "newest" really is newest
                posts.sort(key=lambda p: p["date"], reverse=True)
        except Exception as e:
            self.store.log("warn", f"Couldn't load @{handle}'s {src}: {short(e)}")
            return None
        done = {(r["source"], r["post_id"]): r for r in self.store.q(
            "SELECT source, post_id, status, attempts FROM reposts WHERE account_id=?", (acc["id"],))}
        for p in posts:
            r = done.get((p["source"], p["post_id"]))
            if r is None or (r["status"] == "failed" and r["attempts"] < MAX_ATTEMPTS):
                return p
        return None

    def post_video(self, s, acc, cr, p):
        """p is a fetched post dict or an existing reposts row. Returns True if it posted."""
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
                path, caption = download_instagram(make_ig_loader(s), p["post_id"], work)
            media_id = post_instagram(s, acc, path, build_caption(s, cr, caption, p["url"]))
            self.store.x("UPDATE reposts SET status='posted', posted_at=?, error=NULL, ig_media_id=? WHERE id=?",
                         (now(), media_id, row["id"]))
            self.store.log("info", f"{acc['name']}: posted {cr['name']}'s video")
            return True
        except Exception as e:
            attempts = (row["attempts"] or 0) + 1
            self.store.x("UPDATE reposts SET status='failed', attempts=?, error=? WHERE id=?",
                         (attempts, short(e), row["id"]))
            self.store.log("error", f"{acc['name']}: couldn't post {cr['name']}'s video ({short(e)})")
            return False
        finally:
            for f in work.glob("*"):
                f.unlink(missing_ok=True)
            work.rmdir()
