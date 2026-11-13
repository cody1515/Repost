"""SQLite storage: Instagram accounts, creators, reposts, settings, activity log."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, username TEXT, ig_user_id TEXT, access_token TEXT,
    times TEXT DEFAULT '[]', enabled INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS creators(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, pull_from TEXT, tiktok TEXT, instagram TEXT, permission TEXT,
    enabled INTEGER DEFAULT 1, seeded INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS reposts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER, creator_id INTEGER, source TEXT, post_id TEXT, url TEXT,
    status TEXT, manual INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, error TEXT,
    ig_media_id TEXT, created_at TEXT, posted_at TEXT,
    UNIQUE(account_id, source, post_id));
CREATE TABLE IF NOT EXISTS slots(account_id INTEGER, slot TEXT, PRIMARY KEY(account_id, slot));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, msg TEXT);
"""

DEFAULTS = {
    "paused": False,
    "timezone": "America/Los_Angeles",
    "scan_depth": 15,
    "caption_template": "{caption}\n\n🎥: {credit}",
    "credit_format": "@{handle}",
    "ig_graph_version": "v21.0",
    # optional login used only to READ creators' Instagram pages
    "ig_reader_username": "",
    "ig_sessionid": "",
}
SECRET_KEYS = {"ig_sessionid"}
DEFAULT_TIMES = ["12:00", "19:00"]


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
        self._migrate()

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
        with self.lock:
            c = self._conn()
            try:
                cur = c.execute(sql, args)
                c.commit()
                return cur.lastrowid, cur.rowcount
            finally:
                c.close()

    def _columns(self, table):
        return {r["name"] for r in self.q(f"PRAGMA table_info({table})")}

    def _migrate(self):
        """Bring data over from the single-account version of the app."""
        if "account_id" not in self._columns("creators"):
            self.x("ALTER TABLE creators ADD COLUMN account_id INTEGER")
        old = {r["key"]: json.loads(r["value"]) for r in self.q("SELECT key, value FROM settings")}
        if not self.q("SELECT 1 FROM accounts LIMIT 1") and old.get("ig_user_id") and old.get("ig_access_token"):
            acc, _ = self.x("INSERT INTO accounts(name, ig_user_id, access_token, times, created_at) VALUES (?,?,?,?,?)",
                            ("My Instagram", old["ig_user_id"], old["ig_access_token"],
                             json.dumps(DEFAULT_TIMES), now()))
            self.x("UPDATE creators SET account_id=? WHERE account_id IS NULL", (acc,))
            tables = {r["name"] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}
            if "posts" in tables:  # remember what was already posted so it isn't reposted
                self.x("INSERT OR IGNORE INTO reposts(account_id, creator_id, source, post_id, url, status, created_at, posted_at) "
                       "SELECT ?, creator_id, source, post_id, url, 'posted', found_at, posted_at FROM posts WHERE status='posted'",
                       (acc,))
            self.log("info", "Moved your Instagram connection into Accounts. Set its posting times there.")
        for k in ("ig_user_id", "ig_access_token", "tiktok_tokens", "tiktok_oauth_state"):
            self.x("DELETE FROM settings WHERE key=?", (k,))

    # ---- settings ----
    def settings(self):
        s = dict(DEFAULTS)
        for r in self.q("SELECT key, value FROM settings"):
            if r["key"] in DEFAULTS:
                s[r["key"]] = json.loads(r["value"])
        return s

    def set_settings(self, values):
        for k, v in values.items():
            if k in DEFAULTS:
                self.x("INSERT INTO settings(key, value) VALUES(?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))

    # ---- accounts ----
    def accounts(self, enabled_only=False):
        rows = self.q("SELECT * FROM accounts" + (" WHERE enabled=1" if enabled_only else "") +
                      " ORDER BY name COLLATE NOCASE")
        for r in rows:
            r["times"] = json.loads(r["times"] or "[]")
        return rows

    def account(self, account_id):
        rows = self.q("SELECT * FROM accounts WHERE id=?", (account_id,))
        if rows:
            rows[0]["times"] = json.loads(rows[0]["times"] or "[]")
        return rows[0] if rows else None

    # ---- activity log ----
    def log(self, level, msg):
        print(f"[{level}] {msg}", flush=True)
        self.x("INSERT INTO events(ts, level, msg) VALUES(?,?,?)", (now(), level, str(msg)[:1000]))
        self.x("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 300)")
