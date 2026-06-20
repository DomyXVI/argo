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

`discovery` (mensile, classifica anche `platform`) → `crawl` (giornaliero) → `scadenza` (regex,
primo passaggio) → **`revisione AI`** (GPT-4o-mini legge il documento) → `snapshot` JSON datato.

- **DB**: SQLite in cache Actions (NON git). **Snapshot**: `data/snapshots/argo-YYYY-MM-DD.json` (git).
- **Portale**: `portal/index.html` statico, legge l'ultimo snapshot via fetch (walk a ritroso).
  Da pubblicare su GitHub Pages (Settings → Pages, deploy da `main` root → `domyxvi.github.io/argo/portal/`). **Pages ancora da abilitare a mano.**
- **Lettura documenti** (in `ai_review.testo_documento`): gestisce PDF, HTML-servito-come-PDF,
  e `.docx` (`fetch.bytes_to_text`); scarta pagine login/errore (`fetch.html_is_noise`); sulle
  pagine-INDICE dell'albo isola il blocco dell'atto giusto col titolo (`_match_atto_block`,
  template `at-item`/Spaggiari + `modalDetail`/trasparenza-pa) e ne salva il link diretto
  (`doc_url`) per il bottone "Apri bando". Fallback regex sulla scadenza se l'AI rende null.

## File chiave

- `run_crawl.py` — orchestratore crawl; step `_arricchisci_scadenze` e `_arricchisci_ai`; scrive
  lo snapshot (usa `titolo_pulito`, esclude `ai_bando==0` e i **doc-vuoti** `ai_checked+ai_bando NULL`,
  mette `esclusi_ai`/`doc_vuoti` nel meta). Flag `--reset` (azzera tutto) e `--reset-ai` (azzera
  `ai_checked` dei non-esclusi → li rilegge col codice nuovo).
- `argo/crawl.py` — `crawl()`/`_scan_page`; **drill-down** verso le liste-bandi: scarta i link
  self/#frammento (gli skip-link AGID "Vai ai contenuti" sprecavano gli slot) e ordina per path
  (`_listing_rank`). `classify` gira sull'**anchor** dei link + sul corpo pagina (`require_strong`).
- `argo/classifier.py` — filtro a regole (STRONG/MEDIUM/WEAK + NEGATIVE_GUARDS + VETO_MARKERS).
  **Sano, NON è il collo di bottiglia del recall** (verificato: i borderline 0.45-0.50 sono bandi veri).
- `argo/ai_review.py` — `rivedi_bando()` (ritorna anche `doc_url`), `testo_documento()`,
  `_match_atto_block()`, `_is_doc_link()`, `_chiama_openai()` (max_retries=5, logga su stderr).
- `argo/fetch.py` — `bytes_to_text`/`looks_like_html`/`html_is_noise`/`_docx_text` (zero deps,
  pypdf passato come callback), `fetch_with_fallback`, `extract_links`, `visible_text`.
- `argo/store.py` — SQLite; colonne AI: `ai_checked, ai_bando, titolo_pulito, profilo, doc_url`;
  `findings_needing_ai()`, `set_ai_review()` (scadenza AI **sostituisce** la regex), `reset_ai_visible()`.
- `run_discovery.py` — `_discover_resilient()` (retry+backoff vs 509), dedup, classifica `platform`.
- `data/schools_map.csv` — colonna **`platform`** (tema sito) per ogni scuola (chiave per gli adapter).
- `config.json` → sezione `ai`: `enabled, model=gpt-4o-mini, workers=3, timeout=30, max_per_run=400, max_doc_chars=26000`.

## Verifica veloce dell'AI (locale)

```bash
export OPENAI_API_KEY=sk-...   # NON incollarla in chat
PYTHONPATH=. python3 -c "from argo.ai_review import rivedi_bando; print(rivedi_bando('https://pertinimagliano.edu.it/avvio-selezione-docenti-esperti-competenze-base-dm-176/'))"
```
Deve stampare un dict `{is_bando, scadenza, titolo, profilo}`. Se dà `403 model_not_found`, il
**progetto OpenAI** della chiave non ha `gpt-4o-mini` abilitato (Settings → Project → Limits).

---

## 🧭 DIREZIONE ARCHITETTURALE decisa il 2026-06-19 (leggere prima di lavorare sul crawl)

