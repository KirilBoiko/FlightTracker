#!/usr/bin/env python3
"""
kayak_scraper.py
================
Scrapes cheapest flight prices from Kayak via ScrapingBee API for the
planned flight schedule (TBS/TLV/BUS). Flights <= 90 days out are scraped daily;
flights > 90 days out are scraped every 3 days.

Usage:
    export SCRAPINGBEE_API_KEY="your_key_here"
    python3 kayak_scraper.py                # daily cadence run
    python3 kayak_scraper.py --initial      # initial full pass over the entire schedule
    python3 kayak_scraper.py --dry-run      # preview search count without making requests
    python3 kayak_scraper.py --export-schedule  # export proposed_schedule.csv for Tableau Prep
"""

from __future__ import annotations

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
from dataclasses import dataclass
from zoneinfo import ZoneInfo
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import argparse
import collections


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

SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"

MAX_WORKERS = int(os.environ.get("KAYAK_MAX_WORKERS", "4"))  # concurrent ScrapingBee sessions

RAW_HTML_DIR  = Path("api_responses") / "kayak_raw"

JS_WAIT_MS = 15000  # ms to wait after page load before capturing HTML
DELAY_MIN = 10
DELAY_MAX = 25
MAX_RETRIES = 3  # Attempts 1 & 2: premium proxy (handles 500s / timeouts); Attempt 3: stealth proxy
RETRY_BACKOFF = 20
REQUEST_TIMEOUT = 150

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
    """Yields (origin, destination, date_str, url) for routes and dates."""
    for origin, dest in routes:
        for offset in range(days):
            dep_date = start + datetime.timedelta(days=offset)
            date_str = dep_date.isoformat()
            url = (
                f"https://www.kayak.com/flights/{origin}-{dest}/{date_str}"
                f"?sort=price_a&stops=~0"
            )
            yield origin, dest, date_str, url


