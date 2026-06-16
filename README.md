# Argo

> Argo Panoptes, il guardiano dai cento occhi. Scandaglia gli **Albi Pretori** e
> le sezioni **Amministrazione Trasparente** dei siti delle scuole per
> intercettare i bandi *"esperto esterno"* **alla fonte**, senza dipendere da
> Google Alerts.

Progetto **sperimentale e separato** da
[`bando-interceptor`](https://github.com/DomyXVI/bando-interceptor) (il sistema
live che usa i feed RSS di Google Alerts). Argo non lo sostituisce: serve a
**misurare quanti bandi in più** si intercettano crawlando direttamente le
~30.000 scuole del Nord+Centro Italia (fino al Lazio).

## L'ipotesi da verificare

Google Alerts è incompleto e spesso vuoto. I bandi però *esistono* sui siti
scolastici (obbligo di pubblicazione legale). Su un campione di test in
Lombardia: l'**84%** dei siti era raggiungibile e nel **~44%** comparivano
menzioni di bandi/incarichi esterni. Argo serve a trasformare quel "campione
suggerisce" in un **numero vero**: girando per qualche settimana in parallelo a
v1 e confrontando i risultati (`compare.py`).

## Come funziona

Due fasi distinte:

1. **Discovery** (`run_discovery.py`) — *saltuaria, es. mensile.* Dall'anagrafe
   MIUR (`dati.istruzione.it`) ricava i siti delle scuole delle regioni in
   `config.json`, e per ognuna individua la pagina trasparenza/albo (probe di
   path noti + scan dei link). Salva in SQLite.
2. **Crawl** (`run_crawl.py`) — *quotidiana.* Riscarica solo le pagine già note,
   classifica le voci con `classifier.py` (lo **stesso** scorer di v1, copiato
   apposta per confrontabilità), deduplica e salva uno snapshot JSON datato.
3. **Confronto** (`compare.py`) — Argo vs Google Alerts: comuni, esclusivi
   Argo (= uplift), esclusivi Alerts.

### Accorgimenti già incorporati (dal prototipo validato)
- **Fallback `.gov.it` → `.edu.it`**: molte scuole sono migrate di TLD ma il CSV
  MIUR riporta il dominio morto. Recupera ~23 punti di raggiungibilità.
- **`clean_url` robusto**: gestisce `http//x`, `http:/x`, maiuscole, spazi.
- **Concorrenza educata**: ~30k domini *distinti* = 1 richiesta per dominio, non
  si martella nessun sito.

## Uso locale

```bash
python3 run_discovery.py --config config.json --limit 100   # prova su 100 scuole
python3 run_crawl.py     --config config.json --limit 100
python3 compare.py --argo data/snapshots/argo-AAAA-MM-GG.json \
                   --alerts ../bando-interceptor/site/bandi.json
```

Solo **stdlib** (urllib, sqlite3, concurrent.futures): nessuna dipendenza.

## Infrastruttura

GitHub Actions (`.github/workflows/crawl.yml`): crawl giornaliero schedulato,
discovery on-demand, stato in `actions/cache`, snapshot committati nel repo.
Scelta deliberata vs. un Raspberry Pi: il lavoro è un **batch giornaliero**, non
un processo always-on — terreno di casa di Actions, zero hardware da gestire.
Unico rischio da monitorare: gli IP datacenter di Actions potrebbero prendere
più 403/challenge degli IP residenziali; se succede, *quello* sarebbe il motivo
per spostare il crawl su un Pi.

## Stato

`v0.2` — scaffold runnable + crawler profondo + classifier migliorato.

Fatto:
- [x] smoke test discovery+crawl (25 scuole): discovery ~83%, crawl trova bandi reali
- [x] **crawler profondo**: dall'indice trasparenza scende nelle liste bandi
  (Bandi di gara/concorso, Albo Pretorio), stesso dominio, `max_subpages` limitato
- [x] **classifier migliorato** (divergente da v1): tier forte/medio/debole +
  *veto* per atti a valle (graduatoria/nomina/verbale sui titoli) + *require_strong*
  sul corpo pagina per non flaggare il rumore dei menu. Test: 8/8 positivi, 8/8 trappole.

Da fare prima del lancio "vero":
- [ ] primo giro di discovery completo sulle ~29k scuole in config
- [ ] aggiungere Trentino-AA e Valle d'Aosta (dataset MIUR "AUT" separati)
- [ ] distinguere **bando aperto** vs atto a valle (verbale/graduatoria) a livello
  di pagina-lista: serve parsing delle date/scadenze (oggi il match di pagina dice
  solo "questa scuola ha attivita' esperto esterno", non se il bando e' aperto)
- [ ] classificazione sul PDF del bando (non solo anchor/pagina)
- [ ] 3-4 settimane in parallelo a v1, poi `compare.py` per il numero di uplift

### Limiti noti (onesti)
- Il match a livello di pagina cattura anche atti a valle (verbali/esiti) presenti
  nell'albo: utile come segnale "c'e' attivita'", impreciso su "bando aperto ora".
- La categoria di un hit di pagina puo' derivare da parole-menu (es. `coding`,
  `pon`): cosmetica, non influenza `is_match` (guidato dal segnale forte reale).
