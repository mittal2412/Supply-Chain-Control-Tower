"""
SCM Control Tower - Day 4: Inventory Optimization
==================================================
Calculates, per product:
  1. Safety Stock  - buffer for demand AND lead time variability
  2. Reorder Point - when to place an order
  3. EOQ           - how much to order

Then SIMULATES what would have happened in 2025 if the company had used
the calculated reorder-point policy instead of its current (naive) policy,
and compares the two on stockouts, service level, holding cost and orders.

This before/after comparison is the core deliverable - it turns the
project from "I built a dashboard" into "I found a policy change worth
quantifying".

KEY INPUT NOTE: demand comes from sales (TRUE demand), not from
inventory.consumption (demand net of stockouts). Using consumption here
would understate demand for exactly the products that stock out most -
i.e. it would hide the problem we are trying to fix.
"""

import sqlite3
import numpy as np
import pandas as pd
from datetime import date

DB_PATH = "/home/claude/scm_control_tower.db"
START_DATE = date(2025, 1, 6)
N_WEEKS = 52

SERVICE_LEVEL_Z = 1.65      # z-score for a 95% cycle service level
HOLDING_COST_RATE = 0.22    # annual holding cost = 22% of unit cost
ORDERING_COST = 4000.0      # fixed cost per purchase order placed (INR)

conn = sqlite3.connect(DB_PATH)

# ---------------------------------------------------------------
# 1. TRUE weekly demand per product (from sales, not consumption)
# ---------------------------------------------------------------
sales = pd.read_sql("SELECT product_id, date, quantity FROM sales", conn)
sales["date"] = pd.to_datetime(sales["date"])
sales["week_idx"] = (sales["date"] - pd.Timestamp(START_DATE)).dt.days // 7

products = pd.read_sql("SELECT * FROM products", conn)
product_ids = products["product_id"].tolist()

weekly = sales.groupby(["product_id", "week_idx"])["quantity"].sum().reset_index()
grid = pd.MultiIndex.from_product([product_ids, range(N_WEEKS)], names=["product_id", "week_idx"])
weekly = weekly.set_index(["product_id", "week_idx"]).reindex(grid, fill_value=0).reset_index()

demand_stats = weekly.groupby("product_id")["quantity"].agg(
    avg_weekly_demand="mean", std_weekly_demand="std", annual_demand="sum"
).reset_index()

# ---------------------------------------------------------------
# 2. MEASURED lead time per product (mean and std, in days)
#    Taken from actual PO history, not the contracted figure.
# ---------------------------------------------------------------
lead_times = pd.read_sql("""
    SELECT product_id,
           JULIANDAY(actual_delivery_date) - JULIANDAY(order_date) AS lead_days
    FROM purchase_orders
""", conn)
lt_stats = lead_times.groupby("product_id")["lead_days"].agg(
    avg_lead_days="mean", std_lead_days="std", n_pos="count"
).reset_index()

df = (demand_stats
      .merge(lt_stats, on="product_id", how="left")
      .merge(products[["product_id", "unit_cost", "primary_supplier_id"]], on="product_id", how="left"))

# Products with no PO history (made in-house only): fall back to a nominal lead time
df["avg_lead_days"] = df["avg_lead_days"].fillna(7.0)
df["std_lead_days"] = df["std_lead_days"].fillna(1.0)

# ---------------------------------------------------------------
# 3. SAFETY STOCK, ROP, EOQ
# ---------------------------------------------------------------
# Work in daily units so lead time (days) and demand line up.
df["avg_daily_demand"] = df["avg_weekly_demand"] / 7.0
df["std_daily_demand"] = df["std_weekly_demand"] / np.sqrt(7.0)

# Safety stock accounting for BOTH demand and lead-time variability:
#   SS = z * sqrt( LT * sigma_d^2  +  d^2 * sigma_LT^2 )
df["safety_stock"] = SERVICE_LEVEL_Z * np.sqrt(
    df["avg_lead_days"] * df["std_daily_demand"] ** 2
    + (df["avg_daily_demand"] ** 2) * (df["std_lead_days"] ** 2)
)

# Reorder point = expected demand during lead time + safety stock
df["lead_time_demand"] = df["avg_daily_demand"] * df["avg_lead_days"]
df["reorder_point"] = df["lead_time_demand"] + df["safety_stock"]

# EOQ = sqrt( 2 * D * S / H )
df["holding_cost_per_unit"] = df["unit_cost"] * HOLDING_COST_RATE
df["eoq"] = np.sqrt(2 * df["annual_demand"] * ORDERING_COST / df["holding_cost_per_unit"])

for col in ["safety_stock", "reorder_point", "eoq", "lead_time_demand"]:
    df[col] = df[col].round(0)

