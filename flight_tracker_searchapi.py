#!/usr/bin/env python3
"""
Flight Price Tracker — TBS/TLV/BUS Multi-Route (SearchApi.io version)
=====================================================================
This script tracks flight prices across four routes:
  TBS → TLV, TBS → BUS, TLV → TBS, BUS → TBS
for every departure date in July, August, and September 2026. It collects the top 5 cheapest
flights per route/date and appends them to a historical CSV dataset.

Installation:
    pip install pandas requests

Environment Variable Setup:
    Before running the script, set your SearchApi API key:
    
    On macOS/Linux:
        export SEARCHAPI_KEY="your_actual_searchapi_key"
        
    On Windows (Command Prompt):
        set SEARCHAPI_KEY="your_actual_searchapi_key"
        
    On Windows (PowerShell):
        $env:SEARCHAPI_KEY="your_actual_searchapi_key"

Execution:
    python flight_tracker_searchapi.py
"""

import os
import sys
import json
import time
import datetime
import logging
import requests
import pandas as pd

# Configure Logging for production readiness
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Constants
ROUTES = [
    ("TBS", "TLV"),
    ("BUS", "TLV"),
    ("TLV", "TBS"),
    ("TLV", "BUS"),
]
SEARCHAPI_URL = "https://www.searchapi.io/api/v1/search"
CSV_FILE_NAME = "q3_2026_pricing_data_searchapi.csv"
SLEEP_DELAY_SECONDS = 2.0


def get_api_key() -> str:
    """
    Securely retrieves the SearchApi key from environment variables.
    Exits the script if the key is not defined.
    """
    api_key = os.environ.get("SEARCHAPI_KEY")
    if not api_key:
        logger.error(
            "Environment variable 'SEARCHAPI_KEY' is missing.\n"
            "Please set it in your environment. Example:\n"
            "  export SEARCHAPI_KEY=\"your_api_key_here\""
        )
        sys.exit(1)
    return api_key


def parse_price(price_val, fallback="N/A"):
    """
    Safely parses the price value to an integer.
    Handles numeric types as well as formatted currency strings like '$1,234' or '$500'.
    If the price is missing or invalid, returns the fallback value.
    """
    if price_val is None:
        return fallback
    if isinstance(price_val, (int, float)):
        return int(price_val)

    price_str = str(price_val).strip()
    if not price_str:
        return fallback

    # Strip any currency symbols, commas, or spaces
    cleaned = "".join([c for c in price_str if c.isdigit()])
    try:
        return int(cleaned)
    except ValueError:
        logger.warning(f"Could not parse price value: '{price_val}'. Using fallback '{fallback}'.")
        return fallback


def normalize_aircraft(name: str) -> str:
    """
    Converts verbose aircraft names to short codes.
    Examples: 'Boeing 737' → 'B737', 'Airbus A321neo' → 'A321n', 'Airbus A320' → 'A320'.
    """
    if not name:
        return name
    s = name.strip()
    if s.lower().startswith("boeing "):
        short = s[7:].strip()
        short = short.replace(" MAX", "M").replace(" Max", "M").replace(" ", "")
        return "B" + short
    if s.lower().startswith("airbus "):
        short = s[7:].strip()
        short = short.replace("neo", "n").replace("Neo", "n").replace("ceo", "").replace("CEO", "")
        return short.replace(" ", "")
    return s.replace(" ", "")


