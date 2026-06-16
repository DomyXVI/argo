#!/usr/bin/env python3
"""
Fase 2 — CRAWL incrementale (quotidiana).

Per ogni scuola con una pagina trasparenza nota (dalla discovery), scarica
quella pagina, classifica i candidati e registra i bandi "esperto esterno" in
SQLite (dedup via fingerprint). Salva anche uno snapshot JSON datato del giro,
per il confronto con Google Alerts.

Uso:
    python3 run_crawl.py [--config config.json] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from argo.crawl import crawl
from argo.store import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    store = Store(cfg["db"])
    schools = store.schools_with_trasparenza()
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
    done = total_hits = new_hits = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(crawl, s["code"], s["trasparenza_url"], threshold,
                          timeout, max_subpages): s["code"] for s in schools}
        for fut in as_completed(futs):
            res = fut.result()
            school = by_code[res.code]
            for h in res.hits:
                total_hits += 1
                is_new = store.record_finding(
                    school, h.title, h.url, h.category, h.score,
                    school["trasparenza_url"])
                new_hits += 1 if is_new else 0
            done += 1
            if done % 200 == 0:
                store.commit()
                print(f"  {done}/{len(schools)} | hit {total_hits} | "
                      f"nuovi {new_hits} | {time.time()-t0:.0f}s")

    store.log_run("crawl", done, total_hits)
    store.commit()

    # snapshot JSON datato del giro (per confronto con Alerts)
    snap_dir = Path(cfg["snapshot_dir"])
    snap_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    findings = [dict(r) for r in store.all_findings()]
    snap = snap_dir / f"argo-{stamp}.json"
    snap.write_text(json.dumps({
        "meta": {"date": stamp, "scuole_crawlate": done,
                 "hit_totali": total_hits, "nuovi_oggi": new_hits},
        "findings": findings,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    store.close()
    print(f"\nFatto. Crawlate {done} | hit {total_hits} | nuovi oggi {new_hits} "
          f"| {time.time()-t0:.0f}s\nSnapshot: {snap}")


if __name__ == "__main__":
    main()
