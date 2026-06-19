"""
Revisione AI dei findings sopravvissuti al classificatore a regole.

Per ogni candidato legge il TESTO del documento (HTML della pagina + PDF allegati)
e chiede a un LLM economico (GPT-4o-mini di default) di restituire, in JSON:
  - is_bando: e' davvero un avviso/bando APERTO per un ESPERTO/figura ESTERNA a cui
    candidarsi? (false per atti chiusi, moduli, allegati, contesto sbagliato)
  - scadenza: il TERMINE per presentare domanda (YYYY-MM-DD) o null se non indicato.
    Da NON confondere con la data di pubblicazione/protocollo o di fine-progetto
    (es. PNRR "attivita' entro il 30/06/2026"). Piu' affidabile dell'euristica a
    parole-chiave, che sceglieva "la data piu' tardiva" e sbagliava bersaglio.
  - titolo: una riga pulita e leggibile (i titoli grezzi sono frammenti di HTML).
  - profilo: la figura cercata (madrelingua, psicologo, RSPP, progettista...).

Degrada con grazia: senza chiave API o senza la libreria `openai`, `rivedi_bando`
ritorna None e il resto del crawler funziona (stesso patto di pypdf in scadenza.py).
"""
from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher

# Cap di DEFAULT del testo inviato al modello (sovrascrivibile da config.ai.max_doc_chars).
# Regola spannometrica per l'italiano col tokenizer GPT: ~3,7 caratteri per token,
# quindi 26.000 char ~= 7.000 token. NB: non scendere troppo, la scadenza a volte
# e' in fondo al PDF e un cap basso la taglierebbe (-> "scadenza non indicata").
_MAX_CHARS = 26_000

_SYSTEM = (
    "Sei un assistente che analizza avvisi pubblicati dalle scuole italiane "
    "nell'albo pretorio / amministrazione trasparente. Ricevi il testo (estratto, "
    "possibilmente sporco) di un documento. Rispondi SOLO con un oggetto JSON con "
    "ESATTAMENTE questi campi:\n"
    '- "is_bando" (bool): true SOLO se e\' un avviso/bando APERTO per la selezione '
    "di un ESPERTO o figura ESTERNA (docente esperto, tutor, madrelingua, psicologo, "
    "RSPP, progettista/collaudatore...) a cui ci si puo\' candidare. false se e\' un "
    "atto gia\' PERFEZIONATO o a valle della selezione: graduatoria, nomina, "
    "contratto (anche 'a titolo gratuito' o 'di prestazione d'opera' gia\' "
    "stipulato), conferimento/affidamento di incarico gia\' disposto, determina "
    "di aggiudicazione/a contrarre, avviso di aggiudicazione, verbale, esito; "
    "oppure un modulo/allegato, un regolamento, o non pertinente. Nel dubbio tra "
    "un AVVISO che apre le candidature e un ATTO che le chiude, guarda se c'e\' un "
    "termine per presentare domanda: se manca ed e\' gia\' indicato un beneficiario, "
    "e\' chiuso (false).\n"
    '- "scadenza" (string|null): la data del TERMINE ULTIMO per presentare la domanda, '
    "in formato YYYY-MM-DD. E\' la data entro cui i candidati devono far pervenire la "
    "domanda. NON e\' la data di pubblicazione, NON e\' la data di protocollo, NON e\' la "
    "data di fine attivita\'/progetto (es. 'le attivita\' dovranno concludersi entro il "
    "30/06/2026' NON e\' la scadenza). Se il termine domande non e\' indicato chiaramente, "
    "metti null: non inventare. Se manca l'anno, deducilo dal resto del documento.\n"
    '- "titolo" (string): un titolo pulito di UNA riga (figura cercata + progetto/ambito), '
    "max ~100 caratteri, senza frammenti di menu o HTML.\n"
    '- "profilo" (string): la figura cercata in 1-4 parole (es. \"esperto madrelingua '
    'inglese\", \"psicologo\", \"RSPP\", \"progettista PON\"). Stringa vuota se non chiaro.'
)


