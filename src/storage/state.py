"""Transactional slot claims and append-preserving attempt reservations."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from datetime import time, timedelta
from zoneinfo import ZoneInfo


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO meta VALUES(1);
            CREATE TABLE IF NOT EXISTS slots(
                study TEXT, registry TEXT, scheduled TEXT, snapshot TEXT, status TEXT,
                PRIMARY KEY(study,registry,scheduled));
            CREATE TABLE IF NOT EXISTS attempts(
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL, provider TEXT NOT NULL,
                snapshot TEXT NOT NULL, resource TEXT NOT NULL, number INTEGER NOT NULL,
                reserved_at TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT 'reserved',
                UNIQUE(snapshot,resource,number));
            CREATE TABLE IF NOT EXISTS provider_stops(
                scope TEXT, provider TEXT, reason TEXT, PRIMARY KEY(scope,provider));
            CREATE TABLE IF NOT EXISTS etl_jobs(
                job_key TEXT PRIMARY KEY, study TEXT, registry TEXT, scheduled TEXT,
                snapshot TEXT, status TEXT, result_path TEXT, error TEXT, updated_at TEXT);
            """)
            if db.execute("SELECT version FROM meta").fetchone()[0] != 1:
                raise ValueError("Unsupported state schema")
            columns={r[1] for r in db.execute('PRAGMA table_info(provider_stops)')}
            for name in ('stopped_at','expires_at'):
                if name not in columns:db.execute(f'ALTER TABLE provider_stops ADD COLUMN {name} TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS attempts_provider_time ON attempts(provider,reserved_at)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(
        self, study: str, registry: str, scheduled: str, snapshot: str, status: str = "collecting"
    ) -> bool:
        """Atomically claim natural slot key; never create a second snapshot."""
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO slots VALUES(?,?,?,?,?)",
                (study, registry, scheduled, snapshot, status),
            )
            return cursor.rowcount == 1

    def reserve(
        self,
        scope: str,
        provider: str,
        snapshot: str,
        resource: str,
        number: int,
        cap: int | None,
        remaining: int | None,
        *, daily_cap: int = 200, accounting_timezone: str = 'Asia/Ho_Chi_Minh', reserved_at=None, clock=None,
    ) -> int:
        """Reserve before HTTP; crashes retain debit. No transaction spans network."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            instant=clock() if clock else reserved_at or datetime.now(timezone.utc)
            if instant.tzinfo is None:raise ValueError('Reservation timestamp must be timezone-aware')
            instant=instant.astimezone(timezone.utc)
            if self._stop_reason(db,provider,instant,accounting_timezone):
                raise RuntimeError("PROVIDER_STOPPED")
            if self._daily_count(db,provider,instant.astimezone(ZoneInfo(accounting_timezone)).date(),accounting_timezone)>=daily_cap:
                raise RuntimeError('DAILY_ATTEMPT_CAP_EXHAUSTED')
            count = db.execute(
                "SELECT count(*) FROM attempts WHERE provider=?", (provider,)
            ).fetchone()[0]
            effective = (
                min(x for x in (cap, remaining) if x is not None)
                if any(x is not None for x in (cap, remaining))
                else None
            )
            if effective is not None and count >= effective:
                raise RuntimeError("ATTEMPT_BUDGET_EXHAUSTED")
            cur = db.execute(
                "INSERT INTO attempts(scope,provider,snapshot,resource,number,reserved_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    scope,
                    provider,
                    snapshot,
                    resource,
                    number,
                    instant.isoformat(),
                ),
            )
            return cur.lastrowid

    def finish(self, reservation: int, outcome: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE attempts SET outcome=? WHERE id=?", (outcome, reservation))

    def stop(self, scope: str, provider: str, reason: str, *, stopped_at=None, expires_at=None) -> None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT reason FROM provider_stops WHERE scope=? AND provider=?',(scope,provider)).fetchone()
            if existing and existing['reason'] in ('AUTH_FAILURE','QUOTA_EXHAUSTED') and reason not in ('AUTH_FAILURE','QUOTA_EXHAUSTED'):
                return
            db.execute(
                "INSERT OR REPLACE INTO provider_stops(scope,provider,reason,stopped_at,expires_at) VALUES(?,?,?,?,?)",
                (scope,provider,reason,(stopped_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),expires_at.astimezone(timezone.utc).isoformat() if expires_at else None)
            )

    @staticmethod
    def _daily_count(db,provider,day,accounting_timezone):
        zone=ZoneInfo(accounting_timezone)
        start=datetime.combine(day,time.min,zone).astimezone(timezone.utc)
        end=datetime.combine(day+timedelta(days=1),time.min,zone).astimezone(timezone.utc)
        # Existing timestamps are UTC ISO strings. Whole-second UTC midnight
        # boundaries preserve fractional attempts and accept both T/space and Z/+00.
        return db.execute("SELECT count(*) FROM attempts WHERE provider=? AND substr(replace(reserved_at,'T',' '),1,19)>=? AND substr(replace(reserved_at,'T',' '),1,19)<?",
            (provider,start.strftime('%Y-%m-%d %H:%M:%S'),end.strftime('%Y-%m-%d %H:%M:%S'))).fetchone()[0]

    def daily_counts(self,instant=None,accounting_timezone='Asia/Ho_Chi_Minh',*,day=None):
        instant=instant or datetime.now(timezone.utc)
        if instant.tzinfo is None:raise ValueError('Accounting timestamp must be timezone-aware')
        day=day or instant.astimezone(ZoneInfo(accounting_timezone)).date()
        with self.connect() as db:
            return {p:self._daily_count(db,p,day,accounting_timezone) for p in ('traffic','weather')}

    @staticmethod
    def _stop_reason(db,provider,instant,accounting_timezone):
        for row in db.execute('SELECT reason,stopped_at,expires_at FROM provider_stops WHERE provider=?',(provider,)):
            if row['expires_at'] and instant>=datetime.fromisoformat(row['expires_at']):continue
            if row['reason'] in ('DAILY_ATTEMPT_CAP_EXHAUSTED','APPLICATION_DAILY_CAP') and row['stopped_at']:
                zone=ZoneInfo(accounting_timezone)
                if datetime.fromisoformat(row['stopped_at']).astimezone(zone).date()!=instant.astimezone(zone).date():continue
            return row['reason']
        return None

    def active_stop(self,provider,instant=None,accounting_timezone='Asia/Ho_Chi_Minh'):
        with self.connect() as db:return self._stop_reason(db,provider,instant or datetime.now(timezone.utc),accounting_timezone)

    def counts(self, scope: str | None = None) -> dict:
        with self.connect() as db:
            return dict(
                db.execute(
                    "SELECT provider,count(*) FROM attempts "+("WHERE scope=? " if scope is not None else "")+"GROUP BY provider",
                    (scope,) if scope is not None else (),
                ).fetchall()
            )

    def job(self,key):
        with self.connect() as db:
            row=db.execute('SELECT * FROM etl_jobs WHERE job_key=?',(key,)).fetchone()
            return dict(row) if row else None

    def set_job(self,key,study,registry,scheduled,snapshot,status,*,result_path=None,error=None):
        with self.connect() as db:
            db.execute('INSERT INTO etl_jobs VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET status=excluded.status,result_path=excluded.result_path,error=excluded.error,updated_at=excluded.updated_at',
                (key,study,registry,scheduled,snapshot,status,result_path,error,datetime.now(timezone.utc).isoformat()))
