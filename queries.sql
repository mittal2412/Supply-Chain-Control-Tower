-- =====================================================================
-- SCM Control Tower — Day 2 SQL Queries
-- Database: scm_control_tower.db
-- =====================================================================

-- 1. CURRENT INVENTORY POSITION (latest week, per product per warehouse)
SELECT i.product_id, p.product_name, i.warehouse, i.closing_stock, i.week_start_date
FROM inventory i
JOIN products p ON p.product_id = i.product_id
WHERE i.week_start_date = (SELECT MAX(week_start_date) FROM inventory)
ORDER BY i.product_id, i.warehouse;

-- 2. INVENTORY TURNOVER (annualized) per product
--    Turnover = Total consumption / Average TOTAL inventory (summed across warehouses first)
WITH weekly_inventory AS (
    SELECT product_id, week_start_date,
           SUM(consumption) AS weekly_consumption,
           SUM(closing_stock) AS total_closing_stock
    FROM inventory
    GROUP BY product_id, week_start_date
)
SELECT product_id,
       SUM(weekly_consumption) AS annual_consumption,
       ROUND(AVG(total_closing_stock), 1) AS avg_inventory,
       ROUND(SUM(weekly_consumption) * 1.0 / NULLIF(AVG(total_closing_stock), 0), 2) AS inventory_turnover
FROM weekly_inventory
GROUP BY product_id
ORDER BY inventory_turnover DESC;

-- 3. DAYS OF INVENTORY ON HAND per product
--    DOH = Avg TOTAL inventory / Avg daily consumption (summed across warehouses first)
WITH weekly_inventory AS (
    SELECT product_id, week_start_date,
           SUM(consumption) AS weekly_consumption,
           SUM(closing_stock) AS total_closing_stock
    FROM inventory
    GROUP BY product_id, week_start_date
)
SELECT product_id,
       ROUND(AVG(total_closing_stock), 1) AS avg_inventory,
       ROUND(AVG(weekly_consumption) / 7.0, 2) AS avg_daily_consumption,
       ROUND(AVG(total_closing_stock) / NULLIF(AVG(weekly_consumption) / 7.0, 0), 1) AS days_of_inventory
FROM weekly_inventory
GROUP BY product_id
ORDER BY days_of_inventory DESC;

-- 4. STOCKOUT FREQUENCY — how often (and how badly) each product stocks out
--    (aggregated across warehouses per week for a clean per-product-per-week view)
WITH weekly_stockout AS (
    SELECT product_id, week_start_date,
           SUM(stockout_units) AS weekly_stockout_units,
           SUM(consumption) AS weekly_consumption
    FROM inventory
    GROUP BY product_id, week_start_date
)
SELECT product_id,
       COUNT(*) FILTER (WHERE weekly_stockout_units > 0) AS weeks_with_stockout,
       COUNT(*) AS total_weeks,
       ROUND(100.0 * COUNT(*) FILTER (WHERE weekly_stockout_units > 0) / COUNT(*), 1) AS stockout_frequency_pct,
       SUM(weekly_stockout_units) AS total_units_short,
       ROUND(100.0 * SUM(weekly_stockout_units) / NULLIF(SUM(weekly_consumption), 0), 2) AS stockout_rate_pct_of_demand
FROM weekly_stockout
GROUP BY product_id
HAVING weeks_with_stockout > 0
ORDER BY stockout_rate_pct_of_demand DESC;

-- 5. ABC CLASSIFICATION by revenue contribution
WITH product_revenue AS (
    SELECT product_id, SUM(revenue) AS total_revenue
    FROM sales
    GROUP BY product_id
),
ranked AS (
    SELECT product_id, total_revenue,
           SUM(total_revenue) OVER (ORDER BY total_revenue DESC) * 1.0
             / SUM(total_revenue) OVER () AS cum_pct
    FROM product_revenue
)
SELECT product_id, total_revenue,
       ROUND(cum_pct * 100, 1) AS cumulative_revenue_pct,
       CASE WHEN cum_pct <= 0.70 THEN 'A'
            WHEN cum_pct <= 0.90 THEN 'B'
            ELSE 'C' END AS abc_class
