"""
Crawl incrementale: data la pagina trasparenza/albo gia' nota di una scuola,
scarica, estrae i link/voci candidati e li passa al classifier per individuare
i bandi "esperto esterno".

Fase "veloce" e quotidiana: 1 richiesta per scuola (la pagina gia' nota), niente
discovery. Per ogni link la cui anchor classifica come match si emette un
finding. Si classifica anche il testo della pagina (cattura bandi citati inline
senza link diretto).

NB: classificazione sull'anchor + contesto, non sul PDF del bando. Un passo piu'
fine (scaricare e classificare il PDF) e' un miglioramento futuro, qui si punta
a misurare la copertura rispetto a Google Alerts.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .classifier import classify
from .fetch import extract_links, fetch_with_fallback, visible_text


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
    note: str = ""


def crawl(code: str, trasparenza_url: str, threshold: float = 0.45,
          timeout: int = 10) -> CrawlResult:
    """Scarica la pagina trasparenza e ritorna i bandi candidati. No eccezioni."""
    if not trasparenza_url:
        return CrawlResult(code, False, note="no_trasparenza_url")

    page = fetch_with_fallback(trasparenza_url, timeout=timeout)
    if not page.ok:
        return CrawlResult(code, False, note=page.error or "unreachable")

    hits: dict[str, Hit] = {}

    # 1) link la cui anchor classifica come bando esperto esterno
    for href, text in extract_links(page.html, page.final_url or trasparenza_url):
        if not text or len(text) < 8:
            continue
        c = classify(text, title=text, threshold=threshold)
        if c.is_match:
            hits[href] = Hit(title=text[:300], url=href,
                             category=c.category, score=c.score)

    # 2) la pagina stessa (titolo bando citato inline senza link diretto)
    body = visible_text(page.html)
    page_c = classify(body, title="", threshold=threshold)
    if page_c.is_match and (page.final_url or trasparenza_url) not in hits:
        # titolo euristico: prima frase con "esperto" o i primi ~120 char
        snippet = body[:300]
        hits[page.final_url or trasparenza_url] = Hit(
            title=snippet, url=page.final_url or trasparenza_url,
            category=page_c.category, score=page_c.score,
        )

    return CrawlResult(code, True, hits=list(hits.values()), note="ok")
