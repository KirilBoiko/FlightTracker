#!/usr/bin/env python3
"""
kayak_scraper.py
================
Scrapes daily cheapest flight prices from Kayak for 01 Oct 2026 – 31 Mar
2027 across four bidirectional routes, using the ScrapingBee API for JS
rendering and premium (escalating to stealth) proxies to defeat Kayak's
bot-protection stack.

Routes:
    TBS (Tbilisi)  → TLV (Tel Aviv)
    TLV (Tel Aviv) → TBS (Tbilisi)
    BUS (Batumi)   → TLV (Tel Aviv)
    TLV (Tel Aviv) → BUS (Batumi)

Architecture:
    ┌─────────────────────────────────────────────────────┐
    │  URL Generator  →  ScrapingBee  →  BeautifulSoup    │
    │  (one URL/day)      (headless       (structural +   │
    │                      browser)        regex parse)   │
    │                                                      │
    │  Retry layer  →  Raw HTML archive  →  DataFrame     │
    │  (3 attempts,     (api_responses/)    → CSV export  │
    │   escalating to                                      │
    │   stealth_proxy)                                     │
    └─────────────────────────────────────────────────────┘

Usage:
    pip install requests beautifulsoup4 pandas lxml

    export SCRAPINGBEE_API_KEY="your_key_here"
    python3 kayak_scraper.py

Output:
    <date>_TBS_to_TLV.csv, etc.  (one per route)
    api_responses/kayak_raw/kayak_<route>_<date>.html  (one file per request)

IMPORTANT — Credit cost estimate:
    4 routes × 182 days = 728 requests. Best case (everything clears on
    the first premium_proxy attempt): ~18,200 credits. Worst case
    (every request exhausts retries into the stealth_proxy escalation):
    ~72,800 credits. Actual cost will land somewhere in between depending
    on how often Kayak's anti-bot triggers the escalation.
    Verify your plan quota before running the full set.
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import re
import sys
import json
import time
import random
import logging
import datetime
import requests
import pandas as pd
from pathlib import Path
from bs4 import BeautifulSoup, Tag
from typing import Iterator, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ---------------------------------------------------------------------------
# Logging — structured, timestamped, written to both stdout and a log file
# ---------------------------------------------------------------------------
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
log_filename = LOG_DIR / f"kayak_scraper_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_filename, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"

ROUTES: list[tuple[str, str]] = [
    ("TBS", "TLV"),
    ("TLV", "TBS"),
    ("BUS", "TLV"),
    ("TLV", "BUS"),
]

WINTER_2026_START = datetime.date(2026, 10, 1)     # October 1
WINTER_2026_DAYS  = 182                          # 182 days (Oct -> Mar)

RAW_HTML_DIR  = Path("api_responses") / "kayak_raw"

# ScrapingBee parameters
# -----------------------------------------------------------------------
# Why no wait_for:
#   wait_for causes ScrapingBee to return HTTP 500 if the target CSS selector
#   never appears (e.g. Kayak's bot-block redirects to a CAPTCHA before
#   [data-resultid] can render).  A fixed wait is safer: ScrapingBee always
#   returns the page state after the specified ms, even if it's a block page,
#   so we at least get the raw HTML back for inspection.
#
# Why country_code=us:
#   Kayak applies geo-based filtering; US residential IPs have the highest
#   success rate for price result pages.
# -----------------------------------------------------------------------
JS_WAIT_MS = 15000  # ms to wait after page load before capturing HTML

# Randomised delay range (seconds) between successive day-level requests
DELAY_MIN = 10
DELAY_MAX = 25

# Retry settings
MAX_RETRIES     = 3
RETRY_BACKOFF   = 20  # additional seconds added per retry attempt

# HTTP timeout for the ScrapingBee call (seconds) — must be > JS_WAIT_MS/1000
REQUEST_TIMEOUT = 150

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_api_key() -> str:
    key = os.environ.get("SCRAPINGBEE_API_KEY", "").strip()
    if not key:
        logger.error(
            "SCRAPINGBEE_API_KEY is not set.\n"
            "  macOS/Linux : export SCRAPINGBEE_API_KEY=\"your_key_here\"\n"
            "  Windows CMD : set SCRAPINGBEE_API_KEY=your_key_here"
        )
        sys.exit(1)
    return key


# ---------------------------------------------------------------------------
# URL Generator
# ---------------------------------------------------------------------------
def kayak_url_generator(
    routes: list[tuple[str, str]],
    start: datetime.date,
    days: int,
) -> Iterator[tuple[str, str, str, str]]:
    """
    Yields (origin, destination, date_str, url) for every combination of
    route × day in the target month, sorted route-first then date-ascending.

    URL format:
        https://www.kayak.com/flights/<ORIG>-<DEST>/<YYYY-MM-DD>?sort=price_a

    sort=price_a  — instructs Kayak to show results sorted cheapest-first,
                    ensuring the first result card is always the cheapest.
    """
    for origin, dest in routes:
        for offset in range(days):
            dep_date = start + datetime.timedelta(days=offset)
            date_str = dep_date.isoformat()
            url = (
                f"https://www.kayak.com/flights/{origin}-{dest}/{date_str}"
                f"?sort=price_a&stops=~0"  # ~0 = nonstop/direct only
            )
            yield origin, dest, date_str, url


# ---------------------------------------------------------------------------
# ScrapingBee request with retry logic
# ---------------------------------------------------------------------------
def fetch_via_scrapingbee(
    target_url: str,
    api_key: str,
    label: str,
) -> Optional[str]:
    """
    Fetches `target_url` via ScrapingBee with JS rendering.

    Retries up to MAX_RETRIES times with increasing backoff. The first
    MAX_RETRIES - 1 attempts use `premium_proxy` (cheaper — 10-25 credits).
    The FINAL attempt escalates to `stealth_proxy` (75 credits), which is
    ScrapingBee's tier for sites whose anti-bot has gotten past premium
    proxies — this is the ladder ScrapingBee itself recommends: start
    cheap, escalate only when still blocked. Escalating only on the last
    attempt (rather than always) keeps most requests at the cheaper tier
    instead of paying the top rate on every single one of the ~728
    requests this run makes.

    Returns the raw HTML string on success, or None if all attempts fail.
    """

    for attempt in range(1, MAX_RETRIES + 1):
        use_stealth = attempt == MAX_RETRIES  # escalate on the last try only

        params = {
            "api_key":         api_key,
            "url":             target_url,
            "render_js":       "true",
            "country_code":    "us",       # US residential IPs = highest Kayak success rate
            "wait":            str(JS_WAIT_MS),  # fixed wait — safe even if page is blocked
            "block_resources": "false",    # keep all JS/CSS so Kayak's app bundle runs
            "device":          "desktop",  # Kayak mobile layout differs significantly
        }
        if use_stealth:
            params["stealth_proxy"] = "true"
        else:
            params["premium_proxy"] = "true"

        try:
            proxy_tier = "stealth_proxy" if use_stealth else "premium_proxy"
            logger.info(
                f"  [ScrapingBee] Attempt {attempt}/{MAX_RETRIES} "
                f"({proxy_tier}): {label}"
            )
            response = requests.get(
                SCRAPINGBEE_ENDPOINT,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            # ScrapingBee returns 200 on success, 4xx/5xx on errors
            if response.status_code == 200:
                html = response.text
                if len(html) < 500:
                    # Suspiciously short — likely a bot-block or empty render
                    logger.warning(
                        f"  [ScrapingBee] Response too short ({len(html)} chars) "
                        f"for {label}. Possible bot-block. Retrying..."
                    )
                elif _looks_like_block_page(html):
                    # A FULL-SIZED page that is still just an anti-bot
                    # interstitial/CAPTCHA shell, not real results. This is
                    # the case a length check alone misses: Akamai's
                    # challenge page can be tens of KB of JS/CSS chrome
                    # with zero flight data, so it used to sail through as
                    # a "success" and burn the request with no retry.
                    logger.warning(
                        f"  [ScrapingBee] {len(html):,} chars received for {label}, "
                        f"but it looks like a bot-block/interstitial page "
                        f"(no [data-resultid] markers, or a known challenge "
                        f"phrase found). Retrying..."
                    )
                else:
                    logger.info(
                        f"  [ScrapingBee] ✓ {len(html):,} chars received for {label}."
                    )
                    return html

            elif response.status_code == 429:
                logger.warning(
                    f"  [ScrapingBee] 429 Rate-limited for {label}. "
                    f"Backing off {RETRY_BACKOFF * attempt}s..."
                )
            else:
                logger.warning(
                    f"  [ScrapingBee] HTTP {response.status_code} for {label}. "
                    f"Body: {response.text[:300]!r}"
                )

        except requests.exceptions.Timeout:
            logger.error(f"  [ScrapingBee] Timeout on attempt {attempt} for {label}.")
        except requests.exceptions.RequestException as e:
            logger.error(f"  [ScrapingBee] Request error on attempt {attempt} for {label}: {e}")

        if attempt < MAX_RETRIES:
            backoff = RETRY_BACKOFF * attempt
            logger.info(f"  Waiting {backoff}s before retry...")
            time.sleep(backoff)

    logger.error(f"  [ScrapingBee] All {MAX_RETRIES} attempts failed for {label}. Skipping.")
    return None


# ---------------------------------------------------------------------------
# Bot-block / interstitial detection
# ---------------------------------------------------------------------------
# Phrases seen on Akamai Bot Manager interstitials and generic anti-bot
# block pages. Kayak sits behind Akamai; when its confidence in a request
# is low it serves a "press & hold" sensor-challenge page or an explicit
# access-denied page instead of results — usually still HTTP 200, and
# often large enough (JS/CSS chrome) to pass a bare length check.
_BLOCK_PAGE_SIGNATURES = (
    "press and hold",
    "press & hold",
    "verify you are human",
    "verify you're human",
    "are you a robot",
    "access denied",
    "additional security check",
    "unusual traffic",
    "reference id",
)


def _looks_like_block_page(html: str) -> bool:
    """
    True if `html` is a full-sized response that is still an anti-bot
    interstitial rather than a real Kayak results page.

    Two independent checks:
      1. Known challenge/block phrasing (see _BLOCK_PAGE_SIGNATURES).
      2. Absence of Kayak's own result-card anchor (data-resultid) AND
         no price ($NNN) pattern anywhere in the page. A genuine results
         page — even a near-empty one, even one that only matches via the
         structural fallback below — always has at least one of these;
         a pure challenge shell has neither.
    """
    lower = html.lower()
    if any(sig in lower for sig in _BLOCK_PAGE_SIGNATURES):
        return True
    if "data-resultid" not in html and not _PRICE_RE.search(html):
        return True
    return False


# ---------------------------------------------------------------------------
# Raw HTML archival
# ---------------------------------------------------------------------------
def save_raw_html(html: str, origin: str, dest: str, date_str: str) -> None:
    """
    Saves the raw HTML returned by ScrapingBee to disk for debugging and
    re-parsing without re-spending API credits.
    """
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    filename = RAW_HTML_DIR / f"kayak_{origin}_{dest}_{date_str}.html"
    try:
        filename.write_text(html, encoding="utf-8")
        logger.debug(f"  Saved raw HTML → {filename}")
    except OSError as e:
        logger.warning(f"  Could not save raw HTML: {e}")


# ===========================================================================
# DOM PARSING — structural + regex, NOT hardcoded class names
# ===========================================================================

# Price pattern: matches $123, $1,234, $1234 (USD Kayak format)
_PRICE_RE   = re.compile(r"\$\s*([\d,]+)")
# Time pattern: matches HH:MM AM/PM or HH:MM 24h
_TIME_RE    = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\s*(?:[AaPp][Mm])?\b")
# Duration pattern: e.g. "2h 35m", "10h 05m"
_DUR_RE     = re.compile(r"\b(\d+h\s*\d*m?|\d+\s*hr?\s*\d*\s*m?)\b", re.I)
# Layover / stops: "Nonstop", "1 stop", "2 stops"
_STOPS_RE   = re.compile(r"\b(nonstop|(\d+)\s+stops?)\b", re.I)


def _text(tag: Optional[Tag]) -> str:
    """Safe .get_text() that returns '' if tag is None."""
    return tag.get_text(separator=" ", strip=True) if tag else ""


def _find_price(soup_fragment: Tag) -> Optional[str]:
    """
    Extracts a USD price from a BeautifulSoup fragment using TWO strategies:

    Strategy A — aria-label attributes:
        Kayak sets aria-label="$NNN" or aria-label="NNN dollars" on price
        elements.  This has been stable across many redesigns.

    Strategy B — regex over all text nodes:
        Walk every text node in the fragment and extract the first $NNN match.
        This is class-name agnostic and robust to DOM restructuring.
    """
    # Strategy A: aria-label
    for tag in soup_fragment.find_all(attrs={"aria-label": _PRICE_RE}):
        m = _PRICE_RE.search(tag.get("aria-label", ""))
        if m:
            return m.group(1).replace(",", "")

    # Strategy B: regex over visible text
    text = soup_fragment.get_text(separator=" ")
    m = _PRICE_RE.search(text)
    if m:
        return m.group(1).replace(",", "")

    return None


def _find_airline(soup_fragment: Tag) -> Optional[str]:
    """
    Extracts the operating carrier name using THREE strategies in priority order:

    A) alt attribute on <img> tags — Kayak always puts airline logo images
       with alt="<Airline Name>" inside result cards.

    B) aria-label on carrier badge containers — a second stable hook.

    C) Text search for known carrier names operating these routes as a fallback.
    """
    # Strategy A: img alt
    for img in soup_fragment.find_all("img", alt=True):
        alt = img["alt"].strip()
        # Filter out generic UI image alts (logos/icons)
        if alt and len(alt) > 2 and not any(
            skip in alt.lower()
            for skip in ["logo", "icon", "arrow", "flag", "star", "kayak"]
        ):
            return alt

    # Strategy B: aria-label on any carrier-role div or span
    for tag in soup_fragment.find_all(True, attrs={"aria-label": True}):
        label = tag["aria-label"].strip()
        # Carrier labels are short and don't contain prices or times
        if (
            label
            and 3 < len(label) < 60
            and not _PRICE_RE.search(label)
            and not _TIME_RE.search(label)
        ):
            return label

    # Strategy C: known carrier fingerprints for these specific routes
    KNOWN_CARRIERS = [
        "Georgian Airways", "Wizz Air", "El Al", "FlyDubai",
        "Turkish Airlines", "Pegasus", "Flydubai", "Air Arabia",
        "LOT", "Lufthansa", "Austrian", "Swiss", "Air France",
        "KLM", "Ryanair", "EasyJet", "Israir", "Arkia",
    ]
    text = soup_fragment.get_text()
    for carrier in KNOWN_CARRIERS:
        if carrier.lower() in text.lower():
            return carrier

    return None


def _find_times(soup_fragment: Tag) -> tuple[Optional[str], Optional[str]]:
    """
    Extracts departure and arrival times from a result card.

    Kayak renders times in <span> or <div> elements whose text matches HH:MM.
    We collect ALL time matches in the fragment and return (first, last) —
    the first being departure, last being arrival.  This holds true whether
    there are 0 or N layovers.
    """
    all_times = []
    for tag in soup_fragment.find_all(True):
        # Only inspect leaf-ish nodes to avoid duplicates from parent containers
        if tag.find(True):
            continue
        text = tag.get_text(strip=True)
        m = _TIME_RE.match(text)
        if m and text == m.group(0):  # entire text is just a time string
            all_times.append(text)

    if len(all_times) >= 2:
        return all_times[0], all_times[-1]
    elif len(all_times) == 1:
        return all_times[0], None
    return None, None


def _find_stops(soup_fragment: Tag) -> Optional[str]:
    """
    Extracts stop information: 'Nonstop', '1 stop', '2 stops', etc.
    Uses regex over full text — stop labels are a plain-text string, not
    locked in any particular tag.
    """
    text = soup_fragment.get_text(separator=" ")
    m = _STOPS_RE.search(text)
    return m.group(0).strip().title() if m else None


def _find_duration(soup_fragment: Tag) -> Optional[str]:
    """
    Extracts total flight duration (e.g. '2h 35m').
    """
    text = soup_fragment.get_text(separator=" ")
    m = _DUR_RE.search(text)
    return m.group(0).strip() if m else None


def parse_kayak_html(
    html: str,
    origin: str,
    dest: str,
    date_str: str,
) -> list[dict]:
    """
    Parses the full rendered HTML from a Kayak results page and extracts
    flight records.

    Result card detection strategy:
    -------------------------------------------------------
    Primary: Kayak currently renders each result card as a <div> with
    class "nrc6-mod-pres-default".  This has been confirmed as the live
    markup as of September 2026.

    Fallback: If the primary selector finds nothing (markup change, bot-block,
    failed render), we fall back to a structural heuristic — any <div> that
    contains both a price AND a time pattern.  This is class-name agnostic
    and resilient to future Kayak redesigns.

    Returns a list of record dicts (one per result card found).
    An empty list signals that the page yielded no usable data.
    """
    soup = BeautifulSoup(html, "html.parser")
    snap = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    records: list[dict] = []

    # ── Strategy 1: nrc6-mod-pres-default class (current Kayak markup) ─────
    cards = soup.find_all("div", class_="nrc6-mod-pres-default")

    if cards:
        logger.info(
            f"  [Parser] Found {len(cards)} [nrc6-mod-pres-default] cards "
            f"for {origin}→{dest} {date_str}."
        )

    # ── Strategy 2 (fallback): structural heuristic ────────────────────────
    if not cards:
        logger.warning(
            f"  [Parser] No [nrc6-mod-pres-default] cards found for "
            f"{origin}→{dest} {date_str}. Falling back to structural heuristic."
        )
        candidates = soup.find_all("div")
        for div in candidates:
            text = div.get_text()
            # A flight card must contain at least a price AND a time
            if _PRICE_RE.search(text) and _TIME_RE.search(text):
                # Avoid parent containers that wrap multiple cards
                inner_prices = len(_PRICE_RE.findall(text))
                if inner_prices <= 3:   # at most ~3 prices = base + taxes + total
                    cards.append(div)

        logger.info(
            f"  [Parser] Structural heuristic found {len(cards)} candidate cards "
            f"for {origin}→{dest} {date_str}."
        )

    if not cards:
        logger.error(
            f"  [Parser] Zero result cards extracted for {origin}→{dest} {date_str}. "
            f"Page may be a CAPTCHA or bot-block. Check saved raw HTML."
        )
        return []

    # ── Extract fields from each card ──────────────────────────────────────
    seen_prices: set[str] = set()   # deduplicate cards that share a price

    for card in cards:
        try:
            price_str   = _find_price(card)
            airline     = _find_airline(card)
            dep_time, arr_time = _find_times(card)
            stops       = _find_stops(card)
            duration    = _find_duration(card)

            # Skip cards with no price — they're UI chrome, not flight results
            if price_str is None:
                continue

            # Deduplicate by strict flight footprint (date + times + airline + price + stops)
            # The user requested to remove duplicates that are the exact same flight
            flight_key = f"{date_str}_{dep_time}_{arr_time}_{airline}_{price_str}_{stops}"
            if flight_key in seen_prices:
                continue
            seen_prices.add(flight_key)

            records.append({
                "snapshot_ts":    snap,
                "scrape_source":  "Kayak via ScrapingBee",
                "origin":         origin,
                "destination":    dest,
                "depart_date":    date_str,
                "airline":        airline or "Unknown",
                "dep_time":       dep_time,
                "arr_time":       arr_time,
                "duration":       duration,
                "stops":          stops,
                "price_usd":      int(price_str) if price_str.isdigit() else price_str,
            })

        except Exception as e:
            # Per-card errors must never crash the entire run
            logger.warning(f"  [Parser] Error extracting card for {origin}→{dest} {date_str}: {e}")
            continue

    logger.info(
        f"  [Parser] ✓ {len(records)} flight records extracted "
        f"for {origin}→{dest} {date_str}."
    )
    return records


# ---------------------------------------------------------------------------
# Save checkpoint — append to CSV after each date so data is never lost
# ---------------------------------------------------------------------------
def append_checkpoint(records: list[dict], filepath: str, is_first_write: bool) -> None:
    """
    Appends a batch of records to the CSV file immediately after each
    successful page parse, so a mid-run crash loses at most one day's data.
    """
    if not records:
        return
    df = pd.DataFrame(records)
    df.to_csv(
        filepath,
        mode="w" if is_first_write else "a",
        index=False,
        header=is_first_write,
        encoding="utf-8-sig",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Per-route worker — runs inside its own thread
# ---------------------------------------------------------------------------
def scrape_route(
    origin: str,
    dest: str,
    api_key: str,
    temp_csv: Path,
) -> tuple[int, int]:
    """
    Scrapes all WINTER_2026_DAYS dates for a single route and writes records
    to a per-route temporary CSV.  Returns (success_count, failure_count).
    """
    route_label = f"{origin}→{dest}"
    success_count = 0
    failure_count = 0
    is_first_write = True
    total = WINTER_2026_DAYS

    for offset in range(total):
        dep_date = WINTER_2026_START + datetime.timedelta(days=offset)
        date_str = dep_date.isoformat()
        url = (
            f"https://www.kayak.com/flights/{origin}-{dest}/{date_str}"
            f"?sort=price_a&stops=~0"
        )
        label = f"{route_label} {date_str} (day {offset+1}/{total})"
        logger.info(f"[{route_label}] Processing: {label}")

        html = fetch_via_scrapingbee(url, api_key, label)

        if html is None:
            failure_count += 1
            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            time.sleep(delay)
            continue

        save_raw_html(html, origin, dest, date_str)
        records = parse_kayak_html(html, origin, dest, date_str)

        if records:
            append_checkpoint(records, str(temp_csv), is_first_write)
            is_first_write = False
            success_count += 1
        else:
            failure_count += 1

        if offset < total - 1:
            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            time.sleep(delay)

    logger.info(f"[{route_label}] ✓ Done — {success_count} ok / {failure_count} failed.")
    return success_count, failure_count


def main() -> None:
    logger.info("=" * 70)
    logger.info("Kayak Flight Scraper — Winter 2026/27 (Oct-Mar) (via ScrapingBee)")
    logger.info(f"  Routes  : {' | '.join(f'{o}→{d}' for o, d in ROUTES)}")
    logger.info(f"  Period  : 2026-10-01 → 2027-03-31  ({WINTER_2026_DAYS} days)")
    logger.info(f"  Workers : {len(ROUTES)} concurrent sessions (one per route)")
    logger.info(f"  Output  : Multiple route-specific CSV files")
    logger.info(f"  Log     : {log_filename}")
    logger.info("=" * 70)

    api_key = get_api_key()

    total_requests = len(ROUTES) * WINTER_2026_DAYS
    # Best case: every request succeeds on attempt 1 at premium_proxy (~25cr).
    # Worst case: every request exhausts all retries and lands on the
    # stealth_proxy escalation on the final attempt (~75cr, plus the
    # premium_proxy attempts before it).
    best_case = total_requests * 25
    worst_case = total_requests * (25 * (MAX_RETRIES - 1) + 75)
    logger.info(
        f"\n⚠  Credit estimate: {total_requests} requests, {best_case:,}–{worst_case:,} "
        f"ScrapingBee credits depending on how often the stealth_proxy escalation "
        f"kicks in (all {len(ROUTES)} routes run simultaneously).\n"
    )

    # Create one final CSV per route with the current date
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    final_csvs: dict[tuple[str, str], Path] = {
        (o, d): Path(f"{today_str}_{o}_to_{d}.csv") for o, d in ROUTES
    }
    for p in final_csvs.values():
        if p.exists():
            p.unlink()
            logger.info(f"Removed existing '{p}' — creating fresh file.")

    # ── Launch 4 concurrent scraping sessions ────────────────────────────
    logger.info(f"Launching {len(ROUTES)} concurrent scraping sessions...\n")
    total_success = 0
    total_failure = 0

    with ThreadPoolExecutor(max_workers=len(ROUTES)) as executor:
        futures = {
            executor.submit(scrape_route, o, d, api_key, final_csvs[(o, d)]): (o, d)
            for o, d in ROUTES
        }
        for future in as_completed(futures):
            o, d = futures[future]
            try:
                s, f = future.result()
                total_success += s
                total_failure += f
            except Exception as e:
                logger.error(f"Route {o}→{d} raised an unexpected error: {e}")

    # ── Clean and deduplicate final datasets ───────────────────────────
    logger.info(f"\n{'=' * 70}")
    logger.info("All sessions complete. Cleaning up final datasets...")

    column_order = [
        "snapshot_ts", "scrape_source",
        "origin", "destination", "depart_date",
        "airline", "dep_time", "arr_time", "duration", "stops",
        "price_usd",
    ]

    total_records_saved = 0
    generated_files = []

    for (o, d), final_path in final_csvs.items():
        if final_path.exists():
            try:
                df = pd.read_csv(final_path)
                if df.empty:
                    continue
                
                df = df[[c for c in column_order if c in df.columns]]
                
                # Deduplicate (keep cheapest)
                initial_len = len(df)
                df["price_usd"] = pd.to_numeric(df["price_usd"], errors="coerce")
                df = df.sort_values("price_usd")
                dedup_cols = ["origin", "destination", "depart_date", "airline", "dep_time"]
                df = df.drop_duplicates(subset=dedup_cols, keep="first")
                
                # Sort: route → date → price
                df.sort_values(
                    by=["origin", "destination", "depart_date", "price_usd"],
                    ascending=True,
                    inplace=True,
                )
                df.reset_index(drop=True, inplace=True)
                df.to_csv(final_path, index=False, encoding="utf-8-sig")
                
                total_records_saved += len(df)
                generated_files.append(str(final_path))
                
                if len(df) < initial_len:
                    logger.info(f"[{o}→{d}] Removed {initial_len - len(df)} duplicate records. Saved {len(df)} total.")
                else:
                    logger.info(f"[{o}→{d}] Saved {len(df)} records.")
                    
            except Exception as e:
                logger.warning(f"Could not read/clean final file {final_path}: {e}")

    if not generated_files:
        logger.error(
            "No records were collected across any route.\n"
            "Check the saved raw HTML files in api_responses/kayak_raw/ for\n"
            "CAPTCHA pages or empty responses."
        )
        sys.exit(1)

    # ── Summary ──────────────────────────────────────────────────────────
    logger.info("=" * 70)
    logger.info("Run Summary")
    logger.info("-" * 70)
    logger.info(f"  Concurrent sessions      : {len(ROUTES)}")
    logger.info(f"  Successful pages parsed  : {total_success}")
    logger.info(f"  Failed / empty pages     : {total_failure}")
    logger.info(f"  Total flight records     : {total_records_saved}")
    logger.info(f"  Output CSVs              : {', '.join(generated_files)}")
    logger.info(f"  Raw HTML archives        : {RAW_HTML_DIR}/")
    logger.info(f"  Run log                  : {log_filename}")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
