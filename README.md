# Dynamic Pricing & Flight Analytics Engine

An end-to-end Machine Learning pipeline and Dynamic Pricing Engine built to optimize revenue management and predict market pricing behavior in the aviation sector.

## Overview

This repository contains tools for:
1.  **Data Extraction:** Automated web scrapers targeting Google Flights and Kayak.
2.  **Predictive Modeling:** An XGBoost Regressor that accurately forecasts the prices of competitors based on historical price curves, seasonality, and advance-purchase windows.
3.  **Prescriptive Pricing Engine:** An algorithmic engine that simulates real-world airline dynamic pricing—setting its own ticket prices by blending competitor baselines with internal load factor (unsold seats) pacing.

## Core Architecture

*   `kayak_scraper.py` / `flight_tracker_searchapi.py`
    *   Web scrapers integrating headless browser rendering via ScrapingBee to extract live flight inventory and JSON payload metadata from aggregators.
*   `train_xgboost.py`
    *   ETL and ML script. Normalizes JSON/CSV payloads, engineers time-series lag features, and trains the `XGBRegressor` on historical pricing behavior.
*   `pricing_engine.py`
    *   The Dynamic Pricer. Queries the XGBoost model for market baselines and applies yield-optimization rules (surges and discounts based on target booking curves).
*   `simulate_sales.py`
    *   Monte Carlo-style simulator used to stress-test the pricing engine under various demand and elasticity constraints over a 90-day booking window.
*   `plot_price_history_v2.py`
    *   Analytics and visualization suite using Matplotlib to map out booking window sweet spots and price volatility curves.

## Setup

```bash
pip install pandas numpy scikit-learn xgboost beautifulsoup4 requests matplotlib
```

To run the scrapers, you must export an active API key:
```bash
export SCRAPINGBEE_API_KEY="your_api_key"
```

## Note on Repository Maintenance
This repository has been cleaned of all temporary, deprecated, and intermediate data files. All raw CSV and JSON datasets, as well as the compiled `.pkl` model artifacts, are excluded via `.gitignore` to preserve repository hygiene.
