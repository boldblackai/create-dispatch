"""Durable inbox on a local filesystem. Never automatically replay a started tool run."""
import json
import sqlite3
import time


class Inbox:
    def __init__(self, path):
        path.touch(mode=0o600, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
            session TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS progress (
            event_id TEXT PRIMARY KEY, started_at REAL NOT NULL,
            updated_at REAL NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL)""")
        self.db.execute("UPDATE events SET state='interrupted' WHERE state='running'")
        self.db.commit()

    def add(self, record, *, state="pending"):
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO events(id,session,payload,state) VALUES(?,?,?,?)",
            (record["id"], record["payload"]["agentSession"]["id"], json.dumps(record["payload"]), state))
        self.db.commit()
        return cursor.rowcount == 1

    def next(self, session):
        row = self.db.execute(
            "SELECT id,payload,state FROM events WHERE session=? AND state IN ('pending','interrupted') ORDER BY seq LIMIT 1",
            (session,)).fetchone()
        return (row[0], json.loads(row[1]), row[2]) if row else None

    def sessions(self):
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT session FROM events WHERE state IN ('pending','interrupted')")]

    def set_state(self, event_id, state):
        self.db.execute("UPDATE events SET state=? WHERE id=?", (state, event_id))
        self.db.commit()

    def stop(self, session):
        self.db.execute("UPDATE events SET state='done' WHERE session=? AND state='pending'", (session,))
        self.db.commit()

    def status_requests(self):
        return self.db.execute(
            "SELECT id,session FROM events WHERE state='status_pending' ORDER BY seq").fetchall()

    def record_progress(self, event_id, body, kind="thought"):
        now = time.time()
        self.db.execute("""INSERT INTO progress VALUES(?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                updated_at=excluded.updated_at, body=excluded.body, kind=excluded.kind""",
            (event_id, now, now, body, kind))
        self.db.commit()

    def snapshot(self, session):
        # Status-only events never get progress rows and cannot hide the last run.
        row = self.db.execute("""SELECT e.state,p.started_at,p.updated_at,p.body,p.kind
            FROM events e JOIN progress p ON p.event_id=e.id
            WHERE e.session=? ORDER BY e.seq DESC LIMIT 1""", (session,)).fetchone()
        pending = self.db.execute(
            "SELECT count(*) FROM events WHERE session=? AND state='pending'", (session,)).fetchone()[0]
        return row, pending
