WITH big_orders AS (
  SELECT id AS order_id, cust_id, amount * 2 AS doubled
  FROM sales.orders
  WHERE status = 'PAID' -- keyword WHERE inside comment must be ignored
)
SELECT c.name AS customer,
       b.doubled + 1 AS adjusted,
       CASE WHEN c.tier = 'GOLD' THEN b.doubled ELSE 0 END AS tiered,
       COUNT(*) AS n
FROM big_orders b
INNER JOIN sales.customers c ON b.cust_id = c.id
GROUP BY c.name, b.doubled
HAVING COUNT(*) > 1;
