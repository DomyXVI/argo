"""
Estrazione della SCADENZA di un bando (termine per la presentazione delle
domande). E' il segnale che dice se un bando e' ancora APERTO: scadenza >= oggi
-> aperto, < oggi -> scaduto, assente -> ignota.

Strategia "HTML-first, PDF-fallback":
  1. la scadenza in chiaro nell'HTML della pagina-bando (alcune piattaforme albo
     espongono il campo) -> gratis;
  2. altrimenti la scadenza vive quasi sempre nel PDF allegato -> si scaricano i
     primi PDF della pagina e li si legge con pypdf.

pypdf e' l'UNICA dipendenza esterna del progetto e SOLO qui: l'import e'
protetto, se manca la scadenza resta ignota e il resto del crawler funziona.
"""
from __future__ import annotations

import io
import re
from datetime import date

_MESI = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5,
    "giugno": 6, "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10,
    "novembre": 11, "dicembre": 12,
}

# Parole che PRECEDONO la data di scadenza. Ancorare a queste evita di scambiare
# la data di protocollo/pubblicazione (vicina ma diversa) per la scadenza.
_KW = re.compile(
    r"(scadenz\w*"
    r"|termine\s+(?:ultimo|perentorio|finale|di\s+presentazione|di\s+scadenza)"
    r"|entro\s+e\s+non\s+oltre"
    r"|entro\s+(?:le\s+ore\s+[\d.:]+\s+)?(?:del\s+giorno\s+|del\s+|il\s+)"
    r"|pena\s+(?:di\s+)?esclusione"
    r"|(?:presentazione|invio|inoltro|trasmissione)\s+(?:delle?\s+|dell['’]\s*)?"
    r"(?:domand\w+|candidatur\w+|istanz\w+|offert\w+|manifestazion\w+)"
    r"|far\s+pervenire"
    r"|dovranno\s+pervenire)",
    re.I,
)
_DNUM = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")
_DTXT = re.compile(
    r"\b(\d{1,2})\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|"
    r"agosto|settembre|ottobre|novembre|dicembre)\s+(\d{4})\b",
    re.I,
)


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _first_date_after(text: str, pos: int, span: int = 130) -> date | None:
    """Prima data (numerica o estesa) entro `span` caratteri dopo `pos`."""
    win = text[pos:pos + span]
    hits: list[tuple[int, date]] = []
    for m in _DNUM.finditer(win):
        d = _mk(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if d:
            hits.append((m.start(), d))
    for m in _DTXT.finditer(win):
        d = _mk(int(m.group(3)), _MESI[m.group(2).lower()], int(m.group(1)))
        if d:
            hits.append((m.start(), d))
    if not hits:
        return None
    hits.sort()
    return hits[0][1]


def estrai_scadenza(text: str) -> date | None:
    """Scadenza dal testo, o None. Tra le date ancorate a una keyword di
    scadenza sceglie la PIU' TARDIVA: protocolli/pubblicazioni eventualmente
    citati accanto sono precedenti alla scadenza vera."""
    if not text:
        return None
    cands: list[date] = []
    for m in _KW.finditer(text):
        d = _first_date_after(text, m.end())
        if d:
            cands.append(d)
    return max(cands) if cands else None


def _pdf_text(data: bytes, max_pages: int = 8) -> str:
    """Testo dalle prime pagine di un PDF. "" se pypdf manca o il PDF e' illeggibile."""
    try:
        import pypdf
    except ImportError:
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        parts = [(p.extract_text() or "") for p in reader.pages[:max_pages]]
        return "\n".join(parts)
    except Exception:
        return ""


def _is_pdf_url(url: str) -> bool:
    return url.lower().split("?")[0].split("#")[0].endswith(".pdf")


def scadenza_da_url(url: str, timeout: int = 10, max_pdf: int = 3) -> date | None:
    """Scadenza per un URL-bando. HTML-first, poi i primi `max_pdf` PDF allegati.
    Non solleva mai: in caso di problemi ritorna None (scadenza ignota)."""
    from .fetch import (bytes_to_text, extract_links, fetch_bytes,
                        fetch_with_fallback, visible_text)

    if _is_pdf_url(url):
        ok, ct, data = fetch_bytes(url, timeout)
        return estrai_scadenza(bytes_to_text(ct, data, _pdf_text)) if ok else None

    r = fetch_with_fallback(url, timeout)
    if not r.ok:
        return None
    found = estrai_scadenza(visible_text(r.html))
    if found:
        return found
    base = r.final_url or url
    pdfs = [u for u, _t in extract_links(r.html, base) if _is_pdf_url(u)]
    for pu in pdfs[:max_pdf]:
        ok, ct, data = fetch_bytes(pu, timeout)
        if ok:
            found = estrai_scadenza(bytes_to_text(ct, data, _pdf_text))
            if found:
                return found
    return None


def stato_da_scadenza(scadenza: date | None, oggi: date | None = None) -> str:
    """'aperto' | 'scaduto' | 'ignota'. Calcolato a runtime: un bando aperto
    diventa scaduto col passare del tempo, quindi NON va congelato nel DB."""
    if scadenza is None:
        return "ignota"
    return "aperto" if scadenza >= (oggi or date.today()) else "scaduto"
