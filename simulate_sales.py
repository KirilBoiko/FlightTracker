import pandas as pd
import numpy as np
import random
from pricing_engine import DynamicPricer
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def simulate_flight():
    pricer = DynamicPricer()
    
    # Flight details: August flight from TBS to TLV
    flight = {
        'origin': 'TBS',
        'destination': 'TLV',
        'month': 8,
        'day_of_week': 4,
        'competitor_airline': 'ALL',
        'pi_lowest_price': 280,
        'pi_typical_low': 250,
        'pi_typical_high': 400,
        'pi_price_trend_7d': 5,
        'pi_price_vs_typical_low': 30,
        'price_7d_ago': 270,
        'price_14d_ago': 260
    }
    
    seats_sold = 0
    total_revenue = 0
    history = []
    
    # Simulate 90 days before departure down to 0 days
    for day in range(90, -1, -1):
        flight['days_until_departure'] = day
        
        # 1. Get today's dynamic price from the engine
        price_data = pricer.get_optimal_price(flight, seats_sold)
        today_price = price_data['final_price']
        
        # 2. Simulate customer demand
        # Demand naturally increases as we get closer to departure
        base_demand = 1.0 + ((90 - day) / 90.0) * 3.0  # Ranges from 1 to 4 people wanting to buy
        
        # Price elasticity: if we are cheaper than market, more people buy
        price_competitiveness = price_data['market_baseline'] / today_price
        demand = base_demand * price_competitiveness
        
        # Add some random daily noise
        demand *= random.uniform(0.5, 1.5)
        
        # Calculate actual tickets sold (cap at remaining capacity)
        tickets_sold_today = int(round(demand))
        tickets_sold_today = min(tickets_sold_today, pricer.total_capacity - seats_sold)
        
        seats_sold += tickets_sold_today
        total_revenue += tickets_sold_today * today_price
        
        history.append({
            'days_out': day,
            'market_baseline': price_data['market_baseline'],
            'our_price': today_price,
            'seats_sold_total': seats_sold,
            'tickets_sold_today': tickets_sold_today,
            'load_factor_multiplier': price_data['load_factor_multiplier']
        })
        
        if seats_sold >= pricer.total_capacity:
            break # Sold out!
            
    df = pd.DataFrame(history)
    
    # Plotting the simulation
    fig, ax1 = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor('#0f1117')
    ax1.set_facecolor('#1a1d27')
    
    color1 = '#5b8dee'
    ax1.set_xlabel('Days Before Departure', color='white')
    ax1.set_ylabel('Price (USD)', color=color1)
    ax1.plot(df['days_out'], df['our_price'], color=color1, linewidth=3, label='Our Dynamic Price')
    ax1.plot(df['days_out'], df['market_baseline'], color='#f5a623', linestyle='--', label='Competitor Market Price')
    ax1.tick_params(axis='y', labelcolor=color1, colors='white')
    ax1.tick_params(axis='x', colors='white')
    ax1.invert_xaxis()
    ax1.legend(loc='upper left', facecolor='#1a1d27', labelcolor='white')
    
    ax2 = ax1.twinx()  
    color2 = '#3ecf8e'
    ax2.set_ylabel('Seats Sold', color=color2)  
    ax2.plot(df['days_out'], df['seats_sold_total'], color=color2, linewidth=2, label='Total Seats Sold')
    ax2.tick_params(axis='y', labelcolor=color2, colors='white')
    
    # Plot target load curve
    target_curve = [pricer._get_target_booking_curve(d) * pricer.total_capacity for d in df['days_out']]
    ax2.plot(df['days_out'], target_curve, color='gray', linestyle=':', label='Target Booking Curve')
    ax2.legend(loc='lower right', facecolor='#1a1d27', labelcolor='white')
    
    plt.title('Dynamic Pricing Engine Simulation (TBS → TLV, August)', color='white', pad=20)
    plt.tight_layout()
    plt.savefig('pricing_simulation.png', dpi=150, facecolor='#0f1117')
    
    print(f"Simulation Complete. Sold {seats_sold}/{pricer.total_capacity} seats.")
    print(f"Total Revenue: ${total_revenue:,.2f}")
    print(f"Average Ticket Price: ${total_revenue/max(1, seats_sold):.2f}")
    
if __name__ == "__main__":
    simulate_flight()