# Priorita' degli allegati: vogliamo IL bando, non moduli/domanda/cv/privacy.
_PDF_PRIO = re.compile(r"avvis|band|selezion|decret|determin|incaric|reclutam", re.IGNORECASE)
_PDF_SKIP = re.compile(r"modul|domand|allegat|\bcv\b|privacy|informativa|liberatoria", re.IGNORECASE)
# Link a un documento scaricabile ANCHE senza estensione .pdf: sui portali
# scolastici (Spaggiari/PA digitale) il decreto sta a `.../Documenti/<id>` o
# `?download=1`, che il vecchio filtro `_is_pdf_url` scartava -> non leggevamo
# mai il documento, solo l'indice (che ha la data di pubblicazione, non la
# scadenza). bytes_to_text capisce poi da solo se e' PDF o HTML.
_DOC_HINT = re.compile(r"/documenti/|/sdg\d|[?&]download|/download|/allegat|"
                       r"/uploads?/|/file/|attachment", re.IGNORECASE)
_ATITEM_RE = re.compile(r'class="[^"]*at-item[^"]*".*?'
                        r'(?=class="[^"]*at-item|</main|</body|\Z)', re.IGNORECASE | re.DOTALL)
_TAG_ONLY = re.compile(r"<[^>]+>")


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def _rank_docs(links: list[tuple[str, str]]) -> list[str]:
    """Ordina (url, anchor) mettendo in cima il documento che PROBABILMENTE e'
    il bando e in fondo moduli/domande/cv, cosi' i primi `max_pdf` letti sono i
    piu' utili (collegato alla roadmap: 'selezione PDF migliore')."""
    def score(testo: str) -> int:
        return (1 if _PDF_PRIO.search(testo) else 0) - (1 if _PDF_SKIP.search(testo) else 0)
    return [u for u, _t in sorted(links, key=lambda x: -score(x[1]))]


def _is_doc_link(url: str, anchor: str) -> bool:
    from .scadenza import _is_pdf_url
    return _is_pdf_url(url) or bool(_DOC_HINT.search(url))


def _match_atto_block(html: str, titolo_hint: str) -> str:
    """Su una pagina-INDICE dell'albo (template 'at-item', che lista decine di
    atti) ritorna l'HTML del SOLO blocco il cui titolo somiglia di piu' a
    `titolo_hint`. Serve a leggere il documento DELL'ATTO GIUSTO: prendere i
    link a caso dalla lista leggerebbe la scadenza di un altro bando. "" se la
    pagina non e' un indice o nessun blocco somiglia abbastanza."""
    if not titolo_hint:
        return ""
    blocks = _ATITEM_RE.findall(html)
    if len(blocks) < 2:
        return ""   # non e' una pagina-lista: nessun blocco da isolare
    target = _norm(titolo_hint)
    words = [w for w in target.split() if len(w) > 4]
    best, best_sc = "", 0.0
    for b in blocks:
        txt = _norm(_TAG_ONLY.sub(" ", b))
        ratio = SequenceMatcher(None, target[:80], txt[:300]).ratio()
        hit = (sum(1 for w in words if w in txt) / len(words)) if words else 0.0
        sc = ratio * 0.4 + hit * 0.6
        if sc > best_sc:
            best_sc, best = sc, b
    return best if best_sc >= 0.35 else ""