# ---------------------------------------------------------------
# 4. SIMULATION: current (naive) policy vs proposed ROP/EOQ policy
# ---------------------------------------------------------------
# "Before" policy: a believable stand-in for how a company runs on gut
# feel rather than a calculated policy - reorder when stock falls to
# ~2 weeks of average demand, with NO safety stock buffer, and order a
# fixed quantity (4 weeks of average demand) each time. This is
# deliberately a real, plausible policy - not a strawman - so that
# beating it is actually evidence of something.
df["naive_rop"] = (df["avg_weekly_demand"] * 2).round(0)
df["naive_order_qty"] = (df["avg_weekly_demand"] * 4).round(0)


def simulate_rop_policy(pid, rop, order_qty, lead_days, demand_series, start_stock):
    """Day-level continuous-review (s, Q) simulation over 52 weeks.

    Each week: receive anything due, ship what demand allows, and if
    inventory position falls to or below the reorder point, place an
    order of size `order_qty` that arrives after the lead time.
    Used for BOTH the naive baseline and the proposed policy, with
    different (rop, order_qty) inputs, so the comparison is apples to
    apples - same simulator, same lead times, same demand series.
    """
    lead_weeks = max(1, int(round(lead_days / 7.0)))
    on_hand = start_stock
    on_order = 0
    pipeline = {}                 # week_idx -> qty arriving
    stockout_units = 0
    shipped = 0
    inventory_levels = []
    n_orders = 0

    for w in range(N_WEEKS):
        # receive
        arriving = pipeline.pop(w, 0)
        on_hand += arriving
        on_order -= arriving

        # ship against true demand
        demand = demand_series[w]
        ship = min(on_hand, demand)
        on_hand -= ship
        shipped += ship
        stockout_units += demand - ship

        # review inventory position and reorder
        position = on_hand + on_order
        if position <= rop:
            qty = int(max(order_qty, 1))
            pipeline[w + lead_weeks] = pipeline.get(w + lead_weeks, 0) + qty
            on_order += qty
            n_orders += 1

        inventory_levels.append(on_hand)

    return {
        "product_id": pid,
        "stockout_units": int(stockout_units),
        "units_shipped": int(shipped),
        "avg_inventory": float(np.mean(inventory_levels)),
        "n_orders": n_orders,
    }


demand_lookup = weekly.pivot(index="product_id", columns="week_idx", values="quantity")

before_rows, after_rows = [], []
for r in df.itertuples():
    series = demand_lookup.loc[r.product_id].values

    # BEFORE: naive fixed-ROP, no safety stock
    before_start = r.naive_rop + r.naive_order_qty
    before_rows.append(simulate_rop_policy(
        r.product_id, r.naive_rop, r.naive_order_qty, r.avg_lead_days, series, before_start
    ))

    # AFTER: calculated safety stock / ROP / EOQ
    after_start = r.reorder_point + r.eoq
    after_rows.append(simulate_rop_policy(
        r.product_id, r.reorder_point, r.eoq, r.avg_lead_days, series, after_start
    ))

current = pd.DataFrame(before_rows)
sim = pd.DataFrame(after_rows).rename(columns={
    "stockout_units": "sim_stockout_units",
    "units_shipped": "sim_units_shipped",
    "avg_inventory": "sim_avg_inventory",
    "n_orders": "sim_n_orders",
})

# ---------------------------------------------------------------
# 5. BEFORE vs AFTER comparison
# ---------------------------------------------------------------
true_demand = weekly.groupby("product_id")["quantity"].sum().rename("true_demand").reset_index()

comp = (current.merge(sim, on="product_id")
        .merge(true_demand, on="product_id")
        .merge(df[["product_id", "unit_cost", "safety_stock", "reorder_point",
                   "eoq", "avg_lead_days", "primary_supplier_id"]], on="product_id"))

comp["before_service_level_pct"] = 100 * comp["units_shipped"] / comp["true_demand"]
comp["after_service_level_pct"] = 100 * comp["sim_units_shipped"] / comp["true_demand"]

comp["before_holding_cost"] = comp["avg_inventory"] * comp["unit_cost"] * HOLDING_COST_RATE
comp["after_holding_cost"] = comp["sim_avg_inventory"] * comp["unit_cost"] * HOLDING_COST_RATE

comp["before_ordering_cost"] = comp["n_orders"] * ORDERING_COST
comp["after_ordering_cost"] = comp["sim_n_orders"] * ORDERING_COST

comp["before_total_cost"] = comp["before_holding_cost"] + comp["before_ordering_cost"]
comp["after_total_cost"] = comp["after_holding_cost"] + comp["after_ordering_cost"]

