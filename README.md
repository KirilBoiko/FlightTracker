# Flight Pricing Data Scrapers

This repository contains automated tools designed to extract and visualize real-time and historical flight pricing data from major aggregators (Kayak and Google Flights). 

By acting as a distributed network of hidden browsers, these tools allow us to continuously monitor competitors' pricing changes and build a proprietary dataset of market prices without being blocked by anti-bot protections.

---

## The Scrapers

**Why do we need two different scrapers?**
The primary reason is a trade-off between volume and historical context. The Google Flights API does not show all available flights for these routes, meaning it misses a large portion of the market inventory. However, for the flights it *does* show, it provides critical historical price curves. Conversely, Kayak shows almost all available flights on the market today, but lacks the historical pricing context. By combining them, we get both total market coverage and historical baselines.

The system relies on two distinct scraping scripts and an analytics visualizer, each serving a specific purpose:

### 1. The Kayak Scraper (`kayak_scraper.py`)
**Purpose:** Scrape live, real-time ticket prices and flight inventory directly from Kayak's search results.

**How it Works:** 
Kayak is highly aggressive at blocking automated bots. To get around this, we use a service called **ScrapingBee**. ScrapingBee routes our requests through residential proxy networks and renders the page in a real, hidden Chrome browser instance.
The script performs the following:
1.  It iterates through our defined routes (e.g., TBS to TLV, TLV to TBS).
2.  It opens 4 concurrent browser sessions simultaneously using a `ThreadPoolExecutor`. This parallelization reduces a 6-hour data collection job down to just ~1.5 hours.
3.  It bypasses cookie banners, waits for the flight results to fully load via JavaScript, and intercepts the hidden JSON payloads that contain the pricing data.
4.  It normalizes and saves the extracted data into a structured CSV format.

### 2. The Google Flights Scraper (`flight_tracker_searchapi.py`)
**Purpose:** Extract historical pricing curves and "Price Insights" from Google Flights.

**How it Works:** 
While Kayak tells us the price *today*, Google Flights has a unique feature that shows if a price is "Typical", "Low", or "High" based on historical averages. 
1.  This script utilizes the **SearchAPI** service, which provides a dedicated endpoint to cleanly interact with Google Flights.
2.  It extracts the Google Flights "Price Insights" module, giving us crucial historical baselines and identifying exactly where current prices sit relative to the market average over the past 90 days.

### 3. The Price Analytics Visualizer (`plot_price_history_v2.py`)
**Purpose:** Transform raw CSV pricing data into understandable analytical charts and visualizations.

**How it Works:** 
Raw scraped data is hard to interpret. This script reads the generated CSV datasets and builds high-resolution visual reports:
1.  It maps out the booking curve, showing exactly how prices rise or drop as the departure date approaches.
2.  It generates heatmaps identifying the cheapest windows to book and highlights periods of extreme price volatility.
3.  Charts are output directly as PNG files into designated analytics folders.
---

## Setup & Installation

### 1. Requirements
Ensure you have Python 3 installed. Install the required dependencies:
```bash
pip install pandas requests beautifulsoup4 matplotlib
```

### 2. Environment Variables (API Keys)
Because these scripts rely on third-party proxy and rendering services to avoid getting banned, you must export your API keys before running them:

```bash
# Required for kayak_scraper.py
export SCRAPINGBEE_API_KEY="your_scrapingbee_api_key_here"

# Required for flight_tracker_searchapi.py
export SEARCHAPI_API_KEY="your_searchapi_api_key_here"
```
*(Contact the administrator if you do not have access to these keys).*

---

## How to Use the Scrapers

### Running the Kayak Scraper
To collect the daily real-time pricing data across all routes, simply run:
```bash
python3 kayak_scraper.py
```
*   **What to expect:** The script will output its progress to the terminal. It creates temporary CSV files for each route as it runs (e.g., `_temp_TBS_TLV.csv`). Once all 4 concurrent threads finish, it merges them into a final output file (e.g., `combined_q3_2026_flights.csv`) and automatically deletes the temporary files.

### Running the Google Flights Scraper
To collect historical price insights, run:
```bash
python3 flight_tracker_searchapi.py
```
*   **What to expect:** This script runs much faster as it relies on an established API rather than rendering full browser sessions. It will output an enriched dataset (e.g., `q3_2026_pricing_data_searchapi_enriched.csv`) containing the historical insight metrics.

### Generating Analytical Charts
To convert the scraped CSV data into visual charts, run:
```bash
python3 plot_price_history_v2.py
```
*   **What to expect:** The script will process the latest CSV data and generate a series of `.png` charts inside the `price_history_charts_v2/` directory, illustrating price bands, booking window heatmaps, and route comparisons.

---

## Configuration: Changing Routes and Dates

If you want to track different destinations or change the time period being scraped, you will need to edit the Python files directly.

### Changing Destinations
In both `kayak_scraper.py` and `flight_tracker_searchapi.py`, look for the `ROUTES` list near the top of the file:
```python
ROUTES = [
    ("TBS", "TLV"),
    ("TLV", "TBS"),
    # Add new routes using their IATA codes here:
    ("JFK", "LHR"), 
]
```

### Changing the Scraping Dates
Currently, the scrapers are configured to scrape flights for a 92-day period starting from July 1, 2026. 

**To change this in `kayak_scraper.py`:**
Look for these constants near line 88:
```python
Q3_2026_START = datetime.date(2026, 7, 1)     # Set your start date here
Q3_2026_DAYS  = 92                            # Number of days to scrape forward
```

**To change this in `flight_tracker_searchapi.py`:**
Look inside the `main()` function around line 324:
```python
    q3_start = datetime.date(2026, 7, 1)      # Set your start date here
    target_dates = [
        (q3_start + datetime.timedelta(days=i)).isoformat()
        for i in range(92)                    # Number of days to scrape forward
    ]
```

---

## Important Notes on Data Storage
These scripts generate large amounts of CSV data. To keep the code repository clean and fast, **all `.csv` files are explicitly excluded via `.gitignore`**. 

The generated data files will remain on your local machine, but they will not be uploaded or synced to GitHub. If you need to share the raw datasets, do so via secure cloud storage or your internal data warehouse.
