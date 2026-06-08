#!/usr/bin/env python3
"""
Flight Price Tracker from Tbilisi (TBS) to Target Destinations (SearchApi.io version)
===================================================================================
This script tracks flight prices from Tbilisi (TBS) to a list of target
destinations using the SearchApi.io Google Flights API. It collects the top 5
cheapest flights and appends them to a historical CSV dataset.

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
ORIGIN = "TBS"
DESTINATIONS = ['IST']
SEARCHAPI_URL = "https://www.searchapi.io/api/v1/search"
CSV_FILE_NAME = "tbs_pricing_data.csv"
SLEEP_DELAY_SECONDS = 2.0
COMPARE_BAGGAGE_PRICES = True  # Compare base fare vs fare with checked bag to check if bag is included


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


def fetch_flights(origin: str, destination: str, departure_date: str, api_key: str, checked_bags: int = 0) -> list:
    """
    Queries the SearchApi.io Google Flights API for a specific route and departure date.
    Extracts, cleans, and returns the top 5 cheapest flights.
    """
    # Define query parameters for SearchApi Google Flights
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": departure_date,
        "flight_type": "one_way",  # Specifies 'one-way' flight search for SearchApi
        "stops": "nonstop",        # Exclusively return direct flights for SearchApi
        "currency": "USD",         # Ensures prices are returned in USD
        "hl": "en",                # English language localization
        "gl": "us",                # US geolocation context
        "api_key": api_key
    }
    if checked_bags > 0:
        params["checked_bags"] = checked_bags

    logger.info(f"Fetching {origin} to {destination} for {departure_date}...")

    try:
        response = requests.get(SEARCHAPI_URL, params=params, timeout=30)
        # Check for HTTP errors (e.g., 401 Unauthorized, 403 Forbidden, 500 Server Error)
        response.raise_for_status()
        results = response.json()
        
        # Debug: Save last response to a file so we can inspect the exact baggage fields
        try:
            import json
            filename = f"searchapi_response_{destination}_bags_{checked_bags}.json"
            with open(filename, "w") as f:
                json.dump(results, f, indent=2)
        except Exception:
            pass
    except requests.exceptions.Timeout:
        logger.error(f"Timeout occurred while fetching flight data for {destination} on {departure_date}.")
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP Request error for {destination} on {departure_date}: {e}")
        return []

    # Check for SearchApi error in the returned JSON
    if "error" in results:
        logger.error(f"SearchApi returned an error: {results.get('error')}")
        return []

    snapshot_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    flights_records = []

    # SearchApi Google Flights splits results into 'best_flights' and 'other_flights'
    best_flights = results.get("best_flights", [])
    other_flights = results.get("other_flights", [])
    all_flights_raw = best_flights + other_flights

    if not all_flights_raw:
        logger.warning(f"No flights returned in response for {destination} on {departure_date}.")
        return []

    for flight in all_flights_raw:
        segments = flight.get("flights", [])
        if not segments:
            continue

        # Extract carrier names of all legs, remove duplicates preserving order
        airlines = [seg.get("airline") for seg in segments if seg.get("airline")]
        unique_airlines = list(dict.fromkeys(airlines))
        airline_name = ", ".join(unique_airlines) if unique_airlines else "Unknown Carrier"

        # Extract unique aircraft models preserving order
        airplanes = [seg.get("airplane") for seg in segments if seg.get("airplane")]
        unique_airplanes = list(dict.fromkeys(airplanes))
        airplane_name = ", ".join(unique_airplanes) if unique_airplanes else "Unknown Aircraft"

        # Extract unique flight numbers preserving order
        flight_numbers = [seg.get("flight_number") for seg in segments if seg.get("flight_number")]
        unique_flight_numbers = list(dict.fromkeys(flight_numbers))
        flight_no = ", ".join(unique_flight_numbers) if unique_flight_numbers else "Unknown Flight"

        # Extract scheduled departure time of the first leg
        dep_time = segments[0].get("departure_airport", {}).get("time")

        raw_price = flight.get("price")
        price_usd = parse_price(raw_price, fallback="N/A")

        duration = flight.get("total_duration")
        if duration is None:
            # Fallback: sum duration of all segments
            duration = sum(seg.get("duration", 0) for seg in segments)

        # Check if checked bag is included
        extensions = flight.get("extensions", [])
        checked_bag_included = None
        for ext in extensions:
            ext_lower = ext.lower()
            if "checked bag" in ext_lower or "checked baggage" in ext_lower:
                if "free" in ext_lower or "included" in ext_lower:
                    checked_bag_included = True
                    break
                elif "fee" in ext_lower or "not included" in ext_lower or "no " in ext_lower:
                    checked_bag_included = False
                    break

        flights_records.append({
            "snapshot_date": snapshot_date,
            "departure_date": departure_date,
            "destination": destination,
            "airline": airline_name,
            "flight_number": flight_no,
            "departure_time": dep_time,
            "aircraft": airplane_name,
            "price_usd": price_usd,
            "price_with_bag_usd": None,  # Will be populated in main if compared
            "duration_minutes": int(duration) if duration is not None else None,
            "is_direct": True,
            "checked_bag_included": checked_bag_included
        })

    # Sort flights by price ascending, placing 'N/A' or 0 fallbacks at the end
    def get_sort_key(x):
        val = x["price_usd"]
        if val == "N/A" or val == 0:
            return float('inf')
        return float(val)

    flights_records.sort(key=get_sort_key)

    logger.info(f"Successfully processed {len(flights_records)} flight options.")
    return flights_records


def process_baggage_comparisons(records: list, records_with_bag: list) -> list:
    """
    Compares flights without bags and flights with bags.
    Determines if checked baggage is included, and sets the price with bag.
    """
    parsed_flights = {}
    
    # 1 & 2. Loop through the results of the first API call (checked_bags = 0)
    for flight in records:
        flight_number = flight.get("flight_number")
        departure_time = flight.get("departure_time")
        match_key = f"{flight_number}_{departure_time}"
        
        flight["base_price"] = flight.get("price_usd")
        parsed_flights[match_key] = flight

    final_records = []
    
    # 3. Loop through the results of the second API call (checked_bags = 1)
    for flight in records_with_bag:
        flight_number = flight.get("flight_number")
        departure_time = flight.get("departure_time")
        match_key = f"{flight_number}_{departure_time}"
        
        if match_key in parsed_flights:
            total_with_bag_price = flight.get("price_usd")
            matched_flight = parsed_flights[match_key]
            base_price = matched_flight["base_price"]
            
            # Calculate the fee
            if isinstance(total_with_bag_price, int) and isinstance(base_price, int):
                baggage_fee = total_with_bag_price - base_price
            else:
                baggage_fee = "N/A"
            
            # Append combined metrics
            matched_flight["price_with_bag_usd"] = total_with_bag_price
            matched_flight["baggage_fee"] = baggage_fee
            
            if baggage_fee == 0:
                matched_flight["checked_bag_included"] = True
            elif baggage_fee != "N/A" and baggage_fee > 0:
                matched_flight["checked_bag_included"] = False
            elif baggage_fee == "N/A":
                matched_flight["checked_bag_included"] = None
                
            final_records.append(matched_flight)
            
    return final_records


def append_to_csv(records: list, file_name: str) -> None:
    """
    Appends flight records to the historical CSV dataset.
    Creates the file with a header if it does not exist; otherwise, appends without header.
    """
    if not records:
        return

    df = pd.DataFrame(records)
    
    # Ensure correct columns order
    columns_order = [
        "snapshot_date",
        "departure_date",
        "destination",
        "airline",
        "flight_number",
        "departure_time",
        "aircraft",
        "price_usd",
        "price_with_bag_usd",
        "duration_minutes",
        "is_direct",
        "checked_bag_included"
    ]
    df = df[columns_order]

    try:
        # Append to CSV. If file exists, do not write the header.
        df.to_csv(file_name, mode='a', index=False, header=not os.path.exists(file_name))
        logger.info(f"Successfully saved {len(records)} records to '{file_name}'.")
    except Exception as e:
        logger.error(f"Failed to write records to {file_name}: {e}")


def main():
    logger.info("Initializing TBS Route Intelligence Tracker (SearchApi version)...")
    
    # 1. Secure API Key validation
    api_key = get_api_key()

    # Target dates: last full week of June 2026 and last full week of July 2026 (August dates removed for now)
    today = datetime.date.today()
    target_dates = [
        '2026-07-19'
    ]
    
    logger.info(f"System date: {today}")
    logger.info(f"Calculated departure dates: {target_dates}")
    logger.info(f"Target destinations: {DESTINATIONS}")

    all_scraped_records = []
    success_count = 0
    failure_count = 0

    # 3. Main execution loop
    for dest in DESTINATIONS:
        for dep_date in target_dates:
            try:
                # Fetch base flights (0 checked bags)
                records = fetch_flights(ORIGIN, dest, dep_date, api_key, checked_bags=0)
                if records:
                    if COMPARE_BAGGAGE_PRICES:
                        logger.info("Comparing prices with 1 checked bag to detect if bag is included...")
                        time.sleep(SLEEP_DELAY_SECONDS)
                        # Fetch flights with 1 checked bag
                        records_with_bag = fetch_flights(ORIGIN, dest, dep_date, api_key, checked_bags=1)
                        records = process_baggage_comparisons(records, records_with_bag)
                                
                    # Keep only the top 5 cheapest flights (records is already sorted)
                    all_scraped_records.extend(records[:5])
                    success_count += 1
                else:
                    failure_count += 1
            except Exception as e:
                logger.error(f"Unexpected error tracking {ORIGIN} to {dest} on {dep_date}: {e}")
                failure_count += 1

            # Polite sleep to respect API rate limits and avoid throttling
            time.sleep(SLEEP_DELAY_SECONDS)

    # 4. Save parsed results to CSV in a single high-integrity operation
    if all_scraped_records:
        append_to_csv(all_scraped_records, CSV_FILE_NAME)
    else:
        logger.warning("No records were successfully collected during this run.")

    # 5. Output concise execution summary
    logger.info("=========================================")
    logger.info("Flight tracking run complete.")
    logger.info(f"Successful queries: {success_count}")
    logger.info(f"Failed/Empty queries: {failure_count}")
    logger.info(f"Total flight records saved: {len(all_scraped_records)}")
    logger.info("=========================================")


if __name__ == "__main__":
    main()
