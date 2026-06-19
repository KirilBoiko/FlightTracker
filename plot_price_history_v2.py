#!/usr/bin/env python3
"""
plot_price_history_v2.py
========================
Generates business-focused price history charts from price_history_q3_2026.csv.

Plots produced:
  1. Month-by-month curve per route: Clear comparison of July/August/September.
  2. Booking Window Sweet Spot: Heatmap of avg price at specific booking buckets.
  3. Weekly departure groups: Smoother curves grouped by departure week.
  4. Price Volatility by Route and Month: Bar chart showing price variance.

Run:
    python3 plot_price_history_v2.py
"""

import pandas as pd
import matplotlib
matplotlib.use("Agg")   # non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np
from pathlib import Path
import seaborn as sns

CSV_PATH   = "price_history_q3_2026.csv"
OUT_DIR    = Path("price_history_charts_v2")
OUT_DIR.mkdir(exist_ok=True)

ROUTES = [
    ("TBS", "TLV"),
    ("TLV", "TBS"),
    ("BUS", "TLV"),
    ("TLV", "BUS"),
]

ROUTE_LABELS = {
    ("TBS", "TLV"): "Tbilisi → Tel Aviv",
    ("TLV", "TBS"): "Tel Aviv → Tbilisi",
    ("BUS", "TLV"): "Batumi → Tel Aviv",
    ("TLV", "BUS"): "Tel Aviv → Batumi",
}

BRAND_BG    = "#0f1117"
BRAND_PANEL = "#1a1d27"
BRAND_GRID  = "#2a2d3a"
TEXT_COLOR  = "#e8eaf0"

# Colors for months
MONTH_COLORS = {
    7: "#5b8dee", # Blue for July
    8: "#f5a623", # Orange for August
    9: "#3ecf8e"  # Green for September
}
MONTH_NAMES = {7: "July", 8: "August", 9: "September"}

def style_ax(ax, title="", xlabel="", ylabel=""):
    ax.set_facecolor(BRAND_PANEL)
    ax.set_title(title, color=TEXT_COLOR, fontsize=13, fontweight="bold", pad=10)
    ax.set_xlabel(xlabel, color=TEXT_COLOR, fontsize=10)
    ax.set_ylabel(ylabel, color=TEXT_COLOR, fontsize=10)
    ax.tick_params(colors=TEXT_COLOR, labelsize=9)
    ax.grid(True, color=BRAND_GRID, linewidth=0.6, linestyle="--", alpha=0.7)
    for spine in ax.spines.values():
        spine.set_edgecolor(BRAND_GRID)

print("Loading data...")
df = pd.read_csv(CSV_PATH)
df["departure_date"] = pd.to_datetime(df["departure_date"])
df["history_date"]   = pd.to_datetime(df["history_date"])
df["price_usd"]      = pd.to_numeric(df["price_usd"], errors="coerce")
df["days_before_dep"]= pd.to_numeric(df["days_before_dep"], errors="coerce")
df = df.dropna(subset=["price_usd", "days_before_dep"])
df["month"] = df["departure_date"].dt.month
# Ensure we only have July, August, September
df = df[df["month"].isin([7, 8, 9])]

# ─────────────────────────────────────────────────────────────────
# PLOT 1 — Month-by-month curve per route
# ─────────────────────────────────────────────────────────────────
print("Plotting Chart 1: Month-by-month average price curve...")
fig, axes = plt.subplots(2, 2, figsize=(18, 11))
fig.patch.set_facecolor(BRAND_BG)
fig.suptitle("Average Price Curve by Departure Month", color=TEXT_COLOR, fontsize=16, fontweight="bold", y=1.02)

for ax, (orig, dest) in zip(axes.flat, ROUTES):
    subset = df[(df["origin"] == orig) & (df["destination"] == dest)]
    
    for month in [7, 8, 9]:
        month_data = subset[subset["month"] == month]
        if month_data.empty: continue
        
        agg = month_data.groupby("days_before_dep")["price_usd"].mean().reset_index()
        agg = agg.sort_values("days_before_dep", ascending=False)
        
        # Smooth the line
        agg["smoothed_price"] = agg["price_usd"].rolling(window=3, min_periods=1, center=True).mean()
        
        ax.plot(agg["days_before_dep"], agg["smoothed_price"], 
                color=MONTH_COLORS[month], linewidth=2.5, label=f"{MONTH_NAMES[month]}")
        
    style_ax(ax, title=ROUTE_LABELS[(orig, dest)], xlabel="Days Before Departure", ylabel="Average Price (USD)")
    ax.invert_xaxis()
    ax.legend(facecolor=BRAND_PANEL, edgecolor=BRAND_GRID, labelcolor=TEXT_COLOR, fontsize=10)

