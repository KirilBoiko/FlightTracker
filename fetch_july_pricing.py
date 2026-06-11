#!/usr/bin/env python3
"""
fetch_july_pricing.py
=====================
Fetches July 2026 flight pricing for four routes using TWO complementary
Travelpayouts endpoints:

  ① /v2/prices/month-matrix  →  tbs_bus_tlv_july_BEST_PRICE.csv
     One row per date per route — the single cheapest price Travelpayouts
     has cached for that day.  Great for a price-calendar overview.

  ② /v2/prices/latest        →  tbs_bus_tlv_july_ALL_OPTIONS.csv
     Up to 1 000 recently-cached individual tickets per route, filtered to
     July 2026.  Can return many rows per date (different airlines, stop
     counts, booking sites).  This is the "full list" view.

Routes queried:
    TBS (Tbilisi)  → TLV (Tel Aviv)
    TLV (Tel Aviv) → TBS (Tbilisi)
    BUS (Batumi)   → TLV (Tel Aviv)
    TLV (Tel Aviv) → BUS (Batumi)

Usage:
    pip install requests pandas

    export TRAVELPAYOUTS_API_KEY="your_token_here"
    python3 fetch_july_pricing.py
"""

from __future__ import annotations  # enables X | Y and list[X] hints on Python 3.9

import os
import sys
import time
import logging
import datetime
import requests
import pandas as pd

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MONTH_MATRIX_URL = "https://api.travelpayouts.com/v2/prices/month-matrix"
LATEST_URL       = "https://api.travelpayouts.com/v2/prices/latest"
AIRLINES_URL     = "https://api.travelpayouts.com/data/en/airlines.json"

ROUTES: list[tuple[str, str]] = [
    ("TBS", "TLV"),
    ("TLV", "TBS"),
    ("BUS", "TLV"),
    ("TLV", "BUS"),
]

TARGET_MONTH  = "2026-07"          # YYYY-MM used by month-matrix
PERIOD_START  = "2026-07-01"       # beginning_of_period for /latest (YYYY-MM-DD)
CURRENCY      = "USD"

# Output files
CSV_BEST   = "tbs_bus_tlv_july_BEST_PRICE.csv"    # month-matrix: 1 row / date
CSV_ALL    = "tbs_bus_tlv_july_ALL_OPTIONS.csv"   # /latest: many rows / date

REQUEST_DELAY   = 1.0   # seconds between requests
REQUEST_TIMEOUT = 30    # seconds per request

TRIP_CLASS_LABELS = {0: "Economy", 1: "Business", 2: "First"}

TODAY = datetime.date.today()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_api_key() -> str:
    key = os.environ.get("TRAVELPAYOUTS_API_KEY", "").strip()
    if not key:
        logger.error(
            "TRAVELPAYOUTS_API_KEY is not set.\n"
            "  macOS/Linux : export TRAVELPAYOUTS_API_KEY=\"your_token_here\"\n"
            "  Windows CMD : set TRAVELPAYOUTS_API_KEY=your_token_here"
        )
        sys.exit(1)
    return key


