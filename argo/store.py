"""
Store SQLite: anagrafe+discovery delle scuole e findings deduplicati.

Eredita la logica di dedup via fingerprint (URL+contenuto) da
bandi_monitor/store.py di v1, cosi' i findings di Argo sono confrontabili con
quelli del path RSS. Due tabelle principali:
  - schools:  esito della discovery per scuola (url trasparenza, piattaforma).
  - findings: bandi candidati trovati dal crawl, dedup + data di prima vista.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS schools (
    code            TEXT PRIMARY KEY,
    name            TEXT,
    region          TEXT,
    province        TEXT,
    city            TEXT,
    site_url        TEXT,
    resolved_url    TEXT,
    platform        TEXT,
    trasparenza_url TEXT,
    reachable       INTEGER DEFAULT 0,
    last_discovery  TEXT,
    discovery_note  TEXT
);
CREATE INDEX IF NOT EXISTS idx_schools_region ON schools(region);
CREATE INDEX IF NOT EXISTS idx_schools_trasp  ON schools(trasparenza_url);

CREATE TABLE IF NOT EXISTS findings (
    fingerprint  TEXT PRIMARY KEY,
    school_code  TEXT NOT NULL,
    school_name  TEXT,
    province     TEXT,
    region       TEXT,
    title        TEXT,
    url          TEXT,
    category     TEXT,
    score        REAL,
    source_page  TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_first ON findings(first_seen);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    phase      TEXT,
    processed  INTEGER DEFAULT 0,
    hits       INTEGER DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(url: str, content: str) -> str:
    base = (url.strip().lower() + "|" + (content or "")).encode("utf-8", "ignore")
    return hashlib.sha256(base).hexdigest()[:32]


class Store:
    def __init__(self, path: str = "argo.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- discovery ---------------------------------------------------------
    def upsert_school_meta(self, code, name, region, province, city, site_url):
        self.conn.execute(
            """INSERT INTO schools (code, name, region, province, city, site_url)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, region=excluded.region,
                 province=excluded.province, city=excluded.city,
                 site_url=excluded.site_url""",
            (code, name, region, province, city, site_url),
        )

    def update_discovery(self, code, resolved_url, platform, trasparenza_url,
                         reachable, note):
        self.conn.execute(
            """UPDATE schools SET resolved_url=?, platform=?, trasparenza_url=?,
               reachable=?, last_discovery=?, discovery_note=? WHERE code=?""",
            (resolved_url, platform, trasparenza_url, 1 if reachable else 0,
             _now(), note, code),
        )

    def schools_to_discover(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT code, site_url FROM schools WHERE site_url != ''"
        ).fetchall()

    def schools_with_trasparenza(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT code, name, region, province, trasparenza_url
               FROM schools WHERE trasparenza_url != ''"""
        ).fetchall()

    # --- findings ----------------------------------------------------------
    def record_finding(self, school: sqlite3.Row, title, url, category, score,
                       source_page) -> bool:
        """Inserisce/aggiorna. True se NOVITA' (mai vista prima)."""
        fp = fingerprint(url, title)
        now = _now()
        existing = self.conn.execute(
            "SELECT 1 FROM findings WHERE fingerprint=?", (fp,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE findings SET last_seen=? WHERE fingerprint=?", (now, fp)
            )
            return False
        self.conn.execute(
            """INSERT INTO findings (fingerprint, school_code, school_name,
               province, region, title, url, category, score, source_page,
               first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fp, school["code"], school["name"], school["province"],
             school["region"], title, url, category, score, source_page,
             now, now),
        )
        return True

    def all_findings(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM findings ORDER BY first_seen DESC"
        ).fetchall()

    # --- runs --------------------------------------------------------------
    def log_run(self, phase: str, processed: int, hits: int) -> None:
        self.conn.execute(
            "INSERT INTO runs (started_at, phase, processed, hits) VALUES (?,?,?,?)",
            (_now(), phase, processed, hits),
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
