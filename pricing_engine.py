import pandas as pd
import numpy as np
import pickle
import json
from datetime import datetime

class DynamicPricer:
    def __init__(self, model_path='xgb_pricing_model.pkl', features_path='xgb_model_features.json'):
        self.model_path = model_path
        self.features_path = features_path
        
        with open(self.model_path, 'rb') as f:
            self.model = pickle.load(f)
            
        with open(self.features_path, 'r') as f:
            self.expected_features = json.load(f)

        # Strategic defaults (can be overridden)
        self.market_posture = 0.95  # 5% undercut
        self.hard_floor = 150.0     # Never sell below $150
        self.hard_ceiling = 600.0   # Cap at $600
        self.total_capacity = 180   # 180 seats per plane

    def _get_target_booking_curve(self, days_out):
        """Returns the expected % of seats sold at a given days out."""
        # Simple linear assumption for now: 0% at 90 days, 100% at 0 days
        if days_out >= 90:
            return 0.0
        elif days_out <= 0:
            return 1.0
        else:
            return 1.0 - (days_out / 90.0)

    def _calculate_load_factor_adjustment(self, seats_sold, days_out):
        """Calculates price multiplier based on how full the plane is compared to target."""
        current_load = seats_sold / self.total_capacity
        target_load = self._get_target_booking_curve(days_out)
        
        load_diff = current_load - target_load
        
        # If we are ahead of target, raise price (+20% max)
        # If we are behind target, drop price (-20% max)
        # 10% ahead -> 5% price increase, etc.
        adjustment_factor = 1.0 + (load_diff * 0.5) 
        
        # Clamp adjustment to max 20% swing either way
        return max(0.80, min(1.20, adjustment_factor))

    def predict_market_price(self, flight_info):
        """Builds the feature vector and asks XGBoost what competitors are charging."""
        airlines = flight_info.get('competitor_airline', 'GEORGIAN AIRWAYS')
        if isinstance(airlines, str):
            if airlines == 'ALL':
                airlines = ['GEORGIAN AIRWAYS', 'EL AL', 'ISRAIR AIRLINES']
            else:
                airlines = [airlines]
                
        prices = []
        for airline_name in airlines:
            # Create a single-row dataframe with all expected features set to 0 initially
            df = pd.DataFrame(columns=self.expected_features)
            df.loc[0] = 0
            
            # Fill in known numeric features
            df.loc[0, 'days_until_departure'] = flight_info.get('days_until_departure', 30)
            df.loc[0, 'day_of_week'] = flight_info.get('day_of_week', 0)
            df.loc[0, 'month'] = flight_info.get('month', 7)
            df.loc[0, 'duration_minutes'] = flight_info.get('duration_minutes', 150)
            df.loc[0, 'departure_hour'] = flight_info.get('departure_hour', 12)
            
            # Provide reasonable defaults for the price insights and lags
            df.loc[0, 'pi_lowest_price'] = flight_info.get('pi_lowest_price', 250)
            df.loc[0, 'pi_typical_low'] = flight_info.get('pi_typical_low', 200)
            df.loc[0, 'pi_typical_high'] = flight_info.get('pi_typical_high', 350)
            df.loc[0, 'pi_price_trend_7d'] = flight_info.get('pi_price_trend_7d', 0)
            df.loc[0, 'pi_price_vs_typical_low'] = flight_info.get('pi_price_vs_typical_low', 50)
            df.loc[0, 'price_7d_ago'] = flight_info.get('price_7d_ago', 250)
            df.loc[0, 'price_14d_ago'] = flight_info.get('price_14d_ago', 240)
            
            # Fill categorical flags
            orig = f"origin_{flight_info.get('origin', 'TBS')}"
            if orig in df.columns: df.loc[0, orig] = 1
                
            dest = f"destination_{flight_info.get('destination', 'TLV')}"
            if dest in df.columns: df.loc[0, dest] = 1
                
            airline_col = f"airline_clean_{airline_name}"
            if airline_col in df.columns: df.loc[0, airline_col] = 1
                
            # Predict
            market_price = self.model.predict(df)[0]
            prices.append(float(market_price))
            
        return sum(prices) / len(prices)

    def get_optimal_price(self, flight_info, seats_sold):
        """Calculates our final ticket price."""
        # 1. Competitor Baseline
        market_price = self.predict_market_price(flight_info)
        
        # 2. Base Strategy Overlay
        strategic_price = market_price * self.market_posture
        
        # 3. Dynamic Load Factor Adjustment
        days_out = flight_info.get('days_until_departure', 30)
        lf_adj = self._calculate_load_factor_adjustment(seats_sold, days_out)
        
        dynamic_price = strategic_price * lf_adj
        
        # 4. Enforce Bounds
        final_price = max(self.hard_floor, min(self.hard_ceiling, dynamic_price))
        
        return {
            'market_baseline': round(market_price, 2),
            'strategic_price': round(strategic_price, 2),
            'load_factor_multiplier': round(lf_adj, 2),
            'final_price': round(final_price, 2)
        }

if __name__ == "__main__":
    pricer = DynamicPricer()
    
    # Test a scenario: TBS -> TLV, 30 days out, 50 seats sold
    flight = {
        'origin': 'TBS',
        'destination': 'TLV',
        'month': 8,
        'day_of_week': 2,
        'days_until_departure': 30,
        'competitor_airline': 'GEORGIAN AIRWAYS'
    }
    
    res = pricer.get_optimal_price(flight, seats_sold=50)
    print("Test Output:", res)