# ---------------------------------------------------------------------------
# Shared: airline IATA → name lookup
# ---------------------------------------------------------------------------
def load_airline_lookup(api_key: str) -> dict[str, str]:
    """
    Downloads the Travelpayouts airline reference list once and returns an
    IATA-code → airline-name mapping.
    Falls back to {} on any error so pricing data is never lost.
    """
    logger.info("Fetching airline reference data...")
    try:
        resp = requests.get(
            AIRLINES_URL,
            headers={"x-access-token": api_key},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        airlines: list[dict] = resp.json()
    except Exception as err:
        logger.warning(f"  [AIRLINE LOOKUP] Failed: {err}. Names will fall back to IATA codes.")
        return {}

    lookup = {
        (a.get("iata") or "").strip(): (a.get("name") or "").strip()
        for a in airlines
        if (a.get("iata") or "").strip()
    }
    logger.info(f"  ✓ {len(lookup)} airlines loaded.")
    return lookup


# ---------------------------------------------------------------------------
# Shared: safe HTTP GET with uniform error handling
# ---------------------------------------------------------------------------
def _get_json(url: str, headers: dict, params: dict, label: str) -> dict | None:
    """
    Makes a GET request and returns the parsed JSON dict.
    Logs a specific error for each failure mode and returns None on any error.
    """
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error(f"  [TIMEOUT] {label}")
        return None
    except requests.exceptions.HTTPError as e:
        logger.error(f"  [HTTP {resp.status_code}] {label}: {e}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"  [REQUEST ERROR] {label}: {e}")
        return None

    try:
        return resp.json()
    except ValueError as e:
        logger.error(f"  [JSON ERROR] {label}: {e}. Snippet: {resp.text[:200]!r}")
        return None


# ===========================================================================
# ENDPOINT ①  /v2/prices/month-matrix  →  one cheapest price per date
# ===========================================================================
def fetch_month_matrix(
    origin: str,
    destination: str,
    month: str,
    api_key: str,
    airline_lookup: dict[str, str],
) -> list[dict]:
    """
    Returns one record per departure date for the given route/month — the
    single cheapest ticket cached by Travelpayouts for that day.
    """
    label = f"month-matrix {origin}→{destination} {month}"
    logger.info(f"→ [{label}]")

    payload = _get_json(
        MONTH_MATRIX_URL,
        headers={"x-access-token": api_key, "Accept-Encoding": "gzip, deflate"},
        params={
            "origin": origin, "destination": destination,
            "month": month, "currency": CURRENCY,
            "show_to_affiliates": "true",
        },
        label=label,
    )
    if payload is None:
        return []
    if not payload.get("success", True):
        logger.warning(f"  [API WARNING] success=False for {label}")
        return []

    entries = payload.get("data") or []
    if not entries:
        logger.warning(f"  [NO DATA] {label}")
        return []

    snap = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    records = []

    for e in entries:
        depart_date       = e.get("depart_date", "")
        price             = e.get("value")
        if not depart_date or price is None:
            continue

        airline_iata      = (e.get("airline") or "").strip().upper()
        number_of_changes = e.get("number_of_changes", -1)
        trip_class        = e.get("trip_class", 0)

        try:
            days_till_dep = (datetime.date.fromisoformat(depart_date) - TODAY).days
        except ValueError:
            days_till_dep = None

        records.append({
            "snapshot_ts":       snap,
            "origin":            origin,
            "destination":       destination,
            "depart_date":       depart_date,
            "days_till_dep":     days_till_dep,
            "price_usd":         price,
            "airline_iata":      airline_iata or None,
            "airline_name":      airline_lookup.get(airline_iata, airline_iata or "Unknown"),
            "is_direct":         (number_of_changes == 0) if number_of_changes != -1 else None,
            "number_of_changes": number_of_changes if number_of_changes != -1 else None,
            "trip_class":        trip_class,
            "trip_class_label":  TRIP_CLASS_LABELS.get(int(trip_class), str(trip_class)),
            "distance_km":       e.get("distance"),
            "found_at":          e.get("found_at", ""),
            "actual":            e.get("actual"),
        })

    logger.info(f"  ✓ {len(records)} best-price entries.")
    return records


# ===========================================================================
# ENDPOINT ②  /v2/prices/latest  →  many tickets per date (full options)
# ===========================================================================
def fetch_latest(
    origin: str,
    destination: str,
    period_start: str,
    api_key: str,
    airline_lookup: dict[str, str],
) -> list[dict]:
    """
    Pulls up to 1 000 recently-cached individual tickets for the given route,
    then filters to rows where depart_date is within the TARGET_MONTH.

    Each row is one distinct ticket option — so a single date can appear many
    times with different airlines, stop counts, durations, and booking sites.

    Parameters
    ----------
    period_start : First day of the target month, e.g. "2026-07-01".
                   Used as beginning_of_period with period_type=month.
    """
    label = f"latest {origin}→{destination} {period_start[:7]}"
    logger.info(f"→ [{label}]")

    payload = _get_json(
        LATEST_URL,
        headers={"x-access-token": api_key, "Accept-Encoding": "gzip, deflate"},
        params={
            "origin":              origin,
            "destination":         destination,
            "currency":            CURRENCY,
            "period_type":         "month",
            "beginning_of_period": period_start,
            "one_way":             "true",
            "sorting":             "price",      # cheapest first
            "limit":               1000,          # maximum allowed
        },
        label=label,
    )
    if payload is None:
        return []
    if not payload.get("success", True):
        logger.warning(f"  [API WARNING] success=False for {label}")
        return []

    entries = payload.get("data") or []
    if not entries:
        logger.warning(f"  [NO DATA] {label}")
        return []

    # Filter to the target month only (some entries may spill into adjacent months)
    month_prefix = period_start[:7]          # "2026-07"
    entries = [e for e in entries if str(e.get("depart_date", "")).startswith(month_prefix)]

    if not entries:
        logger.warning(f"  [NO DATA after month filter] {label}")
        return []

    snap = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    records = []

    for e in entries:
        depart_date       = e.get("depart_date", "")
        price             = e.get("price")          # 'price' key in /latest, not 'value'
        if not depart_date or price is None:
            continue

        airline_iata      = (e.get("airline") or "").strip().upper()
        number_of_changes = e.get("number_of_changes", -1)
        duration_min      = e.get("duration")       # total flight duration in minutes
        gate              = e.get("gate", "")       # booking site / agency
        found_at          = e.get("found_at", "")
        expires_at        = e.get("expires_at", "")

        try:
            days_till_dep = (datetime.date.fromisoformat(depart_date) - TODAY).days
        except ValueError:
            days_till_dep = None

        # Convert raw duration minutes into a human-readable "Xh Ym" string
        if duration_min is not None:
            try:
                dm = int(duration_min)
                duration_str = f"{dm // 60}h {dm % 60}m"
            except (ValueError, TypeError):
                duration_str = str(duration_min)
        else:
            duration_str = None

        records.append({
            "snapshot_ts":       snap,
            "origin":            origin,
            "destination":       destination,
            "depart_date":       depart_date,
            "days_till_dep":     days_till_dep,
            "price_usd":         price,
            "airline_iata":      airline_iata or None,
            "airline_name":      airline_lookup.get(airline_iata, airline_iata or "Unknown"),
            "is_direct":         (number_of_changes == 0) if number_of_changes != -1 else None,
            "number_of_changes": number_of_changes if number_of_changes != -1 else None,
            "duration":          duration_str,
            "duration_minutes":  duration_min,
            "booking_gate":      gate,
            "found_at":          found_at,
            "expires_at":        expires_at,
        })

    logger.info(f"  ✓ {len(records)} individual ticket options (across all dates).")
    return records


# ---------------------------------------------------------------------------
# Save helper
# ---------------------------------------------------------------------------
def save_csv(records: list[dict], filepath: str, column_order: list[str]) -> None:
    """Builds a DataFrame, enforces column order, and writes to CSV."""
    if not records:
        logger.warning(f"  No records to save — skipping '{filepath}'.")
        return

    df = pd.DataFrame(records)
    df = df[[col for col in column_order if col in df.columns]]
    df.sort_values(
        by=["origin", "destination", "depart_date"],
        ascending=True,
        inplace=True,
    )
    df.reset_index(drop=True, inplace=True)

    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    logger.info(f"  ✅  {len(df)} rows → '{filepath}'")
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    logger.info("=" * 65)
    logger.info("Travelpayouts Dual-Endpoint Fetcher — July 2026")
    logger.info(f"  Routes : {' | '.join(f'{o}→{d}' for o, d in ROUTES)}")
    logger.info(f"  Month  : {TARGET_MONTH}  |  Currency: {CURRENCY}")
    logger.info(f"  Output : {CSV_BEST}  +  {CSV_ALL}")
    logger.info("=" * 65)

    api_key        = get_api_key()
    airline_lookup = load_airline_lookup(api_key)

    best_records: list[dict] = []
    all_records:  list[dict] = []
    best_ok = best_fail = all_ok = all_fail = 0

    # -----------------------------------------------------------------------
    # Pass 1 — month-matrix (best price per day)
    # -----------------------------------------------------------------------
    logger.info("\n── PASS 1: /v2/prices/month-matrix (best price per date) ──")
    for idx, (origin, dest) in enumerate(ROUTES):
        rows = fetch_month_matrix(origin, dest, TARGET_MONTH, api_key, airline_lookup)
        if rows:
            best_records.extend(rows)
            best_ok += 1
        else:
            best_fail += 1
        if idx < len(ROUTES) - 1:
            time.sleep(REQUEST_DELAY)

    # -----------------------------------------------------------------------
    # Pass 2 — /latest (all cached tickets, many per date)
    # -----------------------------------------------------------------------
    logger.info("\n── PASS 2: /v2/prices/latest (all options per date) ──")
    for idx, (origin, dest) in enumerate(ROUTES):
        rows = fetch_latest(origin, dest, PERIOD_START, api_key, airline_lookup)
        if rows:
            all_records.extend(rows)
            all_ok += 1
        else:
            all_fail += 1
        if idx < len(ROUTES) - 1:
            time.sleep(REQUEST_DELAY)

    # -----------------------------------------------------------------------
    # Write CSVs
    # -----------------------------------------------------------------------
    logger.info("\n── Saving output files ──")

    BEST_COLS = [
        "snapshot_ts", "origin", "destination", "depart_date", "days_till_dep",
        "price_usd", "airline_iata", "airline_name", "is_direct",
        "number_of_changes", "trip_class", "trip_class_label",
        "distance_km", "found_at", "actual",
    ]
    ALL_COLS = [
        "snapshot_ts", "origin", "destination", "depart_date", "days_till_dep",
        "price_usd", "airline_iata", "airline_name", "is_direct",
        "number_of_changes", "duration", "duration_minutes",
        "booking_gate", "found_at", "expires_at",
    ]

    save_csv(best_records, CSV_BEST, BEST_COLS)
    save_csv(all_records,  CSV_ALL,  ALL_COLS)

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    logger.info("\n" + "=" * 65)
    logger.info("Run Summary")
    logger.info("-" * 65)
    logger.info(f"  month-matrix  — routes OK/fail : {best_ok}/{best_fail}  "
                f"| total rows: {len(best_records)}")
    logger.info(f"  /latest       — routes OK/fail : {all_ok}/{all_fail}   "
                f"| total rows: {len(all_records)}")
    logger.info(f"  {CSV_BEST}")
    logger.info(f"  {CSV_ALL}")
    logger.info("=" * 65)

    # -----------------------------------------------------------------------
    # Preview — show a slice of the richer "all options" data
    # -----------------------------------------------------------------------
    if all_records:
        preview_cols = [
            "depart_date", "origin", "destination",
            "price_usd", "airline_name", "is_direct",
            "number_of_changes", "duration", "booking_gate",
        ]
        df_all = pd.DataFrame(all_records)
        df_all.sort_values(["origin", "destination", "depart_date", "price_usd"], inplace=True)
        preview = df_all[[c for c in preview_cols if c in df_all.columns]].head(12)
        logger.info("\nAll-options preview (first 12 rows, sorted by route→date→price):")
        print(preview.to_string(index=False))


if __name__ == "__main__":
    main()