FROM ranked
ORDER BY total_revenue DESC;

-- 6. FAST-MOVING vs SLOW-MOVING products (by total units sold)
SELECT product_id, SUM(quantity) AS total_units_sold,
       CASE WHEN SUM(quantity) >= (SELECT AVG(t) FROM (SELECT SUM(quantity) t FROM sales GROUP BY product_id)) * 1.5
            THEN 'Fast-moving'
            WHEN SUM(quantity) <= (SELECT AVG(t) FROM (SELECT SUM(quantity) t FROM sales GROUP BY product_id)) * 0.5
            THEN 'Slow-moving'
            ELSE 'Normal' END AS movement_class
FROM sales
GROUP BY product_id
ORDER BY total_units_sold DESC;

-- 7. SUPPLIER ON-TIME DELIVERY % (measured, not the baseline)
SELECT supplier_id,
       COUNT(*) AS total_pos,
       ROUND(100.0 * SUM(on_time) / COUNT(*), 1) AS on_time_delivery_pct
FROM purchase_orders
GROUP BY supplier_id
ORDER BY on_time_delivery_pct ASC;

-- 8. SUPPLIER AVERAGE ACTUAL LEAD TIME (order to delivery, in days)
SELECT supplier_id,
       ROUND(AVG(JULIANDAY(actual_delivery_date) - JULIANDAY(order_date)), 1) AS avg_actual_lead_time_days
FROM purchase_orders
GROUP BY supplier_id
ORDER BY avg_actual_lead_time_days DESC;

-- 9. SUPPLIER DEFECT RATE
SELECT supplier_id,
       SUM(defect_units) AS total_defect_units,
       SUM(order_qty) AS total_ordered_units,
       ROUND(100.0 * SUM(defect_units) / NULLIF(SUM(order_qty), 0), 2) AS defect_rate_pct
FROM purchase_orders
GROUP BY supplier_id
ORDER BY defect_rate_pct DESC;

-- 10. PURCHASE PRICE VARIANCE (actual PO price vs product's standard unit cost)
SELECT po.product_id, p.unit_cost AS standard_cost,
       ROUND(AVG(po.unit_price), 2) AS avg_actual_price,
       ROUND(AVG(po.unit_price) - p.unit_cost, 2) AS avg_price_variance,
       ROUND(100.0 * (AVG(po.unit_price) - p.unit_cost) / p.unit_cost, 2) AS variance_pct
FROM purchase_orders po
JOIN products p ON p.product_id = po.product_id
GROUP BY po.product_id
ORDER BY variance_pct DESC;

-- 11. MONTHLY DEMAND TREND (units sold per month, all products combined)
SELECT strftime('%Y-%m', date) AS month, SUM(quantity) AS total_units, SUM(revenue) AS total_revenue
FROM sales
GROUP BY month
ORDER BY month;

-- 12. SUPPLIER SCORECARD — combined view for the Power BI Supplier page
SELECT po.supplier_id, s.supplier_name,
       COUNT(*) AS total_pos,
       ROUND(100.0 * SUM(po.on_time) / COUNT(*), 1) AS otd_pct,
       ROUND(AVG(JULIANDAY(po.actual_delivery_date) - JULIANDAY(po.order_date)), 1) AS avg_lead_time_days,
       ROUND(100.0 * SUM(po.defect_units) / NULLIF(SUM(po.order_qty), 0), 2) AS defect_rate_pct,
       CASE WHEN 100.0 * SUM(po.on_time) / COUNT(*) >= 90 AND
                 100.0 * SUM(po.defect_units) / NULLIF(SUM(po.order_qty), 0) < 2
            THEN 'Excellent'
            WHEN 100.0 * SUM(po.on_time) / COUNT(*) >= 75
            THEN 'Good'
            ELSE 'Poor' END AS rating
FROM purchase_orders po
JOIN suppliers s ON s.supplier_id = po.supplier_id
GROUP BY po.supplier_id
ORDER BY otd_pct DESC;
