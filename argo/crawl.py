"""
Crawl incrementale: data la pagina trasparenza/albo nota di una scuola, scende
di UN livello fino alla lista bandi vera e classifica le voci candidate.

Perche' scendere: la discovery di solito approda all'INDICE di "Amministrazione
Trasparente" (un menu di sezioni: "Bandi di gara e contratti", "Bandi di
concorso", "Albo Pretorio"...), non alla lista dei bandi. I bandi veri stanno un
livello sotto. Quindi:
  1. scarica la pagina d'ingresso, classifica i suoi link e il suo testo;
  2. individua i link-sezione verso le liste bandi (per testo/href), stesso
     dominio, e ne segue un numero limitato (`max_subpages`);
  3. su ogni lista, classifica i link (titoli dei bandi) e il testo.

Fase "veloce" quotidiana ma ora 1 + N richieste per scuola (N piccolo). I
domini sono tutti distinti, quindi la concorrenza resta educata per ogni sito.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .classifier import classify
from .fetch import extract_links, fetch_with_fallback, visible_text

# Testi/href che indicano una LISTA di bandi da seguire (drill-down).
LISTING_HINTS = [
    "bandi di gara", "bandi di concorso", "bandi e gare", "bandi e concorsi",
    "albo pretorio", "albo online", "albo-pretorio", "albo-online",
    "avvisi", "bandi", "concorsi", "contratti", "gare",
    "esperti esterni", "selezioni", "incarichi",
]
MIN_ANCHOR_LEN = 8     # ignora anchor troppo corti ("vai", "qui", date sole)


@dataclass
class Hit:
    title: str
    url: str
    category: str
    score: float


@dataclass
class CrawlResult:
    code: str
    ok: bool
    hits: list[Hit] = field(default_factory=list)
    pages_fetched: int = 0
    note: str = ""


def _same_site(a: str, b: str) -> bool:
    na = urlparse(a).netloc.lower().removeprefix("www.")
    nb = urlparse(b).netloc.lower().removeprefix("www.")
    return na == nb and bool(na)


def _looks_like_listing(text: str, href: str) -> bool:
    blob = f"{text} {href}".lower()
    return any(k in blob for k in LISTING_HINTS)


def _best_snippet(body: str) -> str:
    """Per un hit a livello di pagina, estrae il testo ATTORNO al segnale forte
    invece dei primi 300 char (di solito menu/navigazione). Piu' leggibile e
    verificabile a mano."""
    low = body.lower()
    for kw in ("esperto esterno", "esperti esterni", "esperto madrelingua",
               "esperto"):
        i = low.find(kw)
        if i != -1:
            start = max(0, i - 90)
            return body[start:i + 170].strip()
    return body[:300]


def _scan_page(html: str, page_url: str, threshold: float,
               hits: dict[str, Hit]) -> None:
    """Classifica i link (titoli bandi) e il testo della pagina. Aggiorna hits."""
    for href, text in extract_links(html, page_url):
        if not text or len(text) < MIN_ANCHOR_LEN:
            continue
        c = classify(text, title=text, threshold=threshold)
        if c.is_match:
            prev = hits.get(href)
            if prev is None or c.score > prev.score:
                hits[href] = Hit(text[:300], href, c.category, c.score)
    # la pagina nel suo insieme (bando citato inline senza link diretto):
    # veto disattivato (qui "graduatoria"/"nomina" possono comparire accanto a un
    # bando vero) ma require_strong attivo: serve un segnale forte ancorato a
    # "esperto", per non flaggare pagine il cui punteggio viene da parole-menu.
    body = visible_text(html)
    page_c = classify(body, title="", threshold=threshold, veto=False,
                      require_strong=True)
    if page_c.is_match and page_url not in hits:
        hits[page_url] = Hit(_best_snippet(body), page_url, page_c.category,
                             page_c.score)


def crawl(code: str, trasparenza_url: str, threshold: float = 0.45,
          timeout: int = 10, max_subpages: int = 3) -> CrawlResult:
    """Scarica entry + sotto-liste e ritorna i bandi candidati. No eccezioni."""
    if not trasparenza_url:
        return CrawlResult(code, False, note="no_trasparenza_url")

    entry = fetch_with_fallback(trasparenza_url, timeout=timeout)
    if not entry.ok:
        return CrawlResult(code, False, note=entry.error or "unreachable")

    entry_url = entry.final_url or trasparenza_url
    hits: dict[str, Hit] = {}
    pages = 1
    _scan_page(entry.html, entry_url, threshold, hits)

    # drill-down: link-sezione verso liste bandi, stesso dominio, deduplicati
    seen = {entry_url.rstrip("/")}
    drill: list[str] = []
    for href, text in extract_links(entry.html, entry_url):
        key = href.rstrip("/")
        if key in seen or not _same_site(href, entry_url):
            continue
        if _looks_like_listing(text, href):
            seen.add(key)
            drill.append(href)
        if len(drill) >= max_subpages:
            break

    for url in drill:
        sub = fetch_with_fallback(url, timeout=max(6, timeout - 2))
        pages += 1
        if sub.ok:
            _scan_page(sub.html, sub.final_url or url, threshold, hits)

    return CrawlResult(code, True, hits=list(hits.values()),
                       pages_fetched=pages, note="ok")