comp["stockout_units_avoided"] = comp["stockout_units"] - comp["sim_stockout_units"]
comp["service_level_gain_pts"] = comp["after_service_level_pct"] - comp["before_service_level_pct"]
comp["cost_delta"] = comp["after_total_cost"] - comp["before_total_cost"]

# lost revenue avoided (using margin on units that would have been missed)
prices = pd.read_sql("SELECT product_id, unit_price, unit_cost FROM products", conn)
comp = comp.merge(prices[["product_id", "unit_price"]], on="product_id")
comp["margin_per_unit"] = comp["unit_price"] - comp["unit_cost"]
comp["margin_recovered"] = comp["stockout_units_avoided"] * comp["margin_per_unit"]
comp["net_benefit"] = comp["margin_recovered"] - comp["cost_delta"]

# ---------------------------------------------------------------
# 6. REPORT
# ---------------------------------------------------------------
pd.set_option("display.width", 200)

tot_before_so = comp["stockout_units"].sum()
tot_after_so = comp["sim_stockout_units"].sum()
tot_demand = comp["true_demand"].sum()

print("=" * 70)
print("AGGREGATE: CURRENT POLICY vs PROPOSED ROP/EOQ POLICY")
print("=" * 70)
print(f"  Total true demand (units)      : {tot_demand:,.0f}")
print(f"  Stockout units  BEFORE         : {tot_before_so:,.0f}")
print(f"  Stockout units  AFTER          : {tot_after_so:,.0f}")
print(f"  Units recovered                : {tot_before_so - tot_after_so:,.0f}")
print(f"  Service level   BEFORE         : {100*(1-tot_before_so/tot_demand):.2f}%")
print(f"  Service level   AFTER          : {100*(1-tot_after_so/tot_demand):.2f}%")
print()
print(f"  Avg inventory   BEFORE (units) : {comp['avg_inventory'].sum():,.0f}")
print(f"  Avg inventory   AFTER  (units) : {comp['sim_avg_inventory'].sum():,.0f}")
print()
print(f"  Holding cost    BEFORE (INR)   : {comp['before_holding_cost'].sum():,.0f}")
print(f"  Holding cost    AFTER  (INR)   : {comp['after_holding_cost'].sum():,.0f}")
print(f"  Ordering cost   BEFORE (INR)   : {comp['before_ordering_cost'].sum():,.0f}")
print(f"  Ordering cost   AFTER  (INR)   : {comp['after_ordering_cost'].sum():,.0f}")
print(f"  TOTAL inv. cost BEFORE (INR)   : {comp['before_total_cost'].sum():,.0f}")
print(f"  TOTAL inv. cost AFTER  (INR)   : {comp['after_total_cost'].sum():,.0f}")
print(f"  Cost delta             (INR)   : {comp['cost_delta'].sum():,.0f}")
print()
print(f"  Margin recovered from avoided stockouts (INR): {comp['margin_recovered'].sum():,.0f}")
print(f"  NET BENEFIT                             (INR): {comp['net_benefit'].sum():,.0f}")

print("\n" + "=" * 70)
print("TOP 8 PRODUCTS BY NET BENEFIT")
print("=" * 70)
cols = ["product_id", "primary_supplier_id", "avg_lead_days", "safety_stock", "reorder_point",
        "eoq", "stockout_units", "sim_stockout_units", "service_level_gain_pts", "net_benefit"]
top = comp.sort_values("net_benefit", ascending=False).head(8)[cols].copy()
top["avg_lead_days"] = top["avg_lead_days"].round(1)
top["service_level_gain_pts"] = top["service_level_gain_pts"].round(1)
top["net_benefit"] = top["net_benefit"].round(0)
print(top.to_string(index=False))

print("\n" + "=" * 70)
print("WORST CURRENT SERVICE LEVELS (the products the business is failing on)")
print("=" * 70)
worst = comp.sort_values("before_service_level_pct").head(8)[
    ["product_id", "primary_supplier_id", "avg_lead_days", "before_service_level_pct",
     "after_service_level_pct", "safety_stock", "reorder_point"]].copy()
for c in ["before_service_level_pct", "after_service_level_pct", "avg_lead_days"]:
    worst[c] = worst[c].round(1)
print(worst.to_string(index=False))

# ---------------------------------------------------------------
# 7. SAVE
# ---------------------------------------------------------------
policy_cols = ["product_id", "primary_supplier_id", "avg_weekly_demand", "std_weekly_demand",
               "avg_lead_days", "std_lead_days", "safety_stock", "reorder_point", "eoq",
               "unit_cost", "holding_cost_per_unit"]
df[policy_cols].round(2).to_csv("/home/claude/scm_data/inventory_policy.csv", index=False)
comp.round(2).to_csv("/home/claude/scm_data/policy_comparison.csv", index=False)
print("\nSaved: inventory_policy.csv, policy_comparison.csv")
