import json
import os
import hashlib
import re
import requests
import time
import atexit
import io
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
import cloudscraper
from playwright.sync_api import sync_playwright
try:
    from curl_cffi import requests as cffi_requests
    HAVE_CURL_CFFI = True
except Exception:  # optional dep; only sites with tls_impersonate need it
    HAVE_CURL_CFFI = False
from report import save_report
import issues
import jd_docs

# ─── Parallelism: per-thread stdout capture ───
# The site loop runs sites concurrently (network-bound work), but every handler
# logs with bare print(). If 12+ threads wrote to the real stdout at once the
# log would be an unreadable interleave. This proxy routes each worker thread's
# output into its OWN buffer (set via set_buffer); the main thread — which has
# no buffer set — writes straight through. The main thread then replays each
# site's captured buffer in site order, reproducing the exact sequential log.
import sys as _sys

class _ThreadCapStdout:
    def __init__(self, real):
        self._real = real
        self._tls = threading.local()
    def set_buffer(self, buf):
        self._tls.buf = buf
    def clear_buffer(self):
        self._tls.buf = None
    def write(self, s):
        buf = getattr(self._tls, "buf", None)
        (buf if buf is not None else self._real).write(s)
        return len(s)
    def flush(self):
        if getattr(self._tls, "buf", None) is None:
            self._real.flush()
    def isatty(self):
        return False
    def __getattr__(self, name):
        return getattr(self._real, name)

_sys.stdout = _ThreadCapStdout(_sys.stdout)

def _set_capture(buf):
    """Begin capturing this thread's print output into buf. No-ops safely if
    something has replaced sys.stdout (e.g. a test/logging harness) so capture
    never crashes a run — output just falls through uncaptured in that case."""
    s = _sys.stdout
    if isinstance(s, _ThreadCapStdout):
        s.set_buffer(buf)

def _end_capture():
    """Stop capturing this thread's print output."""
    s = _sys.stdout
    if isinstance(s, _ThreadCapStdout):
        s.clear_buffer()

# ─── De-duplication & hash-stability helpers ───
# Job-board URLs often carry volatile tracking params, and hash-check pages
# carry rotating tokens (cookie-consent IDs, CSRF nonces, timestamps). Both
# make an unchanged posting look "new" every run, re-triggering paid LLM
# calls. These helpers normalise away the volatile parts.

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "referrer", "source",
    "_ga", "igshid", "yclid", "msclkid", "cmpid", "campaign",
}

def _norm_url(u):
    """Canonicalise a URL for dedup. Non-URL strings (e.g. job IDs used as
    seen-keys) pass through unchanged. Drops fragments and tracking params,
    lowercases scheme/host, strips a trailing slash, and sorts the remaining
    query so param-order churn does not create a 'new' URL. Meaningful params
    (gh_jid, jobId, etc.) are preserved."""
    if not isinstance(u, str) or not u.lower().startswith(("http://", "https://")):
        return u
    try:
        p = urlsplit(u.strip())
        path = p.path.rstrip("/") or "/"
        q = [(k, v) for (k, v) in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), path,
                           urlencode(sorted(q)), ""))
    except Exception:
        return u

class _NormSet(set):
    """A set that normalises URLs on the way in and on every membership test,
    so handlers' `job_url in seen_urls` checks survive tracking-param churn
    without any change to the handlers themselves."""
    def __contains__(self, item):
        return super().__contains__(_norm_url(item))
    def add(self, item):
        super().add(_norm_url(item))

_HASH_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_HASH_HEX  = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_HASH_NUM  = re.compile(r"\b\d{8,}\b")
_HASH_WS   = re.compile(r"\s+")

def _stable_hash_text(text):
    """Strip volatile tokens (UUIDs, long hex nonces, long digit runs e.g.
    timestamps) and collapse whitespace BEFORE hashing a page. Only the hash
    input is normalised — the full text is still sent to the LLM if a real
    change is detected, so this cannot hide a genuinely new posting."""
    t = _HASH_WS.sub(" ", text)
    t = _HASH_UUID.sub("", t)
    t = _HASH_HEX.sub("", t)
    t = _HASH_NUM.sub("", t)
    return t.strip()
try:
    from playwright_stealth import stealth_sync as _stealth_sync
    _STEALTH_MODE = "page"
except ImportError:
    try:
        from playwright_stealth import Stealth
        _STEALTH_MODE = "context"
    except ImportError:
        _STEALTH_MODE = None

# ─── Load configuration ───
with open(os.environ.get("CONFIG_FILE", "config.json"), "r", encoding="utf-8") as f:
    config = json.load(f)

QUALIFICATIONS = config["qualifications"]
SITES = config["sites"]

# ─── Load CV ───
CV_FILE = "cv.txt"
if os.path.exists(CV_FILE):
    with open(CV_FILE, "r", encoding="utf-8") as f:
        CANDIDATE_CV = f.read().strip()
    print(f"Loaded CV from {CV_FILE} ({len(CANDIDATE_CV)} chars)")
else:
    CANDIDATE_CV = QUALIFICATIONS
    print(f"⚠️ {CV_FILE} not found — falling back to qualifications summary")

# ─── Secrets from environment ───
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
DRY_RUN_FILE = "dry_run.txt"
if os.path.exists(DRY_RUN_FILE):
    with open(DRY_RUN_FILE, "r") as f:
        DRY_RUN = f.read().strip().lower() == "true"
else:
    DRY_RUN = False
CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "")
CF_WORKER_TOKEN = os.environ.get("CF_WORKER_TOKEN", "")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")  # optional: higher r.jina.ai rate limit; keyless free tier works without it
RUN_LABEL = os.environ.get("RUN_LABEL", "")

# ─── Scoring / notification thresholds ───
# A role reaches Telegram only when it is ELIGIBLE and its PRIORITY (0–100)
# meets this threshold. Tunable; lower = more notifications.
PRIORITY_NOTIFY_THRESHOLD = int(os.environ.get("PRIORITY_NOTIFY_THRESHOLD", "55"))

# ─── Feedback loop (opt-in; a no-op until the env flags below are set) ───
# SITE_DATA_DIR is committed wholesale by the workflow, so feedback files live
# inside it and persist with no workflow change.
_SITE_DATA_DIR = os.environ.get("SITE_DATA_DIR", os.path.join("site", "data"))
# This lane's own record of jobs it notified (fid -> meta) so a tap resolves
# back to a role. Each lane writes ONLY its own index (conflict-safe).
FEEDBACK_INDEX_FILE = os.path.join(_SITE_DATA_DIR, "fb_index.json")
# The shared, human-meaningful feedback log the scorer learns from. Exactly ONE
# lane (FEEDBACK_DRAIN=1) writes it by draining Telegram taps; both lanes read
# it. Point both lanes at the SAME path via env (the drain lane's data dir).
FEEDBACK_LOG_FILE = os.environ.get("FEEDBACK_LOG_FILE", os.path.join(_SITE_DATA_DIR, "fb_log.json"))
# getUpdates offset, owned by the drain lane only.
FEEDBACK_OFFSET_FILE = os.path.join(_SITE_DATA_DIR, "fb_offset.json")
# Only the lane with FEEDBACK_DRAIN=1 calls getUpdates — two lanes draining one
# bot's update stream would race. Leave unset on every other lane.
FEEDBACK_DRAIN = os.environ.get("FEEDBACK_DRAIN", "") == "1"
# How many recent pursued / dismissed exemplars to fold into the prompt.
FEEDBACK_EXEMPLARS = int(os.environ.get("FEEDBACK_EXEMPLARS", "6"))


# ─── Anthropic rate limiter ───
# Sized against Claude Opus 4.7 Tier 1 (50 RPM, ~30K ITPM, ~8K OTPM); now running Opus 4.8.
# At ~3K input tokens per call the ITPM is the real bottleneck (~10 calls/min).
# We cap at 9 RPM to stay safely under the per-minute input-token ceiling.
_anthropic_calls = []
_MAX_RPM = int(os.environ.get("ANTHROPIC_MAX_RPM", "9"))
_anthropic_lock = threading.Lock()

def anthropic_rate_limit():
    """Enforce max _MAX_RPM calls per 60 seconds (staying under our Tier 1 ITPM
    ceiling). LLM evaluation runs in the sequential phase of main(), so in
    practice only one thread is here at a time — but the lock keeps the sliding
    window correct if that ever changes, at zero cost in the common case."""
    global _anthropic_calls
    with _anthropic_lock:
        now_ts = time.time()
        _anthropic_calls = [t for t in _anthropic_calls if now_ts - t < 60]
        if len(_anthropic_calls) >= _MAX_RPM:
            wait = 60 - (now_ts - _anthropic_calls[0]) + 1
            print(f"    ⏳ Anthropic rate limit — waiting {wait:.0f}s")
            time.sleep(wait)
        _anthropic_calls.append(time.time())

# ─── Error capture for issues log ───
# Handlers swallow exceptions internally and return None on failure, so the
# actual error message is otherwise lost. _record_error() is called inside
# each exception block; the main loop calls _consume_error() after every
# handler call to read and clear the captured message.
#
# IMPORTANT: error messages from libraries like `requests` often include the
# full request URL, which can contain API keys as query parameters. We redact
# common secret patterns before storing so they never reach issues.json (which
# is committed to the public repo).
# Error capture is per-thread: under the parallel site loop, many handlers may
# be recording/consuming errors at the same instant. A shared global would let
# one thread read another thread's error. threading.local keeps each worker's
# record→consume pair isolated; the main (sequential) phase uses its own slot.
_err_tls = threading.local()

# Patterns: key/token/secret in URL query params, Google API keys, Bearer tokens
_REDACT_KV = re.compile(
    r'(\b(?:key|token|api[_-]?key|auth[_-]?token|password|secret|access[_-]?token)=)[^&\s"\'<>]+',
    re.IGNORECASE,
)
_REDACT_GOOGLE_KEY = re.compile(r'AIza[0-9A-Za-z\-_]{35}')
_REDACT_BEARER = re.compile(r'(Bearer\s+)[A-Za-z0-9\-_\.]+', re.IGNORECASE)

def _redact_secrets(text):
    """Strip common secret patterns from a string before logging."""
    if not text:
        return text
    out = _REDACT_KV.sub(r'\1***', text)
    out = _REDACT_GOOGLE_KEY.sub('AIza***REDACTED***', out)
    out = _REDACT_BEARER.sub(r'\1***', out)
    return out

def _record_error(msg):
    _err_tls.value = _redact_secrets(str(msg))

def _consume_error():
    msg = getattr(_err_tls, "value", "")
    _err_tls.value = ""
    return msg


# ─── State management ───
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Cloudflare WAF on several sites (HIS Hamburg, IfG, PHF, twentyfifty,
    # Ceasefire) returns HTTP 415 when the requests-default Accept header is
    # sent. Mirror a real browser to avoid the filter.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

NO_VACANCY_PHRASES = [
    "no current vacancies",
    "no vacancies",
    "no positions available",
    "no openings",
    "check back later",
    "check back at a later date",
    "currently no vacancies",
    "no current opportunities",
    "no open positions",
    "no jobs are available",
    "no items found",
    "keine offenen positionen",
    "keine offenen stellen",
    "derzeit keine",
    "do not have any vacancies",
    "not currently recruiting",
    "currently not recruiting",
    "not currently hiring",
    "do not accept unsolicited",
    "keine ausschreibungen",
    "keine stellen ausgeschrieben",
    "keine stellenangebote ausgeschrieben",
    "no posts on the list",
]

# Anti-bot interstitial / challenge pages (Vercel, Cloudflare, etc.). When a fetch
# returns one of these short challenge stubs instead of the real page, that is a
# BLOCK, not content — and many embed a per-request nonce that would otherwise
# churn the content hash and fire an LLM call on every run. Matched only on short
# pages (see the length guard at the call site) so a real, full content page that
# merely mentions "enable JavaScript" somewhere cannot trip it.
CHALLENGE_PHRASES = [
    "vercel security checkpoint",
    "verifying your browser",
    "checking your browser",
    "enable javascript to continue",
    "just a moment",
    "ddos protection by cloudflare",
]

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════

# Retryable HTTP status codes: rate limiting + transient 5xx (incl. Cloudflare
# 521/522/523/524 which generally mean "origin temporarily unreachable").
# Note: 403/404 are NOT retried — those are persistent and need a config fix.
RETRY_STATUS_CODES = {429, 500, 502, 503, 504, 521, 522, 523, 524}
FETCH_MAX_ATTEMPTS = 3
# Backoff is now short + jittered. The old flat 10s meant a dead host could burn
# 2×10s of pure sleeping per transport before the proxy fallback even started;
# combined with a 30s connect timeout that was ~110s for ONE unreachable site,
# serially. We now fail fast on connect/DNS errors (see _classify_net_error) and
# only back off for genuinely transient server-side errors (429/5xx/read-timeout).
FETCH_RETRY_BACKOFF = 4   # base seconds; actual sleep is jittered around this

# ─── Timeouts ───
# (connect, read) tuple. The connect timeout is the important one for run speed:
# an Azure-blocked or down host used to hang for the full 30s on connect. 8s is
# plenty for any reachable host's TCP+TLS handshake; a slower connect almost
# always means "blocked / dead" → fail fast and let the proxy fallback take over.
# Read timeouts stay generous so a slow-but-alive origin is never cut off.
CONNECT_TIMEOUT = 8
HTTP_TIMEOUT = (CONNECT_TIMEOUT, 30)       # default for listing/detail fetches
HTTP_TIMEOUT_DOC = (CONNECT_TIMEOUT, 45)   # JD docs / PDFs may be large
JINA_TIMEOUT = (CONNECT_TIMEOUT, 60)       # r.jina.ai renders, so reads are slow

# ─── Concurrency ───
# The site loop is network-bound, so threads (which release the GIL during I/O)
# give near-linear speedup. HTTP/API sites run in this pool; Playwright sites run
# in their own thread group (the sync API is not thread-safe to share). Tunable
# via env so the runner can be dialled up/down without a code change.
HTTP_WORKERS = int(os.environ.get("MONITOR_HTTP_WORKERS", "12"))
PW_WORKERS = int(os.environ.get("MONITOR_PW_WORKERS", "1"))  # 2 = 2 browsers (more CPU)

# ─── Shared HTTP session (connection pooling + keep-alive) ───
# A module-level Session reuses TCP/TLS connections instead of opening a fresh
# one per request. The big win is the hosts we hit repeatedly — the Cloudflare
# Worker proxy and r.jina.ai — where keep-alive removes a handshake per call.
# urllib3's pool is thread-safe for concurrent requests; we only configure it
# once here (single-threaded) and the workers just call .get/.post/.request.
# max_retries=0 because our own ladder/_classify logic owns retry decisions.
_SESSION = requests.Session()
_pool = max(HTTP_WORKERS * 2, 20)
_adapter = requests.adapters.HTTPAdapter(pool_connections=_pool, pool_maxsize=_pool, max_retries=0)
_SESSION.mount("https://", _adapter)
_SESSION.mount("http://", _adapter)


def _backoff_sleep(attempt):
    """Short jittered backoff for transient (server-side) retries."""
    time.sleep(FETCH_RETRY_BACKOFF + random.uniform(0, 1.5) * attempt)


def _classify_net_error(e):
    """Decide what to do with a network exception:
      'switch' — egress-level failure (connect timeout, DNS, unreachable). Retrying
                 the SAME transport won't help; jump straight to the next transport
                 (the proxy fallback) with no backoff sleep. This is what turns a
                 dead host from ~110s into a few seconds.
      'retry'  — transient server-side blip (read timeout, reset/refused mid-stream);
                 worth another attempt on the same transport after a short backoff.
      'fail'   — anything else (treat as permanent).
    """
    s = str(e).lower()
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return "switch"
    if isinstance(e, requests.exceptions.ReadTimeout):
        return "retry"
    if isinstance(e, requests.exceptions.ConnectionError):
        if any(m in s for m in (
            "name or service not known", "nodename nor servname",
            "temporary failure in name resolution", "failed to resolve",
            "no address associated", "network is unreachable",
            "no route to host", "name resolution",
        )):
            return "switch"
        return "retry"
    if isinstance(e, requests.exceptions.Timeout):
        return "retry"
    return "fail"


def fetch_page(url, extra_headers=None, proxy=None, tls_impersonate=False):
    """Fetch a URL and return a BeautifulSoup object, or None on error.

    Transport ladder: the configured transport is tried first, then — if it hits
    a hard block (flat 403 / handshake refusal / connection reset), which on
    GitHub Actions is almost always Azure IP/ASN reputation filtering — the
    request is automatically retried through the Cloudflare Worker proxy, which
    egresses from a clean Cloudflare edge IP. A site that blocks the runner's IP
    therefore self-heals with no per-site config.

    Note this only changes the egress IP; it cannot solve a Cloudflare *managed
    JS challenge* (those need a real browser — see check_playwright + stealth).

    Within each transport, retries up to FETCH_MAX_ATTEMPTS on transient errors
    (HTTP 429 / 5xx incl. Cloudflare 52x, timeouts, connection errors).
    """
    hdrs = {**HEADERS, **(extra_headers or {})}

    def _do(transport):
        if transport == "tls":
            # Real Chrome JA3 fingerprint — gets past non-browser-TLS blocks
            # (e.g. SRT, Reprieve) that flat-403 or refuse the handshake.
            if not HAVE_CURL_CFFI:
                raise RuntimeError(
                    "tls_impersonate requires curl_cffi (add it to requirements.txt)"
                )
            # curl_cffi takes a single timeout value; keep it modest so a blocked
            # TLS handshake doesn't stall the worker.
            return cffi_requests.get(url, impersonate="chrome", timeout=25)
        if transport == "proxy":
            return _SESSION.get(
                CF_WORKER_URL,
                params={"url": url},
                headers={"X-Proxy-Token": CF_WORKER_TOKEN},
                timeout=HTTP_TIMEOUT,
            )
        if transport == "jina":
            # Third-party fetch-and-render (r.jina.ai): egresses from Jina's own
            # IPs and executes JS, so it reaches sites that ASN-block every cloud
            # IP we control (the Azure runner AND the Cloudflare edge). Returns
            # clean page text — ideal for page-level hashing. Keyless free tier
            # works; JINA_API_KEY just raises the rate limit.
            jhdrs = {
                "X-Return-Format": "text",
                "User-Agent": HEADERS.get("User-Agent", "Mozilla/5.0"),
            }
            if JINA_API_KEY:
                jhdrs["Authorization"] = f"Bearer {JINA_API_KEY}"
            return _SESSION.get(f"https://r.jina.ai/{url}", headers=jhdrs, timeout=JINA_TIMEOUT)
        return _SESSION.get(url, headers=hdrs, timeout=HTTP_TIMEOUT)

    primary = (
        "tls" if tls_impersonate
        else "jina" if proxy == "jina"
        else "proxy" if (proxy == "cloudflare_worker" and CF_WORKER_URL)
        else "plain"
    )
    ladder = [primary]
    # Automatic IP-reputation fallback applies only to the cloud-IP transports
    # (plain / curl_cffi); 'jina' and 'proxy' already egress off-runner, so they
    # get no redundant fallback.
    if primary in ("plain", "tls") and CF_WORKER_URL:
        ladder.append("proxy")

    last_err = None
    for transport in ladder:
        for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
            try:
                resp = _do(transport)

                # Retryable status codes: log and try again (same transport)
                if resp.status_code in RETRY_STATUS_CODES and attempt < FETCH_MAX_ATTEMPTS:
                    print(
                        f"    HTTP {resp.status_code} for {url} — retry "
                        f"(attempt {attempt}/{FETCH_MAX_ATTEMPTS})"
                    )
                    last_err = f"HTTP {resp.status_code}"
                    _backoff_sleep(attempt)
                    continue

                resp.raise_for_status()
                if transport == "proxy" and transport != primary:
                    print(f"    ↻ recovered via Cloudflare Worker proxy "
                          f"(primary '{primary}' was blocked)")
                return BeautifulSoup(resp.text, "html.parser")
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_err = e
                action = _classify_net_error(e)
                if action == "switch":
                    # Egress-level failure (connect timeout / DNS / unreachable):
                    # retrying the same transport is futile — go to the proxy now.
                    print(f"    ⇥ {type(e).__name__} for {url} — switching transport (no retry)")
                    break
                if attempt < FETCH_MAX_ATTEMPTS:
                    print(
                        f"    Network error for {url} — retry "
                        f"(attempt {attempt}/{FETCH_MAX_ATTEMPTS}): {e}"
                    )
                    _backoff_sleep(attempt)
                    continue
                break  # transient retries exhausted → fall to next transport
            except Exception as e:
                # Hard error (4xx incl. 403, handshake refusal, parse error).
                last_err = e
                break  # → fall to next transport in the ladder (e.g. proxy)
        # primary failed; loop continues to the proxy fallback if present

    _record_error(f"fetch_page: {last_err}")
    print(f"    Error fetching {url}: {last_err}")
    return None


def request_with_retry(method, url, **kwargs):
    """Like requests.request(), but retries transient failures.

    Used by the JSON/XML/RSS API handlers, which previously called
    requests.get/post directly with no retry — so a single transient blip
    (HTTP 429/5xx, a Cloudflare 52x, or a connection timeout from a GHA
    runner's datacenter IP) killed the whole site for that run. fetch_page
    already had this resilience for HTML sites; this brings the API handlers
    to parity.

    Retry policy mirrors fetch_page exactly:
      - HTTP 429 + 5xx (incl. Cloudflare 521-524): retry with backoff
      - Timeouts / connection errors: retry with backoff
      - Permanent errors (403, 404, other 4xx, parse errors): fail fast
    Returns the Response object WITHOUT calling raise_for_status(), so the
    caller keeps its existing raise_for_status()/.json()/try-except flow
    unchanged (its except block still records the error message). On a final
    transient failure the underlying exception is re-raised for the same
    reason.
    """
    last_exc = None
    # Inject a fast connect timeout if the caller passed a bare number (the API
    # handlers all pass timeout=30, meaning "30s for everything"). We keep their
    # read budget but cap the connect phase so a blocked/dead host fails fast.
    _t = kwargs.get("timeout")
    if isinstance(_t, (int, float)):
        kwargs["timeout"] = (CONNECT_TIMEOUT, _t)
    elif _t is None:
        kwargs["timeout"] = HTTP_TIMEOUT
    for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
        try:
            resp = _SESSION.request(method, url, **kwargs)
            if resp.status_code in RETRY_STATUS_CODES and attempt < FETCH_MAX_ATTEMPTS:
                print(
                    f"    HTTP {resp.status_code} for {url} — retry "
                    f"(attempt {attempt}/{FETCH_MAX_ATTEMPTS})"
                )
                _backoff_sleep(attempt)
                continue
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            # Egress-level failure won't fix on retry — surface it now so the
            # caller (which has no proxy fallback here) fails fast instead of
            # sleeping through doomed retries.
            if _classify_net_error(e) == "switch":
                raise
            if attempt < FETCH_MAX_ATTEMPTS:
                print(
                    f"    Network error for {url} — retry "
                    f"(attempt {attempt}/{FETCH_MAX_ATTEMPTS}): {e}"
                )
                _backoff_sleep(attempt)
                continue
            raise
        except Exception:
            # Permanent / non-retryable — let the caller's except handle it.
            raise
    # Exhausted retries on a retryable connection error.
    if last_exc:
        raise last_exc
    return resp


def fetch_bytes(url, proxy=None, tls_impersonate=False):
    """Download raw bytes for a linked document (PDF/DOCX), reusing the same
    transport ideas as fetch_page: plain -> curl_cffi TLS -> Cloudflare Worker.
    Returns b'' on any failure so a bad attachment never breaks a job."""
    if tls_impersonate and HAVE_CURL_CFFI:
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=30)
            if r.status_code < 400 and r.content:
                return r.content
        except Exception:
            pass
    try:
        r = _SESSION.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT_DOC, allow_redirects=True)
        if r.status_code < 400 and r.content:
            return r.content
    except Exception:
        pass
    if CF_WORKER_URL:
        try:
            r = _SESSION.get(CF_WORKER_URL, params={"url": url},
                             headers={"X-Proxy-Token": CF_WORKER_TOKEN}, timeout=HTTP_TIMEOUT_DOC)
            if r.status_code < 400 and r.content:
                return r.content
        except Exception:
            pass
    return b""


