#!/usr/bin/env python3
"""
Fase 1 — DISCOVERY (saltuaria, es. mensile).

Carica l'anagrafe MIUR delle regioni in config, e per ogni scuola individua la
pagina "Amministrazione Trasparente" / "Albo Pretorio". Salva tutto in SQLite
(tabella schools). Va rifatta di rado: la struttura dei siti cambia raramente.

Uso:
    python3 run_discovery.py [--config config.json] [--limit N]

--limit serve per provare su un sottoinsieme prima di lanciare le ~30k scuole.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from argo import anagrafe
from argo.discovery import discover
from argo.store import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--limit", type=int, default=0, help="0 = tutte")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    csv_path = anagrafe.ensure_csv(cfg["anagrafe"]["csv_cache"],
                                   cfg["anagrafe"]["csv_url"])
    schools = anagrafe.load(csv_path, cfg["regioni"])
    if args.limit:
        schools = schools[: args.limit]
    print(f"Scuole da processare: {len(schools)} "
          f"(regioni: {', '.join(cfg['regioni'])})")

    store = Store(cfg["db"])
    for s in schools:
        store.upsert_school_meta(s.code, s.name, s.region, s.province, s.city,
                                 s.website)
    store.commit()

    timeout = cfg["crawl"]["timeout"]
    workers = cfg["crawl"]["workers"]
    pause = cfg["crawl"].get("discovery_pause", 0.0)

    t0 = time.time()
    done = found = reachable = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(discover, s.code, s.website, timeout, pause): s
                for s in schools}
        for fut in as_completed(futs):
            d = fut.result()
            store.update_discovery(d.code, d.resolved_url, d.platform,
                                   d.trasparenza_url, d.reachable, d.note)
            done += 1
            reachable += 1 if d.reachable else 0
            found += 1 if d.trasparenza_url else 0
            if done % 200 == 0:
                store.commit()
                print(f"  {done}/{len(schools)} | raggiungibili {reachable} | "
                      f"trasparenza {found} | {time.time()-t0:.0f}s")

    store.log_run("discovery", done, found)
    store.close()
    print(f"\nFatto. Processate {done} | raggiungibili {reachable} "
          f"({100*reachable/max(done,1):.1f}%) | pagina trasparenza {found} "
          f"({100*found/max(done,1):.1f}%) | {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
