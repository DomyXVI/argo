"""
Classificazione dei documenti scolastici per individuare i bandi "esperto esterno".

NB SULLA PROVENIENZA: nasce come copia di `bandi_monitor/classifier.py` di v1
(path RSS), ma da Argo v0.2 e' DIVERGENTE e migliorato per il contesto del
crawl diretto (titoli/anchor di pagine intere, non snippet da ~280 char di
Google Alerts). Il confronto Argo-vs-Alerts in `compare.py` e' per URL, quindi
resta valido a prescindere dallo scorer usato: misura quali bandi ciascuna
fonte scopre, non quale classifier gira.

Approccio a quattro livelli di segnale (forte / medio / debole / guardie):
non un singolo match, ma un punteggio. Cosi' la pipeline puo' tarare via config
cosa tenere. Solo stdlib: testabile senza dipendenze esterne.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


def normalize(text: str) -> str:
    """Minuscolo, senza accenti, spazi compattati. Robusto a PDF/HTML sporchi."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    # i PDF spesso spezzano le parole con a-capo: "espe-\nrto"
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Segnali FORTI (0.5): da soli quasi bastano. Ancorati a "esperto" + contesto
# di selezione/esternalita'. Coprono le formule ricorrenti dei bandi reali.
STRONG_SIGNALS = [
    r"espert[oi] estern[oi]",
    r"espert[oi] madrelingua",
    r"selezione .{0,40}?espert[oi]",
    r"individuazione .{0,40}?espert[oi]",
    r"reclutamento .{0,40}?espert[oi]",
    r"reperimento .{0,40}?espert[oi]",
    r"conferimento .{0,40}?incarico .{0,40}?espert",
    r"avviso .{0,60}?espert[oi] estern",
    r"avviso pubblico .{0,60}?espert[oi]",
    r"incarico .{0,30}?espert[oi] estern",
    r"art\.?\s*7\s*,?\s*comma\s*6",                 # base giuridica tipica
    r"collaboraz\w* estern\w* .{0,40}?espert",
    r"personale estern[oi] .{0,30}?espert",
    r"prestazione d'?opera .{0,40}?espert",
]

# Segnali MEDI (0.3): figure professionali tipicamente esternalizzate, anche
# senza la parola "esperto/esterno" esplicita (es. "selezione psicologo").
MEDIUM_SIGNALS = [
    r"selezione .{0,25}?psicolog",
    r"avviso .{0,30}?psicolog",
    r"sportello (di )?ascolto",
    r"selezione .{0,25}?madrelingua",
    r"selezione .{0,25}?formatore",
    r"formatore estern",
    r"collaudatore",
    r"progettista",
    r"\brspp\b",
    r"medico competente",
    r"\bdpo\b|\brpd\b",
    r"animatore digitale",
]

# Segnali DEBOLI (0.08, cap 4): da soli non bastano, sommati alzano il punteggio.
WEAK_SIGNALS = [
    r"avviso pubblico",
    r"avviso di selezione",
    r"\bband[oi]\b",
    r"manifestazione di interesse",
    r"incarico di",
    r"prestazione d'?opera",
    r"contratto di collaborazione",
    r"\bco\.?co\.?co\b",
    r"incarico professionale",
    r"\bespert[oi]\b",
]

# GUARDIE (-0.35): abbassano il punteggio (segnale debole di contesto sbagliato).
NEGATIVE_GUARDS = [
    r"\bproroga\b",
    r"\brinnovo\b",
]

# VETO: marcatori che, su un TITOLO/anchor, identificano un atto a valle del
# bando (graduatoria, nomina, aggiudicazione, verbale...) o il contesto sbagliato
# (personale interno). Se presenti -> is_match=False a prescindere dal punteggio.
# Applicato solo ai titoli brevi (`veto=True`), NON al corpo lungo di una pagina
# -lista, dove queste parole possono comparire accanto a un bando aperto vero.
VETO_MARKERS = [
    # --- atti a VALLE del bando (selezione gia' chiusa/decisa) ---
    r"graduatori\w*",
    r"\bnomin[ae]\b",
    r"commissione giudicatrice",
    r"aggiudicazi\w+",
    r"\bverbale\b",
    r"esito .{0,20}?selezione",
    r"\bconvocazione\b",
    r"avviso di rettifica",
    r"decreto\b.{0,40}(conferimento|attribuzione|approvazione)",
    r"determin\w+.{0,40}(conferimento|attribuzione|aggiudicaz)",
    r"autoattestazione",
    r"autovalutazione",
    r"valutazione delle candidature",
    # --- NON sono bandi: allegati, moduli, regolamenti, CV ---
    r"\bscheda\b",
    r"modulistic",
    r"\bmodulo (di )?(domanda|richiesta|volontari|autorizzazione|ingresso)",
    r"modello (di |per )?(domanda|richiesta|autorizzazione|ingresso)",
    r"dichiarazione sostitutiva",
    r"curriculum vitae",
    r"\bcv\b",
    r"\bregolamento\b",
    r"autorizzazione ingresso",
    # --- header/landing di pagina, non un bando specifico ---
    r"^(albo (online|pretorio)|amministrazione trasparente)\b",
    r"bandi di gara[ \-]",
    # --- contesto sbagliato: selezione riservata al personale interno ---
    r"espert[oi] intern[oi]",
    r"personale (docente )?intern[oi]",
    r"riservat[oa] al personale intern",
]

