"""SQLite storage: creators, posts, settings, and an activity log."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, pull_from TEXT, tiktok TEXT, instagram TEXT, permission TEXT,
    enabled INTEGER DEFAULT 1, seeded INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS posts(
    source TEXT, post_id TEXT, creator_id INTEGER, url TEXT, caption TEXT,
    file TEXT, status TEXT, ig_status TEXT, tt_status TEXT, tt_caption TEXT,
    attempts INTEGER DEFAULT 0, error TEXT, manual INTEGER DEFAULT 0,
    found_at TEXT, posted_at TEXT,
    PRIMARY KEY(source, post_id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, msg TEXT);
"""

DEFAULTS = {
    "paused": False,
    "check_every_minutes": 15,
    "minutes_between_posts": 20,
    "scan_depth": 10,
    "first_run_backfill": 0,
    "post_to_instagram": True,
    "post_to_tiktok": True,
    "caption_template": "{caption}\n\n🎥: {credit}",
    "credit_format": "@{handle}",
    # Instagram (your account, official Graph API)
    "ig_user_id": "",
    "ig_access_token": "",
    "ig_graph_version": "v21.0",
    # Instagram reading (optional login used to look at creators' profiles)
    "ig_reader_username": "",
    "ig_sessionid": "",
    # TikTok
    "tiktok_mode": "inbox",
    "tiktok_privacy": "PUBLIC_TO_EVERYONE",
    "tiktok_tokens": {},
    "tiktok_oauth_state": "",
    # internal timers
    "last_scan_at": 0,
    "next_post_at": 0,
}

# Never sent to the browser in full
SECRET_KEYS = {"ig_access_token", "ig_sessionid", "tiktok_tokens", "tiktok_oauth_state"}
INTERNAL_KEYS = {"tiktok_oauth_state"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        with self.lock:
            c = self._conn()
            c.executescript(SCHEMA)
            c.commit()
            c.close()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def q(self, sql, args=()):
        with self.lock:
            c = self._conn()
            try:
                rows = [dict(r) for r in c.execute(sql, args).fetchall()]
                c.commit()
                return rows
            finally:
                c.close()

    def x(self, sql, args=()):
        """Execute; return (lastrowid, rowcount)."""
        with self.lock:
            c = self._conn()
            try:
                cur = c.execute(sql, args)
                c.commit()
                return cur.lastrowid, cur.rowcount
            finally:
                c.close()

    # ---- settings ----
    def settings(self):
        s = {k: (v.copy() if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
        for r in self.q("SELECT key, value FROM settings"):
            s[r["key"]] = json.loads(r["value"])
        return s

    def set_settings(self, values):
        for k, v in values.items():
            if k in DEFAULTS:
                self.x("INSERT INTO settings(key, value) VALUES(?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))

    # ---- activity log ----
    def log(self, level, msg):
        print(f"[{level}] {msg}", flush=True)
        self.x("INSERT INTO events(ts, level, msg) VALUES(?,?,?)", (now(), level, str(msg)[:1000]))
        self.x("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 300)")

    # ---- posts ----
    def add_post(self, source, post, creator_id, status, manual=False):
        _, n = self.x(
            "INSERT OR IGNORE INTO posts(source, post_id, creator_id, url, caption, status, manual, found_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (source, post["post_id"], creator_id, post["url"], post.get("caption", ""),
             status, int(manual), now()))
        return n > 0

    def update_post(self, source, post_id, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE posts SET {cols} WHERE source=? AND post_id=?",
               (*fields.values(), source, post_id))
