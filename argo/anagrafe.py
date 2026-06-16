"""
Caricamento anagrafe scuole dai dati aperti MIUR (dati.istruzione.it).

Erede di legacy/anagrafe.py del progetto v1, con due differenze:
  - filtro su un INSIEME di regioni (non solo Lombardia), preso da config;
  - usa `fetch.clean_url` (corretto) invece del `_clean_url` buggato originale.

Il dataset MIUR "anagrafica scuole statali" usa virgola come separatore e
intestazioni in MAIUSCOLO. La colonna SITOWEBSCUOLA e' popolata per ~98% delle
scuole (verificato sul file 2025-26). Loader tollerante: rileva il delimitatore
e mappa i campi per alias.
"""
from __future__ import annotations

import csv
import io
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .fetch import clean_url

# URL diretto del CSV statali (anno 2025-26). Aggiornare a inizio anno scolastico.
DEFAULT_CSV_URL = (
    "https://dati.istruzione.it/opendata/SCUANAGRAFESTAT20252620250901.csv"
)

HEADER_ALIASES = {
    "code":     ["codicescuola", "codiceistitutoriferimento", "codicemeccanografico"],
    "name":     ["denominazionescuola", "denominazioneistitutoriferimento"],
    "region":   ["regione", "descrizioneregione"],
    "province": ["provincia", "descrizioneprovincia", "siglaprovincia"],
    "city":     ["descrizionecomune", "comune"],
    "website":  ["sitowebscuola", "sitoweb", "indirizzowebscuola"],
    "email":    ["indirizzoemailscuola", "indirizzopecscuola", "email"],
}


@dataclass
class School:
    code: str
    name: str
    region: str
    province: str
    city: str
    website: str   # gia' passato per clean_url


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


def _sniff_delimiter(sample: str) -> str:
    first = sample.splitlines()[0] if sample.splitlines() else ""
    for d in (",", ";", "|", "\t"):
        if d in first:
            return d
    return ","


def _build_index(fieldnames: list[str]) -> dict[str, str]:
    norm_to_real = {_norm_header(f): f for f in fieldnames}
    index: dict[str, str] = {}
    for logical, aliases in HEADER_ALIASES.items():
        for a in aliases:
            if a in norm_to_real:
                index[logical] = norm_to_real[a]
                break
    return index


def ensure_csv(path: str | Path, url: str = DEFAULT_CSV_URL) -> Path:
    """Scarica il CSV se non gia' in cache locale. Ritorna il percorso."""
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, path)
    return path


def load(path: str | Path, regions: list[str]) -> list[School]:
    """Carica le scuole delle `regions` indicate (case-insensitive)."""
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    delim = _sniff_delimiter(raw)
    reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
    if not reader.fieldnames:
        return []
    idx = _build_index(reader.fieldnames)
    if "code" not in idx or "region" not in idx:
        raise ValueError(f"Schema CSV non riconosciuto: {reader.fieldnames}")

    targets = {r.strip().upper() for r in regions}
    by_code: dict[str, School] = {}
    for row in reader:
        region = (row.get(idx.get("region", ""), "") or "").strip().upper()
        if region not in targets:
            continue
        website = clean_url(row.get(idx.get("website", ""), ""))
        s = School(
            code=(row.get(idx["code"], "") or "").strip(),
            name=(row.get(idx.get("name", ""), "") or "").strip(),
            region=region,
            province=(row.get(idx.get("province", ""), "") or "").strip(),
            city=(row.get(idx.get("city", ""), "") or "").strip(),
            website=website,
        )
        if not s.code:
            continue
        # dedup per codice, preferendo chi ha un sito valido
        if s.code not in by_code or (s.website and not by_code[s.code].website):
            by_code[s.code] = s
    return list(by_code.values())
