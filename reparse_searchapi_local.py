import os
import json
import datetime
import pandas as pd
from pathlib import Path

# Important: these MUST match the logic from the fixed flight_tracker_searchapi.py
def parse_price(price_int, fallback="N/A"):
    if price_int is not None:
        return price_int
    return fallback

def normalize_aircraft(name: str) -> str:
    if not name: return name
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

RAW_DIR = Path("api_responses/raw")
OUTPUT_CSV = "FlightData_SearchAPI_reparsed.csv"
print(f"Scanning {RAW_DIR} for downloaded SearchAPI JSON files...")

all_flight_records = []

for p in RAW_DIR.glob("*.json"):
    try:
        results = json.loads(p.read_text(encoding="utf-8"))
    except:
        continue
    
    if "error" in results:
        continue

    # Try to extract destination and date from filename, else from JSON
    # Format: 20260611_143027_TLV_TBS_dep2026-07-01.json
    parts = p.name.split("_")
    if len(parts) >= 5 and parts[4].startswith("dep"):
        destination = parts[3]
        departure_date = parts[4].replace("dep", "").replace(".json", "")
    else:
        continue

    best_flights = results.get("best_flights", [])
    other_flights = results.get("other_flights", [])
    all_flights_raw = best_flights + other_flights

    if not all_flights_raw:
        continue

    try:
        dep_date_obj = datetime.date.fromisoformat(departure_date)
        days_till_departure = (dep_date_obj - datetime.date.today()).days
    except ValueError:
        days_till_departure = None

    snapshot_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

        # FIXED DEDUPLICATION LOGIC
        match_key = f"{airline_name}_{flight_no}_{dep_time_norm}"
        
        if match_key in flights_by_key:
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

        flights_by_key[match_key] = {
            "snapshot_date": snapshot_date,
            "departure_date": departure_date,
            "days_till_departure": days_till_departure,
            "destination": destination,
            "airline": airline_name,
            "flight_number": flight_no,
            "departure_time": dep_time_norm if dep_time_norm != "UnknownTime" else None,
            "aircraft": airplane_name,
            "price_usd": parse_price(fare.get("price"), fallback="N/A"),
            "duration_minutes": int(duration) if duration is not None else None,
            "is_direct": True,
        }

    all_flight_records.extend(list(flights_by_key.values()))

if all_flight_records:
    df = pd.DataFrame(all_flight_records)
    df.sort_values(by=["departure_date", "destination"], inplace=True)
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n✅ DONE! Extracted {len(df)} SearchAPI flights from {len(list(RAW_DIR.glob('*.json')))} saved responses.")
    print(f"Saved to: {OUTPUT_CSV}")
else:
    print("No flights could be parsed from the JSON files.")
