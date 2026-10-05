"""Single-container durable storage. Credentials never leave the backend."""
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "videos").mkdir(exist_ok=True)
        key_file = self.root / "encryption.key"
        if not key_file.exists():
            with key_file.open("xb") as f:
                f.write(Fernet.generate_key())
            key_file.chmod(0o600)
        self.cipher = Fernet(key_file.read_bytes())
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "relay.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS oauth (state TEXT PRIMARY KEY, session TEXT NOT NULL,
                platform TEXT NOT NULL, expires REAL NOT NULL, config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                created REAL NOT NULL, filename TEXT NOT NULL, caption TEXT NOT NULL,
                path TEXT NOT NULL, size INTEGER NOT NULL, meta TEXT NOT NULL, options TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS targets (job_id TEXT NOT NULL REFERENCES jobs(id),
                platform TEXT NOT NULL, status TEXT NOT NULL, remote_id TEXT,
                result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                next_check REAL NOT NULL DEFAULT 0, account TEXT NOT NULL,
                PRIMARY KEY(job_id, platform));
        """)
        self.db.commit()

    def execute(self, sql, args=()):
        with self.lock:
            cursor = self.db.execute(sql, args)
            self.db.commit()
            return cursor.rowcount

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def get(self, key, default=None):
        rows = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(self.cipher.decrypt(rows[0]["value"].encode())) if rows else default

    def set(self, key, value):
        sealed = self.cipher.encrypt(json.dumps(value).encode()).decode()
        self.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, sealed))

    def new_session(self):
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        self.execute("INSERT INTO sessions VALUES (?,?)", (digest, time.time() + 86400 * 7))
        return token

    def session(self, token):
        digest = hashlib.sha256((token or "").encode()).hexdigest()
        return digest if self.rows("SELECT hash FROM sessions WHERE hash=? AND expires>?", (digest, time.time())) else None

    def create_job(self, job, platforms, accounts):
        with self.lock, self.db:
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)", (
                job["id"], job["request_id"], time.time(), job["filename"], job["caption"],
                job["path"], job["size"], json.dumps(job["meta"]), json.dumps(job["options"]),
            ))
            for p in platforms:
                self.db.execute("INSERT INTO targets(job_id,platform,status,account) VALUES (?,?,?,?)",
                                (job["id"], p, "queued", accounts[p]))

    def job(self, job_id):
        rows = self.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            return None
        job = rows[0]
        job["meta"] = json.loads(job["meta"])
        job["options"] = json.loads(job["options"])
        job["targets"] = self.rows("SELECT * FROM targets WHERE job_id=?", (job_id,))
        for target in job["targets"]:
            target["result"] = json.loads(target["result"])
        return job

    def update_target(self, job_id, platform, **fields):
        allowed = {"status", "remote_id", "result", "error", "next_check"}
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid target update")
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"])
        self.execute("UPDATE targets SET " + ",".join(k + "=?" for k in fields) + " WHERE job_id=? AND platform=?",
                     (*fields.values(), job_id, platform))

    def recover(self):
        # A remote write may have happened before shutdown. Never blindly post again.
        self.execute("UPDATE targets SET status='attention', error=? WHERE status IN ('uploading','publishing')",
                     ("Interrupted during upload. Check the platform before creating another post. Use Check status if available.",))

    def close(self):
        self.db.close()