# Categorie, per filtrare via config. Ordine = priorita' di assegnazione.
CATEGORIES = [
    ("madrelingua",   [r"madrelingua", r"lettore di lingua", r"conversazione in lingua"]),
    ("psicologo",     [r"psicolog", r"sportello (di )?ascolto"]),
    ("coding",        [r"\bcoding\b", r"programmazion\w+", r"pensiero computazionale"]),
    ("robotica",      [r"\brobotica\b", r"\brobot\b", r"stem\b"]),
    ("digitale",      [r"transizione digitale", r"competenz\w+ digital\w+",
                       r"animatore digitale", r"\bdigital\w+\b"]),
    ("dm219",         [r"dm\s*219", r"d\.m\.?\s*219"]),
    ("pon_progettista_collaudatore",
                      [r"progettista", r"collaudatore", r"\bpon\b", r"pnrr"]),
    ("formazione",    [r"formatore", r"corso di formazione", r"docenza"]),
    ("sicurezza",     [r"\brspp\b", r"medico competente", r"\bdpo\b", r"\brpd\b",
                       r"d\.?lgs\.?\s*81"]),
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
_MEDIUM = _compile(MEDIUM_SIGNALS)
_WEAK = _compile(WEAK_SIGNALS)
_NEG = _compile(NEGATIVE_GUARDS)
_VETO = _compile(VETO_MARKERS)
_CATS = [(name, _compile(pats)) for name, pats in CATEGORIES]


def classify(text: str, title: str = "", threshold: float = 0.45,
             veto: bool = True, require_strong: bool = False) -> Classification:
    """
    Valuta un documento/titolo. `title` (oggetto/anchor) pesa di piu' del corpo.
    `threshold` regola la sensibilita': piu' basso = piu' richiami, piu' rumore.
    `veto`: se True (titoli brevi) un marcatore di atto-a-valle annulla il match;
    metterlo False sul corpo lungo di una pagina-lista.
    `require_strong`: se True, il match richiede ALMENO un segnale forte (ancorato
    a "esperto"), non la sola somma di segnali medi/deboli. Da usare sul corpo di
    pagina intera, dove parole da menu (coding, progettista, pnrr...) gonfiano il
    punteggio senza che la pagina contenga un vero bando esperto esterno.
    """
    body = normalize(text)
    head = normalize(title)
    hay = f"{head} {body}"

    matched: list[str] = []
    score = 0.0
    title_hit = False
    strong_hit = False

    for rx in _STRONG:
        if rx.search(hay):
            matched.append(rx.pattern)
            strong_hit = True
            score += 0.5
            if rx.search(head):
                title_hit = True
                score += 0.25     # nel titolo conta doppio

    for rx in _MEDIUM:
        if rx.search(hay):
            matched.append(rx.pattern)
            score += 0.3
            if rx.search(head):
                title_hit = True
                score += 0.1

    weak_hits = sum(1 for rx in _WEAK if rx.search(hay))
    score += min(weak_hits, 4) * 0.08

    for rx in _NEG:
        if rx.search(hay):
            matched.append(f"-{rx.pattern}")
            score -= 0.35

    score = max(0.0, min(score, 1.0))

    vetoed = False
    if veto:
        for rx in _VETO:
            if rx.search(hay):
                matched.append(f"VETO:{rx.pattern}")
                vetoed = True
                break

    category = "generico"
    for name, pats in _CATS:
        if any(rx.search(hay) for rx in pats):
            category = name
            break

    is_match = (score >= threshold) and not vetoed
    if require_strong and not strong_hit:
        is_match = False

    return Classification(
        is_match=is_match,
        score=round(score, 3),
        category=category,
        matched_signals=matched,
        title_hit=title_hit,
    )