def extract_text(soup, selector=""):
    """Extract cleaned text from a BeautifulSoup object, optionally within a CSS selector."""
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    for sel in ["[class*='cookie']", "[id*='cookie']", "[class*='consent']", "[id*='consent']"]:
        for tag in soup.select(sel):
            tag.decompose()
    if selector:
        for sel in selector.split(","):
            target = soup.select_one(sel.strip())
            if target:
                text = target.get_text(separator="\n", strip=True)
                break
        else:
            text = soup.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)

def get_nested(obj, dotted_key, default=""):
    """Safely get a nested value from a dict using dot notation, e.g. 'location.city'."""
    keys = dotted_key.split(".")
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k, default)
        else:
            return default
    return obj


# ═══════════════════════════════════════════════════════════════
#  METHOD HANDLERS — each returns:
#    None                              → error / could not fetch
#    {"type": "hash_check", ...}       → content-hash sites
#    {"total": int, "new": list}       → normal job list
# ═══════════════════════════════════════════════════════════════

# ─── 1. HTML SCRAPING ───
def check_html(site, seen_urls):
    """Scrape a standard HTML careers page for job links, then fetch detail pages."""
    listing_url = site["url"]
    link_selector = site.get("link_selector", "")
    base_url = site.get("base_url", "")
    selector = site.get("selector", "")
    location_filter = site.get("location_filter", "")

    scraper = cloudscraper.create_scraper() if site.get("use_cloudscraper") else None

    def _fetch_listing(u):
        if scraper is not None:
            try:
                resp = scraper.get(u, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
                return BeautifulSoup(resp.text, "html.parser")
            except Exception as e:
                _record_error(f"cloudscraper listing: {e}")
                print(f"    Error fetching {u}: {e}")
                return None
        return fetch_page(u, proxy=site.get("proxy"), tls_impersonate=site.get("tls_impersonate", False))

    soup = _fetch_listing(listing_url)
    if not soup:
        return None

    if not link_selector:
        text = extract_text(soup, selector)
        if len(text) < 600 and any(p in text.lower() for p in CHALLENGE_PHRASES):
            # An anti-bot interstitial (Vercel/Cloudflare challenge), not the real
            # page. These carry a rotating per-request nonce, so hashing them churns
            # and fires an LLM call every run. Treat exactly like an inconclusive
            # fetch: no LLM, keep last known state, retry next run. Never a real
            # vacancy (and the real page, being long, won't reach this branch).
            print(f"    🛡️ Anti-bot challenge page detected ({len(text)} chars) — treating as blocked, keeping last state (no LLM).")
            return {"type": "insufficient_content", "chars": len(text)}
        if len(text) < 50 and not any(p in text.lower() for p in NO_VACANCY_PHRASES):
            # Sub-threshold extraction with no explicit "no vacancies" phrase is
            # almost always a flaky/partial fetch (JS shell, or a CDN node serving
            # an empty body), NOT a real content state. Returning it as a normal
            # hash_check result makes a site that intermittently returns empty
            # oscillate empty<->full and re-fire an LLM call on every run. Treat
            # it as an inconclusive read and skip (handled in main).
            print(f"    ⚠️ Very little content extracted ({len(text)} chars) — skipping (likely a flaky/JS render; not treating as a change)")
            return {"type": "insufficient_content", "chars": len(text)}
        text_hash = hashlib.sha256(_stable_hash_text(text).encode()).hexdigest()
        titles = []
        target_el = soup.select_one(selector) if selector else soup
        if target_el:
            for h in target_el.select("h2, h3, h4, h5"):
                h_text = h.get_text(strip=True)
                if h_text and len(h_text) < 100:
                    titles.append(h_text)
        jd_doc_links = []
        if site.get("follow_jd_docs"):
            jd_doc_links = jd_docs.find_doc_links(target_el or soup, listing_url)
        return {"type": "hash_check", "text": text, "hash": text_hash, "titles": titles, "jd_doc_links": jd_doc_links}


    def _collect_anchors(page_soup):
        if location_filter:
            picked = []
            for section in page_soup.select("section.openings-section"):
                header = section.select_one("header, .opening-header")
                if header and location_filter.lower() in header.get_text().lower():
                    picked.extend(section.select(link_selector))
            return picked
        if site.get("scope_links") and selector:
            scope_el = page_soup.select_one(selector.split(",")[0].strip())
            return scope_el.select(link_selector) if scope_el else []
        return page_soup.select(link_selector)

    anchors = _collect_anchors(soup)
    if location_filter:
        print(f"    Filtered to {len(anchors)} links in '{location_filter}' sections")

    # Optional pagination for sites that page results behind a query param
    # (e.g. SuccessFactors `&startrow=20`). Gated behind the "paginate" config
    # key, so every other site is byte-for-byte unaffected. Terminates at the
    # first empty/duplicate page or after max_pages, whichever comes first.
    pag = site.get("paginate")
    if pag and not location_filter and anchors:
        param = pag.get("param", "startrow")
        step = int(pag.get("step", 20))
        max_pages = int(pag.get("max_pages", 6))
        sep = "&" if "?" in listing_url else "?"
        seen_hrefs = {a.get("href", "") for a in anchors}
        for i in range(1, max_pages):
            page_url = f"{listing_url}{sep}{param}={step * i}"
            page_soup = _fetch_listing(page_url)
            if not page_soup:
                break
            new_anchors = [a for a in _collect_anchors(page_soup)
                           if a.get("href", "") not in seen_hrefs]
            if not new_anchors:
                break
            anchors.extend(new_anchors)
            seen_hrefs.update(a.get("href", "") for a in new_anchors)
            time.sleep(1)
        print(f"    Paginated '{param}': {len(anchors)} link(s) collected")
    if not anchors:
        check_text = extract_text(soup, selector)
        if len(check_text) < 100 and not any(p in check_text.lower() for p in NO_VACANCY_PHRASES):
            print(f"    ⚠️ No job links found and page content is minimal — likely JS-rendered")

    all_urls = set()
    jobs = []
    seen_in_batch = set()
    for a in anchors:
        href = a.get("href", "").strip()
        if not href or href == "#" or href == listing_url:
            continue

        title = a.get_text(strip=True) or "Untitled"
        # If the link wraps a card, prefer the heading inside it
        inner_heading = a.select_one("h2, h3, h4, h5")
        if inner_heading:
            title = inner_heading.get_text(strip=True)
        if title.lower() in ("view job", "apply", "apply now", "learn more",
                              "read more", "click here", "view"):
            card = None
            node = a.parent
            card_keywords = ("item", "card", "career", "listing", "posting", "vacancy")
            while node and node.name:
                classes = " ".join(node.get("class", []))
                role = node.get("role", "")
                if any(kw in classes.lower() for kw in card_keywords) or role == "listitem":
                    card = node
                    break
                if node.name in ("article", "li"):
                    card = node
                    break
                node = node.parent

            if card:
                heading = card.select_one(
                    "[class*='subheadline'], [class*='headline'], [class*='heading'], h2, h3, h4, h5"
                )
                if heading:
                    title = heading.get_text(strip=True)
                else:
                    continue
            else:
                continue

        full_url = urljoin(base_url + "/", href) if base_url else urljoin(listing_url, href)
        full_url = full_url.rstrip("/")

        all_urls.add(full_url)

        if full_url not in seen_urls and full_url not in seen_in_batch:
            jobs.append({"title": title, "url": full_url})
            seen_in_batch.add(full_url)

    follow_docs = site.get("follow_jd_docs")
    def _doc_bytes(u):
        return fetch_bytes(u, proxy=site.get("proxy"), tls_impersonate=site.get("tls_impersonate", False))
    new_jobs = []
    loc_excl = site.get("location_exclude")
    for job in jobs:
        if loc_excl and any(tok.lower() in job["title"].lower() for tok in loc_excl):
            print(f"    location_exclude: skipping '{job['title'][:60]}'")
            continue
        detail_soup = None
        # If the listing links directly to a document (PDF/DOCX) instead of an
        # HTML detail page (e.g. Uni Potsdam's PDF job ads), fetch the bytes and
        # extract the text directly. gather_jd_text only finds docs linked *inside*
        # a page, so it cannot handle a URL that is itself a document.
        if job["url"].split("?")[0].lower().endswith((".pdf", ".docx", ".doc")):
            time.sleep(1)
            _data = _doc_bytes(job["url"])
            _txt = jd_docs.extract_text_from_bytes(_data) if _data else ""
            new_jobs.append({
                "title": job["title"],
                "url": job["url"],
                "detail_text": (f"Title: {job['title']}\n\n{_txt}") if _txt
                               else f"Title: {job['title']} (document could not be read)",
            })
            continue
        if site.get("skip_detail_fetch") and not follow_docs:
            detail_text = f"Title: {job['title']}"
        else:
            time.sleep(1)
            if site.get("use_cloudscraper"):
                try:
                    dr = scraper.get(job["url"], timeout=HTTP_TIMEOUT)
                    dr.raise_for_status()
                    detail_soup = BeautifulSoup(dr.text, "html.parser")
                except Exception as e:
                    print(f"    Error fetching detail {job['url']}: {e}")
                    detail_soup = None
            else:
                detail_soup = fetch_page(job["url"], proxy=site.get("proxy"), tls_impersonate=site.get("tls_impersonate", False))
            if site.get("skip_detail_fetch"):
                detail_text = f"Title: {job['title']}"
            else:
                detail_text = extract_text(detail_soup) if detail_soup else ""
        if follow_docs and detail_soup is not None:
            _scope = detail_soup.select_one(site["detail_selector"].split(",")[0].strip()) if site.get("detail_selector") else detail_soup
            _doc_text, _doc_srcs = jd_docs.gather_jd_text(
                _scope or detail_soup, job["url"], _doc_bytes,
                max_docs=int(site.get("jd_max_docs", 6)),
                max_total_chars=int(site.get("jd_max_chars", 18000)))
            if _doc_text:
                detail_text = (detail_text or f"Title: {job['title']}") + _doc_text
                print(f"    + {len(_doc_srcs)} JD doc(s) appended for '{job['title'][:40]}'")
        new_jobs.append({
            "title": job["title"],
            "url": job["url"],
            "detail_text": detail_text or f"Title: {job['title']} (detail page could not be loaded)"
        })
    
    return {"total": len(all_urls), "new": new_jobs}


# ─── 2. WORKDAY API ───
def check_workday(site, seen_urls):
    """Query Workday's public JSON API for job listings, with pagination."""
    api = site["api"]
    fields = api["job_fields"]
    base_url = site["base_url"]
    detail_api_template = site.get("job_detail_api", "")

    all_postings = []
    offset = 0
    limit = api["body"].get("limit", 20)

    while True:
        body = {**api["body"], "offset": offset, "limit": limit}
        try:
            resp = request_with_retry("POST", api["url"], headers=api["headers"], json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            _record_error(f"Workday API: {e}")
            print(f"    Workday API error: {e}")
            return None if not all_postings else {"total": len(all_postings), "new": []}

        postings = data.get(api["response_key"], [])
        total = data.get(api.get("total_key", "total"), 0)
        all_postings.extend(postings)

        print(f"    Fetched {len(all_postings)}/{total} jobs (offset {offset})")

        if len(all_postings) >= total or not postings:
            break
        offset += limit
        time.sleep(1)

    new_jobs = []
    for posting in all_postings:
        title = posting.get(fields["title"], "Untitled")
        path = posting.get(fields["path"], "")
        location = posting.get(fields.get("location", ""), "")
        job_url = f"{base_url}{path}" if path else ""

        if job_url in seen_urls:
            continue

        detail_text = f"Title: {title}\nLocation: {location}"
        if detail_api_template and path:
            detail_url = detail_api_template.replace("{externalPath}", path.lstrip("/"))
            try:
                time.sleep(1)
                dr = _SESSION.get(detail_url, headers=api["headers"], timeout=HTTP_TIMEOUT)
                dr.raise_for_status()
                dd = dr.json()
                info = dd.get("jobPostingInfo", {})
                desc_html = info.get("jobDescription", "")
                if desc_html:
                    desc_soup = BeautifulSoup(desc_html, "html.parser")
                    detail_text = f"Title: {title}\nLocation: {location}\n\n{desc_soup.get_text(separator=chr(10), strip=True)}"
            except Exception as e:
                print(f"    Could not fetch detail for {title}: {e}")

        new_jobs.append({"title": title, "url": job_url, "detail_text": detail_text})

    return {"total": len(all_postings), "new": new_jobs}


# ─── 3. GREENHOUSE API ───
def check_greenhouse(site, seen_urls):
    """Query Greenhouse's public JSON API. With ?content=true, descriptions are included."""
    api = site["api"]
    fields = api["job_fields"]
    location_filter = site.get("location_filter", "")
    department_filter = site.get("department_filter", "")

    try:
        resp = request_with_retry("GET", api["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Greenhouse API: {e}")
        print(f"    Greenhouse API error: {e}")
        return None

    postings = data.get(api["response_key"], [])
    print(f"    API returned {len(postings)} jobs")

    total_after_filter = 0
    new_jobs = []
    for posting in postings:
        title = posting.get(fields["title"], "Untitled")
        job_url = posting.get(fields["url"], "")
        location = get_nested(posting, fields.get("location", ""))
        desc_html = posting.get(fields.get("description_html", ""), "")
        job_id = str(posting.get(fields.get("id", ""), ""))

        # Department filter: Greenhouse returns departments as a list of objects.
        # Accepts a string OR a list of strings — matches if ANY filter value is
        # a substring of the (joined, lowercased) department names. The list form
        # exists because a single exact label is brittle: Greenhouse boards often
        # name the department something other than you'd guess (e.g. "Policy" or
        # "Public Policy & Partnerships" rather than "Public Policy"), which
        # silently filters everything out. Use the diagnostic to find the real
        # label, or list a few plausible variants here.
        if department_filter:
            departments = posting.get("departments", [])
            dept_names = " ".join(d.get("name", "") for d in departments).lower()
            wanted = department_filter if isinstance(department_filter, list) else [department_filter]
            if not any(w.lower() in dept_names for w in wanted if w):
                continue

        # Location filter
        if location_filter:
            if location_filter.lower() not in (location or "").lower():
                continue

        total_after_filter += 1

        if job_url in seen_urls or job_id in seen_urls:
            continue

        detail_text = f"Title: {title}\nLocation: {location}"
        if desc_html:
            desc_soup = BeautifulSoup(desc_html, "html.parser")
            detail_text += f"\n\n{desc_soup.get_text(separator=chr(10), strip=True)}"

        new_jobs.append({
            "title": title,
            "url": job_url or job_id,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, job_id] if u]
        })

    if location_filter or department_filter:
        print(f"    After filtering: {total_after_filter} jobs")

    return {
        "total": total_after_filter if (location_filter or department_filter) else len(postings),
        "new": new_jobs
    }



# ─── 4. WORKABLE API ───
def check_workable(site, seen_urls):
    """Query Workable's public widget API."""
    api = site["api"]
    fields = api["job_fields"]

    try:
        resp = request_with_retry("GET", api["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Workable API: {e}")
        print(f"    Workable API error: {e}")
        return None

    postings = data.get(api["response_key"], [])
    print(f"    API returned {len(postings)} jobs")

    total_after_filter = 0
    new_jobs = []
    for posting in postings:
        title = posting.get(fields["title"], "Untitled")
        job_url = posting.get(fields["url"], "")
        city = get_nested(posting, fields.get("location_city", ""))
        country = get_nested(posting, fields.get("location_country", ""))
        department = posting.get(fields.get("department", ""), "")

        location_filter = site.get("location_filter", "")
        if location_filter:
            location_str = f"{city} {country}".lower()
            if location_filter.lower() not in location_str:
                continue

        total_after_filter += 1

        if job_url in seen_urls:
            continue

        detail_text = f"Title: {title}\nLocation: {city}, {country}\nDepartment: {department}"
        shortcode = posting.get(fields.get("shortcode", ""), "")
        if not shortcode:
            sc_m = re.search(r"/j/([A-Za-z0-9]+)", job_url or "")
            shortcode = sc_m.group(1) if sc_m else ""
        slug_m = re.search(r"/accounts/([^/?#]+)", api["url"])
        slug = slug_m.group(1) if slug_m else ""
        body = ""
        if slug and shortcode:
            time.sleep(1)
            md_url = f"https://apply.workable.com/{slug}/jobs/view/{shortcode}.md"
            try:
                mr = request_with_retry("GET", md_url, headers=HEADERS, timeout=30)
                if mr.status_code == 200 and len(mr.text) > 200:
                    body = mr.text.strip()
            except Exception as e:
                print(f"    Workable .md fetch failed ({shortcode}): {e}")
        if not body and job_url:
            time.sleep(1)
            detail_soup = fetch_page(job_url, proxy=site.get("proxy"))
            if detail_soup:
                body = extract_text(detail_soup)
        if body:
            detail_text = f"Title: {title}\nLocation: {city}, {country}\n\n{body}"

        new_jobs.append({"title": title, "url": job_url, "detail_text": detail_text})

    return {"total": total_after_filter, "new": new_jobs}


# ─── 5. PERSONIO XML ───
def check_personio(site, seen_urls):
    """Parse Personio's XML job feed."""
    api = site["api"]
    detail_template = site.get("job_detail_url_template", "")

    try:
        resp = request_with_retry("GET", api["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        _record_error(f"Personio XML: {e}")
        print(f"    Personio XML error: {e}")
        return None

    positions = root.findall(f".//{api['position_element']}")
    print(f"    XML feed returned {len(positions)} positions")

    new_jobs = []
    for pos in positions:
        job_id = pos.findtext("id", "")
        title = pos.findtext("name", "Untitled")
        office = pos.findtext("office", "")
        department = pos.findtext("department", "")
        emp_type = pos.findtext("employmentType", "")
        schedule = pos.findtext("schedule", "")

        job_url = detail_template.replace("{id}", job_id) if detail_template and job_id else ""

        if job_url in seen_urls or job_id in seen_urls:
            continue

        detail_text = (
            f"Title: {title}\nOffice: {office}\nDepartment: {department}\n"
            f"Employment Type: {emp_type}\nSchedule: {schedule}"
        )

        if job_url:
            time.sleep(1)
            detail_soup = fetch_page(job_url, proxy=site.get("proxy"))
            if detail_soup:
                page_text = extract_text(detail_soup)
                if len(page_text) > len(detail_text):
                    detail_text += f"\n\n{page_text}"

        new_jobs.append({"title": title, "url": job_url or job_id, "detail_text": detail_text})

    return {"total": len(positions), "new": new_jobs}


# ─── 6. TALEO RSS ───
def check_taleo(site, seen_urls):
    """Parse Taleo's RSS feed for job listings."""
    primary = site.get("primary_approach", {})
    rss_url = primary.get("url", "")

    if not rss_url:
        _record_error("Taleo: no RSS URL configured")
        print("    No RSS URL configured for Taleo site.")
        return None

    try:
        resp = request_with_retry("GET", rss_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        _record_error(f"Taleo RSS: {e}")
        print(f"    Taleo RSS error: {e}")
        print("    (This site may need a headless browser — see notes in config)")
        return None

    items = root.findall(".//item")
    print(f"    RSS feed returned {len(items)} items")

    new_jobs = []
    for item in items:
        title = item.findtext("title", "Untitled")
        link = item.findtext("link", "")
        description = item.findtext("description", "")

        if link in seen_urls:
            continue

        detail_text = f"Title: {title}\n\n{description}"

        if link:
            time.sleep(1)
            detail_soup = fetch_page(link, proxy=site.get("proxy"))
            if detail_soup:
                page_text = extract_text(detail_soup)
                if len(page_text) > len(detail_text):
                    detail_text = f"Title: {title}\n\n{page_text}"

        new_jobs.append({"title": title, "url": link, "detail_text": detail_text})

    return {"total": len(items), "new": new_jobs}

# ─── 7. PALLADIUM AJAX API ───
def check_palladium(site, seen_urls):
    """Query Palladium's internal AJAX endpoint and filter by country."""
    api = site["api"]
    fields = api["job_fields"]
    country_filter = api.get("country_filter", "")
    url_template = api.get("job_url_template", "")

    hdrs = {**HEADERS, **api.get("headers", {})}

    try:
        resp = request_with_retry("GET", api["url"], headers=hdrs, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Palladium API: {e}")
        print(f"    Palladium API error: {e}")
        return None

    all_jobs = data.get(api["response_key"], [])

    if country_filter:
        filtered = [j for j in all_jobs if j.get(fields["country"], "") == country_filter]
        print(f"    API returned {len(all_jobs)} jobs, {len(filtered)} in '{country_filter}'")
    else:
        filtered = all_jobs
        print(f"    API returned {len(all_jobs)} jobs")

    new_jobs = []
    for job in filtered:
        job_id = str(job.get(fields["id"], ""))
        title = job.get(fields["title"], "Untitled")
        country = job.get(fields["country"], "")

        job_url = url_template.replace("{job_id}", job_id) if url_template and job_id else ""

        if job_url in seen_urls or job_id in seen_urls:
            continue

        detail_text = f"Title: {title}\nLocation: United Kingdom\nOrganisation: Palladium Group"

        new_jobs.append({
            "title": title,
            "url": job_url or job_id,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, job_id] if u]
        })

    return {"total": len(filtered), "new": new_jobs}

# ─── 8. PINPOINT API (with cloudscraper for Cloudflare bypass) ───
def check_pinpoint(site, seen_urls):
    """Query a Pinpoint ATS /postings.json endpoint using cloudscraper."""
    api = site["api"]
    fields = api["job_fields"]
    base_url = site.get("base_url", "")

    scraper = cloudscraper.create_scraper()

    try:
        resp = scraper.get(api["url"], timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Pinpoint API: {e}")
        print(f"    Pinpoint API error: {e}")
        return None

    postings = data.get(api.get("response_key", "data"), [])
    if not isinstance(postings, list):
        postings = []
    print(f"    API returned {len(postings)} jobs")

    location_filter = site.get("location_filter", "")
    total_after_filter = 0
    new_jobs = []
    for posting in postings:
        title = posting.get(fields.get("title", "title"), "Untitled")
        job_url = posting.get(fields.get("url", "url"), "")
        location = posting.get(fields.get("location", "locationName"), "")
        if isinstance(location, dict):
            location = location.get("name", "") or location.get("label", "") or str(location)
        department = posting.get(fields.get("department", "departmentName"), "")

        # Location filter
        if location_filter and location_filter.lower() not in location.lower():
            continue

        total_after_filter += 1

        # Make URL absolute if needed
        if job_url and not job_url.startswith("http"):
            job_url = base_url.rstrip("/") + "/" + job_url.lstrip("/")

        if job_url in seen_urls:
            continue

        detail_text = f"Title: {title}\nLocation: {location}\nDepartment: {department}"

        # Fetch detail page
        if job_url:
            time.sleep(1)
            try:
                detail_resp = scraper.get(job_url, timeout=HTTP_TIMEOUT)
                if detail_resp.status_code == 200:
                    detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                    page_text = extract_text(detail_soup)
                    if len(page_text) > len(detail_text):
                        detail_text = f"Title: {title}\nLocation: {location}\n\n{page_text}"
            except Exception as e:
                print(f"    Could not fetch detail for {title}: {e}")

        new_jobs.append({"title": title, "url": job_url, "detail_text": detail_text})

    if location_filter:
        print(f"    After filtering: {total_after_filter} jobs")

    return {"total": total_after_filter if location_filter else len(postings), "new": new_jobs}

# ─── 9. PLAYWRIGHT (headless browser for JS-rendered sites) ───
def _pw_launch(p):
    """Launch a Chromium tuned for headless scraping on a CI runner. The flags
    avoid the small /dev/shm on GitHub runners and trim startup work."""
    return p.chromium.launch(args=[
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
    ])


def _pw_render(page, url, *, wait_until="networkidle", wait_selector="",
               wait_ms=5000, wait_timeout=15000, nav_timeout=30000, idle_timeout=6000):
    """Navigate and wait for content, then return page.content().

    Key change from the old inline logic: navigation ALWAYS uses
    'domcontentloaded' (fast, deterministic). If the site asked for
    'networkidle' we wait for it SEPARATELY with a bounded timeout, so a page
    that never goes idle (analytics beacons / websockets / long-polling) no
    longer stalls for the full 30s nav timeout and no longer hard-fails the
    site — we just proceed once the bound elapses. The wait_selector / wait_ms
    readiness logic is otherwise identical to before, so what counts as "loaded"
    is unchanged for every existing site.
    """
    page.goto(url, timeout=nav_timeout, wait_until="domcontentloaded")
    if wait_until == "networkidle":
        try:
            page.wait_for_load_state("networkidle", timeout=idle_timeout)
        except Exception:
            pass  # never settled — fall through to selector/timeout waits
    if wait_selector:
        try:
            page.wait_for_selector(wait_selector, timeout=wait_timeout)
        except Exception:
            # Slow or occasionally-missing render: do not hard-fail the site.
            # Wait a little longer and extract whatever loaded. A bad cycle just
            # finds 0 links and re-checks next run instead of erroring out.
            page.wait_for_timeout(wait_ms)
    else:
        page.wait_for_timeout(wait_ms)
    return page.content()


def fetch_detail_playwright(url, wait_selector="", wait_ms=5000, browser=None):
    """Fetch a detail page using Playwright and return extracted text.

    If `browser` is supplied (by the shared Playwright group), a fresh context
    is opened on it instead of launching a whole new browser per detail page.
    """
    try:
        if browser is None:
            with sync_playwright() as p:
                _b = _pw_launch(p)
                context = _b.new_context()
                page = context.new_page()
                html = _pw_render(page, url, wait_until="domcontentloaded",
                                  wait_selector=wait_selector, wait_ms=wait_ms)
                _b.close()
        else:
            context = browser.new_context()
            try:
                page = context.new_page()
                html = _pw_render(page, url, wait_until="domcontentloaded",
                                  wait_selector=wait_selector, wait_ms=wait_ms)
            finally:
                context.close()
        soup = BeautifulSoup(html, "html.parser")
        return extract_text(soup)
    except Exception as e:
        # Non-fatal: caller falls back to listing-derived text. We still record
        # so a systemic detail-fetch failure isn't completely invisible. (main()
        # consumes-and-discards this when the handler ultimately returns a valid
        # result, so it never masks a healthy run.)
        _record_error(f"fetch_detail_playwright: {e}")
        print(f"    Playwright detail fetch error: {e}")
        return ""


def strip_query_params(url):
    """Remove query parameters from a URL for cleaner deduplication."""
    return url.split("?")[0].rstrip("/")


def check_playwright(site, seen_urls, browser=None):
    """Use headless Chromium to scrape JS-rendered job listing pages.

    `browser` lets the caller pass a shared Chromium so the whole Playwright
    group reuses one process (a fresh context per site keeps cookie/cache
    isolation). When None — standalone/diagnostic use — it launches its own.
    """
    listing_url = site["url"]
    link_selector = site.get("link_selector", "")
    base_url = site.get("base_url", "")
    selector = site.get("selector", "")
    wait_selector = site.get("wait_selector", "")
    wait_ms = site.get("wait_ms", 5000)
    card_selector = site.get("card_selector", "")
    title_selector = site.get("title_selector", "")
    should_strip_params = site.get("strip_url_params", False)
    detail_via_pw = site.get("detail_via_playwright", False)
    detail_wait_sel = site.get("detail_wait_selector", "")

    use_stealth = site.get("stealth", False)
    wait_until = site.get("wait_until", "networkidle")
    wait_timeout = site.get("wait_timeout", 15000)
    nav_timeout = site.get("nav_timeout", 30000)
    idle_timeout = site.get("networkidle_timeout", 6000)

    # context-mode stealth wraps the whole sync_playwright lifecycle, so it can't
    # share the pooled browser — fall back to launching its own in that case.
    own_browser = (browser is None) or (use_stealth and _STEALTH_MODE == "context")
    _render_kw = dict(wait_until=wait_until, wait_selector=wait_selector, wait_ms=wait_ms,
                      wait_timeout=wait_timeout, nav_timeout=nav_timeout, idle_timeout=idle_timeout)
    print(f"    {'Launching' if own_browser else 'Reusing'} headless Chromium"
          f"{'  (stealth)' if use_stealth else ''}...")
    html = None
    try:
        if own_browser:
            if use_stealth and _STEALTH_MODE == "context":
                pw_cm = Stealth().use_sync(sync_playwright())
            else:
                pw_cm = sync_playwright()
            with pw_cm as p:
                _b = _pw_launch(p)
                context = _b.new_context()
                page = context.new_page()
                if use_stealth and _STEALTH_MODE == "page":
                    _stealth_sync(page)
                html = _pw_render(page, listing_url, **_render_kw)
                _b.close()
        else:
            context = browser.new_context()
            try:
                page = context.new_page()
                if use_stealth and _STEALTH_MODE == "page":
                    _stealth_sync(page)
                html = _pw_render(page, listing_url, **_render_kw)
            finally:
                context.close()
    except Exception as e:
        _record_error(f"Playwright: {e}")
        print(f"    Playwright error: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")
    if use_stealth:
        _h = [h.get_text(strip=True)[:50] for h in soup.select("h1, h2, h3")[:5]]
        print(f"    [stealth debug] {len(html)} chars, headings: {_h}")

    # Hash-check mode (no link_selector)
    if not link_selector:
        text = extract_text(soup, selector)
        if len(text) < 50:
            print(f"    ⚠️ Very little content ({len(text)} chars)")
        text_hash = hashlib.sha256(_stable_hash_text(text).encode()).hexdigest()
        titles = []
        target_el = soup.select_one(selector) if selector else soup
        if target_el:
            for h in target_el.select("h3, h4, h5"):
                h_text = h.get_text(strip=True)
                if h_text and len(h_text) < 100:
                    titles.append(h_text)
        return {"type": "hash_check", "text": text, "hash": text_hash, "titles": titles, "soup": soup}

    # ── Card-based extraction (for Oracle HCM and similar SPAs) ──
    if card_selector and title_selector:
        cards = soup.select(card_selector)
        print(f"    Found {len(cards)} job card(s) via card_selector")

        all_urls = set()
        jobs = []
        seen_in_batch = set()

        for card in cards:
            # Extract title from dedicated selector
            t_el = card.select_one(title_selector)
            title = t_el.get_text(strip=True) if t_el else "Untitled"

            # Extract link
            a = card.select_one(link_selector)
            if not a:
                continue
            href = a.get("href", "").strip()
            if not href or href == "#":
                continue

            full_url = urljoin(base_url + "/", href) if base_url else urljoin(listing_url, href)

            # Optionally strip query params for dedup
            dedup_url = strip_query_params(full_url) if should_strip_params else full_url.rstrip("/")

            all_urls.add(dedup_url)
            if dedup_url not in seen_urls and dedup_url not in seen_in_batch:
                jobs.append({"title": title, "url": dedup_url, "full_url": full_url})
                seen_in_batch.add(dedup_url)

        new_jobs = []
        for job in jobs:
            time.sleep(1)
            if detail_via_pw:
                detail_text = fetch_detail_playwright(
                    job["full_url"], detail_wait_sel, wait_ms, browser=browser
                )
            else:
                detail_soup = fetch_page(job["full_url"], proxy=site.get("proxy"))
                detail_text = extract_text(detail_soup) if detail_soup else ""
            new_jobs.append({
                "title": job["title"],
                "url": job["url"],
                "detail_text": detail_text or f"Title: {job['title']} (detail page could not be loaded)"
            })

        return {"total": len(all_urls), "new": new_jobs}

    # ── Standard anchor-based extraction (existing logic) ──
    if site.get("scope_links") and selector:
        scope_el = soup.select_one(selector.split(",")[0].strip())
        anchors = scope_el.select(link_selector) if scope_el else []
    else:
        anchors = soup.select(link_selector)
    print(f"    Found {len(anchors)} job links after JS rendering")

    all_urls = set()
    jobs = []
    seen_in_batch = set()
    for a in anchors:
        href = a.get("href", "").strip()
        if not href or href == "#":
            continue

        title = a.get_text(strip=True) or "Untitled"
        # If the link wraps a card, prefer the heading inside it
        inner_heading = a.select_one("span[class*='h4'], h2, h3, h4, h5")
        if inner_heading:
            title = inner_heading.get_text(strip=True)
        if title.lower() in ("view job", "apply", "apply now", "learn more",
                              "read more", "click here", "view"):
            card = None
            node = a.parent
            card_keywords = ("item", "card", "career", "listing", "posting", "vacancy")
            while node and node.name:
                classes = " ".join(node.get("class", []))
                role = node.get("role", "")
                if any(kw in classes.lower() for kw in card_keywords) or role == "listitem":
                    card = node
                    break
                if node.name in ("article", "li"):
                    card = node
                    break
                node = node.parent

            if card:
                heading = card.select_one(
                    "[class*='subheadline'], [class*='headline'], [class*='heading'], h2, h3, h4, h5"
                )
                if heading:
                    title = heading.get_text(strip=True)
                else:
                    continue
            else:
                continue

        full_url = urljoin(base_url + "/", href) if base_url else urljoin(listing_url, href)
        full_url = full_url.rstrip("/")

        all_urls.add(full_url)
        if full_url not in seen_urls and full_url not in seen_in_batch:
            jobs.append({"title": title, "url": full_url})
            seen_in_batch.add(full_url)

    follow_docs = site.get("follow_jd_docs")
    def _pw_doc_bytes(u):
        return fetch_bytes(u, proxy=site.get("proxy"), tls_impersonate=site.get("tls_impersonate", False))
    new_jobs = []
    for job in jobs:
        time.sleep(1)
        detail_soup = None
        if detail_via_pw:
            detail_text = fetch_detail_playwright(job["url"], detail_wait_sel, wait_ms, browser=browser)
        else:
            detail_soup = fetch_page(job["url"], proxy=site.get("proxy"))
            detail_text = extract_text(detail_soup) if detail_soup else ""
        if follow_docs and detail_soup is not None:
            _scope = detail_soup.select_one(site["detail_selector"].split(",")[0].strip()) if site.get("detail_selector") else detail_soup
            _doc_text, _doc_srcs = jd_docs.gather_jd_text(
                _scope or detail_soup, job["url"], _pw_doc_bytes,
                max_docs=int(site.get("jd_max_docs", 6)),
                max_total_chars=int(site.get("jd_max_chars", 20000)))
            if _doc_text:
                detail_text = (detail_text or f"Title: {job['title']}") + _doc_text
                print(f"    + {len(_doc_srcs)} JD doc(s) appended for '{job['title'][:40]}'")
        _job_entry = {
            "title": job["title"],
            "url": job["url"],
            "detail_text": detail_text or f"Title: {job['title']} (detail page could not be loaded)"
        }
        # Optional deterministic pre-LLM filter (e.g. Marburg): only evaluate a job
        # if its DETAIL page matches `detail_must_match` (a regex). Marburg's
        # Fachbereich (fb03/fb06) appears only in the detail-page Ausschreibungs-ID,
        # never in the listing link — so we read the detail (already fetched above),
        # keep faculty matches, and mark the rest seen WITHOUT an LLM call. Fail open:
        # a failed/empty detail fetch is never filtered, so a genuinely relevant role
        # is never silently dropped on a transient fetch hiccup.
        _mm = site.get("detail_must_match")
        if _mm and detail_text and not re.search(_mm, detail_text, re.IGNORECASE):
            _job_entry["_skip_eval"] = True
            _job_entry["_skip_reason"] = f"detail_must_match /{_mm}/ not in detail page"
        new_jobs.append(_job_entry)

    return {"total": len(all_urls), "new": new_jobs}

# ─── 10. ORACLE HCM REST API ───
def check_oracle_hcm(site, seen_urls):
    """Query Oracle HCM Cloud's public REST API for job listings."""
    api = site["api"]
    listing_url = api["listing_url"]
    detail_template = api.get("detail_url_template", "")
    page_template = api.get("job_page_template", "")

    try:
        resp = request_with_retry("GET", listing_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Oracle HCM API: {e}")
        print(f"    Oracle HCM API error: {e}")
        return None

    items = data.get("items", [])
    if not items:
        print(f"    No items in API response")
        return {"total": 0, "new": []}

    search_result = items[0]
    jobs = search_result.get("requisitionList", [])
    total = search_result.get("TotalJobsCount", len(jobs))
    print(f"    API returned {len(jobs)} job(s) (total: {total})")

    new_jobs = []
    for job in jobs:
        job_id = str(job.get("Id", ""))
        title = job.get("Title", "Untitled")
        posted = job.get("PostedDate", "")
        country = job.get("PrimaryLocationCountry", "")
        primary_loc = job.get("PrimaryLocation", "")
        category = job.get("Category", "")
        short_desc = job.get("ShortDescriptionStr", "")

        # Build the human-facing job page URL
        job_url = page_template.replace("{job_id}", job_id) if page_template else ""

        if job_url in seen_urls or job_id in seen_urls:
            continue

        # Build basic detail text from listing data
        detail_text = f"Title: {title}\nLocation: {primary_loc or country}\nCategory: {category}\nPosted: {posted}"

        # Fetch full description from detail API
        if detail_template and job_id:
            detail_api_url = detail_template.replace("{job_id}", job_id)
            try:
                time.sleep(1)
                dr = _SESSION.get(detail_api_url, headers=HEADERS, timeout=HTTP_TIMEOUT)
                dr.raise_for_status()
                dd = dr.json()
                detail_items = dd.get("items", [])
                if detail_items:
                    d = detail_items[0]
                    # Combine all description fields
                    desc_parts = []
                    for key in ["ExternalDescriptionStr", "ExternalQualificationsStr",
                                "ExternalResponsibilitiesStr"]:
                        html_val = d.get(key, "")
                        if html_val:
                            clean = BeautifulSoup(html_val, "html.parser").get_text(separator="\n", strip=True)
                            if clean:
                                desc_parts.append(clean)
                    if desc_parts:
                        detail_text = f"Title: {title}\nLocation: {primary_loc or country}\nCategory: {category}\nPosted: {posted}\n\n" + "\n\n".join(desc_parts)
            except Exception as e:
                print(f"    Could not fetch detail for {title}: {e}")

        new_jobs.append({
            "title": title,
            "url": job_url or job_id,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, job_id] if u]
        })

    return {"total": total, "new": new_jobs}

# ─── 11. HIRESERVE ATS JSON API ───
def check_hireserve(site, seen_urls):
    """Query a Hireserve ATS JSON feed, with optional category filtering."""
    api = site["api"]
    fields = api["job_fields"]

    # Build URL with params + category filters
    params = api.get("params", {})
    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    cat_filters = api.get("category_filters", [])
    if cat_filters:
        query_string += "&" + "&".join(
            f"p_category_code_arr={code}" for code in cat_filters
        )
    full_url = f"{api['url']}?{query_string}"

    try:
        resp = request_with_retry("GET", full_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Hireserve API: {e}")
        print(f"    Hireserve API error: {e}")
        return None

    postings = data.get(api["response_key"], [])
    print(f"    API returned {len(postings)} job(s)")

    new_jobs = []
    for posting in postings:
        job_id = str(posting.get(fields["id"], ""))
        title = posting.get(fields["title"], "Untitled")
        job_url = posting.get(fields["url"], "")

        # Extract business unit from classifications for detail text
        business_unit = ""
        classifications = posting.get("classifications", {})
        bu_class = classifications.get("class_17744", {})
        bu_values = bu_class.get("values", [])
        if bu_values:
            business_unit = bu_values[0].get("class_val", "")

        # Extract closing date
        closing = ""
        pub = posting.get("publication", {}).get("internet", {})
        closing = pub.get("closing_date", "")

        if job_url in seen_urls or job_id in seen_urls:
            continue

        detail_text = f"Title: {title}\nBusiness Unit: {business_unit}\nClosing: {closing}"

        # Fetch detail page
        if job_url:
            time.sleep(1)
            detail_soup = fetch_page(job_url, proxy=site.get("proxy"))
            if detail_soup:
                page_text = extract_text(detail_soup)
                if len(page_text) > len(detail_text):
                    detail_text = f"Title: {title}\nBusiness Unit: {business_unit}\nClosing: {closing}\n\n{page_text}"
                if site.get("follow_jd_docs"):
                    _hb = lambda u: fetch_bytes(u, proxy=site.get("proxy"), tls_impersonate=site.get("tls_impersonate", False))
                    _dt, _ds = jd_docs.gather_jd_text(detail_soup, job_url, _hb, max_docs=int(site.get("jd_max_docs", 6)), max_total_chars=int(site.get("jd_max_chars", 20000)))
                    if _dt:
                        detail_text = detail_text + _dt
                        print(f"    + {len(_ds)} JD doc(s) appended for '{title[:40]}'")

        new_jobs.append({
            "title": title,
            "url": job_url or job_id,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, job_id] if u]
        })

    return {"total": len(postings), "new": new_jobs}


# ─── 12. PLUMM CAREERS API (heyplumm.com) ───
def _plumm_slug(title):
    """Replicate Plumm's URL slug: lowercase, spaces→hyphens, strip punctuation
    (except hyphens and en-/em-dashes), URL-encode."""
    import re as _re
    import urllib.parse as _up
    s = title.lower().replace(" ", "-")
    # Keep word chars (letters/digits/underscore), hyphens, en-dash, em-dash
    s = _re.sub(r"[^\w\-\u2013\u2014]", "", s)
    return _up.quote(s, safe="-")


def check_plumm_api(site, seen_urls):
    """Query Plumm's CareerSite/GetOpenJobs endpoint."""
    api = site["api"]
    body = api.get("body", {})
    referer = api.get("referer", site.get("url", ""))
    location_filter = site.get("location_filter", "")
    job_url_template = site.get(
        "job_url_template",
        "https://app.heyplumm.com/jobs/{company}/{slug}/{id}?refer=Plumm",
    )

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "User-Agent": HEADERS["User-Agent"],
    }

    try:
        resp = request_with_retry("POST", api["url"], headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Plumm API: {e}")
        print(f"    Plumm API error: {e}")
        return None

    if not data.get("status"):
        _record_error(f"Plumm API: status=false (raw: {str(data)[:200]})")
        print(f"    Plumm API returned status=false (raw: {str(data)[:200]})")
        return None

    postings = data.get("data", []) or []
    print(f"    API returned {len(postings)} job(s)")

    total_after_filter = 0
    new_jobs = []
    for posting in postings:
        title = posting.get("JobTitle", "Untitled")
        encoded_id = posting.get("EncodedId", "")
        company = (posting.get("companyNameUrl", "") or "").lower()
        city = posting.get("City", "") or ""
        country = posting.get("Country", "") or ""
        salary = posting.get("Salary", "") or ""
        job_type = posting.get("JobType", "") or ""
        workplace = posting.get("WorkplaceType", "") or ""
        time_elapsed = posting.get("TimeElapsed", "") or ""
        experience = posting.get("ExperienceRequired", "")

        # Location filter (case-insensitive substring on city + country)
        if location_filter:
            location_str = f"{city} {country}".lower()
            if location_filter.lower() not in location_str:
                continue

        total_after_filter += 1

        # Build job URL
        slug = _plumm_slug(title)
        job_url = (
            job_url_template
            .replace("{company}", company)
            .replace("{slug}", slug)
            .replace("{id}", encoded_id)
        )

        if job_url in seen_urls or encoded_id in seen_urls:
            continue

        detail_text = (
            f"Title: {title}\n"
            f"Location: {city}, {country}\n"
            f"Workplace Type: {workplace}\n"
            f"Job Type: {job_type}\n"
            f"Salary: {salary}\n"
            f"Experience Required: {experience} years\n"
            f"Posted: {time_elapsed}"
        )

        new_jobs.append({
            "title": title,
            "url": job_url,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, encoded_id] if u],
        })

    if location_filter:
        print(f"    After filtering: {total_after_filter} job(s)")

    return {
        "total": total_after_filter if location_filter else len(postings),
        "new": new_jobs,
    }


# ─── 13. RIPPLING ATS API ───
def check_rippling_api(site, seen_urls):
    """Query Rippling's public board API.

    Endpoint shape: https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs
    Returns a JSON array of job objects with keys: uuid, name, url,
    workLocation: {label, id}, department: {label, id}.
    """
    api = site["api"]
    location_filter = site.get("location_filter", "")

    try:
        resp = request_with_retry(
            "GET",
            api["url"],
            headers={**HEADERS, "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Rippling API: {e}")
        print(f"    Rippling API error: {e}")
        return None

    # Response is a list at the top level
    postings = data if isinstance(data, list) else data.get("jobs", []) or []
    print(f"    API returned {len(postings)} job(s)")

    total_after_filter = 0
    new_jobs = []
    for posting in postings:
        title = posting.get("name", "Untitled")
        job_url = posting.get("url", "")
        uuid = posting.get("uuid", "")
        loc = posting.get("workLocation") or {}
        location = loc.get("label", "") if isinstance(loc, dict) else str(loc)
        dept = posting.get("department") or {}
        department = dept.get("label", "") if isinstance(dept, dict) else str(dept)

        # Location filter: case-insensitive substring match.
        # Note: Rippling returns one primary workLocation, but multi-city jobs
        # often list every city in the job's title or detail page rather than
        # this field. Sites that want multi-location coverage should leave
        # location_filter empty and let the LLM filter on detail text.
        if location_filter:
            if location_filter.lower() not in location.lower():
                continue

        total_after_filter += 1

        if job_url in seen_urls or uuid in seen_urls:
            continue

        detail_text = (
            f"Title: {title}\n"
            f"Location: {location}\n"
            f"Department: {department}"
        )

        # Fetch detail page for full description if not explicitly disabled
        if not site.get("skip_detail_fetch") and job_url:
            time.sleep(1)
            detail_soup = fetch_page(job_url, proxy=site.get("proxy"))
            if detail_soup:
                page_text = extract_text(detail_soup)
                if len(page_text) > len(detail_text):
                    detail_text = (
                        f"Title: {title}\n"
                        f"Location: {location}\n"
                        f"Department: {department}\n\n{page_text}"
                    )

        new_jobs.append({
            "title": title,
            "url": job_url or uuid,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, uuid] if u],
        })

    if location_filter:
        print(f"    After filtering: {total_after_filter} job(s)")

    return {
        "total": total_after_filter if location_filter else len(postings),
        "new": new_jobs,
    }