def fetch_flights(origin: str, destination: str, departure_date: str, api_key: str) -> list:
    """
    Queries the SearchApi.io Google Flights API for a specific route and departure date.
    Returns the base price for each flight, sorted cheapest first.
    """
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": departure_date,
        "flight_type": "one_way",  # one-way search
        "stops": "nonstop",        # direct flights only
        "currency": "USD",
        "hl": "en",
        "gl": "us",
        "api_key": api_key
    }

    logger.info(f"Fetching {origin} to {destination} for {departure_date}...")

    try:
        response = requests.get(SEARCHAPI_URL, params=params, timeout=30)
        response.raise_for_status()
        results = response.json()

        # Save raw API response to a timestamped file so every run is preserved
        try:
            raw_dir = os.path.join("api_responses", "raw")
            os.makedirs(raw_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            raw_file = os.path.join(raw_dir, f"{ts}_{origin}_{destination}_dep{departure_date}.json")
            with open(raw_file, "w") as f:
                json.dump(results, f, indent=2)
            logger.info(f"Raw API response saved → {raw_file}")
        except Exception as e:
            logger.warning(f"Could not save raw API response: {e}")

    except requests.exceptions.Timeout:
        logger.error(f"Timeout occurred while fetching flight data for {destination} on {departure_date}.")
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP Request error for {destination} on {departure_date}: {e}")
        return []

    if "error" in results:
        logger.error(f"SearchApi returned an error: {results.get('error')}")
        return []

    snapshot_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    best_flights = results.get("best_flights", [])
    other_flights = results.get("other_flights", [])
    all_flights_raw = best_flights + other_flights

    if not all_flights_raw:
        logger.warning(f"No flights returned in response for {destination} on {departure_date}.")
        return []

    # Days remaining until departure (useful for price trend analysis)
    try:
        dep_date_obj = datetime.date.fromisoformat(departure_date)
        days_till_departure = (dep_date_obj - datetime.date.today()).days
    except ValueError:
        days_till_departure = None

    # ── Price Insights from Google Flights ───────────────────────────────────
    # Google returns route-level price context that is very valuable as a
    # model feature even though it is not broken down by individual airline.
    pi = results.get("price_insights", {})
    pi_lowest_price      = pi.get("lowest_price")          # all-time low seen
    pi_price_level       = pi.get("price_level")            # 'low'/'typical'/'high'
    pi_typical_low       = (pi.get("typical_price_range") or {}).get("low_price")
    pi_typical_high      = (pi.get("typical_price_range") or {}).get("high_price")

    # Derive trend from price_history: compare last 7 days vs prior 7 days
    price_history = pi.get("price_history", [])
    pi_price_trend_7d = None
    if len(price_history) >= 14:
        recent_avg  = sum(h["price"] for h in price_history[-7:])  / 7
        earlier_avg = sum(h["price"] for h in price_history[-14:-7]) / 7
        pi_price_trend_7d = round(recent_avg - earlier_avg, 2)  # positive = rising

    # How far is today's lowest price from the historical typical low?
    pi_price_vs_typical_low = None
    if pi_lowest_price and pi_typical_low:
        pi_price_vs_typical_low = round(pi_lowest_price - pi_typical_low, 2)

    # Deduplicate by flight number + departure time, keeping cheapest price
    flights_by_key = {}

    for fare in all_flights_raw:
        segments = fare.get("flights", [])
        if not segments:
            continue

        flight_numbers = [seg.get("flight_number") for seg in segments if seg.get("flight_number")]
        unique_flight_numbers = list(dict.fromkeys(flight_numbers))
        flight_no = ", ".join(unique_flight_numbers) if unique_flight_numbers else "Unknown Flight"

        airlines = [seg.get("airline") for seg in segments if seg.get("airline")]
        unique_airlines = list(dict.fromkeys(airlines))
        airline_name = ", ".join(unique_airlines) if unique_airlines else "Unknown Carrier"

        dep_time = segments[0].get("departure_airport", {}).get("time")
        dep_time_norm = str(dep_time)[:5] if dep_time else "UnknownTime"

        # Match key now includes airline to prevent "Unknown Flight" from collapsing distinct flights
        match_key = f"{airline_name}_{flight_no}_{dep_time_norm}"
        
        if match_key in flights_by_key:
            # Keep the cheapest price seen for this exact flight
            existing_price = flights_by_key[match_key]["price_usd"]
            new_price = parse_price(fare.get("price"), fallback="N/A")
            if existing_price == "N/A" or (new_price != "N/A" and new_price < existing_price):
                flights_by_key[match_key]["price_usd"] = new_price
            continue

        airplanes = [normalize_aircraft(seg.get("airplane", "")) for seg in segments if seg.get("airplane")]
        unique_airplanes = list(dict.fromkeys(airplanes))
        airplane_name = ", ".join(unique_airplanes) if unique_airplanes else "Unknown Aircraft"

        duration = fare.get("total_duration")
        if duration is None:
            duration = sum(seg.get("duration", 0) for seg in segments)

        # Keep only HH:MM from the departure time
        dep_time_display = str(dep_time)[:5] if dep_time else None

        price_usd = parse_price(fare.get("price"), fallback="N/A")

        flights_by_key[match_key] = {
            "snapshot_date":            snapshot_date,
            "departure_date":           departure_date,
            "days_till_departure":      days_till_departure,
            "destination":              destination,
            "airline":                  airline_name,
            "flight_number":            flight_no,
            "departure_time":           dep_time_display,
            "aircraft":                 airplane_name,
            "price_usd":                price_usd,
            "duration_minutes":         int(duration) if duration is not None else None,
            "is_direct":                True,
            # ── Price Insights (route-level, from Google Flights) ──────────
            "pi_lowest_price":          pi_lowest_price,
            "pi_price_level":           pi_price_level,
            "pi_typical_low":           pi_typical_low,
            "pi_typical_high":          pi_typical_high,
            "pi_price_trend_7d":        pi_price_trend_7d,
            "pi_price_vs_typical_low":  pi_price_vs_typical_low,
        }

    flights_records = list(flights_by_key.values())

    # Sort cheapest first, N/A prices go to the end
    flights_records.sort(key=lambda x: float(x["price_usd"]) if x["price_usd"] != "N/A" else float("inf"))

    logger.info(f"Successfully processed {len(flights_records)} flight options.")
    return flights_records


def append_to_csv(records: list, file_name: str) -> None:
    """
    Appends flight records to the historical CSV dataset.
    Creates the file with a header if it does not exist; otherwise, appends without header.
    """
    if not records:
        return

    df = pd.DataFrame(records)

    columns_order = [
        "snapshot_date",
        "departure_date",
        "days_till_departure",
        "destination",
        "airline",
        "flight_number",
        "departure_time",
        "aircraft",
        "price_usd",
        "duration_minutes",
        "is_direct",
        # Price Insights columns
        "pi_lowest_price",
        "pi_price_level",
        "pi_typical_low",
        "pi_typical_high",
        "pi_price_trend_7d",
        "pi_price_vs_typical_low",
    ]
    df = df[columns_order]

    try:
        df.to_csv(file_name, mode='a', index=False, header=not os.path.exists(file_name))
        logger.info(f"Successfully saved {len(records)} records to '{file_name}'.")
    except Exception as e:
        logger.error(f"Failed to write records to {file_name}: {e}")


def main():
    logger.info("Initializing Multi-Route Flight Price Tracker (SearchApi version)...")

    api_key = get_api_key()

    # All departure dates in July, August, and September 2026
    q3_start = datetime.date(2026, 7, 1)
    target_dates = [
        (q3_start + datetime.timedelta(days=i)).isoformat()
        for i in range(92)
    ]

    logger.info(f"System date: {datetime.date.today()}")
    logger.info(f"Departure dates: {target_dates[0]} → {target_dates[-1]} ({len(target_dates)} days)")
    logger.info(f"Routes: {[f'{o}→{d}' for o, d in ROUTES]}")

    # Always start fresh — delete any existing CSV for this run
    if os.path.exists(CSV_FILE_NAME):
        os.remove(CSV_FILE_NAME)
        logger.info(f"Removed existing '{CSV_FILE_NAME}' — creating fresh file.")

    all_scraped_records = []
    success_count = 0
    failure_count = 0

    for origin, dest in ROUTES:
        logger.info(f"--- Route: {origin} → {dest} ---")
        for dep_date in target_dates:
            try:
                records = fetch_flights(origin, dest, dep_date, api_key)

                if records:
                    top5 = records[:5]
                    all_scraped_records.extend(top5)
                    success_count += 1

                    # Save processed records to a timestamped JSON file
                    try:
                        proc_dir = os.path.join("api_responses", "processed")
                        os.makedirs(proc_dir, exist_ok=True)
                        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        proc_file = os.path.join(proc_dir, f"{ts}_{origin}_{dest}_dep{dep_date}.json")
                        with open(proc_file, "w") as f:
                            json.dump(records, f, indent=2, default=str)
                        logger.info(f"Processed records saved  → {proc_file}")
                    except Exception as e:
                        logger.warning(f"Could not save processed records: {e}")
                else:
                    failure_count += 1

            except Exception as e:
                logger.error(f"Unexpected error tracking {origin} to {dest} on {dep_date}: {e}")
                failure_count += 1

            time.sleep(SLEEP_DELAY_SECONDS)

    if all_scraped_records:
        append_to_csv(all_scraped_records, CSV_FILE_NAME)
    else:
        logger.warning("No records were successfully collected during this run.")

    logger.info("=========================================")
    logger.info("Flight tracking run complete.")
    logger.info(f"Successful queries: {success_count}")
    logger.info(f"Failed/Empty queries: {failure_count}")
    logger.info(f"Total flight records saved: {len(all_scraped_records)}")
    logger.info("=========================================")


if __name__ == "__main__":
    main()
