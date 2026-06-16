"""
Fetch HTTP tollerante + normalizzazione URL.

Condiviso da discovery.py e crawl.py. Solo stdlib (urllib): niente dipendenze.
Incorpora i due fix emersi dal prototipo di test su scuole reali:
  - `clean_url`: gestisce "http//x" (mancano i due punti), "http:/x" (un solo
    slash), maiuscole e spazi accidentali. Il `_clean_url` originale di
    legacy/anagrafe.py produceva URL rotti come "https://http//www.x.it".
  - `fallback_urls`: molte scuole sono migrate dal vecchio TLD .gov.it al nuovo
    .edu.it ma il CSV MIUR riporta ancora il dominio morto (NXDOMAIN). Si
    ritenta su .edu.it e sullo scheme alternativo. Sul campione di test questo
    ha portato la raggiungibilita' dal 61% all'84%.
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

DEFAULT_TIMEOUT = 10
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "ArgoBot/0.1 (+monitoraggio bandi scuole; uso di ricerca)"
)
MAX_BYTES = 900_000  # cap per pagina: la home/pagina trasparenza basta


def clean_url(u: str | None) -> str:
    """Normalizza un URL grezzo dal CSV MIUR. Stringa vuota se inutilizzabile."""
    u = (u or "").strip()
    if not u or u.upper() in {"NON DISPONIBILE", "N.D.", "ND", "-", ""}:
        return ""
    u = u.replace(" ", "")
    # "http//x" o "https//x" -> aggiunge i due punti
    u = re.sub(r"^(https?)//", r"\1://", u, flags=re.IGNORECASE)
    # "http:/x" (un solo slash) -> due slash
    u = re.sub(r"^(https?):/(?!/)", r"\1://", u, flags=re.IGNORECASE)
    if not re.match(r"^https?://", u, flags=re.IGNORECASE):
        u = "https://" + u
    # dominio in minuscolo, lascia intatto il path
    m = re.match(r"^(https?://)([^/]+)(.*)$", u, flags=re.IGNORECASE)
    if m:
        u = m.group(1).lower() + m.group(2).lower() + m.group(3)
    return u


def fallback_urls(url: str) -> list[str]:
    """Candidati di retry se l'URL originale non risponde."""
    out: list[str] = []
    p = urlparse(url)
    other_scheme = "http" if p.scheme == "https" else "https"
    if p.netloc.lower().endswith(".gov.it"):
        new_netloc = p.netloc[: -len(".gov.it")] + ".edu.it"
        out.append(f"{p.scheme}://{new_netloc}{p.path}")
        out.append(f"{other_scheme}://{new_netloc}{p.path}")
    out.append(f"{other_scheme}://{p.netloc}{p.path}")
    return out


def root_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}"


@dataclass
class FetchResult:
    ok: bool
    status: int | None
    final_url: str | None
    html: str
    error: str | None
    elapsed: float


def fetch(url: str, timeout: int = DEFAULT_TIMEOUT, ua: str = DEFAULT_UA) -> FetchResult:
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                html = raw.decode(charset, errors="replace")
            except LookupError:
                html = raw.decode("utf-8", errors="replace")
            return FetchResult(True, resp.status, resp.geturl(), html, None, time.time() - t0)
    except urllib.error.HTTPError as e:
        return FetchResult(False, e.code, None, "", f"HTTPError {e.code}", time.time() - t0)
    except urllib.error.URLError as e:
        return FetchResult(False, None, None, "", f"URLError {e.reason}", time.time() - t0)
    except Exception as e:  # esplorativo: un sito rotto non deve fermare il run
        return FetchResult(False, None, None, "", f"{type(e).__name__} {e}", time.time() - t0)


def fetch_with_fallback(url: str, timeout: int = DEFAULT_TIMEOUT) -> FetchResult:
    """Prova l'URL; se fallisce, ritenta sui candidati (.edu.it, scheme alt)."""
    res = fetch(url, timeout=timeout)
    if res.ok:
        return res
    for alt in fallback_urls(url):
        r2 = fetch(alt, timeout=timeout)
        if r2.ok:
            return r2
    return res  # l'originale, col suo errore


# --- estrazione link/testo (regex, no dipendenze) ----------------------------
_LINK_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                      re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Ritorna [(url_assoluto, testo_anchor)] dalla pagina."""
    out = []
    for m in _LINK_RE.finditer(html):
        href, inner = m.group(1), m.group(2)
        text = _TAG_RE.sub(" ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        try:
            full = urljoin(base_url, href)
        except ValueError:
            continue
        out.append((full, text))
    return out


def visible_text(html: str) -> str:
    """Testo grezzo della pagina senza tag (per il classifier)."""
    no_script = re.sub(r"<(script|style)\b.*?</\1>", " ", html,
                       flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", no_script)).strip()
