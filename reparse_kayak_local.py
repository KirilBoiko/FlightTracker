import os
import re
import pandas as pd
from pathlib import Path
from kayak_scraper import parse_kayak_html

OUTPUT_CSV = "kayak_route_economics_reparsed.csv"
RAW_DIR = Path("api_responses/kayak_raw")

print(f"Scanning {RAW_DIR} for downloaded Kayak HTML files...")
all_records = []

for p in RAW_DIR.glob("kayak_*.html"):
    m = re.match(r"kayak_([A-Z]{3})_([A-Z]{3})_(\d{4}-\d{2}-\d{2})\.html", p.name)
    if m:
        origin, dest, date_str = m.groups()
        html = p.read_text(encoding="utf-8")
        # parse_kayak_html uses the fixed deduplication logic now!
        records = parse_kayak_html(html, origin, dest, date_str)
        all_records.extend(records)

df = pd.DataFrame(all_records)
column_order = [
    "snapshot_ts", "scrape_source",
    "origin", "destination", "depart_date",
    "airline", "dep_time", "arr_time", "duration", "stops",
    "price_usd",
]
df = df[[c for c in column_order if c in df.columns]]
df.sort_values(by=["origin", "destination", "depart_date", "price_usd"], inplace=True)
df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

print(f"\n✅ DONE! Extracted {len(df)} total Kayak flights from {len(list(RAW_DIR.glob('*.html')))} saved pages.")
print(f"Saved to: {OUTPUT_CSV}")