def fetch_via_scrapingbee(
    target_url: str,
    api_key: str,
    label: str,
) -> Optional[str]:
    """Fetches target_url via ScrapingBee with JS rendering and retry logic."""
    for attempt in range(1, MAX_RETRIES + 1):
        use_stealth = attempt == MAX_RETRIES

        params = {
            "api_key":         api_key,
            "url":             target_url,
            "render_js":       "true",
            "country_code":    "us",
            "wait":            str(JS_WAIT_MS),
            "block_resources": "false",
            "device":          "desktop",
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

            if response.status_code == 200:
                html = response.text
                if len(html) < 500:
                    logger.warning(
                        f"  [ScrapingBee] Response too short ({len(html)} chars) "
                        f"for {label}. Possible bot-block. Retrying..."
                    )
                elif _looks_like_block_page(html):
                    logger.warning(
                        f"  [ScrapingBee] {len(html):,} chars received for {label}, "
                        f"but it looks like a bot-block/interstitial page. Retrying..."
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
    """Checks whether the response looks like an anti-bot challenge or block page."""
    lower = html.lower()
    if any(sig in lower for sig in _BLOCK_PAGE_SIGNATURES):
        return True
    if "nrc6-mod-pres-default" not in html:
        return True
    return False


def save_raw_html(html: str, origin: str, dest: str, date_str: str) -> None:
    """Saves raw HTML returned by ScrapingBee to disk."""
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    filename = RAW_HTML_DIR / f"kayak_{origin}_{dest}_{date_str}.html"
    try:
        filename.write_text(html, encoding="utf-8")
        logger.debug(f"  Saved raw HTML → {filename}")
    except OSError as e:
        logger.warning(f"  Could not save raw HTML: {e}")


_PRICE_RE = re.compile(r"\$\s*([\d,]+)")
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\s*(?:[AaPp][Mm])?\b")
_DUR_RE = re.compile(r"\b(\d+h\s*\d*m?|\d+\s*hr?\s*\d*\s*m?)\b", re.I)
_STOPS_RE = re.compile(r"\b(nonstop|(\d+)\s+stops?)\b", re.I)


def _text(tag: Optional[Tag]) -> str:
    return tag.get_text(separator=" ", strip=True) if tag else ""


def _find_price(soup_fragment: Tag) -> Optional[str]:
    """Extracts USD price from aria-label or visible text."""
    for tag in soup_fragment.find_all(attrs={"aria-label": _PRICE_RE}):
        m = _PRICE_RE.search(tag.get("aria-label", ""))
        if m:
            return m.group(1).replace(",", "")

    text = soup_fragment.get_text(separator=" ")
    m = _PRICE_RE.search(text)
    if m:
        return m.group(1).replace(",", "")

    return None


def _find_airline(soup_fragment: Tag) -> Optional[str]:
    """Extracts operating carrier name via img alt, aria-label, or known carrier names."""
    for img in soup_fragment.find_all("img", alt=True):
        alt = img["alt"].strip()
        if alt and len(alt) > 2 and not any(
            skip in alt.lower()
            for skip in ["logo", "icon", "arrow", "flag", "star", "kayak"]
        ):
            return alt

    for tag in soup_fragment.find_all(True, attrs={"aria-label": True}):
        label = tag["aria-label"].strip()
        if (
            label
            and 3 < len(label) < 60
            and not _PRICE_RE.search(label)
            and not _TIME_RE.search(label)
        ):
            return label

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
    """Extracts departure and arrival times from a result card."""
    all_times = []
    for tag in soup_fragment.find_all(True):
        if tag.find(True):
            continue
        text = tag.get_text(strip=True)
        m = _TIME_RE.match(text)
        if m and text == m.group(0):
            all_times.append(text)

    if len(all_times) >= 2:
        return all_times[0], all_times[-1]
    elif len(all_times) == 1:
        return all_times[0], None
    return None, None


def _find_stops(soup_fragment: Tag) -> Optional[str]:
    """Extracts stop information (e.g. 'Nonstop', '1 stop')."""
    text = soup_fragment.get_text(separator=" ")
    m = _STOPS_RE.search(text)
    return m.group(0).strip().title() if m else None


def _find_duration(soup_fragment: Tag) -> Optional[str]:
    """Extracts total flight duration (e.g. '2h 35m')."""
    text = soup_fragment.get_text(separator=" ")
    m = _DUR_RE.search(text)
    return m.group(0).strip() if m else None


def parse_kayak_html(
    html: str,
    origin: str,
    dest: str,
    date_str: str,
) -> list[dict]:
    """Parses rendered HTML from Kayak results page and extracts flight records."""
    soup = BeautifulSoup(html, "html.parser")
    snap = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    records: list[dict] = []

    cards = soup.find_all("div", class_="nrc6-mod-pres-default")

    if cards:
        logger.info(
            f"  [Parser] Found {len(cards)} [nrc6-mod-pres-default] cards "
            f"for {origin}→{dest} {date_str}."
        )

    if not cards:
        logger.warning(
            f"  [Parser] No [nrc6-mod-pres-default] cards found for "
            f"{origin}→{dest} {date_str}. Falling back to structural heuristic."
        )
        candidates = soup.find_all("div")
        for div in candidates:
            text = div.get_text()
            if _PRICE_RE.search(text) and _TIME_RE.search(text):
                inner_prices = len(_PRICE_RE.findall(text))
                if inner_prices <= 3:
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

    seen_prices: set[str] = set()

    for card in cards:
        try:
            price_str   = _find_price(card)
            airline     = _find_airline(card)
            dep_time, arr_time = _find_times(card)
            stops       = _find_stops(card)
            duration    = _find_duration(card)

            if price_str is None:
                continue

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
            logger.warning(f"  [Parser] Error extracting card for {origin}→{dest} {date_str}: {e}")
            continue

    logger.info(
        f"  [Parser] ✓ {len(records)} flight records extracted "
        f"for {origin}→{dest} {date_str}."
    )
    return records


def append_checkpoint(records: list[dict], filepath: str, is_first_write: bool) -> None:
    """Appends records to CSV after each date."""
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


def scrape_route(
    origin: str,
    dest: str,
    dates: list[datetime.date],
    api_key: str,
    temp_csv: Path,
) -> tuple[int, int]:
    """Scrapes the given dates for a single route and writes records to CSV."""
    route_label = f"{origin}→{dest}"
    success_count = 0
    failure_count = 0
    is_first_write = True
    total = len(dates)

    for offset, dep_date in enumerate(dates):
        date_str = dep_date.isoformat()
        url = (
            f"https://www.kayak.com/flights/{origin}-{dest}/{date_str}"
            f"?sort=price_a&stops=~0"
        )
        label = f"{route_label} {date_str} (search {offset+1}/{total})"
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


CADENCE_TIERS: list[tuple[Optional[int], int]] = [
    (90, 1),
    (None, 3),
]
MAX_DAYS_AHEAD = 330
LEGACY_DAILY_UNTIL = datetime.date(2027, 3, 31)
LEGACY_ROUTES: list[tuple[str, str]] = [("TBS", "TLV"), ("TLV", "TBS")]

AIRPORT_TZ = {
    "TBS": ZoneInfo("Asia/Tbilisi"),
    "BUS": ZoneInfo("Asia/Tbilisi"),
    "TLV": ZoneInfo("Asia/Jerusalem"),
}
UTC = datetime.timezone.utc


@dataclass(frozen=True)
class Leg:
    day: int                 # 1=Mon .. 7=Sun (UTC)
    origin: str
    dest: str
    dep_utc: Optional[str]   # "HH:MM" or None (TBD)


@dataclass(frozen=True)
class Period:
    start: datetime.date
    end: datetime.date
    legs: tuple[Leg, ...]


def _legs(rows: str) -> tuple[Leg, ...]:
    """Parses lines of 'day origin dest time' (time may be TBD)."""
    out = []
    for line in rows.strip().splitlines():
        day, o, d, t = line.split()
        out.append(Leg(int(day), o, d, None if t == "TBD" else t))
    return tuple(out)


D = datetime.date

# ---- 01.12.2026 - 26.03.2027 (times TBD) ----------------------------------
_WINTER = _legs("""
4 TBS TLV TBD
4 TLV TBS TBD
7 TBS TLV TBD
7 TLV TBS TBD
""")

# ---- 28.03.2027 - 30.05.2027 ----------------------------------------------
_SPRING = _legs("""
1 TBS TLV 22:00
1 TLV TBS 02:00
2 TLV TBS 02:00
4 TBS BUS 07:00
4 BUS TLV 09:00
4 TLV BUS 12:30
4 BUS TBS 16:00
4 TBS TLV 22:00
5 TLV TBS 02:00
6 TBS TLV 22:00
7 TLV BUS 02:00
7 BUS TLV 07:30
7 TLV TBS 11:00
7 TBS TLV 22:00
""")

# ---- 01.06.2027 - 28.06.2027 ----------------------------------------------
_JUNE = _legs("""
1 TLV TBS 01:30
1 TBS TLV 22:00
2 TLV BUS 01:30
2 BUS TLV 07:00
2 TLV TBS 10:30
3 TBS TLV 22:00
4 TLV BUS 01:30
4 BUS TLV 07:00
4 TLV TBS 10:30
4 TBS TLV 22:00
5 TLV TBS 01:30
6 TBS TLV 22:00
7 TLV BUS 01:30
7 BUS TLV 07:00
7 TLV TBS 10:30
7 TBS TLV 22:00
""")

# ---- 29.06.2027 - 31.08.2027 ----------------------------------------------
_SUMMER = _legs("""
1 TLV BUS 01:30
1 BUS TLV 07:00
1 TLV TBS 10:30
1 TBS TLV 22:00
2 TLV BUS 01:30
2 BUS TLV 07:00
2 TLV TBS 10:30
2 TBS TLV 22:00
3 TLV BUS 01:30
3 BUS TLV 07:00
3 TLV TBS 10:30
3 TBS TLV 22:00
4 TLV BUS 01:30
4 BUS TLV 07:00
4 TLV TBS 10:30
4 TBS TLV 22:00
5 TLV BUS 01:30
5 BUS TLV 07:00
5 TLV TBS 10:30
6 TBS TLV 22:00
7 TLV BUS 01:30
7 BUS TLV 07:00
7 TLV TBS 10:30
7 TBS TLV 22:00
""")

# ---- 01.09.2027 - 30.09.2027 ----------------------------------------------
_SEPTEMBER = _legs("""
1 TLV TBS 01:30
1 TBS TLV 22:00
2 TLV BUS 01:30
2 BUS TLV 07:00
2 TLV TBS 10:30
3 TBS TLV 22:00
4 TLV BUS 01:30
4 BUS TLV 07:00
4 TLV TBS 10:30
4 TBS TLV 22:00
5 TLV TBS 01:30
6 TBS TLV 22:00
7 TLV BUS 01:30
7 BUS TLV 07:00
7 TLV TBS 09:30
7 TBS TLV 22:00
""")

# ---- 01.10.2027 - 30.10.2027 ----------------------------------------------
_OCTOBER = _legs("""
1 TBS TLV 22:00
2 TLV BUS 01:30
2 BUS TLV 07:00
2 TLV TBS 10:30
3 TBS TLV 22:00
4 TLV BUS 01:30
4 BUS TLV 07:00
4 TLV TBS 10:30
4 TBS TLV 22:00
5 TLV TBS 01:30
6 TBS TLV 22:00
7 TLV BUS 01:30
7 BUS TLV 07:00
7 TLV TBS 10:30
""")

SCHEDULE: list[Period] = [
    Period(D(2026, 12, 1), D(2027, 3, 26), _WINTER),
    Period(D(2027, 3, 28), D(2027, 5, 30), _SPRING),
    Period(D(2027, 6, 1),  D(2027, 6, 28), _JUNE),
    Period(D(2027, 6, 29), D(2027, 8, 31), _SUMMER),
    Period(D(2027, 9, 1),  D(2027, 9, 30), _SEPTEMBER),
    Period(D(2027, 10, 1), D(2027, 10, 30), _OCTOBER),
]


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------
def local_departure_date(utc_date: datetime.date, leg: Leg) -> datetime.date:
    """Local calendar date at the origin airport for a leg departing on utc_date."""
    if leg.dep_utc is None:
        return utc_date  # time unknown: assume local date == scheduled date
    hh, mm = map(int, leg.dep_utc.split(":"))
    dt_utc = datetime.datetime(utc_date.year, utc_date.month, utc_date.day, hh, mm, tzinfo=UTC)
    return dt_utc.astimezone(AIRPORT_TZ[leg.origin]).date()


def scheduled_searches(
    today: datetime.date,
    max_days_ahead: int = MAX_DAYS_AHEAD,
) -> dict[tuple[str, str, datetime.date], list[str]]:
    """
    All distinct (origin, dest, local_date) Kayak searches covering the schedule from
    ``today`` through ``today + max_days_ahead``. Value = UTC departure times covered
    (informational, for logging).
    """
    horizon = today + datetime.timedelta(days=max_days_ahead)
    searches: dict[tuple[str, str, datetime.date], list[str]] = {}

    def add(o: str, d: str, local_date: datetime.date, t: str) -> None:
        if today <= local_date <= horizon:
            searches.setdefault((o, d, local_date), []).append(t)

    # Legacy: current flights, every day, until the planned schedule takes over.
    day = today
    while day <= min(LEGACY_DAILY_UNTIL, horizon):
        for o, d in LEGACY_ROUTES:
            add(o, d, day, "legacy")
        day += datetime.timedelta(days=1)

    # Planned schedule.
    for period in SCHEDULE:
        by_day: dict[int, list[Leg]] = {}
        for leg in period.legs:
            by_day.setdefault(leg.day, []).append(leg)
        utc_day = period.start
        while utc_day <= period.end:
            for leg in by_day.get(utc_day.isoweekday(), []):
                add(leg.origin, leg.dest, local_departure_date(utc_day, leg),
                    leg.dep_utc or "TBD")
            utc_day += datetime.timedelta(days=1)
    return searches


def cadence_step(days_ahead: int) -> int:
    for max_ahead, step in CADENCE_TIERS:
        if max_ahead is None or days_ahead <= max_ahead:
            return step
    return 1


def due_today(
    searches: dict[tuple[str, str, datetime.date], list[str]],
    today: datetime.date,
    initial: bool = False,
) -> list[tuple[str, str, datetime.date]]:
    """
    Searches to run on ``today``.
    - If initial=True: full pass over the entire schedule.
    - Rolling 90 days: departing <= 90 days from today -> scraped every day.
    - Beyond 90 days: departing > 90 days from today  -> scraped when days_ahead % 3 == 0.
    """
    due = []
    for (o, d, date) in sorted(searches):
        days_ahead = (date - today).days
        step = cadence_step(days_ahead)
        if initial or days_ahead % step == 0:
            due.append((o, d, date))
    return due


# ---------------------------------------------------------------------------
# Export for Tableau Prep (union into Booking_Report_Clean)
# ---------------------------------------------------------------------------
def time_slot_utc(hhmm: str) -> str:
    """6-hour UTC slot label used by the price flow: 00-06, 06-12, 12-18, 18-24."""
    h = int(hhmm.split(":")[0]) // 6 * 6
    return f"{h:02d}-{h + 6:02d}"


def proposed_flights(start: datetime.date) -> list[dict]:
    """One row per proposed flight (UTC date) from ``start`` on. TBD-time legs are skipped."""
    rows = []
    for period in SCHEDULE:
        by_day: dict[int, list[Leg]] = {}
        for leg in period.legs:
            if leg.dep_utc:
                by_day.setdefault(leg.day, []).append(leg)
        day = max(period.start, start)
        while day <= period.end:
            for leg in by_day.get(day.isoweekday(), []):
                rows.append({
                    "departure_date": day.isoformat(),
                    "departure_time(UTC)": leg.dep_utc,
                    "time_slot_utc": time_slot_utc(leg.dep_utc),
                    "Origin": leg.origin,
                    "Destination": leg.dest,
                    "Route": f"{leg.origin}-{leg.dest}",
                    "Flight Status": "Proposed",
                })
            day += datetime.timedelta(days=1)
    return rows


def build_plan(initial: bool) -> dict[tuple[str, str], list[datetime.date]]:
    """Returns {(origin, dest): [dates to search today]} per the schedule and cadence."""
    today = datetime.date.today()
    searches = scheduled_searches(today)
    due = due_today(searches, today, initial=initial)
    plan: dict[tuple[str, str], list[datetime.date]] = collections.defaultdict(list)
    for o, d, date in due:
        plan[(o, d)].append(date)
    return dict(sorted(plan.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Kayak flight pricing scraper")
    parser.add_argument("--initial", action="store_true",
                        help="One full pass over every scheduled search, ignoring cadence tiers")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan and credit estimate without making any requests")
    parser.add_argument("--export-schedule", metavar="START_DATE", nargs="?", const="2027-03-26",
                        help="Export proposed schedule CSV for Tableau Prep and exit (default start: 2027-03-26)")
    args = parser.parse_args()

    if args.export_schedule:
        out_file = "proposed_schedule.csv"
        data = proposed_flights(datetime.date.fromisoformat(args.export_schedule))
        with open(out_file, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(data[0]))
            w.writeheader()
            w.writerows(data)
        logger.info(f"Exported {len(data)} proposed flights ({data[0]['departure_date']} → {data[-1]['departure_date']}) to {out_file}")
        return

    plan = build_plan(args.initial)
    ROUTES = list(plan)
    total_requests = sum(len(v) for v in plan.values())
    best_case = total_requests * 25
    worst_case = total_requests * (25 * (MAX_RETRIES - 1) + 75)

    logger.info("=" * 70)
    logger.info(f"Kayak Flight Scraper (via ScrapingBee)  "
                f"[{'INITIAL FULL PASS' if args.initial else 'daily cadence run'}]")
    for (o, d), dates in plan.items():
        span = f"{dates[0]} → {dates[-1]}" if dates else "-"
        logger.info(f"  {o}→{d}: {len(dates):4d} searches ({span})")
    logger.info(f"  Total   : {total_requests} searches, "
                f"{best_case:,}–{worst_case:,} ScrapingBee credits")
    logger.info(f"  Workers : {min(MAX_WORKERS, max(1, len(ROUTES)))} concurrent sessions")
    logger.info(f"  Log     : {log_filename}")
    logger.info("=" * 70)

    if args.dry_run or not ROUTES:
        return

    api_key = get_api_key()

    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    final_csvs: dict[tuple[str, str], Path] = {
        (o, d): Path(f"{today_str}_{o}_to_{d}.csv") for o, d in ROUTES
    }
    for p in final_csvs.values():
        if p.exists():
            p.unlink()
            logger.info(f"Removed existing '{p}' — creating fresh file.")

    workers = min(MAX_WORKERS, len(ROUTES))
    logger.info(f"Launching {workers} concurrent scraping sessions...\n")
    total_success = 0
    total_failure = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(scrape_route, o, d, plan[(o, d)], api_key, final_csvs[(o, d)]): (o, d)
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
                
                initial_len = len(df)
                df["price_usd"] = pd.to_numeric(df["price_usd"], errors="coerce")
                df = df.sort_values("price_usd")
                dedup_cols = ["origin", "destination", "depart_date", "airline", "dep_time"]
                df = df.drop_duplicates(subset=dedup_cols, keep="first")
                
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

    logger.info("=" * 70)
    logger.info("Run Summary")
    logger.info("-" * 70)
    logger.info(f"  Concurrent sessions      : {workers}")
    logger.info(f"  Successful pages parsed  : {total_success}")
    logger.info(f"  Failed / empty pages     : {total_failure}")
    logger.info(f"  Total flight records     : {total_records_saved}")
    logger.info(f"  Output CSVs              : {', '.join(generated_files)}")
    logger.info(f"  Raw HTML archives        : {RAW_HTML_DIR}/")
    logger.info(f"  Run log                  : {log_filename}")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
