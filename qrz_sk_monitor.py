#!/usr/bin/python3
"""
QRZ Silent Key & Page-Update Monitor (v3.3 — external config + Pushover alerts + configurable heartbeat day)

Author
------
- Name/Callsign: Ian (VK1HF)
- Email: ian.vk1hf@gmail.com
- Date Written: 2025-09-15
- Note from the author: If you find this useful, I'd love to hear you're making good use of it!

What it does
------------
- Loads a CALLSIGN list from a separate file (default: ~/SK_WATCH/qrz_callsigns_list.cfg)
  * ONE callsign per line (tolerant of extra spaces and junk chars)
  * Lines starting with '#' are comments
- Loads API config from a JSON file (default: ~/SK_WATCH/api_key.cfg)
  * Example:
      {
        "pushover": {
          "token": "YOUR_APP_TOKEN",
          "user": "YOUR_USER_KEY",
          "device": null,
          "priority": 0
        },
        "qrz_session_cookie": "qrz=YOUR_QRZ_COOKIE; other=value"
      }
- Fetches https://www.qrz.com/db/<CALLSIGN>
- Detects Silent Key using ANY of these signals (any one => SK = True):
    1) Page title (or og:title) contains '/SKYYYY' (e.g. 'CALL/SK2025')
    2) Final URL redirects to '/db/CALL/SKYYYY?aliasFrom=CALL'
    3) Header area (pre-Biography) contains 'Silent Key' (singular)
    4) QSL line shows 'QSL: Reported Silent Key' (robust to spacing/line breaks)
- Extracts 'Last modified:' and parses to ISO timestamp when possible
- Writes a human-readable text log (default: ~/SK_WATCH/qrz_silentkey.log)
- Tracks prior state in a JSON file (default: ~/SK_WATCH/qrz_silentkey_state.json)
- Sends a Pushover message when:
    * Someone BECOMES SK (False/None -> True), or
    * Their "Last modified" value changes
  The message includes what changed and a direct link to their QRZ page.
- Heartbeat: On a configurable weekday (default Sunday, Australia/Sydney), if there are NO changes,
  sends a summary “no changes” Pushover so you know it ran.

Courtesy to QRZ.com
-------------------
- Suggested schedule: run only once per week (e.g., Sundays).
- The script limits the number of callsigns checked per run to a maximum (default 30).

Cron example (weekly Sunday 19:30 local time):
  30 19 * * 0 /home/bitnami/SK_WATCH/qrz_silentkey_monitor.py

Optional environment overrides:
  QRZ_SK_LOG=/path/to/text.log
  QRZ_SK_STATE=/path/to/state.json
  QRZ_SK_CALLS=/path/to/qrz_callsigns_list.cfg
  QRZ_SK_API=/path/to/api_key.cfg
  QRZ_SESSION='qrz=...'          # overrides cookie from api_key.cfg if set
  QRZ_SK_UA='Custom UA'
  QRZ_SK_HEARTBEAT_DAY='Sunday'  # or '6' (0=Mon … 6=Sun); default 'Sunday'
  QRZ_SK_MAX=30                  # max callsigns to check per run (courtesy cap)
  DEBUG_SK=1                     # print extra debug to stdout

Requirements:
  pip install requests beautifulsoup4 python-dateutil
"""

import os
import re
import json
import time
import logging
import datetime
from typing import Optional, Tuple, List, Dict, Any

import requests
from bs4 import BeautifulSoup
try:
    # Python 3.9+
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:
    ZoneInfo = None  # fallback if not available

try:
    from dateutil import parser as dateparser  # type: ignore
except Exception:
    dateparser = None


# ---------- Defaults & Paths ----------
BASE_DIR = os.path.expanduser("~/SK_WATCH")
os.makedirs(BASE_DIR, exist_ok=True)

CALLSIGNS_FILE = os.path.join(BASE_DIR, "qrz_callsigns_list.cfg")
CALLSIGNS_FILE = os.environ.get("QRZ_SK_CALLS", CALLSIGNS_FILE)