# ─── 14. ENGAGE|ats (Havas People) — POST-paginated vacancy search ───
# Platform used by e.g. LSE (jobs.lse.ac.uk). The public vacancy list is
# server-rendered only 5 per page and paginates ONLY via a POST to
# <origin>/V2/Vacancy/ApplySearchFilter that returns JSON {"searchResults":
# "<html>"}. Per-job links live in a data-param1 attribute on
# <button class="btn-search-results-view">, not in an <a href> — so neither the
# plain HTML handler nor its link_selector works here. This handler POSTs
# PageNo=1..N (reading the "Page X of N" footer to learn N), collects every
# (title, url), then fetches each NEW detail page exactly like check_html — and
# additionally pulls the linked job-description / person-specification PDFs into
# the text sent to the API, since on LSE the real selection criteria live in
# those attachments rather than on the page. No login / CSRF / encrypted state
# is needed — the channel is passed in the payload as Type ("Internal"/"External").
#
# Config keys:
#   api_url            : full ApplySearchFilter endpoint                  (required)
#   vac_type           : "Internal" | "External" (the #vacancyType value) (required)
#   landing            : a landing URL fetched once to warm a session cookie (optional;
#                        falls back to url)
#   base_url           : origin, used to absolutise links (already absolute — safety net)
#   detail_selector    : CSS scope for detail extraction (e.g. div.view-vacancy-container)
#   attachment_include : ordered list of attachment categories to fetch & append.
#                        categories: "person_spec", "job_description", "how_to_apply",
#                        "other". Default ["person_spec", "job_description"]. The order
#                        also controls assembly order (person spec first so it survives
#                        any eval truncation). Add "how_to_apply" to include the generic
#                        application-notes PDF too.
#   max_pdf_chars      : per-attachment text cap (default 25000)
#   max_pages          : pagination safety cap (default 25)
# NB: pair this with "eval_max_chars" on the same site (e.g. 30000) so the appended
# attachment text isn't cut by the default 10k prompt truncation.

