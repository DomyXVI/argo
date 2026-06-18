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

# Cap del testo inviato al modello: la scadenza vive nelle prime pagine. Tiene i
# token (~6-7k) e con essi il costo sotto controllo.
_MAX_CHARS = 24_000

_SYSTEM = (
    "Sei un assistente che analizza avvisi pubblicati dalle scuole italiane "
    "nell'albo pretorio / amministrazione trasparente. Ricevi il testo (estratto, "
    "possibilmente sporco) di un documento. Rispondi SOLO con un oggetto JSON con "
    "ESATTAMENTE questi campi:\n"
    '- "is_bando" (bool): true SOLO se e\' un avviso/bando APERTO per la selezione '
    "di un ESPERTO o figura ESTERNA (docente esperto, tutor, madrelingua, psicologo, "
    "RSPP, progettista/collaudatore...) a cui ci si puo\' candidare. false se e\' un "
    "atto gia\' chiuso (graduatoria, nomina, contratto firmato, determina di "
    "aggiudicazione, verbale, esito), un modulo/allegato, un regolamento, o non pertinente.\n"
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


def testo_documento(url: str, timeout: int = 12, max_pdf: int = 3) -> str:
    """Testo del documento-bando: HTML visibile + testo dei primi PDF allegati.
    Riusa il fetch del progetto e `_pdf_text` di scadenza.py. "" se irraggiungibile."""
    from .fetch import extract_links, fetch_bytes, fetch_with_fallback, visible_text
    from .scadenza import _is_pdf_url, _pdf_text

    parti: list[str] = []
    if _is_pdf_url(url):
        ok, _ct, data = fetch_bytes(url, timeout)
        return (_pdf_text(data)[:_MAX_CHARS]) if ok else ""

    r = fetch_with_fallback(url, timeout)
    if not r.ok:
        return ""
    parti.append(visible_text(r.html))
    base = r.final_url or url
    pdfs = [u for u, _t in extract_links(r.html, base) if _is_pdf_url(u)]
    for pu in pdfs[:max_pdf]:
        ok, _ct, data = fetch_bytes(pu, timeout)
        if ok:
            parti.append(_pdf_text(data))
        if sum(len(p) for p in parti) > _MAX_CHARS:
            break
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(parti)).strip()[:_MAX_CHARS]


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
        client = OpenAI(api_key=key, timeout=timeout)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return resp.choices[0].message.content
    except Exception:
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
                 model: str = "gpt-4o-mini", timeout: int = 30) -> dict | None:
    """Rivede un finding leggendone il documento. Ritorna
    {is_bando, scadenza, titolo, profilo} oppure None se l'AI non e' disponibile
    o la chiamata fallisce (in tal caso il finding NON va marcato: si ritenta)."""
    testo = testo_documento(url, timeout=timeout)
    if not testo:
        # Nessun testo leggibile: decisione possibile ma povera. Marchiamo come
        # "non determinato" cosi' non si ri-scarica all'infinito un doc vuoto.
        return {"is_bando": None, "scadenza": None, "titolo": "", "profilo": ""}

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
    return {
        "is_bando": data.get("is_bando"),
        "scadenza": scad,
        "titolo": (data.get("titolo") or "").strip()[:160],
        "profilo": (data.get("profilo") or "").strip()[:60],
    }
