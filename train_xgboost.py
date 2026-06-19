#!/usr/bin/env python3
"""
train_xgboost.py
================
Trains an XGBoost regression model to predict flight prices based on
historical scraping data from Kayak.

Dependencies:
    pip install pandas numpy scikit-learn xgboost
"""

import sys
import pandas as pd
import numpy as np
from datetime import datetime
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error
import pickle
import json

def parse_duration(dur_str):
    """Parses '2h 30m' into total minutes (integer)."""
    if pd.isna(dur_str):
        return 0
    dur_str = str(dur_str).lower()
    hours = 0
    minutes = 0
    if 'h' in dur_str:
        parts = dur_str.split('h')
        try:
            hours = int(parts[0].strip())
            dur_str = parts[1]
        except:
            pass
    if 'm' in dur_str:
        parts = dur_str.split('m')
        try:
            minutes = int(parts[0].strip())
        except:
            pass
    return hours * 60 + minutes

def parse_time_to_hour(time_str):
    """Parses '12:30 am' into an hour integer (0-23)."""
    if pd.isna(time_str):
        return -1
    try:
        t = pd.to_datetime(time_str).time()
        return t.hour
    except:
        return -1

def detect_and_normalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    Detects whether the CSV is from the Kayak scraper or the SearchAPI
    Google Flights scraper, and normalises the columns to a common schema:

        snapshot_ts, depart_date, origin, destination, airline,
        dep_time, duration, stops, price_usd
    """
    cols = set(df.columns.str.strip())

    # ── SearchAPI schema ──────────────────────────────────────────────────────
    # Columns: snapshot_date, departure_date, days_till_departure,
    #          destination, airline, flight_number, departure_time,
    #          aircraft, price_usd, duration_minutes, is_direct
    if 'flight_number' in cols:
        print("   Detected schema: SearchAPI / Google Flights")
        df = df.rename(columns={
            'snapshot_date':   'snapshot_ts',
            'departure_date':  'depart_date',
            'departure_time':  'dep_time',
        })
        # SearchAPI only returns the destination; reconstruct origin from route.
        if 'origin' not in df.columns:
            def infer_origin(row):
                dest = str(row.get('destination', '')).upper()
                return 'TBS' if dest == 'TLV' else 'TLV'
            df['origin'] = df.apply(infer_origin, axis=1)
        # duration already in minutes — convert to "Xh Ym" string for shared parser
        df['duration'] = df['duration_minutes'].apply(
            lambda m: f"{int(m)//60}h {int(m)%60}m" if pd.notna(m) else None
        )
        df['stops'] = df['is_direct'].apply(
            lambda x: 'Nonstop' if str(x).lower() == 'true' else '1 stop'
        )
        return df

    # ── Kayak schema (default) ────────────────────────────────────────────────
    print("   Detected schema: Kayak / ScrapingBee")
    return df


def main():
    print("=" * 60)
    print("Flight Price Predictor - XGBoost Regressor")
    print("=" * 60)

    # Accept an optional CSV path as command-line argument
    file_path = sys.argv[1] if len(sys.argv) > 1 else 'kayak_route_economics_reparsed.csv'
    print(f"1. Loading dataset: {file_path}")
    try:
        df = pd.read_csv(file_path)
    except FileNotFoundError:
        print(f"Error: Could not find {file_path}.")
        return

    # Clean the price column
    df = df.dropna(subset=['price_usd'])
    df['price_usd'] = pd.to_numeric(df['price_usd'], errors='coerce')
    df = df.dropna(subset=['price_usd'])

    print(f"   ✓ Loaded {len(df)} flight records.")

    # Normalise schema (handles both Kayak and SearchAPI CSVs)
    df = detect_and_normalize(df)

    # Filter to NONSTOP flights only — indirect routes can cost $4,000+ for
    # unrelated reasons (e.g., multi-stop premium carriers) which would
    # massively distort the model.
    nonstop_mask = df['stops'].str.lower().str.contains('nonstop', na=False)
    df = df[nonstop_mask].copy()
    print(f"   ✓ After nonstop filter: {len(df)} records remaining.")
    
    print("\n2. Engineering features...")
    # Convert dates to datetime objects
    # Note: snapshot_ts contains ' UTC', so we clean it up before parsing
    df['snapshot_ts'] = df['snapshot_ts'].astype(str).str.replace(' UTC', '')
    df['depart_date'] = pd.to_datetime(df['depart_date'], errors='coerce')
    df['snapshot_ts'] = pd.to_datetime(df['snapshot_ts'], errors='coerce')
    
    # Drop rows where dates couldn't be parsed
    df = df.dropna(subset=['depart_date', 'snapshot_ts'])
    
    # Time Features
    df['day_of_week'] = df['depart_date'].dt.dayofweek
    df['month'] = df['depart_date'].dt.month
    
    # Advance Purchase (days until departure)
    df['days_until_departure'] = (df['depart_date'] - df['snapshot_ts']).dt.days
    df['days_until_departure'] = df['days_until_departure'].apply(lambda x: max(0, x))
    
    # Flight details
    df['duration_minutes'] = df['duration'].apply(parse_duration)
    df['departure_hour'] = df['dep_time'].apply(parse_time_to_hour)
    
    # Categoricals — stops is excluded since all rows are now Nonstop
    df['airline_clean'] = df['airline'].fillna('Unknown').str.upper().str.strip()

    categorical_cols = ['origin', 'destination', 'airline_clean']

    # ── Price Insights features (only present in enriched SearchAPI CSV) ──────
    INSIGHT_NUMERIC_COLS = [
        'pi_lowest_price',
        'pi_typical_low',
        'pi_typical_high',
        'pi_price_trend_7d',
        'pi_price_vs_typical_low',
    ]
    INSIGHT_CAT_COLS = ['pi_price_level']

    available_numeric_insights = [c for c in INSIGHT_NUMERIC_COLS if c in df.columns]
    available_cat_insights     = [c for c in INSIGHT_CAT_COLS      if c in df.columns]

    if available_numeric_insights:
        print(f"   ✓ Price insights detected — adding {len(available_numeric_insights)+len(available_cat_insights)} insight features.")
        for col in available_numeric_insights:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        if available_cat_insights:
            categorical_cols += available_cat_insights
    else:
        print("   ℹ No price insights columns found — training on base features only.")

    # ── Lag Features from Price History ───────────────────────────────────────
    lag_features = []
    try:
        hist_df = pd.read_csv('price_history_q3_2026.csv')
        hist_df['departure_date'] = pd.to_datetime(hist_df['departure_date']).dt.strftime('%Y-%m-%d')
        hist_df = hist_df[['origin', 'destination', 'departure_date', 'days_before_dep', 'price_usd']]
        hist_df = hist_df.rename(columns={'price_usd': 'hist_price'})
        
        df['dep_date_str'] = df['depart_date'].dt.strftime('%Y-%m-%d')
        
        # 7-day lag
        df['target_days_7'] = df['days_until_departure'] + 7
        df = df.merge(
            hist_df, 
            left_on=['origin', 'destination', 'dep_date_str', 'target_days_7'],
            right_on=['origin', 'destination', 'departure_date', 'days_before_dep'],
            how='left'
        ).rename(columns={'hist_price': 'price_7d_ago'}).drop(columns=['departure_date', 'days_before_dep'])
        
        # 14-day lag
        df['target_days_14'] = df['days_until_departure'] + 14
        df = df.merge(
            hist_df, 
            left_on=['origin', 'destination', 'dep_date_str', 'target_days_14'],
            right_on=['origin', 'destination', 'departure_date', 'days_before_dep'],
            how='left'
        ).rename(columns={'hist_price': 'price_14d_ago'}).drop(columns=['departure_date', 'days_before_dep'])
        
        df = df.drop(columns=['dep_date_str', 'target_days_7', 'target_days_14'])
        lag_features = ['price_7d_ago', 'price_14d_ago']
        print("   ✓ Added lag features: price_7d_ago, price_14d_ago.")
    except Exception as e:
        print(f"   ℹ Could not add lag features: {e}")

    # Create modelling dataframe
    base_numeric = ['price_usd', 'day_of_week', 'month', 'days_until_departure',
                    'duration_minutes', 'departure_hour']
    model_df = df[base_numeric + categorical_cols + available_numeric_insights + lag_features].copy()

    # One-hot encode categorical variables (e.g., origin_TLV, airline_EL AL)
    model_df = pd.get_dummies(model_df, columns=categorical_cols, drop_first=True)

    model_df = model_df.dropna()
    print(f"   ✓ Features built. Final dataset shape: {model_df.shape}")
    
    print("\n3. Preparing for training...")
    # Separate Features (X) and Target (y)
    X = model_df.drop('price_usd', axis=1)
    y = model_df['price_usd']
    
    # Train-Test Split (80% train, 20% test)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    print(f"   ✓ Training on {len(X_train)} flights, Testing on {len(X_test)} flights.")
    
    print("\n4. Training XGBoost Model (this may take a few seconds)...")
    xgb_model = xgb.XGBRegressor(
        n_estimators=300,        # Number of trees
        learning_rate=0.05,      # Step size shrinkage
        max_depth=6,             # Maximum depth of a tree
        subsample=0.8,           # Fraction of samples used per tree
        colsample_bytree=0.8,    # Fraction of features used per tree
        random_state=42,
        n_jobs=-1                # Use all CPU cores
    )
    
    xgb_model.fit(X_train, y_train)
    print("   ✓ Training complete.")
    
    print("\n5. Evaluating Model on unseen Test Data...")
    y_pred = xgb_model.predict(X_test)
    
    mae = mean_absolute_error(y_test, y_pred)
    rmse = np.sqrt(mean_squared_error(y_test, y_pred))
    
    print("-" * 50)
    print(f"Average Flight Price in Test Data : ${y_test.mean():.2f}")
    print(f"Model Mean Absolute Error (MAE)   : ${mae:.2f}")
    print(f"Model Root Mean Squared Error     : ${rmse:.2f}")
    print("-" * 50)
    print("Interpretation: On average, the model's price prediction")
    print(f"is off by about ${mae:.2f} from the actual price.")
    
    print("\n6. Top 10 Most Important Features:")
    importances = xgb_model.feature_importances_
    features = X.columns
    importance_df = pd.DataFrame({'Feature': features, 'Importance': importances})
    importance_df = importance_df.sort_values(by='Importance', ascending=False)
    
    for i, row in importance_df.head(10).iterrows():
        print(f"   - {row['Feature']:<30} : {row['Importance']:.4f}")
        
    print("\n7. Saving model artifacts...")
    with open('xgb_pricing_model.pkl', 'wb') as f:
        pickle.dump(xgb_model, f)
    with open('xgb_model_features.json', 'w') as f:
        json.dump(list(X.columns), f)
    print("   ✓ Saved xgb_pricing_model.pkl and xgb_model_features.json")
        
    print("\nScript completed successfully.")

if __name__ == "__main__":
    main()
