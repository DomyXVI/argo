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

import gzip
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

DEFAULT_TIMEOUT = 10
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_BYTES = 900_000  # cap per pagina: la home/pagina trasparenza basta

# Contesto SSL permissivo: molte scuole hanno cert scaduti / hostname mismatch /
# self-signed. Per un crawler di monitoraggio pubblico la verifica del cert non
# aggiunge sicurezza (leggiamo solo HTML pubblico), ma blocca ~300+ siti validi.
_LAX_SSL = ssl.create_default_context()
_LAX_SSL.check_hostname = False
_LAX_SSL.verify_mode = ssl.CERT_NONE


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Decomprime il body secondo Content-Encoding (gzip/deflate)."""
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)  # deflate "raw"
    except (OSError, zlib.error):
        return raw  # se la decompressione fallisce, ritorna il grezzo
    return raw


def clean_url(u: str | None) -> str:
    """Normalizza un URL grezzo dal CSV MIUR. Stringa vuota se inutilizzabile."""
    u = (u or "").strip()
    if not u or u.upper() in {"NON DISPONIBILE", "N.D.", "ND", "-", "", "//"}:
        return ""
    u = u.replace(" ", "")
    # rimuove schema duplicato accidentale: "https://http//x" -> "http//x"
    u = re.sub(r"^https?://(?=https?[:/])", "", u, flags=re.IGNORECASE)
    # "http//x" o "https//x" -> aggiunge i due punti
    u = re.sub(r"^(https?)//", r"\1://", u, flags=re.IGNORECASE)
    # "http:/x" (un solo slash) -> due slash
    u = re.sub(r"^(https?):/(?!/)", r"\1://", u, flags=re.IGNORECASE)
    if not re.match(r"^https?://", u, flags=re.IGNORECASE):
        u = "https://" + u
    # dominio in minuscolo, lascia intatto il path
    m = re.match(r"^(https?://)([^/]+)(.*)$", u, flags=re.IGNORECASE)
    if m:
        netloc = m.group(2).lower()
        # taglia trailing junk nel dominio: "www.x.it." o "www.x.it,"
        netloc = netloc.rstrip(".,;")
        u = m.group(1).lower() + netloc + m.group(3)
    return u


def _toggle_www(netloc: str) -> str:
    """www.x.it <-> x.it. Stringa vuota se non applicabile."""
    if netloc.lower().startswith("www."):
        return netloc[4:]
    # aggiungi www solo se non e' gia' un sottodominio profondo (x.y.it)
    return "www." + netloc


def fallback_urls(url: str) -> list[str]:
    """Candidati di retry se l'URL originale non risponde, in ordine di probabilita'.

    Copre i pattern reali del CSV MIUR:
      - .gov.it morto -> .edu.it (migrazione TLD di massa);
      - scheme alternativo (http<->https);
      - www mancante o di troppo (NXDOMAIN tipico).
    """
    out: list[str] = []
    seen = {url}
    p = urlparse(url)
    other_scheme = "http" if p.scheme == "https" else "https"
    path = p.path or "/"

    def add(scheme: str, netloc: str) -> None:
        cand = f"{scheme}://{netloc}{path}"
        if cand not in seen and netloc:
            seen.add(cand)
            out.append(cand)

    # 1) migrazione TLD .gov.it -> .edu.it (con entrambi gli schemi e www toggle)
    if p.netloc.lower().endswith(".gov.it"):
        edu = p.netloc[: -len(".gov.it")] + ".edu.it"
        add(p.scheme, edu)
        add(other_scheme, edu)
        add(p.scheme, _toggle_www(edu))

    # 2) scheme alternativo sullo stesso dominio
    add(other_scheme, p.netloc)

    # 3) toggle www (su entrambi gli schemi): recupera molti NXDOMAIN
    www_alt = _toggle_www(p.netloc)
    add(p.scheme, www_alt)
    add(other_scheme, www_alt)

    return out


def _registrable(netloc: str) -> str:
    """Dominio 'registrabile' ignorando un eventuale prefisso www. e la porta.

    Serve a capire se due URL puntano allo STESSO host: http/https e il toggle
    www. NON cambiano il dominio (stesso server fisico), mentre il salto
    .gov.it -> .edu.it SI' (host diverso, che va sempre ritentato).
    """
    n = netloc.lower().split("@")[-1].split(":")[0]
    if n.startswith("www."):
        n = n[4:]
    return n


def _same_registrable_domain(a: str, b: str) -> bool:
    """True se a e b differiscono solo per scheme o per il toggle www.
    (es. http<->https, www.x.it<->x.it). False per .gov.it<->.edu.it."""
    return _registrable(urlparse(a).netloc) == _registrable(urlparse(b).netloc)


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


# Codici che indicano throttling/blocco temporaneo: vale la pena ritentare.
# 509 = "Bandwidth Limit Exceeded" tipico degli hosting condivisi (agid_wordpress).
_RETRY_CODES = (403, 429, 503, 509)

# Politeness PER-HOST: con N worker tutti attivi, le piattaforme che ospitano molte
# scuole (portaleargo.it, web.spaggiari.eu, trasparenza-pa.net: decine di istituti
# sullo stesso dominio) verrebbero martellate da decine di richieste insieme,
# scatenando 429/509 e ban WAF. Un semaforo per dominio registrabile limita la
# concorrenza verso lo STESSO host, lasciando pieni paralleli i domini distinti.
PER_HOST_CONCURRENCY = 2
_host_sems: dict[str, threading.Semaphore] = {}
_host_sems_lock = threading.Lock()


def _host_sem(url: str) -> threading.Semaphore:
    try:
        host = _registrable(urlparse(url).netloc)
    except Exception:
        host = url
    with _host_sems_lock:
        sem = _host_sems.get(host)
        if sem is None:
            sem = threading.Semaphore(PER_HOST_CONCURRENCY)
            _host_sems[host] = sem
        return sem


def fetch(url: str, timeout: int = DEFAULT_TIMEOUT, ua: str = DEFAULT_UA,
          _retry: int = 0, _lax_ssl: bool = False) -> FetchResult:
    t0 = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "it-IT,it;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    ctx = _LAX_SSL if _lax_ssl else None
    try:
        with _host_sem(url), urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read(MAX_BYTES)
            raw = _decompress(raw, resp.headers.get("Content-Encoding", ""))
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                html = raw.decode(charset, errors="replace")
            except LookupError:
                html = raw.decode("utf-8", errors="replace")
            return FetchResult(True, resp.status, resp.geturl(), html, None, time.time() - t0)
    except urllib.error.HTTPError as e:
        # Retry su throttling/blocco temporaneo con backoff esponenziale + jitter
        if e.code in _RETRY_CODES and _retry < 3:
            time.sleep((2 ** _retry) + (time.time() % 1))
            return fetch(url, timeout=timeout, ua=ua, _retry=_retry + 1, _lax_ssl=_lax_ssl)
        return FetchResult(False, e.code, None, "", f"HTTPError {e.code}", time.time() - t0)
    except urllib.error.URLError as e:
        # Errore SSL (cert scaduto, hostname mismatch, self-signed): ritenta una
        # volta col contesto permissivo prima di arrendersi.
        if not _lax_ssl and isinstance(e.reason, ssl.SSLError):
            return fetch(url, timeout=timeout, ua=ua, _retry=_retry, _lax_ssl=True)
        # Timeout: spesso transitorio (host lento sotto carico). Un retry recupera
        # molti dei ~4.5k "timed out" senza accanirsi su host davvero morti.
        if isinstance(e.reason, (TimeoutError, OSError)) and "timed out" in str(e.reason).lower() and _retry < 1:
            time.sleep(0.5 + (time.time() % 0.5))
            return fetch(url, timeout=timeout, ua=ua, _retry=_retry + 1, _lax_ssl=_lax_ssl)
        return FetchResult(False, None, None, "", f"URLError {e.reason}", time.time() - t0)
    except TimeoutError:
        if _retry < 1:
            time.sleep(0.5 + (time.time() % 0.5))
            return fetch(url, timeout=timeout, ua=ua, _retry=_retry + 1, _lax_ssl=_lax_ssl)
        return FetchResult(False, None, None, "", "TimeoutError read timed out", time.time() - t0)
    except ssl.SSLError as e:
        if not _lax_ssl:
            return fetch(url, timeout=timeout, ua=ua, _retry=_retry, _lax_ssl=True)
        return FetchResult(False, None, None, "", f"SSLError {e}", time.time() - t0)
    except Exception as e:  # esplorativo: un sito rotto non deve fermare il run
        return FetchResult(False, None, None, "", f"{type(e).__name__} {e}", time.time() - t0)


def fetch_bytes(url: str, timeout: int = DEFAULT_TIMEOUT,
                max_bytes: int = 6_000_000) -> tuple[bool, str, bytes]:
    """Scarica un URL come byte grezzi (per i PDF: non vanno decodificati come
    testo). Ritorna (ok, content_type, data). Best-effort: in caso di errore
    (ok=False, "", b\"\"). Riusa UA/SSL permissivo come fetch()."""
    req = urllib.request.Request(url, headers={
        "User-Agent": DEFAULT_UA,
        "Accept": "application/pdf,*/*",
        "Accept-Encoding": "gzip, deflate",
    })
    for ctx in (None, _LAX_SSL):
        try:
            with _host_sem(url), urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read(max_bytes)
                raw = _decompress(raw, resp.headers.get("Content-Encoding", ""))
                ct = (resp.headers.get_content_type() or "").lower()
                return True, ct, raw
        except urllib.error.URLError as e:
            if ctx is None and isinstance(getattr(e, "reason", None), ssl.SSLError):
                continue  # ritenta col contesto permissivo
            return False, "", b""
        except Exception:
            return False, "", b""
    return False, "", b""


def fetch_with_fallback(url: str, timeout: int = DEFAULT_TIMEOUT) -> FetchResult:
    """Prova l'URL; se fallisce, ritenta sui candidati (.edu.it, scheme alt).

    Early-exit per host morti/throttled (P1): se l'originale e' NXDOMAIN, salta
    i candidati sullo STESSO dominio (toggle www/scheme inutili: l'host non
    risolve comunque); il salto .gov.it->.edu.it (dominio diverso) resta. Per
    timeout/throttling usa un timeout ridotto e disabilita i retry interni sui
    fallback: chi risponde lo fa in fretta, gli altri sono gia' morti.
    """
    res = fetch(url, timeout=timeout)
    if res.ok:
        return res
    err = (res.error or "").lower()
    is_dns = any(s in err for s in
                 ("name or service", "name resolution", "no address", "nodename"))
    is_timeout = "timed out" in err
    is_throttle = res.status in (429, 503, 509, 520, 521)
    for alt in fallback_urls(url):
        # NXDOMAIN: stesso dominio registrabile -> ancora NXDOMAIN, inutile provarlo.
        if is_dns and _same_registrable_domain(alt, url):
            continue
        if is_timeout or is_throttle:
            # timeout ridotto + niente retry interni (_retry=99 supera ogni soglia)
            r2 = fetch(alt, timeout=max(3, timeout // 2), _retry=99)
        else:
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
