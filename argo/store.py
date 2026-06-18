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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Parametri di query da SCARTARE nella normalizzazione URL: sessione e tracking.
# NON tocchiamo id/option/task & co., che identificano il documento specifico
# (es. index.php?option=...&id=... punta a un allegato preciso).
_NOISE_QS = {
    "sid", "sessid", "phpsessid", "jsessionid", "tag", "cerca", "cerca_sito",
    "search", "s", "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "fbclid", "gclid",
}


def normalize_url(url: str) -> str:
    """URL canonico per il dedup: niente fragment (#...), niente parametri di
    sessione/tracking, niente parametri vuoti, host lowercase, slash finale via.
    Cosi' la stessa pagina raggiunta con `?sid=...`/`#footer`/`?tag=...` non
    genera findings duplicati."""
    try:
        s = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    q = [(k, v) for k, v in parse_qsl(s.query)
         if v and k.lower() not in _NOISE_QS]
    return urlunsplit((s.scheme.lower(), s.netloc.lower(),
                       s.path.rstrip("/"), urlencode(sorted(q)), ""))

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
    discovery_note  TEXT,
    first_crawled   TEXT
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
    last_seen    TEXT NOT NULL,
    origin       TEXT DEFAULT 'nuovo',
    scadenza         TEXT,
    scadenza_checked TEXT,
    ai_checked    TEXT,
    ai_bando      INTEGER,
    titolo_pulito TEXT,
    profilo       TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_first  ON findings(first_seen);

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
    base = (normalize_url(url) + "|" + (content or "")).encode("utf-8", "ignore")
    return hashlib.sha256(base).hexdigest()[:32]


class Store:
    def __init__(self, path: str = "argo.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Aggiunge colonne nuove a DB pre-esistenti (CREATE IF NOT EXISTS non
        tocca tabelle gia' create). Idempotente."""
        cols = lambda t: {r[1] for r in self.conn.execute(
            f"PRAGMA table_info({t})")}
        if "first_crawled" not in cols("schools"):
            self.conn.execute("ALTER TABLE schools ADD COLUMN first_crawled TEXT")
        fcols = cols("findings")
        if "origin" not in fcols:
            self.conn.execute(
                "ALTER TABLE findings ADD COLUMN origin TEXT DEFAULT 'nuovo'")
        if "scadenza" not in fcols:
            self.conn.execute("ALTER TABLE findings ADD COLUMN scadenza TEXT")
        if "scadenza_checked" not in fcols:
            self.conn.execute(
                "ALTER TABLE findings ADD COLUMN scadenza_checked TEXT")
        for col, decl in (("ai_checked", "TEXT"), ("ai_bando", "INTEGER"),
                          ("titolo_pulito", "TEXT"), ("profilo", "TEXT")):
            if col not in fcols:
                self.conn.execute(f"ALTER TABLE findings ADD COLUMN {col} {decl}")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_findings_origin ON findings(origin)")

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
            """SELECT code, name, region, province, trasparenza_url, first_crawled
               FROM schools WHERE trasparenza_url != ''"""
        ).fetchall()

    def mark_crawled(self, code: str) -> None:
        """Segna la data del PRIMO crawl della scuola (Delta Temporale: i bandi
        intercettati a quel primo giro sono l'ARCHIVIO storico, non novita')."""
        self.conn.execute(
            "UPDATE schools SET first_crawled=? WHERE code=? AND first_crawled IS NULL",
            (_now(), code),
        )

    def all_schools(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM schools ORDER BY region, province, code"
        ).fetchall()

    # --- findings ----------------------------------------------------------
    def record_finding(self, school: sqlite3.Row, title, url, category, score,
                       source_page, origin: str = "nuovo") -> bool:
        """Inserisce/aggiorna. True se NOVITA' (mai vista prima).
        `origin`: 'archivio' se intercettato al primo crawl della scuola (back-
        catalogue, non e' una pubblicazione recente), altrimenti 'nuovo'."""
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
               first_seen, last_seen, origin)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fp, school["code"], school["name"], school["province"],
             school["region"], title, url, category, score, source_page,
             now, now, origin),
        )
        return True

    def all_findings(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM findings ORDER BY first_seen DESC"
        ).fetchall()

    # --- scadenza (enrichment) --------------------------------------------
    def findings_needing_scadenza(self, limit: int = 0) -> list[sqlite3.Row]:
        """Findings di cui non abbiamo ancora cercato la scadenza."""
        q = ("SELECT fingerprint, url FROM findings "
             "WHERE scadenza_checked IS NULL ORDER BY first_seen DESC")
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.conn.execute(q).fetchall()

    def set_scadenza(self, fingerprint: str, scadenza_iso: str | None) -> None:
        """Registra la scadenza trovata (ISO YYYY-MM-DD o None) e marca come
        controllata, cosi' non la si ri-scarica a ogni run."""
        self.conn.execute(
            "UPDATE findings SET scadenza=?, scadenza_checked=? WHERE fingerprint=?",
            (scadenza_iso, _now(), fingerprint),
        )

    # --- revisione AI (enrichment) ----------------------------------------
    def findings_needing_ai(self, limit: int = 0) -> list[sqlite3.Row]:
        """Findings non ancora rivisti dall'AI."""
        q = ("SELECT fingerprint, url, title, category FROM findings "
             "WHERE ai_checked IS NULL ORDER BY first_seen DESC")
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.conn.execute(q).fetchall()

    def set_ai_review(self, fingerprint: str, is_bando, scadenza_iso: str | None,
                      titolo: str, profilo: str) -> None:
        """Registra l'esito AI e marca ai_checked. La scadenza dell'AI SOSTITUISCE
        quella a parole-chiave (piu' affidabile, anche quando e' None = non
        indicata: meglio 'ignota' che la data sbagliata estratta dall'euristica)."""
        ai_b = None if is_bando is None else (1 if is_bando else 0)
        self.conn.execute(
            """UPDATE findings SET ai_bando=?, scadenza=?, titolo_pulito=?,
               profilo=?, ai_checked=? WHERE fingerprint=?""",
            (ai_b, scadenza_iso, titolo or None, profilo or None, _now(), fingerprint),
        )

    def reset_baseline(self) -> None:
        """Deploy pulito: svuota i findings e azzera first_crawled. Il prossimo
        crawl ricostruisce da zero con fingerprint nuovi e origin corretti
        (tutto 'archivio' al primo giro, novita' reali dal secondo)."""
        self.conn.execute("DELETE FROM findings")
        self.conn.execute("UPDATE schools SET first_crawled=NULL")
        self.conn.commit()

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
