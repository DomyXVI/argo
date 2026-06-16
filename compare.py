#!/usr/bin/env python3
"""
Confronto Argo vs Google Alerts — il punto di tutto l'esperimento.

Dato uno snapshot di findings di Argo e un export dei bandi trovati da Google
Alerts (il `site/bandi.json` di bando-interceptor v1) nello stesso periodo,
calcola: quanti bandi in comune, quanti esclusivi di Argo, quanti esclusivi di
Alerts. Il match e' per URL normalizzato (euristica semplice, rivedibile).

Uso:
    python3 compare.py --argo data/snapshots/argo-2026-07-15.json \\
                       --alerts /percorso/a/bandi.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse


def norm_url(u: str) -> str:
    """Chiave di confronto: host+path senza scheme/www/slash finale/query."""
    p = urlparse((u or "").strip().lower())
    host = p.netloc.removeprefix("www.")
    path = p.path.rstrip("/")
    return f"{host}{path}"


def load_urls(path: str, key: str = "findings", url_field: str = "url") -> set[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get(key) or data.get("bandi") or []
    return {norm_url(it.get(url_field, "")) for it in items if it.get(url_field)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--argo", required=True, help="snapshot JSON di Argo")
    ap.add_argument("--alerts", required=True, help="bandi.json di v1 (Google Alerts)")
    args = ap.parse_args()

    argo = load_urls(args.argo, key="findings")
    alerts = load_urls(args.alerts, key="bandi")

    common = argo & alerts
    only_argo = argo - alerts
    only_alerts = alerts - argo

    print("=== Argo vs Google Alerts ===")
    print(f"Totale Argo:           {len(argo)}")
    print(f"Totale Alerts:         {len(alerts)}")
    print(f"In comune:             {len(common)}")
    print(f"SOLO Argo (uplift):    {len(only_argo)}")
    print(f"SOLO Alerts:           {len(only_alerts)}")
    if alerts:
        print(f"\nUplift = bandi che Argo trova e Alerts no: "
              f"+{len(only_argo)} ({100*len(only_argo)/max(len(alerts),1):.0f}% "
              f"rispetto ad Alerts)")
    if only_argo:
        print("\nEsempi esclusivi di Argo:")
        for u in list(only_argo)[:15]:
            print(f"  {u}")


if __name__ == "__main__":
    main()