plt.tight_layout()
path1 = OUT_DIR / "1_monthly_curves.png"
plt.savefig(path1, dpi=150, bbox_inches="tight", facecolor=BRAND_BG)
plt.close()

# ─────────────────────────────────────────────────────────────────
# PLOT 2 — Booking Window Sweet Spot
# ─────────────────────────────────────────────────────────────────
print("Plotting Chart 2: Booking Window Sweet Spots...")
bins = [-1, 14, 30, 60, 90, 200]
labels = ["0-14 days", "15-30 days", "31-60 days", "61-90 days", "90+ days"]
df["booking_window"] = pd.cut(df["days_before_dep"], bins=bins, labels=labels)

fig, axes = plt.subplots(2, 2, figsize=(18, 12))
fig.patch.set_facecolor(BRAND_BG)
fig.suptitle("Average Price by Booking Window & Month", color=TEXT_COLOR, fontsize=16, fontweight="bold", y=1.02)

for ax, (orig, dest) in zip(axes.flat, ROUTES):
    subset = df[(df["origin"] == orig) & (df["destination"] == dest)]
    if subset.empty: continue
    
    pivot = subset.pivot_table(index="month", columns="booking_window", values="price_usd", aggfunc="mean", observed=False)
    pivot.index = [MONTH_NAMES.get(m, m) for m in pivot.index]
    
    # Ensure columns order and existence
    pivot = pivot.reindex(columns=labels)
    
    sns.heatmap(pivot, annot=True, fmt=".0f", cmap="YlGnBu_r", ax=ax, 
                cbar_kws={'label': 'Average Price (USD)'}, 
                annot_kws={"size": 11, "weight": "bold"})
    
    ax.set_facecolor(BRAND_PANEL)
    ax.set_title(ROUTE_LABELS[(orig, dest)], color=TEXT_COLOR, fontsize=14, fontweight="bold", pad=15)
    ax.set_xlabel("Booking Window (Days Before Departure)", color=TEXT_COLOR, fontsize=11, labelpad=10)
    ax.set_ylabel("Departure Month", color=TEXT_COLOR, fontsize=11, labelpad=10)
    ax.tick_params(colors=TEXT_COLOR, labelsize=10)
    
    cbar = ax.collections[0].colorbar
    cbar.ax.yaxis.set_tick_params(color=TEXT_COLOR)
    cbar.ax.yaxis.label.set_color(TEXT_COLOR)
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color=TEXT_COLOR)

plt.tight_layout()
path2 = OUT_DIR / "2_booking_window_heatmap.png"
plt.savefig(path2, dpi=150, bbox_inches="tight", facecolor=BRAND_BG)
plt.close()

# ─────────────────────────────────────────────────────────────────
# PLOT 3 — Weekly departure groups
# ─────────────────────────────────────────────────────────────────
print("Plotting Chart 3: Weekly departure groups...")
df["week_of_month"] = (df["departure_date"].dt.day - 1) // 7 + 1
df["month_week_label"] = df["month"].map(MONTH_NAMES) + " W" + df["week_of_month"].astype(str)

fig, axes = plt.subplots(2, 2, figsize=(20, 14))
fig.patch.set_facecolor(BRAND_BG)
fig.suptitle("Price Curves Grouped by Departure Week", color=TEXT_COLOR, fontsize=16, fontweight="bold", y=1.02)

