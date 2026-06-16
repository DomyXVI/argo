"""
Classificazione dei documenti scolastici per individuare i bandi "esperto esterno".

COPIA FEDELE di `bandi_monitor/classifier.py` del progetto bando-interceptor
(v1, path RSS). Tenuta identica DI PROPOSITO: per misurare l'uplift di Argo
rispetto a Google Alerts servono gli stessi criteri di scoring/categoria. Se un
domani Argo "promuove", questa logica andra' fattorizzata in un package
condiviso anziche' duplicata.

L'approccio NON e' un singolo match: assegna un punteggio in base a piu' segnali
e attribuisce una categoria. Solo stdlib: testabile senza dipendenze esterne.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


def normalize(text: str) -> str:
    """Minuscolo, senza accenti, spazi compattati. Robusto a PDF sporchi."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    # i PDF spesso spezzano le parole con a-capo: "espe-\nrto"
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Segnali forti: la presenza nel titolo/oggetto pesa molto.
STRONG_SIGNALS = [
    r"espert[oi] estern[oi]",
    r"selezione .{0,40}? esperto",
    r"individuazione .{0,40}? esperto",
    r"reclutamento .{0,40}? esperto",
    r"conferimento .{0,40}? incarico .{0,40}? esperto",
    r"avviso .{0,60}? esperto estern",
    r"art\.?\s*7\s*,?\s*comma\s*6",          # base giuridica tipica
    r"collaboraz\w* estern\w* .{0,40}? esperto",
]

# Segnali deboli: da soli non bastano, ma sommati alzano il punteggio.
WEAK_SIGNALS = [
    r"avviso pubblico",
    r"avviso di selezione",
    r"bando",
    r"manifestazione di interesse",
    r"incarico di",
    r"prestazione d'?opera",
    r"contratto di collaborazione",
    r"esperto",
]

# Guardie: abbassano il punteggio (probabile falso positivo / contesto sbagliato).
NEGATIVE_GUARDS = [
    r"esperto intern[oi]",
    r"personale intern[oi]",
    r"riservat[oa] al personale intern",
    r"graduatoria definitiva",      # spesso e' l'esito, non il bando aperto
    r"esito .{0,20}? selezione",
]

# Categorie tipiche, per filtrare via config. Ordine = priorita' di assegnazione.
CATEGORIES = [
    ("madrelingua",   [r"madrelingua", r"lettore di lingua", r"conversazione in lingua"]),
    ("psicologo",     [r"psicolog", r"sportello (di )?ascolto"]),
    ("coding",        [r"\bcoding\b", r"programmazion\w+", r"pensiero computazionale"]),
    ("robotica",      [r"\brobotica\b", r"\brobot\b", r"stem\b"]),
    ("digitale",      [r"transizione digitale", r"competenz\w+ digital\w+", r"\bdigital\w+\b"]),
    ("dm219",         [r"dm\s*219", r"d\.m\.?\s*219"]),
    ("pon_progettista_collaudatore",
                      [r"progettista", r"collaudatore", r"\bpon\b", r"pnrr"]),
    ("formazione",    [r"formatore", r"corso di formazione", r"docenza"]),
    ("sicurezza",     [r"\brspp\b", r"medico competente", r"\bdpo\b", r"d\.?lgs\.?\s*81"]),
    ("generico",      [r".*"]),
]

@dataclass
class Classification:
    is_match: bool
    score: float                 # 0..1 circa
    category: str
    matched_signals: list[str] = field(default_factory=list)
    title_hit: bool = False


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p) for p in patterns]


_STRONG = _compile(STRONG_SIGNALS)
_WEAK = _compile(WEAK_SIGNALS)
_NEG = _compile(NEGATIVE_GUARDS)
_CATS = [(name, _compile(pats)) for name, pats in CATEGORIES]


def classify(text: str, title: str = "", threshold: float = 0.45) -> Classification:
    """
    Valuta un documento. `title` (oggetto/nome file/anchor) pesa di piu' del corpo.
    `threshold` regola sensibilita': piu' basso = piu' richiami, piu' falsi positivi.
    """
    body = normalize(text)
    head = normalize(title)
    hay = f"{head} {body}"

    matched: list[str] = []
    score = 0.0
    title_hit = False

    for rx in _STRONG:
        if rx.search(hay):
            matched.append(rx.pattern)
            score += 0.5
            if rx.search(head):
                title_hit = True
                score += 0.25     # nel titolo conta doppio

    weak_hits = sum(1 for rx in _WEAK if rx.search(hay))
    score += min(weak_hits, 4) * 0.08

    for rx in _NEG:
        if rx.search(hay):
            matched.append(f"-{rx.pattern}")
            score -= 0.35

    score = max(0.0, min(score, 1.0))

    category = "generico"
    for name, pats in _CATS:
        if any(rx.search(hay) for rx in pats):
            category = name
            break

    return Classification(
        is_match=score >= threshold,
        score=round(score, 3),
        category=category,
        matched_signals=matched,
        title_hit=title_hit,
    )
