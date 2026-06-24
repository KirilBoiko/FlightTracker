#!/usr/bin/env python3
"""
fr24_airport_scraper.py
=======================
Scrapes the Flightradar24 airport departure and arrival boards for TBS (Tbilisi),
and filters exclusively for flights to and from TLV (Tel Aviv).

Uses ScrapingBee with residential proxies to bypass Cloudflare and directly
query the hidden Flightradar24 JSON API.

Usage:
    export SCRAPINGBEE_API_KEY="your_scrapingbee_key"
    python3 fr24_airport_scraper.py
"""

import os
import sys
import json
import time
import datetime
import logging
import requests
import pandas as pd
from urllib.parse import urlencode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"
TARGET_AIRPORT = "tbs"
FILTER_IATA = "TLV"
OUTPUT_CSV = "fr24_tbs_tlv_filtered_flights.csv"

def get_scrapingbee_key():
    api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if not api_key:
        logger.error("SCRAPINGBEE_API_KEY environment variable is missing.")
        sys.exit(1)
    return api_key

def fetch_fr24_api(airport_code: str, mode: str, timestamp: int, sb_key: str) -> dict:
    """
    Fetches the Flightradar24 JSON API using ScrapingBee.
    mode: 'departures' or 'arrivals'
    """
    base_url = "https://api.flightradar24.com/common/v1/airport.json"
    
    # We construct the query string manually to avoid requests messing up the array brackets
    query = (
        f"code={airport_code}"
        f"&plugin[]=schedule"
        f"&plugin-setting[schedule][mode]={mode}"
        f"&plugin-setting[schedule][timestamp]={timestamp}"
        f"&page=1"
        f"&limit=100"
    )
    
    target_url = f"{base_url}?{query}"
    
    params = {
        "api_key": sb_key,
        "url": target_url,
        "premium_proxy": "true",
        # We don't need to render JS because we are hitting a pure JSON endpoint
        "render_js": "false",
    }
    
    logger.info(f"Fetching {mode.upper()} for timestamp {timestamp} via ScrapingBee...")
    
    try:
        response = requests.get(SCRAPINGBEE_ENDPOINT, params=params, timeout=60)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch {mode} from FR24: {e}")
        if 'response' in locals() and hasattr(response, 'text'):
            logger.debug(f"Response text: {response.text[:200]}")
        return {}

def extract_and_filter_flights(data: dict, mode: str, filter_iata: str) -> list:
    """
    Parses the massive FR24 JSON response and extracts only flights that match the filter_iata.
    """
    flights_list = []
    
    try:
        schedule = data.get("result", {}).get("response", {}).get("airport", {}).get("pluginData", {}).get("schedule", {})
        if not schedule:
            logger.warning("Could not find schedule data in response.")
            return flights_list
            
        flights = schedule.get(mode, {}).get("data", [])
        
        for item in flights:
            flight = item.get("flight", {})
            if not flight:
                continue
                
            flight_id = flight.get("identification", {}).get("number", {}).get("default", "N/A")
            airline = flight.get("airline", {}).get("name", "N/A")
            status = flight.get("status", {}).get("text", "Unknown")
            
            time_details = flight.get("time", {}).get("scheduled", {})
            sched_dep = time_details.get("departure")
            sched_arr = time_details.get("arrival")
            
            aircraft = flight.get("aircraft", {}).get("model", {}).get("text", "Unknown")
            
            # Check Origin and Destination
            origin_iata = flight.get("airport", {}).get("origin", {}).get("code", {}).get("iata", "N/A")
            dest_iata = flight.get("airport", {}).get("destination", {}).get("code", {}).get("iata", "N/A")
            
            # Filter condition
            is_match = False
            if mode == "departures" and dest_iata == filter_iata:
                is_match = True
            elif mode == "arrivals" and origin_iata == filter_iata:
                is_match = True
                
            if is_match:
                # Convert UNIX timestamps to readable strings
                sched_dep_str = datetime.datetime.fromtimestamp(sched_dep).strftime('%Y-%m-%d %H:%M') if sched_dep else "N/A"
                sched_arr_str = datetime.datetime.fromtimestamp(sched_arr).strftime('%Y-%m-%d %H:%M') if sched_arr else "N/A"
                
                flights_list.append({
                    "flight_number": flight_id,
                    "airline": airline,
                    "direction": mode[:-1].capitalize(), # Departure / Arrival
                    "origin": origin_iata,
                    "destination": dest_iata,
                    "scheduled_departure": sched_dep_str,
                    "scheduled_arrival": sched_arr_str,
                    "status": status,
                    "aircraft": aircraft
                })
                
    except Exception as e:
        logger.error(f"Error parsing JSON data: {e}")
        
    return flights_list

def main():
    sb_key = get_scrapingbee_key()
    
    # Define the dates we want to scrape.
    # Flightradar24 free accounts usually only allow looking back ~7 days.
    # We will scrape: Yesterday, Today, and Tomorrow.
    today = datetime.date.today()
    target_dates = [
        today - datetime.timedelta(days=1), # Yesterday
        today,                              # Today
        today + datetime.timedelta(days=1), # Tomorrow
    ]
    
    all_filtered_flights = []
    
    for target_date in target_dates:
        # Flightradar expects a UNIX timestamp. 
        # We use Noon (12:00) UTC on the target date to ensure we hit the middle of the day schedule
        dt = datetime.datetime(target_date.year, target_date.month, target_date.day, 12, 0)
        timestamp = int(dt.timestamp())
        
        logger.info(f"=== Scraping for Date: {target_date} (Timestamp: {timestamp}) ===")
        
        # 1. Departures
        dep_data = fetch_fr24_api(TARGET_AIRPORT, "departures", timestamp, sb_key)
        dep_flights = extract_and_filter_flights(dep_data, "departures", FILTER_IATA)
        all_filtered_flights.extend(dep_flights)
        logger.info(f"Found {len(dep_flights)} departures to {FILTER_IATA}.")
        
        time.sleep(2) # Be gentle with ScrapingBee concurrency limits
        
        # 2. Arrivals
        arr_data = fetch_fr24_api(TARGET_AIRPORT, "arrivals", timestamp, sb_key)
        arr_flights = extract_and_filter_flights(arr_data, "arrivals", FILTER_IATA)
        all_filtered_flights.extend(arr_flights)
        logger.info(f"Found {len(arr_flights)} arrivals from {FILTER_IATA}.")
        
        time.sleep(2)
        
    if all_filtered_flights:
        df = pd.DataFrame(all_filtered_flights)
        # Drop exact duplicates if time overlapping returns same flights
        df = df.drop_duplicates(subset=["flight_number", "scheduled_departure"])
        
        df.to_csv(OUTPUT_CSV, index=False)
        logger.info(f"Success! Saved {len(df)} filtered flights to {OUTPUT_CSV}")
    else:
        logger.warning(f"No flights found matching {FILTER_IATA} for the specified dates.")

if __name__ == "__main__":
    main()