for ax, (orig, dest) in zip(axes.flat, ROUTES):
    subset = df[(df["origin"] == orig) & (df["destination"] == dest)]
    weeks = sorted(subset["month_week_label"].unique())
    
    for i, week in enumerate(weeks):
        week_data = subset[subset["month_week_label"] == week]
        agg = week_data.groupby("days_before_dep")["price_usd"].mean().reset_index().sort_values("days_before_dep", ascending=False)
        agg["smoothed"] = agg["price_usd"].rolling(3, center=True, min_periods=1).mean()
        
        month_num = int(week_data["month"].iloc[0])
        line_color = MONTH_COLORS[month_num]
        
        ax.plot(agg["days_before_dep"], agg["smoothed"], 
                color=line_color, alpha=0.8, linewidth=1.8)
                
        # Add inline label at the minimum days_before_dep (the right-most edge)
        valid_points = agg.dropna(subset=["smoothed"])
        if not valid_points.empty:
            min_days_row = valid_points.loc[valid_points["days_before_dep"].idxmin()]
            x_pos = min_days_row["days_before_dep"]
            y_pos = min_days_row["smoothed"]
            
            # Shorten name for space: "July W1" -> "Jul W1"
            short_label = week.replace("July", "Jul").replace("August", "Aug").replace("September", "Sep")
            
            # Offset the text slightly to the right using textcoords
            ax.annotate(short_label, 
                        xy=(x_pos, y_pos), 
                        xytext=(4, 0), 
                        textcoords="offset points",
                        fontsize=8, 
                        color=line_color, 
                        fontweight="bold",
                        ha="left", va="center")
        
    style_ax(ax, title=ROUTE_LABELS[(orig, dest)], xlabel="Days Before Departure", ylabel="Average Price (USD)")
    ax.invert_xaxis()
    # Add a bit of padding on the right (negative values) so the labels don't get cut off
    ax.set_xlim(left=df["days_before_dep"].max() + 5, right=-15)
    
    from matplotlib.lines import Line2D
    custom_lines = [Line2D([0], [0], color=MONTH_COLORS[m], lw=2) for m in [7,8,9]]
    ax.legend(custom_lines, ['July Weeks', 'August Weeks', 'Sept Weeks'], 
              facecolor=BRAND_PANEL, edgecolor=BRAND_GRID, labelcolor=TEXT_COLOR, fontsize=10, loc="upper left")

plt.tight_layout()
path3 = OUT_DIR / "3_weekly_curves.png"
plt.savefig(path3, dpi=150, bbox_inches="tight", facecolor=BRAND_BG)
plt.close()

# ─────────────────────────────────────────────────────────────────
# PLOT 4 — Price Volatility
# ─────────────────────────────────────────────────────────────────
print("Plotting Chart 4: Price Volatility...")
fig, axes = plt.subplots(1, 2, figsize=(16, 6))
fig.patch.set_facecolor(BRAND_BG)
fig.suptitle("Price Volatility by Route and Month", color=TEXT_COLOR, fontsize=16, fontweight="bold", y=1.05)

vol_data = df.groupby(["origin", "destination", "month"])["price_usd"].agg(["std", lambda x: x.max()-x.min()]).reset_index()
vol_data.columns = ["origin", "destination", "month", "std_dev", "price_range"]
vol_data["route"] = vol_data["origin"] + "→" + vol_data["destination"]

ax1 = axes[0]
sns.barplot(data=vol_data, x="route", y="std_dev", hue="month", palette=MONTH_COLORS, ax=ax1)
style_ax(ax1, title="Standard Deviation of Prices", xlabel="Route", ylabel="Standard Deviation (USD)")
ax1.set_xticklabels(ax1.get_xticklabels(), rotation=45)
handles, labels = ax1.get_legend_handles_labels()
ax1.legend(handles, [MONTH_NAMES[int(l)] for l in labels], title="Month", facecolor=BRAND_PANEL, labelcolor=TEXT_COLOR)

ax2 = axes[1]
sns.barplot(data=vol_data, x="route", y="price_range", hue="month", palette=MONTH_COLORS, ax=ax2)
style_ax(ax2, title="Max-Min Price Range", xlabel="Route", ylabel="Price Range (USD)")
ax2.set_xticklabels(ax2.get_xticklabels(), rotation=45)
ax2.get_legend().remove()

plt.tight_layout()
path4 = OUT_DIR / "4_price_volatility.png"
plt.savefig(path4, dpi=150, bbox_inches="tight", facecolor=BRAND_BG)
plt.close()

print(f"\n✅ All v2 charts saved to '{OUT_DIR}/'")
