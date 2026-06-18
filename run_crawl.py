#!/usr/bin/env python3
"""
Fase 2 — CRAWL incrementale (quotidiana).

Per ogni scuola con una pagina trasparenza nota (dalla discovery), scarica
quella pagina, classifica i candidati e registra i bandi "esperto esterno" in
SQLite (dedup via fingerprint). Salva anche uno snapshot JSON datato del giro,
per il confronto con Google Alerts.

Uso:
    python3 run_crawl.py [--config config.json] [--limit N] [--reset]
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

from argo.crawl import crawl
from argo import ai_review
from argo.scadenza import scadenza_da_url, stato_da_scadenza
from argo.store import Store


def _arricchisci_scadenze(store: Store, cfg: dict) -> None:
    """Per i findings non ancora controllati, scarica la pagina/PDF del bando ed
    estrae la scadenza. Resumable: marca ogni finding come controllato, cosi' un
    bootstrap pesante si completa in piu' run senza rifare il lavoro."""
    sc_cfg = cfg.get("scadenza", {})
    limit = sc_cfg.get("max_per_run", 0)
    workers = sc_cfg.get("workers", max(4, cfg["crawl"]["workers"] // 4))
    timeout = sc_cfg.get("timeout", cfg["crawl"]["timeout"])
    todo = store.findings_needing_scadenza(limit)
    if not todo:
        return
    print(f"Scadenze da cercare: {len(todo)} (workers {workers})")
    t0 = time.time()
    done = trovate = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(scadenza_da_url, r["url"], timeout): r["fingerprint"]
                for r in todo}
        for fut in as_completed(futs):
            fp = futs[fut]
            try:
                scad = fut.result()
            except Exception:
                scad = None
            store.set_scadenza(fp, scad.isoformat() if scad else None)
            done += 1
            trovate += 1 if scad else 0
            if done % 100 == 0:
                store.commit()
                print(f"  scadenze {done}/{len(todo)} | trovate {trovate} | "
                      f"{time.time()-t0:.0f}s")
    store.commit()
    print(f"Scadenze: {trovate}/{len(todo)} trovate | {time.time()-t0:.0f}s")


def _arricchisci_ai(store: Store, cfg: dict) -> None:
    """Revisione AI dei findings non ancora rivisti: legge il documento e ricava
    scadenza affidabile + titolo pulito + profilo + conferma-bando. Resumable
    (marca ai_checked). Salta del tutto se l'AI non e' configurata o senza chiave."""
    ai_cfg = cfg.get("ai", {})
    if not ai_cfg.get("enabled", False) or not ai_review.disponibile():
        return
    limit = ai_cfg.get("max_per_run", 0)
    workers = ai_cfg.get("workers", 4)
    timeout = ai_cfg.get("timeout", 30)
    model = ai_cfg.get("model", "gpt-4o-mini")
    todo = store.findings_needing_ai(limit)
    if not todo:
        return
    print(f"Revisione AI: {len(todo)} findings (model {model}, workers {workers})")
    t0 = time.time()
    done = bandi = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(ai_review.rivedi_bando, r["url"], r["title"] or "",
                          r["category"] or "", model, timeout): r["fingerprint"]
                for r in todo}
        for fut in as_completed(futs):
            fp = futs[fut]
            try:
                res = fut.result()
            except Exception:
                res = None
            if res is None:
                continue   # AI non disponibile/errore: non marcare, si ritenta
            store.set_ai_review(fp, res["is_bando"], res["scadenza"],
                                res["titolo"], res["profilo"])
            done += 1
            bandi += 1 if res.get("is_bando") else 0
            if done % 50 == 0:
                store.commit()
                print(f"  AI {done}/{len(todo)} | confermati {bandi} | "
                      f"{time.time()-t0:.0f}s")
    store.commit()
    print(f"Revisione AI: {done} processati, {bandi} confermati | {time.time()-t0:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--reset", action="store_true",
                    help="Deploy pulito: svuota findings e first_crawled prima del crawl")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    store = Store(cfg["db"])
    if args.reset:
        store.reset_baseline()
        print("Baseline azzerata (findings svuotati, first_crawled resettato).")
    schools = store.schools_with_trasparenza()
    # Dedup per trasparenza_url: i plessi di uno stesso istituto comprensivo
    # condividono la medesima pagina trasparenza. Crawlarla una volta sola taglia
    # ~75% delle richieste (14.467 -> ~3.550) e con esse il throttling 509/timeout.
    # Rappresentante STABILE = codice piu' basso del gruppo, cosi' il Delta
    # Temporale (first_crawled/origin) resta ancorato sempre alla stessa scuola.
    # NB: dedup per URL completo, non per dominio: piattaforme condivise
    # (trasparenza-pa.net, spaggiari, argo...) ospitano scuole diverse su path diversi.
    _grezzo = len(schools)
    _by_url: dict = {}
    for s in sorted(schools, key=lambda x: x["code"]):
        _by_url.setdefault(s["trasparenza_url"], s)
    schools = list(_by_url.values())
    if len(schools) < _grezzo:
        print(f"Dedup trasparenza_url: {_grezzo} plessi -> {len(schools)} pagine distinte")
    if args.limit:
        schools = schools[: args.limit]
    if not schools:
        print("Nessuna scuola con pagina trasparenza: lancia prima run_discovery.py")
        store.close()
        return
    print(f"Scuole da crawlare: {len(schools)}")

    timeout = cfg["crawl"]["timeout"]
    workers = cfg["crawl"]["workers"]
    threshold = cfg["crawl"]["classifier_threshold"]
    max_subpages = cfg["crawl"].get("max_subpages", 3)
    by_code = {s["code"]: s for s in schools}

    t0 = time.time()
    done = total_hits = new_hits = archived = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(crawl, s["code"], s["trasparenza_url"], threshold,
                          timeout, max_subpages): s["code"] for s in schools}
        for fut in as_completed(futs):
            res = fut.result()
            school = by_code[res.code]
            # Delta Temporale: se la scuola non era mai stata crawlata, cio' che
            # troviamo ora e' il suo ARCHIVIO storico, non bandi appena usciti.
            first_crawl = not school["first_crawled"]
            origin = "archivio" if first_crawl else "nuovo"
            for h in res.hits:
                total_hits += 1
                is_new = store.record_finding(
                    school, h.title, h.url, h.category, h.score,
                    school["trasparenza_url"], origin=origin)
                if is_new:
                    new_hits += 1
                    archived += 1 if first_crawl else 0
            if res.ok:
                store.mark_crawled(res.code)
            done += 1
            if done % 200 == 0:
                store.commit()
                print(f"  {done}/{len(schools)} | hit {total_hits} | "
                      f"nuovi {new_hits} | {time.time()-t0:.0f}s")

    store.log_run("crawl", done, total_hits)
    store.commit()

    # arricchimento scadenze (HTML-first, fallback PDF) sui findings non ancora visti
    _arricchisci_scadenze(store, cfg)
    # revisione AI: scadenza affidabile + titolo pulito + conferma-bando (se abilitata)
    _arricchisci_ai(store, cfg)

    # snapshot JSON datato del giro. Lo `stato` (aperto/scaduto/ignota) e'
    # calcolato a runtime da scadenza vs oggi: non va congelato nel DB.
    snap_dir = Path(cfg["snapshot_dir"])
    snap_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    oggi = date.today()
    findings = []
    stati = {"aperto": 0, "scaduto": 0, "ignota": 0}
    esclusi_ai = 0
    for r in store.all_findings():
        d = dict(r)
        if d.get("ai_bando") == 0:
            esclusi_ai += 1   # l'AI ha stabilito che non e' un bando aperto
            continue
        scad = date.fromisoformat(d["scadenza"]) if d.get("scadenza") else None
        d["stato"] = stato_da_scadenza(scad, oggi)
        if d.get("titolo_pulito"):       # titolo leggibile dall'AI, se c'e'
            d["titolo"] = d["titolo_pulito"]
        stati[d["stato"]] += 1
        findings.append(d)
    snap = snap_dir / f"argo-{stamp}.json"
    snap.write_text(json.dumps({
        "meta": {"date": stamp, "scuole_crawlate": done,
                 "hit_totali": total_hits, "nuovi_oggi": new_hits,
                 "da_archivio": archived, "nuove_pubblicazioni": new_hits - archived,
                 "aperti": stati["aperto"], "scaduti": stati["scaduto"],
                 "scadenza_ignota": stati["ignota"], "esclusi_ai": esclusi_ai},
        "findings": findings,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    store.close()
    print(f"\nFatto. Crawlate {done} | hit {total_hits} | mai visti {new_hits} "
          f"(archivio {archived}, nuove pubblicazioni {new_hits - archived}) "
          f"| {time.time()-t0:.0f}s\nSnapshot: {snap}")


if __name__ == "__main__":
    main()
