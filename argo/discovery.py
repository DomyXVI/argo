"""
Discovery: per una scuola, individua la pagina "Amministrazione Trasparente" /
"Albo Pretorio" (dove per legge si pubblicano i bandi) e rileva la piattaforma.

Strategia (dal prototipo validato su scuole reali, dove ha trovato la pagina nel
~94-95% dei siti raggiungibili):
  1. probe di path noti (es. /amministrazione-trasparente/) — copre la maggioranza;
  2. se fallisce, scan dei link della home per testi-chiave (albo, trasparenza...).

E' la fase "lenta" ma poco frequente: la struttura dei siti cambia raramente,
quindi la discovery va rifatta saltuariamente (es. mensile), non ogni giorno.
"""
from __future__ import annotations

from dataclasses import dataclass

from .fetch import extract_links, fetch, fetch_with_fallback, root_of

COMMON_PATHS = [
    "/amministrazione-trasparente/",
    "/albo-pretorio-online/",
    "/albo-pretorio/",
    "/albo-online/",
    "/amministrazione-trasparente/bandi-di-gara-e-contratti/",
    "/amministrazione-trasparente/bandi-di-concorso/",
    "/index.php/albo-pretorio",
    "/pvw/app/default/albo_pretorio.php",
]

# Probe platform-aware (P2): la piattaforma e' gia' nota via detect_platform(home).
# Ogni lista e' nell'ordine giusto per quella piattaforma; GLOBAL_FALLBACK resta
# come coda di sicurezza (de-dup) cosi' nessuna scuola oggi trovata via probe si
# perde. I path hanno gia' lo slash finale (slash-normalize: ~450 scuole con
# /albo-online senza slash vengono ora prese subito dal probe invece che dal
# link_scan piu' costoso).
PLATFORM_PATHS = {
    "agid_wordpress": ["/amministrazione-trasparente/", "/albo-online/",
                       "/amministrazione-trasparente/bandi-di-gara-e-contratti/", "/albo-pretorio/"],
    "madisoft":       ["/amministrazione-trasparente/", "/albo-online/"],
    "spaggiari":      ["/amministrazione-trasparente/", "/albo-online/", "/albo-pretorio/"],
    "axios":          ["/amministrazione-trasparente/", "/albo-online/", "/albo-pretorio/"],
    "wordpress_generic": ["/amministrazione-trasparente/",
                          "/amministrazione-trasparente/bandi-di-gara-e-contratti/", "/albo-online/"],
    "generic":        ["/amministrazione-trasparente/", "/albo-online/", "/albo-pretorio/",
                       "/index.php/albo-pretorio"],  # CMS Joomla-style (es. IC con index.php)
    "argo_software":  ["/amministrazione-trasparente/"],
}
GLOBAL_FALLBACK = ["/amministrazione-trasparente/", "/albo-online/",
                   "/amministrazione-trasparente/bandi-di-gara-e-contratti/", "/albo-pretorio/"]

LINK_KEYWORDS = [
    "albo pretorio", "albo online", "amministrazione trasparente",
    "bandi di gara", "bandi e concorsi", "avvisi", "bandi", "concorsi",
    "trasparenza", "albo", "gara", "incarichi", "bandi di concorso",
    "personale", "selezione", "reclutamento",
]


def detect_platform(html: str) -> str:
    h = html.lower()
    if "wp-content" in h or "wp-includes" in h:
        if "amministrazione-trasparente" in h or "trasparenza" in h:
            return "agid_wordpress"
        return "wordpress_generic"
    if "argosoft" in h or "argoscuolanext" in h or "scuolanext" in h:
        return "argo_software"   # gestionale Argo (non confondere col progetto!)
    if "madisoft" in h or "nuvola" in h:
        return "madisoft"
    if "spaggiari" in h or "scuola&territorio" in h or "scuola e territorio" in h:
        return "spaggiari"
    if "axioscloud" in h or "axios" in h:
        return "axios"
    return "generic"


@dataclass
class Discovery:
    code: str
    reachable: bool
    resolved_url: str          # url della home effettivamente raggiunta
    platform: str
    trasparenza_url: str
    method: str                # "path_probe" | "link_scan" | ""
    note: str


def discover(code: str, site_url: str, timeout: int = 10,
             pause: float = 0.0) -> Discovery:
    """Trova la pagina trasparenza/albo per una scuola. Non lancia eccezioni."""
    import time

    if not site_url:
        return Discovery(code, False, "", "unknown", "", "", "no_site_in_csv")

    home = fetch_with_fallback(site_url, timeout=timeout)
    if not home.ok:
        return Discovery(code, False, "", "unknown", "", "", home.error or "unreachable")

    resolved = home.final_url or site_url
    platform = detect_platform(home.html)
    base = root_of(resolved)

    # 1) probe dei path piu' comuni, ordinati per piattaforma (P2).
    # de-dup mantenendo l'ordine; la coda globale resta come rete di sicurezza.
    seq = list(dict.fromkeys(
        PLATFORM_PATHS.get(platform, GLOBAL_FALLBACK) + GLOBAL_FALLBACK))
    for path in seq:
        if pause:
            time.sleep(pause)
        pr = fetch(base + path, timeout=max(6, timeout - 2))
        if pr.ok and pr.status == 200 and len(pr.html) > 200:
            return Discovery(code, True, resolved, platform, base + path,
                             "path_probe", "ok")

    # 2) scan dei link in home
    for href, text in extract_links(home.html, resolved):
        t = text.lower()
        if any(k in t for k in LINK_KEYWORDS):
            return Discovery(code, True, resolved, platform, href,
                             "link_scan", "ok")

    return Discovery(code, True, resolved, platform, "", "", "no_trasparenza_found")
