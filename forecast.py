"""
SCM Control Tower — Day 3: Demand Forecasting
================================================
Forecasts weekly demand per product using two methods:
  1. Moving Average (MA, window=4 weeks)
  2. Simple Exponential Smoothing (SES, alpha=0.3)

Evaluated with a rolling walk-forward backtest over the last 12 weeks
of 2025, using MAE, RMSE, MAPE, and forecast bias.

Why these two methods (not three): scoped down from the original 3-method
plan (MA / WMA / Exponential Smoothing) to keep the 1-week build realistic.
MA and SES are the two most commonly compared baselines in real demand
planning, so this pairing is still solid interview material.

Why sales.csv (not inventory.consumption) as the demand signal: sales
records are generated independently of stock availability, so they
represent TRUE underlying demand. inventory.consumption is demand net of
stockouts, i.e. under-counts true demand for products that stocked out
(exactly the products in Day 2's stockout query) — using it here would
bias the forecast accuracy numbers for the products we most care about.
"""

import sqlite3
import numpy as np
import pandas as pd
from datetime import date, timedelta

DB_PATH = "/home/claude/scm_control_tower.db"
START_DATE = date(2025, 1, 6)
N_WEEKS = 52
TEST_WEEKS = 12          # backtest window
MA_WINDOW = 4            # moving average window
SES_ALPHA = 0.3          # smoothing constant

conn = sqlite3.connect(DB_PATH)
sales = pd.read_sql("SELECT product_id, date, quantity FROM sales", conn)
products = pd.read_sql("SELECT product_id FROM products", conn)["product_id"].tolist()

sales["date"] = pd.to_datetime(sales["date"])
sales["week_idx"] = (sales["date"] - pd.Timestamp(START_DATE)).dt.days // 7

# Full product x week grid, zero-filled where there was no demand that week
weekly = sales.groupby(["product_id", "week_idx"])["quantity"].sum().reset_index()
full_index = pd.MultiIndex.from_product([products, range(N_WEEKS)], names=["product_id", "week_idx"])
weekly = weekly.set_index(["product_id", "week_idx"]).reindex(full_index, fill_value=0).reset_index()
weekly = weekly.sort_values(["product_id", "week_idx"])


def moving_average_forecast(series, window):
    """Forecast for week t = average of the `window` weeks before t."""
    return series.shift(1).rolling(window=window, min_periods=1).mean()


def exponential_smoothing_forecast(series, alpha):
    """One-step-ahead SES forecast: level updated week by week, forecast
    for week t = smoothed level as of t-1."""
    levels = np.zeros(len(series))
    forecasts = np.full(len(series), np.nan)
    levels[0] = series.iloc[0]
    for t in range(1, len(series)):
        forecasts[t] = levels[t - 1]
        levels[t] = alpha * series.iloc[t] + (1 - alpha) * levels[t - 1]
    return pd.Series(forecasts, index=series.index)


results = []
detail_rows = []

for pid, grp in weekly.groupby("product_id"):
    grp = grp.reset_index(drop=True)
    actual = grp["quantity"]

    ma_fc = moving_average_forecast(actual, MA_WINDOW)
    ses_fc = exponential_smoothing_forecast(actual, SES_ALPHA)

    test_start = N_WEEKS - TEST_WEEKS
    test_actual = actual.iloc[test_start:].reset_index(drop=True)
    test_weeks = grp["week_idx"].iloc[test_start:].reset_index(drop=True)

    for method_name, fc in [("Moving Average", ma_fc), ("Exponential Smoothing", ses_fc)]:
        test_fc = fc.iloc[test_start:].reset_index(drop=True)
        err = test_actual - test_fc
        abs_err = err.abs()
        # MAPE: exclude weeks with zero actual demand (undefined % error)
        nonzero = test_actual != 0
        mape = (abs_err[nonzero] / test_actual[nonzero]).mean() * 100 if nonzero.any() else np.nan

        mae = abs_err.mean()
        rmse = np.sqrt((err ** 2).mean())
        bias = err.mean()  # positive = under-forecasting, negative = over-forecasting

        results.append({
            "product_id": pid, "method": method_name,
            "MAE": round(mae, 2), "RMSE": round(rmse, 2),
            "MAPE_pct": round(mape, 2) if pd.notna(mape) else None,
            "forecast_bias": round(bias, 2),
        })

        for wk, a, f in zip(test_weeks, test_actual, test_fc):
            detail_rows.append({
                "product_id": pid, "method": method_name, "week_idx": wk,
                "actual": a, "forecast": round(f, 1) if pd.notna(f) else None,
            })

results_df = pd.DataFrame(results)
detail_df = pd.DataFrame(detail_rows)

# ---- Summary: which method wins more often, and overall accuracy ----
pivot = results_df.pivot(index="product_id", columns="method", values="MAPE_pct")
pivot["better_method"] = pivot.idxmin(axis=1)
win_counts = pivot["better_method"].value_counts()

print("=== Overall accuracy by method (avg across all 40 products) ===")
print(results_df.groupby("method")[["MAE", "RMSE", "MAPE_pct", "forecast_bias"]].mean().round(2))
print("\n=== Which method wins per product (lower MAPE) ===")
print(win_counts)
print("\n=== Worst-forecast products (highest MAPE, either method) ===")
print(results_df.sort_values("MAPE_pct", ascending=False).head(8).to_string(index=False))

# ---- Save outputs ----
results_df.to_csv("/home/claude/scm_data/forecast_accuracy.csv", index=False)
detail_df.to_csv("/home/claude/scm_data/forecast_detail.csv", index=False)
pivot.reset_index().to_csv("/home/claude/scm_data/forecast_method_comparison.csv", index=False)
print("\nSaved: forecast_accuracy.csv, forecast_detail.csv, forecast_method_comparison.csv")