def _engage_attach_category(title):
    """Map an attachment's title to (category, display heading). Titles vary
    ('job description' / 'job\xa0description', 'person specification' /
    'the person specification'), so normalise before matching."""
    t = re.sub(r"\s+", " ", (title or "").replace("\xa0", " ")).strip().lower()
    t = re.sub(r"^the\s+", "", t)
    if "person specification" in t or t == "person spec":
        return "person_spec", "Person Specification"
    if "job description" in t:
        return "job_description", "Job Description"
    if "how to apply" in t:
        return "how_to_apply", "How to Apply"
    return "other", (title or "Attachment").strip()


def _engage_pdf_text(sess, href, cap):
    """Download a ViewAttachment link and return its extracted PDF text (capped).
    Returns '' on any failure so a bad attachment never breaks the job."""
    try:
        r = sess.get(href, headers=HEADERS, timeout=HTTP_TIMEOUT_DOC)
        r.raise_for_status()
        data = r.content
    except Exception as e:
        print(f"      attachment download failed: {e}")
        return ""
    if data[:4] != b"%PDF":
        return ""  # not a PDF (e.g. docx) — skip; the visible text still carries the role
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(data))
        txt = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as e:
        print(f"      PDF parse failed: {e}")
        return ""
    return txt[:cap]