def testo_documento(url: str, titolo_hint: str = "", timeout: int = 12,
                    max_pdf: int = 3, max_chars: int = _MAX_CHARS) -> tuple[str, str]:
    """Ritorna (testo, doc_url). `testo` = atto + suoi documenti allegati,
    troncato a `max_chars`. Se l'URL e' la pagina-indice dell'albo, isola il
    blocco dell'atto che corrisponde a `titolo_hint` e legge SOLO i suoi
    documenti; `doc_url` e' allora il link DIRETTO all'atto (per il bottone
    'Apri bando' del portale, altrimenti si atterra sull'indice). `doc_url` ="" se
    l'URL del finding e' gia' quello giusto. ("","") se irraggiungibile/rumore."""
    from .fetch import (bytes_to_text, extract_links, fetch_bytes,
                        fetch_with_fallback, html_is_noise, visible_text)
    from .scadenza import _is_pdf_url, _pdf_text

    parti: list[str] = []
    if _is_pdf_url(url):
        ok, ct, data = fetch_bytes(url, timeout)
        return ((bytes_to_text(ct, data, _pdf_text)[:max_chars]), "") if ok else ("", "")

    r = fetch_with_fallback(url, timeout)
    if not r.ok:
        return ("", "")
    base = r.final_url or url
    # Pagina-indice? Isola il blocco del nostro atto: la sua parte di testo e i
    # suoi documenti, non l'intera lista (rumore + scadenze di altri bandi).
    block = _match_atto_block(r.html, titolo_hint)
    if block:
        parti.append(visible_text(block))
        scope = block
    else:
        html_txt = visible_text(r.html)
        if not html_is_noise(html_txt):   # niente login/errore all'AI
            parti.append(html_txt)
        scope = r.html
    links = [(u, t) for u, t in extract_links(scope, base) if _is_doc_link(u, t)]
    ranked = _rank_docs(links)
    # Solo se abbiamo isolato il blocco (pagina-indice): il 1o documento e' l'atto
    # nostro -> link diretto per il portale. Su pagina gia' giusta non si tocca.
    doc_url = ranked[0] if (block and ranked) else ""
    for du in ranked[:max_pdf]:
        ok, ct, data = fetch_bytes(du, timeout)
        if ok:
            parti.append(bytes_to_text(ct, data, _pdf_text))
        if sum(len(p) for p in parti) > max_chars:
            break
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(parti)).strip()[:max_chars], doc_url


def _chiama_openai(system: str, user: str, model: str, timeout: int) -> str | None:
    """Chiama l'API. None se manca chiave/libreria o per qualsiasi errore (cosi'
    il finding non viene marcato e si ritenta al run successivo)."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    try:
        client = OpenAI(api_key=key, timeout=timeout, max_retries=5)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return resp.choices[0].message.content
    except Exception as e:
        # NON ingoiare l'errore in silenzio: una chiamata fallita deve essere
        # diagnosticabile dal log (chiave errata, quota, rate limit, modello...).
        import sys
        print(f"[ai_review] chiamata LLM fallita: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None


def disponibile() -> bool:
    """True se l'AI e' utilizzabile (chiave + libreria presenti)."""
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def rivedi_bando(url: str, titolo_hint: str = "", categoria_hint: str = "",
                 model: str = "gpt-4o-mini", timeout: int = 30,
                 max_chars: int = _MAX_CHARS) -> dict | None:
    """Rivede un finding leggendone il documento (troncato a `max_chars`). Ritorna
    {is_bando, scadenza, titolo, profilo, doc_url} oppure None se l'AI non e'
    disponibile o la chiamata fallisce (il finding NON va marcato: si ritenta)."""
    testo, doc_url = testo_documento(url, titolo_hint=titolo_hint, timeout=timeout,
                                     max_chars=max_chars)
    if not testo:
        # Nessun testo leggibile: decisione possibile ma povera. Marchiamo come
        # "non determinato" cosi' non si ri-scarica all'infinito un doc vuoto.
        return {"is_bando": None, "scadenza": None, "titolo": "", "profilo": "",
                "doc_url": ""}

    user = f"Oggetto (grezzo): {titolo_hint}\nCategoria ipotizzata: {categoria_hint}\n\nTESTO:\n{testo}"
    raw = _chiama_openai(_SYSTEM, user, model, timeout)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None

    scad = data.get("scadenza")
    if isinstance(scad, str):
        scad = scad.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", scad):
            scad = None   # accetta solo ISO pulito
    else:
        scad = None
    # Qui il documento NON era vuoto (il ramo doc-vuoto e' uscito prima con
    # is_bando=None). Se il modello non si esprime (campo assente o null),
    # vale "non e' un bando": is_bando=False -> finisce in esclusi_ai, NON in
    # doc_vuoti. Cosi' ai_bando=NULL resta riservato al solo vero doc-vuoto e
    # un finding leggibile non viene mai nascosto silenziosamente.
    ib = data.get("is_bando")
    return {
        "is_bando": False if ib is None else ib,
        "scadenza": scad,
        "titolo": (data.get("titolo") or "").strip()[:160],
        "profilo": (data.get("profilo") or "").strip()[:60],
        "doc_url": doc_url,
    }
