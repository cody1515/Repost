"""Web server: the phone/web app + JSON API + TikTok login. Starts the background worker."""
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

import bot
from store import DEFAULTS, INTERNAL_KEYS, SECRET_KEYS, Store, now

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET = (os.environ.get("SESSION_SECRET") or hashlib.sha256(("rp:" + PASSWORD).encode()).hexdigest()).encode()
SESSION_DAYS = 60

store = Store(DATA_DIR / "repost.db")
engine = bot.Engine(store, DATA_DIR)


def public_url():
    if os.environ.get("PUBLIC_URL"):
        return os.environ["PUBLIC_URL"].rstrip("/")
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        return "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
    return f"http://localhost:{os.environ.get('PORT', '8080')}"


def make_session():
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return exp + "." + hmac.new(SECRET, exp.encode(), "sha256").hexdigest()


def valid_session(tok):
    try:
        exp, sig = tok.split(".", 1)
        good = hmac.new(SECRET, exp.encode(), "sha256").hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False


def public_settings():
    s = store.settings()
    out = {}
    for k, v in s.items():
        if k in INTERNAL_KEYS:
            continue
        if k in SECRET_KEYS:
            out[k] = bool(v)  # only say whether it's set
        else:
            out[k] = v
    out["tiktok_connected"] = bool(s["tiktok_tokens"].get("refresh_token"))
    out["tiktok_app_configured"] = bool(os.environ.get("TIKTOK_CLIENT_KEY"))
    return out


INT_FIELDS = {"check_every_minutes": (5, 1440), "minutes_between_posts": (0, 1440),
              "scan_depth": (1, 50), "first_run_backfill": (0, 20)}
BOOL_FIELDS = {"paused", "post_to_instagram", "post_to_tiktok"}
TEXT_FIELDS = {"caption_template", "credit_format", "ig_user_id", "ig_access_token",
               "ig_graph_version", "ig_reader_username", "ig_sessionid", "tiktok_mode", "tiktok_privacy"}