def _engage_build_detail(sess, detail_soup, detail_selector, include, max_pdf_chars):
    """Visible vacancy text plus the text of the selected linked PDF attachments,
    ordered so the most scoring-relevant content leads."""
    visible = extract_text(detail_soup, detail_selector)
    scope = detail_soup.select_one(detail_selector) if detail_selector else detail_soup
    by_cat = {}
    if scope is not None:
        for a in scope.select("a[href]"):
            href = a.get("href", "")
            if "viewattachment.aspx" not in href.lower():
                continue
            cat, heading = _engage_attach_category(a.get("title") or a.get_text(" ", strip=True))
            if cat not in include:
                continue
            txt = _engage_pdf_text(sess, href, max_pdf_chars)
            if txt:
                by_cat.setdefault(cat, []).append((heading, txt))
    parts = [visible] if visible else []
    for cat in include:                      # include order controls assembly order
        for heading, txt in by_cat.get(cat, []):
            parts.append(f"=== {heading} (attachment) ===\n{txt}")
    return "\n\n".join(parts)


def check_engage_ats(site, seen_urls):
    api_url = site["api_url"]
    vac_type = site.get("vac_type", "External")
    landing = site.get("landing") or site.get("url", "")  # url doubles as cookie warm-up
    base_url = site.get("base_url", "")
    detail_selector = site.get("detail_selector", "")
    attach_include = site.get("attachment_include", ["person_spec", "job_description"])
    max_pdf_chars = int(site.get("max_pdf_chars", 25000))
    max_pages = int(site.get("max_pages", 25))

    sess = requests.Session()
    if landing:
        try:
            sess.get(landing, headers=HEADERS, timeout=HTTP_TIMEOUT)
        except Exception:
            pass  # cookie warm-up is best-effort; Type is sent explicitly anyway

    ajax_headers = {
        **HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    def _post_results(page):
        """POST one page of results; return its searchResults HTML or None."""
        body = {
            "searchControlViewModel[Criteria]": "",
            "searchControlViewModel[PostCode]": "",
            "searchControlViewModel[TravelDistance]": "",
            "searchControlViewModel[SortBy]": "",
            "searchControlViewModel[Type]": vac_type,
            "searchControlViewModel[PageNo]": str(page),
        }
        for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
            try:
                resp = sess.post(api_url, headers=ajax_headers, data=body, timeout=HTTP_TIMEOUT)
                if resp.status_code in RETRY_STATUS_CODES and attempt < FETCH_MAX_ATTEMPTS:
                    time.sleep(FETCH_RETRY_BACKOFF)
                    continue
                resp.raise_for_status()
                return (resp.json() or {}).get("searchResults", "") or ""
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < FETCH_MAX_ATTEMPTS:
                    time.sleep(FETCH_RETRY_BACKOFF)
                    continue
                break
            except Exception as e:
                _record_error(f"engage_ats {vac_type} page {page}: {e}")
                return None
        _record_error(f"engage_ats {vac_type} page {page}: exhausted retries")
        return None

    def _parse(results_html):
        soup = BeautifulSoup(results_html, "html.parser")
        pairs = []
        for b in soup.select(".btn-search-results-view"):
            url = (b.get("data-param1") or "").strip()
            if not url:
                continue
            title = "Untitled"
            node = b
            for _ in range(8):  # walk up to the vacancy card for its heading
                node = node.parent
                if node is None:
                    break
                h = node.select_one(".ats-heading-font")
                if h and h.get_text(strip=True):
                    title = h.get_text(strip=True)
                    break
            pairs.append((title, url))
        m = re.search(r"Page\s+\d+\s+of\s+(\d+)", results_html)
        return pairs, (int(m.group(1)) if m else 1)

    first = _post_results(1)
    if first is None:
        return None  # genuine fetch failure -> counts toward pause, like other handlers
    pairs, n_pages = _parse(first)
    n_pages = min(n_pages, max_pages)

    collected = {}  # url -> title (also dedupes the rare cross-page repeat)
    for title, url in pairs:
        collected.setdefault(url, title)
    for pg in range(2, n_pages + 1):
        time.sleep(1)
        html = _post_results(pg)
        if not html:
            continue  # skip a flaky page rather than abort the whole site
        more, _ = _parse(html)
        for title, url in more:
            collected.setdefault(url, title)

    all_urls = set()
    new_jobs = []
    for url, title in collected.items():
        full_url = urljoin(base_url + "/", url) if base_url else url
        full_url = full_url.rstrip("/")
        all_urls.add(full_url)
        if full_url in seen_urls:
            continue
        if site.get("skip_detail_fetch"):
            detail_text = f"Title: {title}"
        else:
            time.sleep(1)
            detail_soup = None
            try:
                dr = sess.get(full_url, headers=HEADERS, timeout=HTTP_TIMEOUT)
                dr.raise_for_status()
                detail_soup = BeautifulSoup(dr.text, "html.parser")
            except Exception as e:
                print(f"    Error fetching detail {full_url}: {e}")
            detail_text = _engage_build_detail(
                sess, detail_soup, detail_selector, attach_include, max_pdf_chars
            ) if detail_soup is not None else ""
        new_jobs.append({
            "title": title,
            "url": full_url,
            "detail_text": detail_text or f"Title: {title} (detail page could not be loaded)",
        })

    return {"total": len(all_urls), "new": new_jobs}



# ─── 15. BAMBOOHR API ───
def check_bamboohr(site, seen_urls):
    """BambooHR public careers JSON: /careers/list for the listing, then
    /careers/{id}/detail for each description. Job URL is /careers/{id}."""
    api = site["api"]
    company = api["company"]
    list_url = f"https://{company}.bamboohr.com/careers/list"
    try:
        resp = request_with_retry("GET", list_url, headers={**HEADERS, "Accept": "application/json"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"BambooHR API ({site['name']}): {e}")
        print(f"    BambooHR API error: {e}")
        return None

    results = data.get("result", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    print(f"    API returned {len(results)} job(s)")

    new_jobs = []
    for job in results:
        jid = str(job.get("id", ""))
        if not jid:
            continue
        title = job.get("jobOpeningName") or job.get("title") or "Untitled"
        loc = job.get("location") or {}
        if isinstance(loc, dict):
            loc_s = ", ".join(str(loc.get(k, "")) for k in ("city", "state", "country") if loc.get(k))
        else:
            loc_s = str(loc)
        dept = job.get("departmentLabel", "")
        job_url = f"https://{company}.bamboohr.com/careers/{jid}"
        if job_url in seen_urls:
            continue
        detail_text = f"Title: {title}\nLocation: {loc_s}\nDepartment: {dept}"
        try:
            time.sleep(1)
            dr = _SESSION.get(f"https://{company}.bamboohr.com/careers/{jid}/detail",
                              headers={**HEADERS, "Accept": "application/json"}, timeout=HTTP_TIMEOUT)
            dr.raise_for_status()
            dd = dr.json()
            res = dd.get("result", dd) if isinstance(dd, dict) else {}
            desc_html = (res.get("description") or res.get("jobDescription")
                         or res.get("descriptionHtml") or "")
            if desc_html:
                desc = BeautifulSoup(desc_html, "html.parser").get_text(separator="\n", strip=True)
                detail_text = f"Title: {title}\nLocation: {loc_s}\nDepartment: {dept}\n\n{desc}"
        except Exception as e:
            print(f"    Could not fetch BambooHR detail for {title}: {e}")
        new_jobs.append({"title": title, "url": job_url, "detail_text": detail_text})

    return {"total": len(results), "new": new_jobs}


# ─── 16. RECRUITEE API ───
def check_recruitee(site, seen_urls):
    """Recruitee public offers JSON: /api/offers/ (descriptions are inline,
    so no detail fetch is needed). Job URL is the offer's careers_url."""
    api = site["api"]
    try:
        resp = request_with_retry("GET", api["url"], headers={**HEADERS, "Accept": "application/json"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Recruitee API ({site['name']}): {e}")
        print(f"    Recruitee API error: {e}")
        return None

    offers = data.get("offers", []) if isinstance(data, dict) else []
    print(f"    API returned {len(offers)} offer(s)")

    follow_docs = site.get("follow_jd_docs")
    def _doc_bytes(u):
        return fetch_bytes(u, proxy=site.get("proxy"), tls_impersonate=site.get("tls_impersonate", False))

    new_jobs = []
    for o in offers:
        title = o.get("title", "Untitled")
        loc = ", ".join(x for x in [o.get("city", ""), o.get("country", "") or o.get("country_code", "")] if x)
        job_url = o.get("careers_url") or o.get("url") or o.get("careers_apply_url", "")
        if not job_url or job_url in seen_urls:
            continue
        body = (o.get("description") or "") + "\n" + (o.get("requirements") or "")
        body_soup = BeautifulSoup(body, "html.parser")
        body_text = body_soup.get_text(separator="\n", strip=True)

        doc_text = ""
        if follow_docs:
            doc_text, doc_srcs = jd_docs.gather_jd_text(
                body_soup, job_url, _doc_bytes,
                max_docs=int(site.get("jd_max_docs", 6)),
                max_total_chars=int(site.get("jd_max_chars", 20000)))
            if doc_srcs:
                print(f"    + {len(doc_srcs)} JD doc(s) for '{title[:40]}'")

        # Assemble: header, body, linked JD. With jd_priority the JD leads
        # (after the header) so a downstream detail_text[:max_chars] cut drops
        # the trailing on-page summary, never the JD. Else JD appended last.
        jd_first = site.get("jd_priority")
        detail_text = f"Title: {title}\nLocation: {loc}"
        if doc_text and jd_first:
            detail_text += doc_text
        if body_text.strip():
            detail_text += "\n\n" + body_text
        if doc_text and not jd_first:
            detail_text += doc_text
        new_jobs.append({"title": title, "url": job_url, "detail_text": detail_text})

    return {"total": len(offers), "new": new_jobs}


# ─── 17. GENERIC JSON API (SiteHub / Talos360 / EasyWeb / Wagtail) ───
def check_json_api(site, seen_urls):
    """Configurable JSON endpoint reader with defensive field extraction.

    Supports GET/POST, custom headers and POST body, a (possibly nested,
    dot-notation) response_key, and per-item field paths with common-name
    fallbacks. Designed to cope with ATS feeds whose item schema can't be
    seen until a role is actually posted; set "dump_first_item": true to log
    the first live item so the exact field names can be locked in later.
    """
    api = site["api"]
    fields = api.get("job_fields", {})
    method = api.get("http_method", "GET").upper()
    headers = {**HEADERS, "Accept": "application/json", **api.get("headers", {})}
    base = site.get("base_url", "")
    listing_url = site.get("url", "")

    try:
        if method == "POST":
            resp = request_with_retry("POST", api["url"], headers=headers, json=api.get("body", {}), timeout=30)
        else:
            resp = request_with_retry("GET", api["url"], headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"JSON API ({site['name']}): {e}")
        print(f"    JSON API error: {e}")
        return None

    rk = api.get("response_key", "")
    items = get_nested(data, rk, []) if rk else data
    if isinstance(items, dict):
        items = next((v for v in items.values() if isinstance(v, list)), [])
    if not isinstance(items, list):
        items = []
    print(f"    API returned {len(items)} item(s)")
    if api.get("dump_first_item") and items and isinstance(items[0], dict):
        try:
            print(f"    [json_api] first-item keys: {list(items[0].keys())[:30]}")
            print(f"    [json_api] first-item sample: {json.dumps(items[0], default=str)[:800]}")
        except Exception:
            pass

    TITLE_KEYS = ["title", "jobTitle", "name", "position", "vacancyTitle", "jobOpeningName", "job_title", "displayName"]
    URL_KEYS = ["url", "careers_url", "applyUrl", "apply_url", "vacancyUrl", "absolute_url", "jobUrl", "link", "href", "detailUrl", "permalink"]
    LOC_KEYS = ["location", "locationsText", "city", "town", "jobLocation", "location_name", "locationName"]
    ID_KEYS = ["id", "jobId", "job_id", "vacancyId", "shortcode", "slug", "reference", "ref"]
    DESC_KEYS = ["description", "jobDescription", "content", "body", "details", "summary", "descriptionHtml", "job_description"]

    def pick(item, configured, common):
        if configured:
            v = get_nested(item, configured, "")
            if v:
                return v
        for k in common:
            v = item.get(k) if isinstance(item, dict) else None
            if v:
                return v
        return ""

    tmpl = api.get("job_url_template", "")
    new_jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = pick(item, fields.get("title"), TITLE_KEYS) or "Untitled"
        loc = pick(item, fields.get("location"), LOC_KEYS)
        if isinstance(loc, dict):
            loc = ", ".join(str(loc.get(k, "")) for k in ("city", "name", "country") if loc.get(k))
        if isinstance(loc, list):
            loc = ", ".join(str(x) for x in loc if x)
        url = pick(item, fields.get("url"), URL_KEYS)
        if not isinstance(url, str):
            url = ""
        jid = pick(item, fields.get("id"), ID_KEYS)
        if not url and tmpl and jid:
            url = tmpl.replace("{id}", str(jid)).replace("{slug}", str(jid))
        if isinstance(url, str) and url.startswith("/"):
            url = urljoin(base or listing_url, url)
        if not url:
            # _norm_url strips fragments, so a "#{jid}" fallback would collapse every
            # posting to one dedup key (silent miss). Use a query param, which survives
            # normalisation and stays unique per id — covers empty-job_fields ATS feeds
            # (e.g. RSA/ODI) until their real field paths can be locked in.
            if jid:
                _sep = "&" if "?" in listing_url else "?"
                url = f"{listing_url}{_sep}jobId={jid}"
            else:
                url = listing_url
        if url in seen_urls:
            continue
        desc = pick(item, fields.get("description_html") or fields.get("description"), DESC_KEYS)
        detail_text = f"Title: {title}\nLocation: {loc}"
        if desc:
            desc_text = BeautifulSoup(str(desc), "html.parser").get_text(separator="\n", strip=True)
            if desc_text.strip():
                detail_text += "\n\n" + desc_text[:6000]
        elif api.get("fetch_detail_html") and isinstance(url, str) and url.startswith("http"):
            try:
                time.sleep(1)
                detail_soup = fetch_page(url, proxy=site.get("proxy"), tls_impersonate=site.get("tls_impersonate", False))
                if detail_soup:
                    detail_text += "\n\n" + extract_text(detail_soup)[:6000]
            except Exception as e:
                print(f"    Could not fetch detail {url}: {e}")
        new_jobs.append({"title": str(title), "url": url, "detail_text": detail_text})

    return {"total": len(items), "new": new_jobs}


# ═══════════════════════════════════════════════════════════════
#  DISPATCHER — routes each site to the correct handler
# ═══════════════════════════════════════════════════════════════

METHOD_HANDLERS = {
    "html": check_html,
    "workday_api": check_workday,
    "greenhouse_api": check_greenhouse,
    "workable_api": check_workable,
    "personio_xml": check_personio,
    "taleo_rss": check_taleo,
    "palladium_api": check_palladium,
    "pinpoint_api": check_pinpoint,
    "playwright": check_playwright,
    "oracle_hcm_api": check_oracle_hcm,
    "hireserve_api": check_hireserve,
    "plumm_api": check_plumm_api,
    "rippling_api": check_rippling_api,
    "engage_ats": check_engage_ats,
    "bamboohr_api": check_bamboohr,
    "recruitee_api": check_recruitee,
    "json_api": check_json_api,
}


# ═══════════════════════════════════════════════════════════════
#  AI MATCHING — send job details to Anthropic
# ═══════════════════════════════════════════════════════════════
#
# Architecture:
#   - System prompt holds the rubric, format, examples, and CV. It is
#     identical across every call so we mark it as cacheable. Anthropic
#     reads cached input at ~10% of normal price, which roughly cuts
#     monthly cost in half.
#   - User message holds only the per-call data: org name, URL, job text,
#     and a one-line marker switching between page-level and single-job
#     mode. This part is dynamic and not cached.
#   - Determinism on this scoring task comes from the rubric structure and
#     the formula-based MATCH derivation in the system prompt. Note that
#     Claude Opus 4.8 rejects temperature/top_p/top_k parameters outright
#     (400 error) — these must be omitted.

SYSTEM_PROMPT = f"""You are an expert career assistant evaluating job postings against the CV of one specific candidate. The candidate's full CV is included at the end of this prompt — refer to it whenever you assess a role.

────────────────────────────────────────
HOW TO RATE (READ FIRST)
────────────────────────────────────────

You rate each role in two independent layers.

  LAYER 1 — ELIGIBILITY (a hard gate, yes/no).
  Can the candidate actually hold this role, and is it a real paid role they
  would take? A role is INELIGIBLE only when it trips a rule in HARD
  DISQUALIFIERS below — it requires a qualification, language, level or
  experience the candidate plainly lacks, or it is unpaid / student-only.
  Eligibility is about hard blockers, NOT about how exciting the topic is.
  When no hard rule clearly applies, the role is ELIGIBLE — be generous here.

  LAYER 2 — PRIORITY (a single number, 0–100).
  For an eligible role, how strongly should it rise to the top of the
  candidate's limited attention? One holistic score that blends, in roughly
  this order of weight:
    • Paper fit — is it genuinely at the candidate's level (entry / early-
      career), and does the candidate have, or can quickly close, the skills
      it asks for? This is the largest input.
    • Interest — does it sit in one of the candidate's active interest areas?
      A real boost, never a veto.
    • Winnability — given this profile, is it a realistic application rather
      than a long shot?
  An INELIGIBLE role always scores PRIORITY = 0.

Do NOT collapse the score into coarse buckets in your head — reason about
where on the 0–100 line the role honestly sits, using the anchors below. Two
roles that are both "good" can and should land on different numbers.

If a "LEARNED SIGNALS" section is supplied separately (it lists roles the
candidate has personally marked as relevant or not), use it to calibrate
borderline priorities: nudge toward roles resembling the ones the candidate
pursued and away from the ones they dismissed. It refines PRIORITY only; it
never overrides a HARD DISQUALIFIER.

────────────────────────────────────────
CANDIDATE STRENGTHS & GAPS (fixed — do not re-derive each call)
────────────────────────────────────────

Strengths the candidate can credibly offer:
  • LSE MSc IR (in progress) + Bonn BA at 1.3 — a strong academic signal for
    London and European policy, research and risk roles.
  • Distinctive specialism: international law, genocide / atrocity prevention,
    transitional justice, European foreign and security policy.
  • Research and writing: policy and academic writing, literature review,
    qualitative and quantitative analysis (SPSS, Python, Excel at analysis
    level); a published author.
  • Practical: research-assistant work, project administration, event and
    research support, and helping allocate ~€1.7m of federal funding at the
    German Rectors' Conference.
  • Languages: German (native), English (C2).

Hard gaps — real limits on paper fit (these lower PRIORITY, and some trigger a
HARD DISQUALIFIER):
  • No full-time professional experience (student-assistant / RA only) — a
    role demanding several years of post-qualification experience is a poor
    fit, and past roughly 3 years required it is a disqualifier.
  • French is B1 (working, not fluent), with no other working languages — a
    role that requires fluency in French or another non-English language is
    out.
  • Not a qualified lawyer — no UK qualifying law degree / GDL / SQE / CILEX —
    so roles requiring a legal qualification or admission to practise are out.
  • Python / data is analysis-level, NOT ML or software engineering — deep-
    technical research-engineer or ML-heavy roles are a substantial skills
    gap, not a fit.
  • Limited dedicated communications / marketing-campaign and events-
    management experience — comms- or events-specialist roles are a partial
    gap (they lower PRIORITY; they do not disqualify).
  • No security-operations, physical-security, GSOC, military or clearance
    background — operational-security / risk-monitoring roles are a poor fit
    even at otherwise on-target firms.

────────────────────────────────────────
THE CANDIDATE'S INTEREST AREAS (these raise INTEREST and PRIORITY)
────────────────────────────────────────

Three zones, all valid and all genuinely high-interest — do NOT rank zone (a)
above (b) or (c):

  (a) Academic / policy research — think-tanks, universities, multilateral
      bodies, NGOs working on international law, atrocity prevention,
      transitional justice, European foreign policy, security studies,
      higher-education policy.
  (b) Commercial intelligence & geopolitical risk — analyst roles at firms
      like Control Risks, Sibylline, S-RM, IISS, Hakluyt, Verisk Maplecroft,
      Eurasia Group, Teneo, and similar geopolitical / threat-intelligence
      operations.
  (c) Hybrid research / OSINT / digital investigations / tech policy —
      computational research at policy institutes (CETaS at the Alan Turing
      Institute, Ada Lovelace, Oxford Internet Institute, AI Security
      Institute), open-source investigations (Bellingcat, Centre for
      Information Resilience, Forensic Architecture), digital-humanities labs,
      and tech / AI / platform-policy research.

Read past surface framing: a commercial or technical-sounding title in zones
(b) or (c) is a core target, not a stretch. A role outside all three zones
can still be a perfectly good PAPER fit for an eligible, on-level position —
it simply scores lower on the interest component, which lowers but does not
sink its PRIORITY.

Score INTEREST (1–5) by zone fit: 5 = squarely in the candidate's core
mission (international law / atrocity prevention / transitional justice /
human rights / European security); 4 = clearly in zone (b) or (c); 3 =
adjacent policy or research of plausible interest; 2 = tangential; 1 =
topically uninspiring. INTEREST is an INPUT to PRIORITY, reported for
transparency — it is not a separate gate.

────────────────────────────────────────
HARD DISQUALIFIERS (force ELIGIBLE = no, PRIORITY = 0)
────────────────────────────────────────

A role the candidate cannot get, or would not take, is ineligible however
interesting. Mark ELIGIBLE = no if ANY of these clearly applies:

  • Requires, as an ESSENTIAL, a qualification the candidate would already need
    to hold and does not: an already-awarded / completed PhD (e.g. postdoctoral
    roles, lectureships, "PhD required", "PhD holder", "doctorate required");
    or a legal qualification / bar admission (qualified lawyer, solicitor,
    barrister, GDL / SQE / CILEX, "qualified" legal counsel). NOTE: a doctoral
    / PhD *candidate* position — where the PhD is what the candidate would
    pursue, not a prerequisite to be hired — is NOT caught by this; see PhD
    POSITIONS below.
  • Requires fluency in a language beyond German / English as an ESSENTIAL
    (e.g. "fluent French / Arabic / Spanish required", "native-level X"). B1
    French does not meet a fluency requirement. Treat a language as desirable,
    not essential, only when the posting clearly says so.
  • Clearly not entry-level: a Manager / Senior / Lead / Principal / Head /
    Director title, or an ESSENTIAL requirement of roughly 3+ years of
    relevant post-graduation experience. (One to two years that an internship
    or RA work could plausibly satisfy is NOT a disqualifier — it lowers
    PRIORITY instead.)
  • Not a genuine paid role for the candidate: working-student / Werkstudent,
    internship / Praktikum, apprenticeship, volunteer / unpaid, or a purely
    speculative / "register your interest" / talent-pool posting with no
    concrete vacancy. A funded PhD position or scholarship — a salaried
    doctoral post or a stipend / studentship — IS a genuine paid role and is
    NOT excluded here; only a PhD explicitly advertised as unfunded /
    self-funded is.
  • A deep-technical / ML / software-engineering role whose ESSENTIAL
    requirements (production software, ML research, named heavy frameworks)
    the candidate's analysis-level Python cannot meet.

Distinguish ESSENTIAL from DESIRABLE. A "nice to have" the candidate lacks is
NOT a disqualifier — it lowers PRIORITY. Only an unmet ESSENTIAL gates.

────────────────────────────────────────
PhD POSITIONS (eligible — the candidate is open to a doctorate)
────────────────────────────────────────

A funded doctoral / PhD position is a valid and welcome target. Treat it as
ELIGIBLE and score it as an entry-level, early-career role, with INTEREST set
by field as usual (a doctorate on international law / atrocity prevention /
transitional justice / European security is INTEREST 5).

  • ELIGIBLE: salaried PhD / doctoral-researcher posts (e.g. a German
    wissenschaftliche*r Mitarbeiter*in / Promotionsstelle on TV-L), funded
    UK / EU PhD studentships, doctoral fellowships, and "fully funded"
    Promotionsstipendien. A part-time (e.g. 65% / 75%) salaried PhD post is
    normal for the field — do NOT treat the part-time fraction as a
    disqualifier. The candidate's MSc satisfies the entry requirement.
  • NOT eligible: a PhD explicitly advertised as unfunded or requiring
    self-funding (treat it like other unpaid roles). When funding is not
    stated, give the benefit of the doubt and treat the position as eligible.
  • Still NOT eligible (unchanged): roles requiring an ALREADY-COMPLETED PhD —
    postdocs, lectureships, "PhD required / awarded". Here the candidate would
    be pursuing a PhD, not holding one.

────────────────────────────────────────
PRIORITY ANCHORS (0–100)
────────────────────────────────────────

First decide ELIGIBLE. If no, PRIORITY = 0. If yes, place the role on this
scale:

  85–100  Bullseye. Genuinely entry-level, the candidate has (or can trivially
          close) the skills, AND it sits in a core interest area. The handful
          of roles to apply to first.
  70–84   Strong. Eligible and on-level with good skills coverage; either a
          core-interest role with a small gap, or a near-flawless fit in an
          adjacent area. Clearly worth applying to.
  55–69   Good. A solid, realistic application — on-level with a closeable
          skills gap, or a strong fit whose interest is more moderate.
  40–54   Worth a look. Eligible but with a real soft obstacle: a ~1–2 year
          ask the candidate only partly meets, a partial skills gap, or
          limited interest pull.
  20–39   Marginal. Eligible but a weak fit — sizeable (though not
          disqualifying) gaps, or little interest alignment.
   1–19   Barely worth surfacing — eligible on a technicality but a poor fit
          on nearly every axis.
      0   Ineligible (a HARD DISQUALIFIER applies).

When genuinely torn between two adjacent bands for an accessible, on-profile
role, choose the higher one — too few good roles reach the candidate, so a
missed match costs more than an extra look.

────────────────────────────────────────
LOCATION
────────────────────────────────────────
Apply the "LOCATION POLICY" line provided in the user message, exactly:

  • "information only" — do NOT factor location into the rating. Report it for
    information only; a role in another city or country does not change
    eligibility or PRIORITY. (This is the default and applies to most sites.)

  • "London only" — location is a HARD FILTER. If the role would require being
    based outside London with NO London option and NO fully-remote-UK option,
    set ELIGIBLE = no and PRIORITY = 0.
      - EXCLUDE when the posting is tied to another UK city (Manchester,
        Birmingham, Bristol, Leeds, Edinburgh, Glasgow, Cardiff, Belfast,
        Oxford, Cambridge) or another country (The Hague, Geneva, Brussels,
        Berlin, Nairobi, New York) with no London or fully-remote option.
      - DO NOT exclude when the role is in London; lists London among several
        or "flexible" / "multiple" locations; is hybrid with a London office;
        is fully remote and open to UK applicants; or states no location at
        all. Assess those normally and report LOCATION as given (or "Not
        specified").

────────────────────────────────────────
OUTPUT FORMAT
────────────────────────────────────────

Use these exact labels, one per line, in this order. The user message will
provide the ORGANISATION and URL — copy them verbatim into your response.

JOB: [job title]
ORGANISATION: [as provided in the user message]
LOCATION: [city/country from job content; "Not specified" if absent]
TYPE: [Full-time / Part-time / Internship / Contract; "Not specified" if absent]
DEADLINE: [application deadline if stated; "Not specified" if absent]
SALARY: [salary or pay range if stated; "Not specified" if absent]
ELIGIBLE: [yes / no]
PRIORITY: [an integer from 0 to 100; 0 if ELIGIBLE is no]
INTEREST: [1-5]
REASON: [2-3 sentences. Lead with the paper-fit verdict (level and skills), then the interest angle. If ELIGIBLE is no, name the exact disqualifier first.]
URL: [as provided in the user message]

PAGE-LEVEL MODE
If the user message begins with "PAGE-LEVEL SCAN", the content is a careers
page that may list multiple jobs. Identify each individual job posting and
emit the format above for each one, separated by a blank line. If no jobs are
found on the page at all, respond with exactly: NO_JOBS_FOUND

LANGUAGE
Job content may be in German, French, Spanish, etc. Assess regardless of the
source language. Output in English.

────────────────────────────────────────
CALIBRATION EXAMPLES
────────────────────────────────────────

Example 1 — core fit, entry-level (bullseye)
An entry-level "Research Officer" at a human-rights think-tank working on
transitional justice, open to recent graduates, asking for strong research
and writing and one statistical package →
  ELIGIBLE: yes  INTEREST: 5  PRIORITY: 92
  REASON: Genuinely entry-level with the research and writing the candidate
  clearly has; the one named tool is closeable. It sits squarely in the core
  mission, so it belongs at the very top of the list.

Example 2 — commercial intelligence (strong, despite commercial framing)
An entry-level "Geopolitical Risk Analyst" at a consultancy like Sibylline,
open to graduates, wanting research, writing and an interest in international
affairs →
  ELIGIBLE: yes  INTEREST: 4  PRIORITY: 80
  REASON: On-level, and the research and writing transfer directly, so it is a
  winnable application. Commercial intelligence is a core target zone, not a
  fallback, so it rates highly.

Example 3 — adjacent field, on-level (good, lower interest)
An entry-level "Policy & Research Assistant" at a health-policy charity, open
to graduates, doing general research and writing →
  ELIGIBLE: yes  INTEREST: 2  PRIORITY: 58
  REASON: Squarely entry-level with skills the candidate has, so a realistic
  and solid application. Health policy sits outside the core interest zones,
  which is the only thing holding the score down — still well worth surfacing.

Example 4 — senior title (ineligible)
A "Senior Policy Manager" requiring 5+ years of experience and a record of
leading teams →
  ELIGIBLE: no  INTEREST: 4  PRIORITY: 0
  REASON: Not entry-level — it requires several years of experience and team
  leadership the candidate does not have. The field is interesting, but the
  level is a hard mismatch.

Example 5 — working-student role (ineligible)
A "Werkstudent: Research Support" at a peace-research institute — paid, but
explicitly a student side-role →
  ELIGIBLE: no  INTEREST: 5  PRIORITY: 0
  REASON: Working-student / Werkstudent roles are excluded as a matter of
  policy, even when the field is a perfect match.

Example 6 — qualified-lawyer requirement (ineligible)
A "Legal Officer" at an international tribunal requiring a law degree and
qualification to practise →
  ELIGIBLE: no  INTEREST: 5  PRIORITY: 0
  REASON: Requires a legal qualification the candidate does not hold (no
  qualifying law degree / GDL / SQE). The atrocity-justice subject is a
  perfect interest match, but the qualification is an essential the candidate
  fails.

Example 7 — deep-technical ML (ineligible)
A "Machine Learning Research Engineer" requiring production ML and strong
software engineering →
  ELIGIBLE: no  INTEREST: 4  PRIORITY: 0
  REASON: The essential ML and software-engineering requirements are well
  beyond the candidate's analysis-level Python. The tech-policy interest is
  real, but this is the wrong side of zone (c).

Example 8 — soft 1–2 year ask (worth a look, not disqualified)
An entry-to-junior "Research Analyst" in European security at a think-tank
that lists "around 1–2 years' relevant experience" as desirable →
  ELIGIBLE: yes  INTEREST: 5  PRIORITY: 66
  REASON: The field is core and the work fits; the 1–2 year ask is desirable
  rather than essential, and RA experience partly meets it, so it stays a
  realistic eligible application rather than a disqualifier.

Example 9 — funded PhD position (eligible; the candidate is open to a PhD)
A "Doctoral Researcher (m/f/d)" or fully funded PhD studentship on
international law / atrocity prevention — salaried (e.g. TV-L E13 65%) or a
stipend — requiring a Master's →
  ELIGIBLE: yes  INTEREST: 5  PRIORITY: 86
  REASON: A funded doctorate in the core field is exactly what the candidate is
  open to and is qualified for with the MSc; it is entry-level by definition.
  Funded, so it is not an unpaid exclusion, and it is squarely on mission.

Example 10 — postdoc (ineligible: requires an already-awarded PhD)
A "Postdoctoral Research Fellow" requiring a completed PhD →
  ELIGIBLE: no  INTEREST: 5  PRIORITY: 0
  REASON: Requires an already-completed PhD, which the candidate does not hold
  (they would be pursuing a doctorate, not holding one). The field is a perfect
  match, but the qualification is an unmet essential.

────────────────────────────────────────
CANDIDATE CV
────────────────────────────────────────

{CANDIDATE_CV}
"""


def evaluate_with_anthropic(site_name, job_title, job_url, detail_text, is_page_level=False, london_only=False, max_chars=10000):
    anthropic_rate_limit()

    # Build the per-call user message. Everything that varies between calls
    # goes here; everything static lives in SYSTEM_PROMPT and is cached.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    location_policy = "London only" if london_only else "information only"
    if is_page_level:
        user_message = (
            f"PAGE-LEVEL SCAN\n"
            f"TODAY (for deadline checks): {today}\n"
            f"ORGANISATION: {site_name}\n"
            f"LOCATION POLICY: {location_policy}\n"
            f"URL: {job_url}\n\n"
            f"PAGE CONTENT:\n{detail_text[:max_chars]}"
        )
    else:
        user_message = (
            f"SINGLE JOB EVALUATION\n"
            f"TODAY (for deadline checks): {today}\n"
            f"JOB: {job_title}\n"
            f"ORGANISATION: {site_name}\n"
            f"LOCATION POLICY: {location_policy}\n"
            f"URL: {job_url}\n\n"
            f"JOB DESCRIPTION:\n{detail_text[:max_chars]}"
        )

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    # Static prompt stays in its own cached block. Learned feedback (which
    # changes between runs) goes in a SECOND, uncached block appended after it,
    # so the big prompt still gets a cache hit. Omitted entirely when empty.
    system_blocks = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    _learned = _get_learned_block()
    if _learned:
        system_blocks.append({"type": "text", "text": _learned})
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        # NOTE: Claude Opus 4.8 rejects temperature/top_p/top_k with a 400
        # error. Determinism on this scoring task comes from the rubric and
        # the formula-based MATCH derivation in the system prompt, not from
        # sampling parameters. Do not re-add temperature here.
        "system": system_blocks,
        "messages": [{"role": "user", "content": user_message}],
    }

    # Transient API failures (429 rate-limit, 529 overloaded, 5xx) are common
    # and self-resolve. Previously a single one produced a permanent
    # llm_call_failed for that job. Retry those a few times (honouring the
    # Retry-After header) while still failing fast on real errors like 400.
    ANTHROPIC_RETRY_CODES = {429, 500, 502, 503, 504, 529}
    ANTHROPIC_MAX_ATTEMPTS = 4
    last_err = ""
    for attempt in range(1, ANTHROPIC_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=payload, timeout=60,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = f"Anthropic API: {e}"
            if attempt < ANTHROPIC_MAX_ATTEMPTS:
                wait = 5 * attempt
                print(f"    Anthropic network error — retry in {wait}s "
                      f"(attempt {attempt}/{ANTHROPIC_MAX_ATTEMPTS}): {e}")
                time.sleep(wait)
                continue
            break
        except Exception as e:
            _record_error(f"Anthropic API: {e}")
            print(f"    Anthropic error: {e}")
            return None

        # Retryable status: back off (prefer server's Retry-After) and try again
        if resp.status_code in ANTHROPIC_RETRY_CODES and attempt < ANTHROPIC_MAX_ATTEMPTS:
            ra = resp.headers.get("retry-after", "")
            try:
                wait = float(ra)
            except (TypeError, ValueError):
                wait = 5 * attempt
            wait = min(wait, 30)
            last_err = f"Anthropic API: HTTP {resp.status_code}"
            print(f"    Anthropic HTTP {resp.status_code} — retry in {wait:.0f}s "
                  f"(attempt {attempt}/{ANTHROPIC_MAX_ATTEMPTS})")
            time.sleep(wait)
            continue

        # Surface the API's own error message on non-2xx responses, not just
        # the generic HTTP status. Anthropic returns a JSON body with an
        # explanatory message that is essential for debugging.
        if resp.status_code >= 400:
            body_snippet = (resp.text or "")[:1000]
            err_msg = f"HTTP {resp.status_code}: {body_snippet}"
            _record_error(f"Anthropic API: {err_msg}")
            print(f"    Anthropic error: {err_msg}")
            return None

        try:
            data = resp.json()
        except Exception as e:
            _record_error(f"Anthropic API: invalid JSON response: {e}")
            print(f"    Anthropic error: invalid JSON response: {e}")
            return None
        result = "".join(
            b.get("text", "") for b in data.get("content", [])
            if b.get("type") == "text"
        )
        # Log token usage and cache stats — helps confirm caching is working
        usage = data.get("usage", {})
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        print(
            f"    [tokens] in={in_tok} out={out_tok} "
            f"cache_read={cache_read} cache_write={cache_write}"
        )
        print(f"    --- Anthropic raw response ---")
        print(result)
        print(f"    --- End Anthropic response ---")
        return result

    # Exhausted retries on a transient error.
    _record_error(last_err or "Anthropic API: exhausted retries on transient error")
    print(f"    Anthropic error: exhausted retries ({last_err})")
    return None


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

def escape_html(text):
    """Escape characters that break Telegram's HTML parser."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def parse_gemini_matches(raw_text):
    """Parse Gemini's structured output into a list of match dicts."""
    matches = []
    if not raw_text or "NO_JOBS_FOUND" in raw_text:
        return matches

    FIELDS = ["JOB", "ORGANISATION", "LOCATION", "TYPE", "DEADLINE", "SALARY", "ELIGIBLE", "PRIORITY", "INTEREST", "MATCH", "FIELD", "SKILLS", "SENIORITY", "REASON", "URL"]

    def is_field_line(line):
        """Check if a line starts with a known field label. Returns (field_name, value) or None."""
        # Strip markdown bold/italic markers and whitespace
        cleaned = line.strip().lstrip("*#- ").rstrip("*")
        upper = cleaned.upper()
        for field in FIELDS:
            if upper.startswith(field + ":"):
                value = cleaned[len(field) + 1:].strip().lstrip("*").rstrip("*").strip()
                return field.lower(), value
        return None

    # Split on "JOB:" to get individual match blocks
    blocks = raw_text.split("JOB:")
    for block in blocks[1:]:  # skip everything before the first JOB:
        match = {}
        lines = block.strip().splitlines()
        # First line is the job title (remainder after "JOB:" split)
        match["job"] = lines[0].strip().strip("* ")

        current_field = None
        for line in lines[1:]:
            parsed = is_field_line(line)
            if parsed:
                current_field, value = parsed
                match[current_field] = value
            elif current_field == "reason":
                # Append continuation lines to the reason field
                continuation = line.strip()
                if continuation:
                    match["reason"] = match.get("reason", "") + " " + continuation

        if match.get("job"):
            matches.append(match)
    return matches

def _coerce_priority(value):
    """Parse the model's PRIORITY into an int in [0, 100], or None if absent/unparseable."""
    try:
        p = int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, p))


def _is_eligible(value):
    """Interpret the ELIGIBLE label. Defaults to True when the field is blank/missing
    (an older response with no ELIGIBLE line is treated as eligible)."""
    v = str(value).strip().lower()
    if v == "":
        return True
    return v in ("yes", "true", "1", "y", "eligible")


def _priority_band(priority):
    """Cosmetic tier derived from PRIORITY — used only for report colour and
    backward-compatible 'match' values. Ranking always uses the raw number."""
    if priority is None:
        return "low"
    if priority >= 70:
        return "high"
    if priority >= 45:
        return "medium"
    return "low"


def _match_to_report_entry(m, fallback_org="", fallback_url=""):
    """Convert a parsed match dict into a daily report entry.

    New schema: PRIORITY (0–100, the ranking signal) + ELIGIBLE (hard gate) +
    INTEREST (1–5, transparency). A derived 'match' band is kept so older
    dashboard/report consumers still colour correctly. Legacy field/skills/
    seniority scores are carried through when present so historical or
    mixed-format days still render."""
    eligible = _is_eligible(m.get("eligible", ""))
    priority = _coerce_priority(m.get("priority"))
    if not eligible:
        priority = 0
    # Prefer the band derived from PRIORITY; fall back to a legacy MATCH label
    # if this response predates the priority format.
    band = _priority_band(priority) if priority is not None else m.get("match", "low").lower()
    return {
        "title": m.get("job", "Untitled"),
        "organisation": m.get("organisation", fallback_org),
        "url": m.get("url", fallback_url),
        "priority": priority if priority is not None else "",
        "eligible": eligible,
        "interest_score": m.get("interest", ""),
        "match": band,
        "reason": m.get("reason", ""),
        "location": m.get("location", ""),
        "type": m.get("type", ""),
        "deadline": m.get("deadline", ""),
        "salary": m.get("salary", ""),
        "field_score": m.get("field", ""),
        "skills_score": m.get("skills", ""),
        "seniority_score": m.get("seniority", ""),
    }

def format_match_for_telegram(entry):
    """Format a report entry into a Telegram HTML message block.

    Accepts the canonical report-entry dict (see _match_to_report_entry), so it
    works off PRIORITY/INTEREST. Falls back gracefully when those are absent."""
    title = escape_html(entry.get("title", "Untitled"))
    org = escape_html(entry.get("organisation", ""))
    location = entry.get("location", "")
    job_type = entry.get("type", "")
    deadline = entry.get("deadline", "")
    salary = entry.get("salary", "")
    reason = escape_html(entry.get("reason", ""))
    url = entry.get("url", "")
    interest = entry.get("interest_score", "")
    try:
        p = int(entry.get("priority"))
    except (TypeError, ValueError):
        p = -1

    # Emoji by priority band: ⭐ top tier, 🟢 strong, 🟡 good, ⚪ otherwise.
    if p >= 80:
        emoji = "⭐"
    elif p >= 70:
        emoji = "🟢"
    elif p >= 45:
        emoji = "🟡"
    else:
        emoji = "⚪"

    parts = []
    if p >= 80:
        parts.append("⭐ <b>TOP PRIORITY</b>")
    header = f"{emoji} <b>{title}</b>"
    if p >= 0:
        header += f" — {p}/100"
    parts.append(header)
    if org:
        parts.append(f"🏢 {org}")

    info_bits = []
    if location and location.lower() != "not specified":
        info_bits.append(f"📍 {escape_html(location)}")
    if job_type and job_type.lower() != "not specified":
        info_bits.append(f"📋 {escape_html(job_type)}")
    if deadline and deadline.lower() != "not specified":
        info_bits.append(f"⏰ {escape_html(deadline)}")
    if salary and salary.lower() != "not specified":
        info_bits.append(f"💰 {escape_html(salary)}")
    if info_bits:
        parts.append(" · ".join(info_bits))

    scores = []
    if p >= 0:
        scores.append(f"Priority: {p}/100")
    if interest:
        scores.append(f"Interest: {interest}/5")
    if scores:
        parts.append(f"📊 {' · '.join(scores)}")

    if reason:
        parts.append(f"\n{reason}")

    if url:
        parts.append(f'\n🔗 <a href="{url}">View posting</a>')

    return "\n".join(parts)

def send_telegram(message, reply_markup=None):
    if RUN_LABEL:
           message = f"{RUN_LABEL}\n{message}"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i:i + 4000] for i in range(0, len(message), 4000)]
    for idx, chunk in enumerate(chunks):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        # Attach the inline keyboard (e.g. feedback buttons) only to the final
        # chunk, so the buttons sit beneath the complete message.
        if reply_markup is not None and idx == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 400:
                payload["parse_mode"] = ""
                requests.post(url, json=payload, timeout=15)
        except Exception as e:
            print(f"    Telegram error: {e}")


# ═══════════════════════════════════════════════════════════════
#  FEEDBACK LOOP
# ═══════════════════════════════════════════════════════════════
# Each notification carries 👍 / 👎 inline buttons. One designated lane drains
# the taps via getUpdates at the start of every run and appends them to a shared
# feedback log; recent pursued/dismissed roles are folded back into the scoring
# prompt as a second (uncached) system block. The whole layer degrades to a
# no-op when its files/flags are absent, so it can never break notifications.

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return default


def _save_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _feedback_id(url, title):
    """Stable short id for a (url, title) pair — fits Telegram's 64-byte
    callback_data limit and lets a tap resolve back to a role."""
    raw = ((url or "").strip() + "|" + (title or "").strip().lower()).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _feedback_keyboard(entry):
    """Inline 👍 / 👎 keyboard for one notified job."""
    fid = _feedback_id(entry.get("url", ""), entry.get("title", ""))
    return {
        "inline_keyboard": [[
            {"text": "👍 Relevant", "callback_data": f"fb:{fid}:1"},
            {"text": "👎 Not relevant", "callback_data": f"fb:{fid}:0"},
        ]]
    }


def _load_all_fb_indexes():
    """Merge every lane's fid->meta index (read-only across lanes). The drain
    lane needs this to resolve taps on notifications sent by either lane."""
    import glob as _glob
    merged = {}
    parent = os.path.dirname(_SITE_DATA_DIR) or "."
    for p in _glob.glob(os.path.join(parent, "*", "fb_index.json")):
        d = _load_json(p, {})
        if isinstance(d, dict):
            merged.update(d)
    own = _load_json(FEEDBACK_INDEX_FILE, {})
    if isinstance(own, dict):
        merged.update(own)
    return merged


def register_feedback_targets(entries):
    """Record fid -> meta for the jobs THIS lane just notified, so later taps
    resolve to a role. Writes only this lane's own index file."""
    if not entries:
        return
    try:
        idx = _load_json(FEEDBACK_INDEX_FILE, {})
        if not isinstance(idx, dict):
            idx = {}
        for e in entries:
            fid = _feedback_id(e.get("url", ""), e.get("title", ""))
            idx[fid] = {
                "title": e.get("title", ""),
                "organisation": e.get("organisation", ""),
                "url": e.get("url", ""),
                "priority": e.get("priority", ""),
                "interest": e.get("interest_score", ""),
            }
        if len(idx) > 400:  # bound growth; dicts keep insertion order
            idx = dict(list(idx.items())[-300:])
        _save_json(FEEDBACK_INDEX_FILE, idx)
    except Exception as e:
        print(f"    Feedback: could not register targets ({e}).")


def _tg_api(method, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    return requests.post(url, json=params, timeout=20)


def drain_feedback():
    """Drain Telegram callback taps into the feedback log. Only the lane with
    FEEDBACK_DRAIN=1 runs the network call. Wrapped so any failure is a no-op."""
    if not FEEDBACK_DRAIN or DRY_RUN:
        return
    try:
        offset = _load_json(FEEDBACK_OFFSET_FILE, {}).get("offset", 0)
        resp = _tg_api("getUpdates", offset=offset, timeout=0,
                       allowed_updates=["callback_query"])
        if resp.status_code == 409:
            print("    Feedback: getUpdates 409 — a webhook is set on this bot, so "
                  "polling is disabled. Remove the webhook to capture feedback.")
            return
        if resp.status_code != 200:
            print(f"    Feedback: getUpdates HTTP {resp.status_code} — skipping this run.")
            return
        updates = resp.json().get("result", [])
        if not updates:
            return
        indexes = _load_all_fb_indexes()
        log = _load_json(FEEDBACK_LOG_FILE, [])
        if not isinstance(log, list):
            log = []
        max_uid = (offset - 1) if offset else 0
        recorded = 0
        for upd in updates:
            max_uid = max(max_uid, upd.get("update_id", 0))
            cq = upd.get("callback_query")
            if not cq:
                continue
            cq_id = cq.get("id", "")
            data = cq.get("data", "")
            verdict, fid = None, ""
            if data.startswith("fb:"):
                bits = data.split(":")
                if len(bits) == 3:
                    fid = bits[1]
                    verdict = {"1": "up", "0": "down"}.get(bits[2])
            if verdict is None:
                if cq_id:
                    _tg_api("answerCallbackQuery", callback_query_id=cq_id)
                continue
            meta = indexes.get(fid, {})
            log.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "fid": fid,
                "verdict": verdict,
                "title": meta.get("title", ""),
                "organisation": meta.get("organisation", ""),
                "url": meta.get("url", ""),
                "priority": meta.get("priority", ""),
                "interest": meta.get("interest", ""),
            })
            recorded += 1
            if cq_id:
                label = "👍 noted — more like this" if verdict == "up" else "👎 noted — fewer like this"
                _tg_api("answerCallbackQuery", callback_query_id=cq_id, text=label)
        _save_json(FEEDBACK_LOG_FILE, log[-500:])
        _save_json(FEEDBACK_OFFSET_FILE, {"offset": max_uid + 1})
        if recorded:
            print(f"    Feedback: recorded {recorded} tap(s) into the log.")
    except Exception as e:
        print(f"    Feedback: drain skipped ({e}).")