**Il problema è stato re-inquadrato.** Non sono ~4.716 siti diversi né 7 temi di sito: i bandi
li produce un **pugno di vendor SaaS di albo** (le scuole comprano l'albo, non lo costruiscono).
Il sito-scuola (`agid_wordpress` ecc.) è solo un **guscio** che embedda/rimanda al vendor.

**Distribuzione per BACKEND-albo (misurata, non il tema-sito):**
trasparenza-pa **~44%** · madisoft/Nuvola **~23%** · spaggiari **~15%** · self-hosted **~7%** ·
axios **~5%** · argo **~2%**. ⇒ **3 vendor coprono ~82%.** (NB: la colonna `platform` in
schools_map è il *tema-sito*, NON il backend; il backend va rilevato dall'HTML/URL dell'albo.)

**Architettura giusta = ingestion PER-VENDOR (non crawl per-sito). NON è un rewrite**, è un
nuovo layer dentro questo progetto (strangler-fig): `crawl` diventa un dispatch `vendor → adapter`;
dedup, AI-come-raffinatore, snapshot, portale **restano invariati**. Fallback generico+AI per la
coda lunga self-hosted (~12%). Vincoli invariati: **niente browser headless**, deps solo pypdf+openai.

**Spike vendor (fatto il 2026-06-19):**
- **madisoft (~23%, priorità):** schema URL pulito e **server-rendered** (no SPA-only):
  `nuvola.madisoft.it/bacheca-digitale/bacheca/<codiceMecc>/<id>/IN_PUBBLICAZIONE/<uuid>`. Lo
  stato `IN_PUBBLICAZIONE` = atti attivi ora. Adapter fattibile senza JS. **Da costruire.**
- **trasparenza-pa (~44%):** nessun feed pubblico; DOM-scrape. **Adapter già esistente** in
  `_match_atto_block` (link `data-bs-target=#modalDetail-<id>` → `<div id=modalDetail-<id>>` coi
  `action/download.php?file_id=`). I findings `download.php` sono doc diretti (gestiti).
- **spaggiari (~15%):** nessun feed; `web.spaggiari.eu/sdg2/Documenti/<id>`. Adapter parziale
  (`at-item`). ⚠️ ATTENZIONE: i gusci agid che embeddano Spaggiari **non sempre** usano il DOM
  `at-item` — verificare il vero embed prima di dare l'adapter per buono.
- **Nessun vendor offre RSS/JSON pubblico** (il "sogno feed" non esiste): gli adapter sono DOM/URL-scheme.

**Prossimo passo concreto:** costruire l'**adapter madisoft** (sblocca il 23% oggi quasi tutto
"ignota", lista server-rendered con UUID per atto). Poi consolidare spaggiari. Misurare ogni
adapter testa-a-testa col path generico prima di integrarlo.

## 📍 Stato dati al 2026-06-19 (dopo i fix di sessione)

Snapshot più recente: **405 findings** · `aperti:7` · `scaduti:188` · `scadenza_ignota:210` ·
`esclusi_ai:303` · `doc_vuoti:43`. Il **drill-fix ha alzato il recall** (+120 bandi nuovi vs il
run precedente di 297). Gli `ignota` restano alti perché i bandi nuovi arrivano da template-CMS
non isolabili → è ESATTAMENTE il problema che gli **adapter per-vendor** risolvono alla radice.
Gli "aperti" restano pochi: in un dato giorno ci sono davvero pochi bandi aperti (limite del
dominio, non del sistema). Date corrette ~80-85% dove il documento è leggibile.

## Altri prossimi passi (minori)

1. **Abilitare GitHub Pages** per pubblicare il portale (Settings → Pages, deploy da `main` root).
2. Eventuale **digest email** per l'ufficio.
3. **Rischio non mitigato**: il DB vive solo nella cache Actions → ~7 giorni senza run = stato
   perso (ricostruzione da zero). Valutare il commit periodico del DB in git.
4. **Varianza run-to-run** dell'AI (~±28 findings): gpt-4o-mini non deterministico sui borderline
   + albo che cambiano tra fetch. Valutare `seed` nella chiamata OpenAI. Non rilanciare reset
   completi a ripetizione (ogni rilettura rimescola i borderline).
