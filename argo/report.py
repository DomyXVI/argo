"""
Report leggibili della fase di DISCOVERY.

Il DB (`argo.db`) e' gitignorato e vive solo in cache: di per se' la discovery
non lascia nulla di sfogliabile nel repo. Questo modulo produce due file
COMMITTATI, cosi' il risultato della mappatura e' ispezionabile da browser e
diffabile tra un giro e l'altro:

  - discovery_report.json : statistiche aggregate (per regione, piattaforme,
                            esiti) — il "cruscotto" del giro.
  - schools_map.csv       : una riga per scuola (codice, nome, piattaforma,
                            url trasparenza, esito) — apribile anche in Excel.

NB: questi sono il risultato della MAPPATURA (dove sta l'albo di ogni scuola),
non i bandi. I bandi stanno negli snapshot del crawl (data/snapshots/).
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


CSV_COLUMNS = [
    "code", "name", "region", "province", "city", "platform",
    "reachable", "trasparenza_url", "discovery_note",
]


def write_discovery_report(rows, report_path: str | Path,
                           csv_path: str | Path) -> dict:
    """Genera i due file dai record `schools`. Ritorna il dict del report."""
    rows = list(rows)
    total = len(rows)
    reachable = sum(1 for r in rows if r["reachable"])
    found = sum(1 for r in rows if (r["trasparenza_url"] or ""))

    per_regione: dict[str, dict] = defaultdict(
        lambda: {"totale": 0, "raggiungibili": 0, "trasparenza": 0})
    platforms: Counter = Counter()
    note: Counter = Counter()

    for r in rows:
        reg = r["region"] or "?"
        per_regione[reg]["totale"] += 1
        if r["reachable"]:
            per_regione[reg]["raggiungibili"] += 1
            platforms[r["platform"] or "unknown"] += 1
        if r["trasparenza_url"]:
            per_regione[reg]["trasparenza"] += 1
        note[r["discovery_note"] or "?"] += 1

    def pct(a: int, b: int) -> float:
        return round(100 * a / b, 1) if b else 0.0

    for reg, d in per_regione.items():
        d["raggiungibili_pct"] = pct(d["raggiungibili"], d["totale"])
        d["trasparenza_pct"] = pct(d["trasparenza"], d["totale"])

    report = {
        "meta": {
            "generato": datetime.now(timezone.utc).isoformat(),
            "scuole_totali": total,
            "raggiungibili": reachable,
            "raggiungibili_pct": pct(reachable, total),
            "trasparenza_trovata": found,
            "trasparenza_pct": pct(found, total),
        },
        "per_regione": dict(sorted(per_regione.items())),
        "piattaforme": dict(platforms.most_common()),
        "esiti_discovery": dict(note.most_common()),
    }

    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                           encoding="utf-8")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow([
                r["code"], r["name"], r["region"], r["province"], r["city"],
                r["platform"], r["reachable"], r["trasparenza_url"],
                r["discovery_note"],
            ])

    return report