API_CFG_FILE = os.path.join(BASE_DIR, "api_key.cfg")
API_CFG_FILE = os.environ.get("QRZ_SK_API", API_CFG_FILE)

LOG_FILE = os.path.join(BASE_DIR, "qrz_silentkey.log")
LOG_FILE = os.environ.get("QRZ_SK_LOG", LOG_FILE)

STATE_FILE = os.path.join(BASE_DIR, "qrz_silentkey_state.json")
STATE_FILE = os.environ.get("QRZ_SK_STATE", STATE_FILE)

QRZ_BASE = "https://www.qrz.com/db/"
REQUEST_TIMEOUT = 25
REQUEST_SLEEP_SECONDS = 3

USER_AGENT = os.environ.get(
    "QRZ_SK_UA",
    "Mozilla/5.0 (X11; Linux x86_64) QRZ-SK-Monitor/3.3",
)

# Cookie header for QRZ (overrides api file if set)
QRZ_SESSION_COOKIE = os.environ.get("QRZ_SESSION", "").strip()

# Heartbeat day config (string weekday name or digit 0..6, default Sunday)
HEARTBEAT_DAY = os.environ.get("QRZ_SK_HEARTBEAT_DAY", "Sunday")

# Courtesy cap: limit how many calls we check per run
MAX_CALLSIGNS = int(os.environ.get("QRZ_SK_MAX", "30"))

DEBUG = bool(os.environ.get("DEBUG_SK"))

# ---------- Logging ----------
logger = logging.getLogger("qrz_sk_monitor")
logger.setLevel(logging.INFO)

# File handler
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

# Console handler (concise)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(message)s"))

if not logger.handlers:
    logger.addHandler(fh)
    logger.addHandler(ch)


# ---------- Config loading ----------
def load_api_config(path: str) -> Dict[str, Any]:
    """
    Load JSON config. Expected keys (optional):
      {
        "pushover": {"token": "...", "user": "...", "device": null, "priority": 0},
        "qrz_session_cookie": "qrz=...; ..."
      }
    """
    if not os.path.exists(path):
        logger.info("API config file not found at %s (Pushover disabled unless env provided).", path)
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            if not isinstance(cfg, dict):
                logger.warning("API config file does not contain a top-level object, ignoring.")
                return {}
            return cfg
    except Exception as e:
        logger.warning("Failed to read API config %s: %s", path, e)
        return {}


