# BandIT (ex "Argo") — istruzioni per Claude Code

Crawler Python che scandaglia ~29k siti di scuole italiane (in realtà **~4.716 istituti
autonomi**: gli altri sono plessi che condividono lo stesso sito) per intercettare
**bandi/avvisi per "esperti esterni"** e mostrarli in un portale. Repo: `github.com/DomyXVI/argo`.

> Rename "Argo" → "BandIT" **deciso ma non ancora applicato** a package/repo. Non rinominare
> finché l'utente non lo chiede esplicitamente.

---

## ⛔️ Vincoli che NON si discutono (già decisi e validati sui dati)

- **`docs/` non si committa MAI** (metriche per il capo). È gitignorato; non forzarlo.
- **`OPENAI_API_KEY` è un secret** (GitHub Actions + locale via env). Mai committarlo, mai
  stamparlo, mai chiederne l'incollaggio in chiaro in chat. Se l'utente lo incolla, **digli
  di revocarlo e rigenerarlo**.
- **Committa/pusha SOLO se non rompi il crawl.** Il workflow committa solo
  `data/snapshots/ data/discovery_report.json data/schools_map.csv`. Il DB `data/argo.db` è
  gitignorato e vive **solo nella cache di Actions**.
- **Messaggi di commit** finiscono con:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- **Stack: SQLite + snapshot JSON in git. NIENTE Postgres/VPS/R2/AWS.** Sovradimensionato per
  ~4.700 istituti e qualche centinaio di findings.
- **L'AI sta DOPO il setaccio a regole** (`classifier.py`), sui ~400-500 sopravvissuti per
  giro — NON come primo classificatore (costerebbe 200x sul flusso grezzo).
- **La scadenza la estrae l'AI leggendo il documento**, non le regole a parole-chiave (queste
  prendevano "la data più tardiva" e sbagliavano: fine-progetto PNRR / pubblicazione invece
  del termine domande). Guardrail: l'AI deve dire "non indicato" se assente, non inventare.
- **`pubdate.py` è stato cancellato apposta. Non reintrodurlo.**
- **Dipendenze esterne solo `pypdf` e `openai`**, entrambe con import protetto e degradazione
  morbida: se mancano (o manca la key), il passo si salta e il resto gira.
- **Rifiutato il rewrite proposto da Gemini** (Postgres/VPS + gpt-4o classificatore + date da
  HTML albo). Non riproporlo.

## Architettura / pipeline

`discovery` (mensile) → `crawl` (giornaliero) → `scadenza` (regex, primo passaggio) →
**`revisione AI`** (GPT-4o-mini legge il documento) → `snapshot` JSON datato.

- **DB**: SQLite in cache Actions (NON git). **Snapshot**: `data/snapshots/argo-YYYY-MM-DD.json` (git).
- **Portale**: `portal/index.html` statico, legge l'ultimo snapshot via fetch (walk a ritroso).
  Da pubblicare su GitHub Pages (Settings → Pages, deploy da `main` root → `domyxvi.github.io/argo/portal/`). **Pages ancora da abilitare a mano.**

## File chiave

- `run_crawl.py` — orchestratore crawl; step `_arricchisci_scadenze` e `_arricchisci_ai`; scrive lo snapshot (usa `titolo_pulito`, esclude `ai_bando==0`, mette `esclusi_ai` nel meta).
- `argo/classifier.py` — filtro a regole (VETO_MARKERS per scartare atti chiusi/moduli).
- `argo/ai_review.py` — `rivedi_bando()`, `testo_documento()` (HTML + primi 3 PDF, cap `max_doc_chars`), `_chiama_openai()` (logga gli errori su stderr), `disponibile()`.
- `argo/store.py` — SQLite; colonne AI: `ai_checked, ai_bando, titolo_pulito, profilo`; `findings_needing_ai()`, `set_ai_review()` (la scadenza AI **sostituisce** quella regex).
- `run_discovery.py` — `_discover_resilient()` (4 retry + backoff contro 509/throttling), dedup per `site_key`.
- `config.json` → sezione `ai`: `enabled, model=gpt-4o-mini, workers=3, timeout=30, max_per_run=400, max_doc_chars=26000`.

## Verifica veloce dell'AI (locale)

```bash
export OPENAI_API_KEY=sk-...   # NON incollarla in chat
PYTHONPATH=. python3 -c "from argo.ai_review import rivedi_bando; print(rivedi_bando('https://pertinimagliano.edu.it/avvio-selezione-docenti-esperti-competenze-base-dm-176/'))"
```
Deve stampare un dict `{is_bando, scadenza, titolo, profilo}`. Se dà `403 model_not_found`, il
**progetto OpenAI** della chiave non ha `gpt-4o-mini` abilitato (Settings → Project → Limits).

---

## 📍 Stato al 2026-06-19 (la riga di fronte)

- **Causa del fallimento AI in produzione trovata e risolta**: ogni chiamata LLM dava
  `403 PermissionDeniedError: project does not have access to model gpt-4o-mini`. Non era il
  secret né l'ambiente: era il **progetto OpenAI** senza accesso al modello. L'utente ha
  **cambiato la chiave e sistemato i permessi**; il test locale ora **passa** (ritorna il dict).
- **TODO immediato**: confermare che il **secret su GitHub** sia la chiave NUOVA, poi
  **rilanciare il crawl** (`discovery=false`, `reset=false`). Atteso: nel log `AI .../... | confermati N>0`,
  nello snapshot nuovo `esclusi_ai>0` e `aperti` in calo.
- **Snapshot 2026-06-19** (run col bug): 556 findings, `aperti:9`, `scaduti:77`,
  `scadenza_ignota:470`, `esclusi_ai:0`. I dati grezzi ci sono; manca solo l'arricchimento AI.
  Gli **87 findings "doc vuoto"** già marcati non vengono ripresi; i **~469 restanti** sì.
- **Copertura**: 81% istituti (3.810/4.716) dopo fix dedup+retry.

## Prossimi passi (oltre al rilancio)

1. **Selezione PDF migliore** in `ai_review.testo_documento`: oggi prende i primi 3 PDF in
   ordine di pagina; dare priorità al PDF che **È** il bando (nome/testo-link con
   *avviso/bando/selezione/decreto*) scartando moduli/domanda/privacy/cv.
2. **Abilitare GitHub Pages** per pubblicare il portale.
3. Eventuale **digest email** per l'ufficio.
4. **Rischio non mitigato**: il DB vive solo nella cache Actions → ~7 giorni senza run = stato
   perso (ricostruzione da zero). Valutare il commit periodico del DB in git.
