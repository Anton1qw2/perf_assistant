import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager

from .config import LEASE_SECONDS
from .contracts import Settings, TaskSpec


def uid():
    return str(uuid.uuid4())


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


class Conflict(Exception):
    pass


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "assistant.sqlite3"
        with closing(self.connect()) as initial:
            initial.execute("PRAGMA journal_mode=WAL")
        with self.tx() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("unsupported_database_version")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, task TEXT NOT NULL,
                    settings TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    conversation_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
                    classification TEXT NOT NULL, response_to TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    snapshot TEXT NOT NULL, settings TEXT NOT NULL, policy TEXT NOT NULL,
                    context TEXT NOT NULL, checkpoint TEXT NOT NULL, status TEXT NOT NULL,
                    active_seconds REAL NOT NULL DEFAULT 0, report TEXT,
                    failure TEXT, created REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(conversation_id)
                    WHERE status IN ('queued', 'running', 'waiting');
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL, owner TEXT,
                    lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, deadline REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    conversation_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(conversation_id,request_id));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS questions (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL,
                    answer TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scope TEXT NOT NULL,
                    metadata TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checks (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    source TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS context_ledger (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY, destination TEXT NOT NULL, repository TEXT,
                    ciphertext BLOB NOT NULL, retired INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS consultations (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, prompt TEXT NOT NULL,
                    answer TEXT, status TEXT NOT NULL, created REAL NOT NULL);
                PRAGMA user_version=1;
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextmanager
    def tx(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        with closing(self.connect()) as db:
            return [dict(x) for x in db.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def create_conversation(self, title="Новое исследование"):
        cid = uid()
        with self.tx() as db:
            db.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?)",
                (
                    cid,
                    title[:100],
                    TaskSpec().model_dump_json(),
                    Settings().model_dump_json(),
                    time.time(),
                ),
            )
            self.event(db, cid, "conversation.created", {})
        return cid

    @staticmethod
    def event(db, cid, kind, payload):
        db.execute(
            "INSERT INTO events(conversation_id,kind,payload,created) VALUES(?,?,?,?)",
            (cid, kind, dumps(payload), time.time()),
        )

    @staticmethod
    def message(db, cid, role, text, classification, response_to=None, mid=None):
        mid = mid or uid()
        db.execute(
            "INSERT INTO messages(id,conversation_id,role,text,classification,response_to,created) "
            "VALUES(?,?,?,?,?,?,?)",
            (mid, cid, role, text, classification, response_to, time.time()),
        )
        Store.event(db, cid, "message.created", {"id": mid})
        return mid

    @staticmethod
    def enqueue(db, cid, kind, payload, deadline=None):
        jid = uid()
        db.execute(
            "INSERT INTO jobs(id,conversation_id,kind,payload,status,created,deadline) "
            "VALUES(?,?,?,?,?,?,?)",
            (jid, cid, kind, dumps(payload), "queued", time.time(), deadline or time.time() + 120),
        )
        Store.event(db, cid, "job.queued", {"id": jid, "kind": kind})
        return jid

    @staticmethod
    def dedup(db, cid, request_id, body, operation):
        fingerprint = digest(body)
        row = db.execute(
            "SELECT * FROM requests WHERE conversation_id=? AND request_id=?", (cid, request_id)
        ).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise Conflict("request_id_reused")
            return json.loads(row["result"])
        result = operation()
        db.execute(
            "INSERT INTO requests VALUES(?,?,?,?)", (cid, request_id, fingerprint, dumps(result))
        )
        return result

    def claim(self, owner):
        now = time.time()
        with self.tx() as db:
            row = db.execute(
                """SELECT * FROM jobs j WHERE
                (status='queued' OR (status='running' AND lease_until<?))
                AND NOT EXISTS (SELECT 1 FROM jobs active WHERE
                    active.conversation_id=j.conversation_id AND active.id!=j.id
                    AND active.status='running' AND active.lease_until>=?)
                ORDER BY created, rowid LIMIT 1""",
                (now, now),
            ).fetchone()
            if not row:
                return None
            # Charge abandoned leases conservatively; a crash must not reset active budgets.
            if row["status"] == "running" and row["kind"] == "run":
                rid = json.loads(row["payload"])["run_id"]
                db.execute(
                    "UPDATE runs SET active_seconds=active_seconds+? WHERE id=?",
                    (LEASE_SECONDS, rid),
                )
            db.execute(
                "UPDATE jobs SET status='running',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                (owner, now + LEASE_SECONDS, row["id"]),
            )
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    @staticmethod
    def fence(db, job):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if (
            not row
            or row["status"] != "running"
            or row["owner"] != job["owner"]
            or row["lease_until"] <= time.time()
        ):
            raise Conflict("lease_lost")

    @contextmanager
    def owned(self, job):
        with self.tx() as db:
            self.fence(db, job)
            yield db
            row = db.execute(
                "SELECT owner,lease_until FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if row["owner"] != job["owner"] or row["lease_until"] <= time.time():
                raise Conflict("lease_lost")

    def snapshot(self, cid):
        # One consistent read transaction, including the event cursor.
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if not row:
                raise KeyError("conversation_not_found")
            result = dict(row)
            result["task"] = json.loads(row["task"])
            result["settings"] = json.loads(row["settings"])
            result["readiness"] = TaskSpec.model_validate(result["task"]).readiness()
            result["messages"] = [
                dict(x)
                for x in db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY seq", (cid,)
                )
            ]
            result["runs"] = []
            for run in db.execute(
                "SELECT * FROM runs WHERE conversation_id=? ORDER BY created DESC", (cid,)
            ):
                r = dict(run)
                for key in ("snapshot", "context", "checkpoint", "report"):
                    r[key] = json.loads(r[key]) if r[key] else None
                del r["settings"]
                del r["policy"]
                r["questions"] = [
                    dict(q)
                    for q in db.execute(
                        "SELECT * FROM questions WHERE run_id=? ORDER BY created", (r["id"],)
                    )
                ]
                for q in r["questions"]:
                    q["payload"] = json.loads(q["payload"])
                result["runs"].append(r)
            result["checks"] = {}
            for c in db.execute(
                "SELECT * FROM checks WHERE conversation_id=? ORDER BY rowid", (cid,)
            ):
                check = json.loads(c["payload"])
                old = result["checks"].get(c["source"])
                if not old or check["revision"] >= old["revision"]:
                    check["stale"] = (
                        check["revision"] != result["settings"]["revision"]
                        or check["expires_at"] < time.time()
                    )
                    result["checks"][c["source"]] = check
            result["jobs"] = [
                dict(x)
                for x in db.execute(
                    "SELECT id,kind,status,payload FROM jobs WHERE conversation_id=? AND status IN ('queued','running')",
                    (cid,),
                )
            ]
            for job in result["jobs"]:
                payload = json.loads(job.pop("payload"))
                if job["kind"] == "diagnostics":
                    job["sources"] = payload["sources"]
            result["event_id"] = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM events WHERE conversation_id=?", (cid,)
            ).fetchone()[0]
            return result