class Handler(BaseHTTPRequestHandler):
    server_version = "RepostApp"

    def log_message(self, fmt, *args):
        pass  # keep logs quiet; activity goes to the app's log

    # ---- helpers ----
    def send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, to):
        self.send(302, b"", "text/plain", {"Location": to})

    def authed(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "session" in c and valid_session(c["session"].value)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def cookie(self, value, max_age):
        secure = "; Secure" if public_url().startswith("https") else ""
        return f"session={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    # ---- routes ----
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(path.query)
        p = path.path
        if p == "/health":
            return self.send(200, {"ok": True})
        if p in ("/", "/index.html"):
            return self.send(200, (BASE / "static/index.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/manifest.json":
            return self.send(200, (BASE / "static/manifest.json").read_bytes(), "application/manifest+json")
        if p == "/icon.svg":
            return self.send(200, (BASE / "static/icon.svg").read_bytes(), "image/svg+xml")
        if not self.authed():
            return self.send(401, {"error": "login"}) if p.startswith("/api/") else self.redirect("/")

        if p == "/api/state":
            return self.send(200, self.state())
        if p == "/tiktok/connect":
            key = os.environ.get("TIKTOK_CLIENT_KEY")
            if not key:
                return self.send(400, "Add TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in Railway first.", "text/plain")
            state = secrets.token_urlsafe(16)
            store.set_settings({"tiktok_oauth_state": state})
            return self.redirect("https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
                "client_key": key, "response_type": "code",
                "scope": "user.info.basic,video.upload,video.publish",
                "redirect_uri": public_url() + "/tiktok/callback", "state": state}))
        if p == "/tiktok/callback":
            return self.tiktok_callback(qs)
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        try:
            data = self.body()
        except Exception:
            return self.send(400, {"error": "bad request"})

        if p == "/api/login":
            if PASSWORD and hmac.compare_digest(str(data.get("password", "")), PASSWORD):
                return self.send(200, {"ok": True}, headers={"Set-Cookie": self.cookie(make_session(), SESSION_DAYS * 86400)})
            time.sleep(1.5)
            msg = "Wrong password" if PASSWORD else "Set APP_PASSWORD in Railway first"
            return self.send(401, {"error": msg})
        if not self.authed():
            return self.send(401, {"error": "login"})
        if p == "/api/logout":
            return self.send(200, {"ok": True}, headers={"Set-Cookie": self.cookie("", 0)})

        try:
            result = self.api(p, data)
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        if result is None:
            return self.send(404, {"error": "not found"})
        return self.send(200, result)

    # ---- API ----
    def api(self, p, d):
        if p == "/api/settings":
            clean = {}
            for k, v in d.items():
                if k in BOOL_FIELDS:
                    clean[k] = bool(v)
                elif k in INT_FIELDS:
                    lo, hi = INT_FIELDS[k]
                    clean[k] = max(lo, min(hi, int(v)))
                elif k in TEXT_FIELDS:
                    v = str(v).strip() if k not in ("caption_template",) else str(v)
                    if k in SECRET_KEYS and not v:
                        continue  # blank = keep existing secret
                    clean[k] = v
            if "tiktok_mode" in clean and clean["tiktok_mode"] not in ("inbox", "direct"):
                raise ValueError("bad TikTok mode")
            if "credit_format" in clean and "{handle}" not in clean["credit_format"]:
                raise ValueError("Credit format needs {handle} in it")
            store.set_settings(clean)
            if clean.get("paused") is False:
                engine.poke()
            return {"ok": True}

        if p == "/api/settings/clear":
            key = d.get("key")
            if key in ("ig_access_token", "ig_sessionid", "tiktok_tokens"):
                store.set_settings({key: DEFAULTS[key]})
            return {"ok": True}

        if p == "/api/creators/save":
            src = d.get("pull_from")
            if src not in ("tiktok", "instagram"):
                raise ValueError("Pick where to pull from")
            tt = (d.get("tiktok") or "").strip().lstrip("@")
            ig = (d.get("instagram") or "").strip().lstrip("@")
            if not (tt if src == "tiktok" else ig):
                raise ValueError(f"Add their {'TikTok' if src == 'tiktok' else 'Instagram'} handle")
            name = (d.get("name") or tt or ig).strip()
            fields = (name, src, tt, ig, (d.get("permission") or "").strip(), int(bool(d.get("enabled", True))))
            if d.get("id"):
                old = store.q("SELECT * FROM creators WHERE id=?", (d["id"],))
                if not old:
                    raise ValueError("creator not found")
                old = old[0]
                reseed = old["pull_from"] != src or (old["tiktok"] if src == "tiktok" else old["instagram"]) != (tt if src == "tiktok" else ig)
                store.x("UPDATE creators SET name=?, pull_from=?, tiktok=?, instagram=?, permission=?, enabled=?"
                        + (", seeded=0" if reseed else "") + " WHERE id=?", (*fields, d["id"]))
            else:
                store.x("INSERT INTO creators(name, pull_from, tiktok, instagram, permission, enabled, created_at)"
                        " VALUES (?,?,?,?,?,?,?)", (*fields, now()))
                store.log("info", f"Added creator {name}")
                engine.check_now()
            return {"ok": True}

        if p == "/api/creators/delete":
            store.x("DELETE FROM creators WHERE id=?", (d.get("id"),))
            store.x("UPDATE posts SET status='skipped', error='creator deleted' "
                    "WHERE creator_id=? AND status IN ('queued','partial')", (d.get("id"),))
            return {"ok": True}

        if p == "/api/queue":
            src, pid, url = bot.parse_link(str(d.get("url", "")))
            if not store.q("SELECT 1 FROM creators WHERE id=?", (d.get("creator_id"),)):
                raise ValueError("Pick which creator it's from (for the credit)")
            if not store.add_post(src, {"post_id": pid, "url": url}, d["creator_id"], "queued", manual=True):
                existing = store.q("SELECT status FROM posts WHERE source=? AND post_id=?", (src, pid))[0]
                if existing["status"] == "posted":
                    raise ValueError("That video was already posted")
                store.update_post(src, pid, status="queued", attempts=0, error=None, manual=1)
            store.log("info", "Link added to the queue")
            engine.poke()
            return {"ok": True}

        if p in ("/api/posts/retry", "/api/posts/skip"):
            key = (d.get("source"), d.get("post_id"))
            if p.endswith("retry"):
                store.update_post(*key, status="queued", attempts=0, error=None)
                engine.poke()
            else:
                store.update_post(*key, status="skipped")
            return {"ok": True}

        if p == "/api/check-now":
            engine.check_now()
            return {"ok": True}

        if p == "/api/post-now":
            store.set_settings({"next_post_at": 0})
            engine.poke()
            return {"ok": True}
        return None

    def state(self):
        s = store.settings()
        creators = store.q("SELECT c.*, (SELECT COUNT(*) FROM posts p WHERE p.creator_id=c.id AND p.status='posted') AS posted "
                           "FROM creators c ORDER BY c.name COLLATE NOCASE")
        posts = store.q("SELECT p.source, p.post_id, p.url, p.status, p.ig_status, p.tt_status, p.tt_caption, "
                        "p.attempts, p.error, p.found_at, p.posted_at, p.manual, c.name AS creator "
                        "FROM posts p LEFT JOIN creators c ON c.id=p.creator_id "
                        "WHERE p.status != 'skipped' ORDER BY COALESCE(p.posted_at, p.found_at) DESC LIMIT 60")
        counts = {r["status"]: r["n"] for r in store.q("SELECT status, COUNT(*) n FROM posts GROUP BY status")}
        today = store.q("SELECT COUNT(*) n FROM posts WHERE status='posted' AND posted_at >= ?",
                        (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400)),))[0]["n"]
        return {
            "status": engine.status,
            "settings": public_settings(),
            "creators": creators,
            "posts": posts,
            "events": store.q("SELECT ts, level, msg FROM events ORDER BY id DESC LIMIT 40"),
            "counts": {"queued": counts.get("queued", 0) + counts.get("partial", 0),
                       "posted": counts.get("posted", 0), "failed": counts.get("failed", 0),
                       "last24h": today},
            "next_scan_at": s["last_scan_at"] + s["check_every_minutes"] * 60,
            "next_post_at": s["next_post_at"],
            "server_time": time.time(),
            "callback_url": public_url() + "/tiktok/callback",
        }

    def tiktok_callback(self, qs):
        s = store.settings()
        state = qs.get("state", [""])[0]
        if not s["tiktok_oauth_state"] or not hmac.compare_digest(state, s["tiktok_oauth_state"]):
            return self.send(400, "This TikTok login link expired. Go back and tap Connect TikTok again.", "text/plain")
        if "code" not in qs:
            return self.redirect("/#settings")
        r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": os.environ.get("TIKTOK_CLIENT_KEY", ""),
            "client_secret": os.environ.get("TIKTOK_CLIENT_SECRET", ""),
            "code": qs["code"][0], "grant_type": "authorization_code",
            "redirect_uri": public_url() + "/tiktok/callback"},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        d = r.json() if r.content else {}
        if "access_token" not in d:
            store.log("error", f"TikTok connect failed: {d.get('error_description') or d}")
            return self.redirect("/#settings")
        store.set_settings({"tiktok_oauth_state": "", "tiktok_tokens": {
            "access_token": d["access_token"], "refresh_token": d["refresh_token"],
            "expires_at": time.time() + d["expires_in"], "open_id": d.get("open_id")}})
        store.log("info", "TikTok connected")
        return self.redirect("/#settings")


def main():
    if not PASSWORD:
        print("WARNING: APP_PASSWORD isn't set, so nobody can log in until you add it.", flush=True)
    engine.start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"Repost app running on port {port} ({public_url()})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