def build_learned_preferences():
    """Compact 'LEARNED SIGNALS' block from recent feedback, or '' when empty."""
    log = _load_json(FEEDBACK_LOG_FILE, [])
    if not isinstance(log, list) or not log:
        return ""
    pursued, dismissed, seen = [], [], set()
    for e in reversed(log):  # most recent first
        title = (e.get("title") or "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        org = (e.get("organisation") or "").strip()
        line = title + (f" — {org}" if org else "")
        if e.get("verdict") == "up" and len(pursued) < FEEDBACK_EXEMPLARS:
            pursued.append(line)
        elif e.get("verdict") == "down" and len(dismissed) < FEEDBACK_EXEMPLARS:
            dismissed.append(line)
        if len(pursued) >= FEEDBACK_EXEMPLARS and len(dismissed) >= FEEDBACK_EXEMPLARS:
            break
    if not pursued and not dismissed:
        return ""
    out = [
        "LEARNED SIGNALS",
        "The candidate has personally reviewed past matches and marked the roles "
        "below. Use them to calibrate borderline PRIORITY scores: lean toward "
        "roles resembling the pursued ones and away from the dismissed ones. This "
        "refines PRIORITY only — it never overrides a HARD DISQUALIFIER.",
    ]
    if pursued:
        out.append("")
        out.append("Marked RELEVANT (pursue more like these):")
        out += [f"  • {l}" for l in pursued]
    if dismissed:
        out.append("")
        out.append("Marked NOT RELEVANT (surface fewer like these):")
        out += [f"  • {l}" for l in dismissed]
    return "\n".join(out)


_LEARNED_CACHE = None


def _get_learned_block():
    """Memoise the learned-signals block for the duration of a run."""
    global _LEARNED_CACHE
    if _LEARNED_CACHE is None:
        _LEARNED_CACHE = build_learned_preferences()
    return _LEARNED_CACHE


# ═══════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════

# ─── Parallel execution helpers (Tier 1.A) ───
def _run_handler_capture(site, seen_urls):
    """Phase-1 worker: run ONE site's handler with its print output captured to
    a per-thread buffer and its (thread-local) recorded error consumed. Mutates
    no shared state — only network I/O happens here — so it is safe to run many
    of these at once. Returns a dict the sequential phase consumes."""
    buf = io.StringIO()
    _set_capture(buf)
    result = None
    handler_exc = None
    captured_err = ""
    try:
        handler = METHOD_HANDLERS.get(site["method"])
        result = handler(site, seen_urls)
        captured_err = _consume_error()
    except Exception as e:
        handler_exc = e
    finally:
        _end_capture()
    return {"result": result, "handler_exc": handler_exc,
            "captured_err": captured_err, "log": buf.getvalue()}


def _capture_pw_site(out, job, browser):
    """Run check_playwright for one site (optionally on a shared browser),
    capturing its output + error exactly like _run_handler_capture does."""
    buf = io.StringIO()
    _set_capture(buf)
    result = None
    handler_exc = None
    captured_err = ""
    try:
        result = check_playwright(job["site"], job["seen_urls"], browser=browser)
        captured_err = _consume_error()
    except Exception as e:
        handler_exc = e
    finally:
        _end_capture()
    out[job["index"]] = {"result": result, "handler_exc": handler_exc,
                         "captured_err": captured_err, "log": buf.getvalue()}


def _run_pw_chunk(chunk):
    """Phase-1 worker for a group of Playwright sites. The sync Playwright API
    is not safe to share across threads, so an entire chunk runs in THIS single
    thread and reuses one Chromium process (a fresh context per site preserves
    cookie/cache isolation). Returns {site_index: phase1_dict}.

    Context-mode-stealth sites wrap the whole Playwright lifecycle and so cannot
    share the pooled browser; they run afterwards, each launching its own."""
    out = {}
    solo = [j for j in chunk if j["site"].get("stealth") and _STEALTH_MODE == "context"]
    shared_jobs = [j for j in chunk if j not in solo]
    if shared_jobs:
        try:
            with sync_playwright() as p:
                shared = _pw_launch(p)
                for job in shared_jobs:
                    _capture_pw_site(out, job, shared)
                shared.close()
        except Exception as e:
            for job in shared_jobs:
                out.setdefault(job["index"], {"result": None, "handler_exc": e,
                                              "captured_err": "",
                                              "log": f"    Playwright group launch failed: {e}\n"})
    for job in solo:
        _capture_pw_site(out, job, None)
    return out


def main():
    now = datetime.now(timezone.utc)
    print(f"=== Job Monitor Run: {now.isoformat()} ===")
    if DRY_RUN:
        print("🏃 DRY RUN — populating state only, no LLM calls or notifications\n")
    print()

    state = load_state()
    atexit.register(save_state, state)  # persist progress even on an unexpected crash
    issues_data = issues.load()

    # Drain any 👍/👎 feedback taps before scoring, so the learned-signals block
    # folded into the prompt reflects the latest taps. No-op unless this lane has
    # FEEDBACK_DRAIN=1. Never raises — a feedback failure must not stop a run.
    drain_feedback()

    all_matches = []
    daily_report_jobs = []
    paused_sites = []
    empty_sites = []

    # ── Setup: build the per-site job list (single-threaded). State is mutated
    # here — phase-1 handlers never touch state — so there are no races. ──
    jobs = []
    for i, site in enumerate(SITES):
        name = site["name"]
        method = site["method"]
        site_key = site["url"]
        if site_key not in state:
            state[site_key] = {"seen_urls": [], "last_checked": "", "listing_hash": ""}
        job = {"index": i, "site": site, "site_key": site_key, "name": name, "method": method}

        # Check if site is paused due to repeated failures
        paused_until = state[site_key].get("paused_until", "")
        if paused_until:
            pause_end = datetime.fromisoformat(paused_until)
            if now < pause_end:
                job["status"] = "paused"
                job["pause_until"] = paused_until
                job["pause_remaining_h"] = (pause_end - now).total_seconds() / 3600
                jobs.append(job)
                continue
            else:
                # Pause expired: reset now, before dispatch (handler ignores state).
                state[site_key]["consecutive_errors"] = 0
                state[site_key].pop("paused_until", None)
                job["pause_reset"] = True

        # Load seen URLs as an ordered list (for state persistence) and a
        # parallel set (handlers use O(1) `in` checks). Order is preserved
        # so that the prune-to-last-200 step at the end correctly drops the
        # OLDEST entries rather than an arbitrary subset.
        job["seen_urls_ordered"] = [_norm_url(u) for u in state[site_key].get("seen_urls", [])]
        job["seen_urls"] = _NormSet(job["seen_urls_ordered"])

        if METHOD_HANDLERS.get(method) is None:
            job["status"] = "unknown"
            jobs.append(job)
            continue

        job["status"] = "dispatch"
        job["kind"] = "pw" if method == "playwright" else "http"
        jobs.append(job)

    # ── Phase 1: run every site's fetch concurrently. HTTP/API sites go to a
    # thread pool; Playwright sites run in their own thread group(s) sharing one
    # browser (the sync API can't be shared across threads). This is the whole
    # speedup — the run is bounded by the slowest site, not the sum of all. ──
    http_jobs = [j for j in jobs if j.get("status") == "dispatch" and j["kind"] == "http"]
    pw_jobs = [j for j in jobs if j.get("status") == "dispatch" and j["kind"] == "pw"]
    _nw = max(1, PW_WORKERS)
    pw_chunks = [c for c in (pw_jobs[k::_nw] for k in range(_nw)) if c]

    _pw_note = f", {len(pw_chunks)} Playwright browser(s)" if pw_chunks else ""
    print(f"\n▶ Checking {len(http_jobs) + len(pw_jobs)} site(s) in parallel "
          f"({min(HTTP_WORKERS, len(http_jobs)) if http_jobs else 0} HTTP worker(s){_pw_note})...\n")

    results = {}
    if http_jobs or pw_chunks:
        with ThreadPoolExecutor(max_workers=max(1, HTTP_WORKERS + len(pw_chunks))) as pool:
            futs = {}
            for j in http_jobs:
                futs[pool.submit(_run_handler_capture, j["site"], j["seen_urls"])] = ("http", j["index"])
            for chunk in pw_chunks:
                futs[pool.submit(_run_pw_chunk, chunk)] = ("pw", None)
            for fut in as_completed(futs):
                kind, idx = futs[fut]
                try:
                    r = fut.result()
                except Exception as _e:
                    if kind == "http":
                        results[idx] = {"result": None, "handler_exc": _e, "captured_err": "", "log": ""}
                    continue
                if kind == "http":
                    results[idx] = r
                else:
                    results.update(r)

    # ── Phase 2: process results in SITES order (single-threaded). Every branch
    # below is the original per-site logic, verbatim — only the handler call was
    # hoisted into phase 1. State mutation, LLM evaluation, Telegram and the
    # daily report stay sequential, so the rate limiter and the shared
    # accumulators need no locking. ──
    for job in jobs:
        site = job["site"]
        name = job["name"]
        method = job["method"]
        site_key = job["site_key"]

        print(f"\n[{site.get('id', '?')}] {name} ({method})")

        if job["status"] == "paused":
            paused_until = job["pause_until"]
            remaining = job["pause_remaining_h"]
            print(f"    ⏸️ Paused until {paused_until[:16]} ({remaining:.0f}h remaining) — skipping.")
            continue

        if job["status"] == "unknown":
            print(f"    Unknown method: {method}. Skipping.")
            issues.add(
                issues_data, now, site,
                "unknown_method",
                f"No handler registered for method '{method}'",
            )
            continue

        seen_urls_ordered = job["seen_urls_ordered"]
        seen_urls = job["seen_urls"]
        p1 = results.get(job["index"]) or {"result": None, "handler_exc": RuntimeError("no phase-1 result"), "captured_err": "", "log": ""}

        if job.get("pause_reset"):
            print(f"    🔄 Pause expired — retrying...")
        if p1.get("log"):
            _sys.stdout.write(p1["log"])

        try:
            if p1.get("handler_exc") is not None:
                raise p1["handler_exc"]
            result = p1["result"]
            last_err = p1["captured_err"]

            if result is None:
                prev_errors = state[site_key].get("consecutive_errors", 0)
                state[site_key]["consecutive_errors"] = prev_errors + 1
                state[site_key]["last_checked"] = now.isoformat()
                err_count = state[site_key]["consecutive_errors"]

                issues.add(
                    issues_data, now, site,
                    "fetch_error",
                    last_err or "Handler returned None (no error captured)",
                    consecutive_count=err_count,
                )

                if err_count >= 16:
                    pause_end = now + timedelta(days=2)
                    state[site_key]["paused_until"] = pause_end.isoformat()
                    paused_sites.append(name)
                    print(f"    ⏸️ Failed {err_count} times — pausing until {pause_end.isoformat()[:16]}.")
                    issues.add(
                        issues_data, now, site,
                        "site_paused",
                        f"Site paused after {err_count} consecutive failures",
                        consecutive_count=err_count,
                        paused_until=pause_end.isoformat(),
                    )
                else:
                    print(f"    ⚠️ Failed ({err_count}/16 before pause).")
                continue

            state[site_key]["consecutive_errors"] = 0

            # Inconclusive read: a sub-threshold extraction is a flaky/partial fetch,
            # not a real "no jobs" state. Leave the stored hash untouched so a later
            # good fetch compares against the last real content (instead of the page
            # oscillating empty<->full and firing an LLM call every run). Not a
            # failure, so it carries no pause pressure.
            if isinstance(result, dict) and result.get("type") == "insufficient_content":
                print(f"    ↷ Skipped: inconclusive fetch ({result.get('chars', 0)} chars) — keeping last known state, will retry next run.")
                state[site_key]["last_checked"] = now.isoformat()
                continue

            # Handle the hash-check case (HTML sites without link_selector)
            if isinstance(result, dict) and result.get("type") == "hash_check":
                # Check for "no vacancies" pages
                page_lower = result["text"].lower()
                if not site.get("skip_no_vacancy_check", False) and any(phrase in page_lower for phrase in NO_VACANCY_PHRASES):
                    print(f"    ℹ️ No vacancies listed (page says so).")
                    empty_sites.append(name)
                    state[site_key]["listing_hash"] = result["hash"]
                    state[site_key]["last_checked"] = now.isoformat()
                    continue

                # Check for boilerplate-only pages (e.g. REDRESS)
                boilerplate = site.get("no_vacancy_boilerplate", "")
                if boilerplate:
                    stripped = page_lower.replace(boilerplate.lower(), "").strip()
                    for heading in ["current vacancies", "vacancies", "careers", "jobs"]:
                        stripped = stripped.replace(heading, "").strip()
                    if len(stripped) < 50:
                        print(f"    ℹ️ No vacancies listed (only boilerplate text found).")
                        empty_sites.append(name)
                        state[site_key]["listing_hash"] = result["hash"]
                        state[site_key]["last_checked"] = now.isoformat()
                        continue

                title_selector = site.get("job_title_selector", "")
                if title_selector and result.get("soup"):
                    title_elements = result["soup"].select(title_selector)
                    if title_elements:
                        titles = [el.get_text(strip=True) for el in title_elements if el.get_text(strip=True)]
                        for t in titles:
                            print(f"      → {t}")
                    else:
                        print(f"    (no titles matched selector '{title_selector}')")
                    
                old_hash = state[site_key].get("listing_hash", "")
                empty_hashes = state[site_key].get("empty_hashes", [])
                if result["hash"] != old_hash:
                    for t in result.get("titles", []):
                        print(f"      → {t}")
                    if result["hash"] in empty_hashes:
                        # The LLM has already cleared this exact page-state as "no
                        # jobs". An empty page served from a different runner region /
                        # CDN node hashes differently, so without this guard the same
                        # empty page re-fires an identical NO_JOBS_FOUND call every
                        # time the region flips. Recognising a previously-cleared
                        # state skips that redundant call. A genuinely new posting
                        # changes the page text → a hash NOT in this set → still sent
                        # to the LLM, so this can never hide a real vacancy.
                        print(f"    ℹ️ No new content (matches a previously-cleared empty state — no LLM call).")
                        empty_sites.append(name)
                        state[site_key]["listing_hash"] = result["hash"]
                    elif DRY_RUN:
                        print(f"    Page content changed (dry run — skipping LLM)")
                        # In dry-run we deliberately advance the hash to seed state.
                        state[site_key]["listing_hash"] = result["hash"]
                    else:
                        print(f"    Page content changed! Sending full text to Anthropic...")
                        page_text = result["text"]
                        if site.get("follow_jd_docs") and result.get("jd_doc_links"):
                            _pb = lambda u: fetch_bytes(u, proxy=site.get("proxy"), tls_impersonate=site.get("tls_impersonate", False))
                            _dt, _ds = jd_docs.fetch_links_text(result["jd_doc_links"], _pb, max_docs=int(site.get("jd_max_docs", 8)), max_total_chars=int(site.get("jd_max_chars", 20000)))
                            if _dt:
                                # jd_priority: JD leads so a downstream prefix-cut drops
                                # the trailing page text, never the JD. Else append-last.
                                page_text = (_dt.strip() + "\n\n" + page_text) if site.get("jd_priority") else (page_text + _dt)
                                print(f"    + {len(_ds)} JD doc(s) {'(JD-priority)' if site.get('jd_priority') else 'appended'} from listing")
                        llm_result = evaluate_with_anthropic(name, f"Page update on {name}", site["url"], page_text, is_page_level=True, london_only=site.get("london_only", False), max_chars=site.get("eval_max_chars", 10000))
                        llm_err = _consume_error()
                        if llm_result:
                            parsed = parse_gemini_matches(llm_result)
                            # Page-level sites dedup by page-hash, which can flip spuriously
                            # (e.g. non-deterministic jina text) and re-send identical matches
                            # every run. Guard Telegram with a per-job title signature so each
                            # posting notifies exactly once, independent of hash churn.
                            notified = state[site_key].get("notified_titles", [])
                            for m in parsed:
                                entry = _match_to_report_entry(m, fallback_org=name, fallback_url=site["url"])
                                _prio = entry.get("priority")
                                # Telegram: eligible AND PRIORITY at/above the threshold
                                if entry.get("eligible") and isinstance(_prio, int) and _prio >= PRIORITY_NOTIFY_THRESHOLD:
                                    _sig = " ".join((m.get("job") or m.get("title") or "").lower().split())
                                    if _sig and _sig in notified:
                                        print(f"    ⏭️  already notified, skipping Telegram: {(m.get('job') or '')[:60]}")
                                    else:
                                        all_matches.append({"text": format_match_for_telegram(entry), "entry": entry})
                                        if _sig:
                                            notified.append(_sig)
                                # Daily report: all jobs
                                daily_report_jobs.append(entry)
                            state[site_key]["notified_titles"] = notified[-50:]
                            # If the LLM explicitly found no jobs, memoise this hash so
                            # an identical (e.g. region-variant) empty page never costs
                            # another call. Gated on the explicit NO_JOBS_FOUND sentinel
                            # — not merely an empty parse — so a malformed/garbled
                            # response can't poison the set. Capped to the most recent
                            # few states to bound state.json growth.
                            if "NO_JOBS_FOUND" in llm_result:
                                eh = state[site_key].get("empty_hashes", [])
                                if result["hash"] not in eh:
                                    eh.append(result["hash"])
                                    state[site_key]["empty_hashes"] = eh[-8:]
                            # Only commit the new hash after a successful LLM call.
                            # If the call failed we leave the OLD hash in place so
                            # the next run will retry the page-level evaluation.
                            state[site_key]["listing_hash"] = result["hash"]
                        else:
                            print(f"    ⚠️ LLM call failed — hash NOT updated, will retry next run.")
                            issues.add(
                                issues_data, now, site,
                                "llm_call_failed",
                                llm_err or "LLM returned no result for page-level call",
                                scope="page_level",
                            )
                else:
                    print(f"    No changes detected.")
                state[site_key]["last_checked"] = now.isoformat()
                continue

            # Normal case: unpack total/new dict
            total_found = result["total"]
            new_jobs = result["new"]

            if total_found == 0:
                print(f"    ℹ️ No vacancies listed.")
                empty_sites.append(name)
                state[site_key]["last_checked"] = now.isoformat()
                continue

            if not new_jobs:
                print(f"    No new jobs (of {total_found} listed).")
                state[site_key]["last_checked"] = now.isoformat()
                continue

            print(f"    {len(new_jobs)} new job(s) of {total_found} listed! {'Saving to state (dry run)' if DRY_RUN else 'Evaluating...'}")

            for job in new_jobs:
                print(f"      → {job['title']}")

                if job.get("_skip_eval"):
                    print(f"        ⏭ Skipped (filtered): {job.get('_skip_reason', 'pre-filter')} — marking seen, no LLM.")
                elif not DRY_RUN:
                    llm_result = evaluate_with_anthropic(name, job["title"], job["url"], job["detail_text"], london_only=site.get("london_only", False), max_chars=site.get("eval_max_chars", 10000))
                    llm_err = _consume_error()

                    if llm_result is None:
                        # LLM call failed — do NOT mark as seen, retry next run
                        print(f"        ⚠️ Anthropic call failed — will retry next run.")
                        issues.add(
                            issues_data, now, site,
                            "llm_call_failed",
                            llm_err or "LLM returned no result",
                            job_title=job["title"],
                            job_url=job["url"],
                        )
                        continue

                    parsed = parse_gemini_matches(llm_result)
                    if parsed:
                        m = parsed[0]
                        entry = _match_to_report_entry(m, fallback_org=name, fallback_url=job["url"])
                        _prio = entry.get("priority")

                        # Telegram: eligible AND PRIORITY at/above the threshold
                        if entry.get("eligible") and isinstance(_prio, int) and _prio >= PRIORITY_NOTIFY_THRESHOLD:
                            all_matches.append({"text": format_match_for_telegram(entry), "entry": entry})
                            print(f"        ✅ Match (priority {_prio})!")
                        else:
                            _shown = _prio if isinstance(_prio, int) else "n/a"
                            print(f"        No notify (priority {_shown}, eligible={entry.get('eligible')}).")

                        # Daily report: all jobs
                        daily_report_jobs.append(entry)
                    else:
                        # LLM returned something unparseable — record minimal entry
                        print(f"        ⚠️ Could not parse LLM response.")
                        issues.add(
                            issues_data, now, site,
                            "llm_parse_failed",
                            "LLM response could not be parsed into a match block",
                            job_title=job["title"],
                            job_url=job["url"],
                            raw_snippet=(llm_result or "")[:300],
                        )
                        daily_report_jobs.append({
                            "title": job["title"],
                            "organisation": name,
                            "url": job["url"],
                            "priority": "",
                            "eligible": True,
                            "interest_score": "",
                            "match": "low",
                            "location": "", "type": "", "deadline": "", "salary": "",
                            "field_score": "", "skills_score": "", "seniority_score": "",
                            "reason": "",
                        })

                # Track this URL (and any aliases) in BOTH the set (O(1) lookup
                # for the rest of this run) and the ordered list (preserves
                # insertion order for state persistence + prune-by-recency).
                if job["url"] not in seen_urls:
                    seen_urls.add(job["url"])
                    seen_urls_ordered.append(_norm_url(job["url"]))
                for extra in job.get("_also_track", []):
                    if extra not in seen_urls:
                        seen_urls.add(extra)
                        seen_urls_ordered.append(_norm_url(extra))
                time.sleep(1)

            state[site_key]["seen_urls"] = seen_urls_ordered
            state[site_key]["last_checked"] = now.isoformat()
        except Exception as _site_err:
            # Crash isolation: one site's unexpected failure must never abort the
            # whole run (which would also skip save_state and re-notify next run).
            _crash_name = site.get("name", "?")
            _crash_key = site.get("url", "")
            print(f"    ❌ Unhandled error on {_crash_name}: {type(_site_err).__name__}: {_site_err} — skipping to next site")
            _record_error(f"site_crashed ({_crash_name}): {type(_site_err).__name__}: {_site_err}")
            try:
                issues.add(issues_data, now, site, "site_crashed", f"{type(_site_err).__name__}: {_site_err}")
            except Exception:
                pass
            if _crash_key and _crash_key in state:
                _st = state[_crash_key]
                _st["consecutive_errors"] = _st.get("consecutive_errors", 0) + 1
                _st["last_checked"] = now.isoformat()
            continue

    # ─── Prune seen_urls to prevent state.json bloat ───
    for site_key, site_state in state.items():
        seen = site_state.get("seen_urls", [])
        if len(seen) > 200:
            site_state["seen_urls"] = seen[-200:]
            print(f"  Pruned seen_urls for {site_key} to last 200 entries")

    # ─── Save state ───
    save_state(state)

    # ─── Save issues log ───
    issues.finalize(issues_data, state, now)

    # ─── Send results ───
    if DRY_RUN:
        print(f"\n🏃 Dry run complete. State populated for {len(SITES)} sites.")
        if empty_sites:
            print(f"   Sites with no vacancies: {', '.join(empty_sites)}")
        print(f"   Next run will only flag genuinely new jobs.")
    elif all_matches:
        header = f"🔍 <b>Job Monitor Report</b>\n<i>{now.strftime('%Y-%m-%d %H:%M')} UTC</i>\n<i>{len(all_matches)} match(es) found</i>"
        send_telegram(header)
        for item in all_matches:
            send_telegram(item["text"], reply_markup=_feedback_keyboard(item["entry"]))
            time.sleep(0.5)
        # Record what we notified so 👍/👎 taps resolve back to these roles.
        register_feedback_targets([it["entry"] for it in all_matches])
        print(f"\n✅ Sent {len(all_matches)} match(es) to Telegram.")
    else:
        print("\nNo matching jobs found this run.")

    if paused_sites and not DRY_RUN:
        pause_msg = f"⏸️ <b>Sites Paused (16+ failures)</b>\n{escape_html(', '.join(paused_sites))}\nWill automatically retry in 2 days."
        send_telegram(pause_msg)

    if paused_sites:
        print(f"Paused: {', '.join(paused_sites)}")
    if empty_sites:
        print(f"Empty: {', '.join(empty_sites)}")

    # ─── Write daily report for the dashboard ───
    if not DRY_RUN:
        save_report(daily_report_jobs, now)


if __name__ == "__main__":
    main()