def sanitize_callsign(raw: str) -> Optional[str]:
    """
    Tolerant sanitizer:
      - Trim spaces
      - Ignore empty lines & comments (# ...)
      - Keep only A-Z, 0-9, '/', and '-' (some profiles use portable suffixes)
      - Uppercase result
    Returns None if the line doesn't yield a plausible callsign.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s or s.startswith("#"):
        return None

    # Keep first whitespace-separated token; drop trailing notes
    s = s.split()[0]

    # Allow letters/digits/slash/hyphen only
    s = re.sub(r"[^A-Za-z0-9/\-]", "", s)
    s = s.upper()

    # Minimum plausibility: at least one letter + one digit
    if not re.search(r"[A-Z]", s) or not re.search(r"\d", s):
        return None

    return s


def load_callsigns_list(path: str) -> List[str]:
    """
    Loads callsigns from a simple text file, one per line.
    Tolerant of spaces/junk; ignores invalid lines and duplicates (preserves order).
    """
    calls: List[str] = []
    seen = set()

    if not os.path.exists(path):
        logger.error("Callsigns file not found: %s", path)
        return calls

    try:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                cs = sanitize_callsign(line)
                if not cs:
                    continue
                if cs in seen:
                    continue
                seen.add(cs)
                calls.append(cs)
    except Exception as e:
        logger.error("Failed to read callsigns file %s: %s", path, e)

    if not calls:
        logger.error("No valid callsigns loaded from %s", path)
    return calls


# ---------- HTTP / Parsing ----------
def build_session(api_cfg: Dict[str, Any]) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    # Cookie priority: ENV > api file > none
    cookie = QRZ_SESSION_COOKIE or api_cfg.get("qrz_session_cookie", "") or ""
    if cookie:
        s.headers.update({"Cookie": cookie})

    return s


def fetch_qrz_page(session: requests.Session, callsign: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    url = f"{QRZ_BASE}{callsign}"
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return r.text, r.status_code, r.url
        return None, r.status_code, r.url
    except Exception as e:
        logger.warning("Fetch error for %s: %s", callsign, e)
        return None, None, url


def page_text(soup: BeautifulSoup) -> str:
    return soup.get_text("\n", strip=True)


def normalize_ws(s: str) -> str:
    if not s:
        return s
    s = s.replace("\u00a0", " ")                 # NBSP -> space
    s = re.sub(r"\s+", " ", s, flags=re.UNICODE) # collapse whitespace
    return s.strip()


def text_before_biography(soup: BeautifulSoup) -> str:
    """
    Return visible text up to Biography (or first ~4000 chars).
    Approximates the callsign panel / header where badges & QSL appear.
    """
    full = page_text(soup)
    markers = [r"\bBiography\b", r"\bBio\b", r"JavaScript is required to view user biographies"]
    cut = len(full)
    for m in markers:
        found = re.search(m, full, flags=re.IGNORECASE)
        if found:
            cut = min(cut, found.start())
    return full[:min(cut, 4000)]


def callsign_local_slice(full_text: str, callsign: str, pre: int = 600, post: int = 1600) -> str:
    """
    Fallback region around the first callsign occurrence.
    """
    idx = full_text.upper().find(callsign.upper())
    if idx == -1:
        return ""
    start = max(0, idx - pre)
    end = min(len(full_text), idx + post)
    return full_text[start:end]


# ---- Silent Key signals (ANY => True) ----
def signal_url_alias(final_url: Optional[str]) -> Optional[str]:
    if not final_url:
        return None
    m = re.search(r"/db/[^/]+/SK(\d{4})\b", final_url, flags=re.IGNORECASE)
    if m:
        return f"URL alias SK{m.group(1)}"
    return None


def signal_title_skpath(soup: BeautifulSoup) -> Optional[str]:
    titles: List[str] = []
    if soup.title and soup.title.get_text():
        titles.append(soup.title.get_text(" ", strip=True))
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        titles.append(og["content"])
    for t in titles:
        m = re.search(r"/SK(\d{4})\b", t, flags=re.IGNORECASE)
        if m:
            return f"Title path SK{m.group(1)}"
    return None


def signal_header_silent_key(text_region: str) -> Optional[str]:
    """
    Match 'Silent Key' (singular). Robust to odd spacing; avoids 'Silent Keys'.
    """
    t = normalize_ws(text_region)
    if re.search(r"(?<![A-Za-z])Silent\s+Key(?!s)", t, flags=re.IGNORECASE):
        return "Header shows 'Silent Key'"
    return None


def signal_qsl_reported_sk(text_region: str) -> Optional[str]:
    """
    Detect 'QSL: Reported Silent Key' even if split by spaces/line breaks.
    """
    t = normalize_ws(text_region)
    # allow up to 80 arbitrary chars between 'QSL:' and the value
    if re.search(r"\bQSL\s*:\s*(?:.{0,80})?\bReported\s+Silent\s+Key\b", t, flags=re.IGNORECASE):
        return "QSL: Reported Silent Key"
    return None


def detect_silent_key_signals(soup: BeautifulSoup, final_url: Optional[str], callsign: str) -> Tuple[bool, List[str]]:
    """
    Apply all four signals over multiple regions. Any match => SK=True.
    Returns (is_sk, [reasons]).
    """
    reasons: List[str] = []

    # #2 URL alias
    r = signal_url_alias(final_url)
    if r:
        reasons.append(r)

    # #1 Title path
    r = signal_title_skpath(soup)
    if r:
        reasons.append(r)

    full = page_text(soup)
    header = text_before_biography(soup)
    cs_slice = callsign_local_slice(full, callsign)

    # #3 Header 'Silent Key'
    for region_name, region in (("header", header), ("callsign-slice", cs_slice)):
        if not region:
            continue
        r = signal_header_silent_key(region)
        if r and r not in reasons:
            reasons.append(r + f" ({region_name})")

    # #4 QSL: Reported Silent Key — try header, callsign slice, then whole page as last resort
    r = signal_qsl_reported_sk(header)
    if r and r not in reasons:
        reasons.append(r + " (header)")
    if not r:
        r2 = signal_qsl_reported_sk(cs_slice)
        if r2 and r2 not in reasons:
            reasons.append(r2 + " (callsign-slice)")
    if not any("QSL: Reported Silent Key" in x for x in reasons):
        r3 = signal_qsl_reported_sk(full)
        if r3 and r3 not in reasons:
            reasons.append(r3 + " (page)")

    return (len(reasons) > 0, reasons)


# ---- Last modified parsing ----
def clean_last_modified_raw(raw: str) -> str:
    s = raw.strip()
    s = re.split(r"\bLogin\s+Required\b", s, flags=re.IGNORECASE)[0].strip()
    s = re.sub(r",\s*\d{1,9}\s*bytes.*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r",\s*\d{1,9}\s*$", "", s).strip()
    s = re.split(r"\s+by\s+", s, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return s


def extract_last_modified(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
    text = page_text(soup)
    m = re.search(r"Last\s+modified:\s*([A-Za-z0-9:,\-\/ ]+)", text, flags=re.IGNORECASE)
    if not m:
        return None, None
    raw = clean_last_modified_raw(m.group(1))
    iso = None
    if raw and dateparser:
        try:
            dt = dateparser.parse(raw)
            if dt:
                iso = dt.isoformat()
        except Exception:
            iso = None
    return iso, (raw if raw else None)


# ---- State I/O ----
def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _fmt_duration(seconds: float) -> str:
    """HH:MM:SS formatter for run duration."""
    if seconds < 0:
        seconds = 0
    sec = int(round(seconds))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _parse_weekday(value: str) -> int:
    """
    Parse weekday name or digit to 0..6 (Mon..Sun). Defaults to 6 (Sunday) on bad input.
    """
    if value is None:
        return 6
    v = str(value).strip().lower()
    daymap = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
    if v in daymap:
        return daymap[v]
    if v.isdigit():
        idx = int(v)
        if 0 <= idx <= 6:
            return idx
    # default
    return 6


def _weekday_label(idx: int) -> str:
    names = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    return names[idx] if 0 <= idx <= 6 else f"Day{idx}"


# ---- Pushover Notifications ----
def pushover_enabled(api_cfg: Dict[str, Any]) -> bool:
    # ENV can override/augment JSON
    token = os.environ.get("PUSHOVER_TOKEN") or (api_cfg.get("pushover", {}) or {}).get("token")
    user  = os.environ.get("PUSHOVER_USER")  or (api_cfg.get("pushover", {}) or {}).get("user")
    return bool(token and user)


def send_pushover(api_cfg: Dict[str, Any], title: str, message: str, url: Optional[str] = None) -> bool:
    """
    Sends a Pushover notification. Returns True on HTTP 200, else False.
    Uses ENV overrides if present: PUSHOVER_TOKEN, PUSHOVER_USER
    """
    po = (api_cfg.get("pushover", {}) or {})
    token    = os.environ.get("PUSHOVER_TOKEN") or po.get("token")
    user     = os.environ.get("PUSHOVER_USER")  or po.get("user")
    device   = po.get("device")
    priority = po.get("priority", 0)

    if not token or not user:
        logger.info("Pushover disabled (missing token/user).")
        return False

    data = {
        "token": token,
        "user": user,
        "title": title,
        "message": message,
        "priority": priority,
    }
    if device:
        data["device"] = device
    if url:
        data["url"] = url
        data["url_title"] = "Open QRZ Profile"

    try:
        resp = requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=20)
        if resp.status_code == 200:
            logger.info("  Pushover sent: %s", title)
            return True
        else:
            logger.warning("  Pushover HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        logger.warning("  Pushover error: %s", e)
        return False


# ---------- Main ----------
def main() -> int:
    # Startup note from author + courtesy reminder
    logger.info("QRZ SK Monitor v3.3 — Author: Ian (VK1HF), Email: ian.vk1hf@gmail.com — Written: 2025-09-15")
    logger.info("Courtesy: Consider running this weekly (e.g., Sundays) and limiting callsigns to %d max per run.", MAX_CALLSIGNS)

    api_cfg = load_api_config(API_CFG_FILE)
    session = build_session(api_cfg)

    cookie_in_use = bool(os.environ.get("QRZ_SESSION") or api_cfg.get("qrz_session_cookie"))
    run_start_perf = time.perf_counter()

    # Gentle tip if not Sunday in Australia/Sydney
    try:
        tz = ZoneInfo("Australia/Sydney") if ZoneInfo else None
        now_syd = datetime.datetime.now(tz) if tz else datetime.datetime.now()
    except Exception:
        now_syd = datetime.datetime.now()
    if now_syd.weekday() != 6:
        logger.info("Tip: To be extra fair to QRZ, consider running weekly on Sundays (Australia/Sydney).")

    calls = load_callsigns_list(CALLSIGNS_FILE)
    if not calls:
        logger.error("No callsigns to check. Exiting.")
        return 2

    # Courtesy cap enforcement
    if len(calls) > MAX_CALLSIGNS:
        logger.info("Limiting checks to first %d of %d callsigns to be courteous to QRZ.com.", MAX_CALLSIGNS, len(calls))
        calls = calls[:MAX_CALLSIGNS]

    state = load_state(STATE_FILE)

    run_ts_iso = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    logger.info("===== QRZ SK Watch run at %s (UTC) – %d callsigns =====", run_ts_iso, len(calls))

    change_events: List[str] = []
    http_ok = 0
    totals = {"checked": 0, "sk_true": 0, "sk_false": 0, "sk_changed": 0, "lm_changed": 0, "first_seen": 0}

    po_ok = pushover_enabled(api_cfg)

    for idx, callsign in enumerate(calls, start=1):
        totals["checked"] += 1
        logger.info("(%d/%d) Checking %s", idx, len(calls), callsign)

        html, status, final_url = fetch_qrz_page(session, callsign)
        if not html:
            logger.warning("  Fetch failed. HTTP status=%s url=%s", str(status), final_url)
            time.sleep(REQUEST_SLEEP_SECONDS)
            continue

        soup = BeautifulSoup(html, "html.parser")

        # Detect SK via four-signal logic
        is_sk, reasons = detect_silent_key_signals(soup, final_url, callsign)

        if status == 200:
            http_ok += 1

        # Last modified
        lm_iso, lm_raw = extract_last_modified(soup)

        # Summarize current result
        if is_sk:
            totals["sk_true"] += 1
        else:
            totals["sk_false"] += 1

        reason_str = ("; ".join(reasons)) if reasons else "No SK signals"
        logger.info("  Result: SK=%s  |  LastModifiedISO=%s  |  LastModifiedRaw=%s", is_sk, lm_iso, lm_raw)
        logger.info("  URL: %s", final_url)
        logger.info("  Signals: %s", reason_str)

        # Compare with prior state
        prev = state.get(callsign, {})
        first_seen = not bool(prev)
        if first_seen:
            totals["first_seen"] += 1
            logger.info("  First time seeing %s; storing baseline.", callsign)

        prev_sk = prev.get("is_sk")
        prev_lm_iso = prev.get("last_modified_iso")

        sk_changed = (prev_sk is not None) and (prev_sk != is_sk)
        lm_changed = (prev_lm_iso is not None) and (lm_iso is not None) and (prev_lm_iso != lm_iso)

        # Pushover alerts:
        #  - Only when someone becomes SK (False/None -> True)
        became_sk = (is_sk is True) and (prev_sk in (False, None))
        if became_sk:
            totals["sk_changed"] += 1
            line = f"CHANGE: {callsign} became SK  [{reason_str}]"
            change_events.append(line)
            logger.info("  %s", line)
            if po_ok:
                title = f"QRZ Watch: {callsign} is now Silent Key"
                msg = f"{callsign} became Silent Key.\nSignals: {reason_str}"
                send_pushover(api_cfg, title, msg, url=final_url)

        #  - When LastModified changes (to a new known value)
        if lm_changed:
            totals["lm_changed"] += 1
            line = f"CHANGE: {callsign} LastModified changed {prev_lm_iso} -> {lm_iso}"
            change_events.append(line)
            logger.info("  %s", line)
            if po_ok:
                title = f"QRZ Watch: {callsign} page updated"
                msg = f"{callsign} QRZ page was updated.\nLastModified: {prev_lm_iso} → {lm_iso}"
                send_pushover(api_cfg, title, msg, url=final_url)

        # Update state
        state[callsign] = {
            "is_sk": is_sk,
            "last_modified_iso": lm_iso,
            "last_modified_raw": lm_raw,
            "final_url": final_url,
            "signals": reasons,
            "last_checked_utc": run_ts_iso,
        }

        if DEBUG:
            header_preview = text_before_biography(soup)[:600]
            call_slice = callsign_local_slice(page_text(soup), callsign)[:600]
            print(f"[DEBUG] {callsign} header preview:\n{normalize_ws(header_preview)}\n")
            print(f"[DEBUG] {callsign} callsign slice:\n{normalize_ws(call_slice)}\n")

        time.sleep(REQUEST_SLEEP_SECONDS)

    # Save updated state
    save_state(STATE_FILE, state)

    # Summary
    logger.info("----- SUMMARY -----")
    logger.info("Checked: %d  |  SK true: %d  |  SK false: %d", totals["checked"], totals["sk_true"], totals["sk_false"])
    logger.info("First-seen this run: %d", totals["first_seen"])
    logger.info("Changes since last run: SK flips=%d, LastModified changes=%d", totals["sk_changed"], totals["lm_changed"])
    if change_events:
        logger.info("Changes:")
        for line in change_events:
            logger.info("  %s", line)

    # --- Heartbeat day (configurable, default Sunday) — only if no changes ---
    nothing_changed = (totals["sk_changed"] == 0 and totals["lm_changed"] == 0)
    try:
        tz = ZoneInfo("Australia/Sydney") if ZoneInfo else None
        now_syd = datetime.datetime.now(tz) if tz else datetime.datetime.now()
    except Exception:
        now_syd = datetime.datetime.now()

    target_day_idx = _parse_weekday(HEARTBEAT_DAY)
    is_heartbeat_day = (now_syd.weekday() == target_day_idx)

    if is_heartbeat_day and nothing_changed and po_ok:
        dur_str = _fmt_duration(time.perf_counter() - run_start_perf)
        run_str = now_syd.strftime("%a %Y-%m-%d %H:%M %Z") if now_syd.tzinfo else now_syd.strftime("%a %Y-%m-%d %H:%M")
        label = _weekday_label(target_day_idx)
        title = f"QRZ Watch ({label}): No changes detected"
        msg = (
            f"Run: {run_str}\n"
            f"Checked: {totals['checked']} callsigns\n"
            f"SK flips: {totals['sk_changed']}\n"
            f"Page updates: {totals['lm_changed']}\n\n"
            f"Current snapshot:\n"
            f"  SK=true: {totals['sk_true']}\n"
            f"  SK=false: {totals['sk_false']}\n\n"
            f"All pages reachable (HTTP 200): {http_ok}/{totals['checked']}\n"
            f"Cookie in use: {'yes' if cookie_in_use else 'no'}\n"
            f"Duration: {dur_str}"
        )
        send_pushover(api_cfg, title, msg)

    logger.info("Log file: %s", LOG_FILE)
    logger.info("State file: %s", STATE_FILE)
    logger.info("===== Run complete =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
