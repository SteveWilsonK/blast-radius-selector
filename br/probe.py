"""How fast is this machine at sqlglot's kind of work?

A tight arithmetic loop says little about object-heavy Python. This parses,
optimizes and prints a few fixed queries with the unpatched sqlglot and compares
the time with the machine the run costs were measured on.
"""
import time

SCHEMA = {
    "orders": {"o_id": "int", "o_cust": "int", "o_total": "double", "o_date": "date", "o_status": "text"},
    "customer": {"c_id": "int", "c_name": "text", "c_nation": "int", "c_segment": "text"},
    "nation": {"n_id": "int", "n_name": "text", "n_region": "int"},
    "lineitem": {"l_order": "int", "l_part": "int", "l_qty": "double", "l_price": "double", "l_ship": "date"},
}
QUERIES = (
    """SELECT c.c_name, SUM(o.o_total) AS total, COUNT(*) AS n FROM orders AS o
       JOIN customer AS c ON o.o_cust = c.c_id JOIN nation AS n ON c.c_nation = n.n_id
       WHERE o.o_date >= CAST('1995-01-01' AS DATE) AND n.n_name IN ('FRANCE', 'GERMANY')
       GROUP BY c.c_name HAVING SUM(o.o_total) > 1000 ORDER BY total DESC LIMIT 10""",
    """WITH big AS (SELECT l_order, SUM(l_qty * l_price) AS v FROM lineitem GROUP BY l_order),
       ranked AS (SELECT o.o_id, o.o_cust, big.v, ROW_NUMBER() OVER (PARTITION BY o.o_cust ORDER BY big.v DESC) AS r
                  FROM orders AS o JOIN big ON big.l_order = o.o_id)
       SELECT c.c_segment, AVG(ranked.v) AS avg_v FROM ranked JOIN customer AS c ON c.c_id = ranked.o_cust
       WHERE ranked.r <= 3 AND c.c_segment <> 'X' GROUP BY c.c_segment""",
    """SELECT o_status, CASE WHEN o_total > 100 THEN 'big' WHEN o_total > 10 THEN 'mid' ELSE 'small' END AS size,
       COUNT(DISTINCT o_cust) AS custs FROM orders WHERE o_id IN (SELECT l_order FROM lineitem WHERE l_qty > 5)
       AND NOT EXISTS (SELECT 1 FROM customer WHERE c_id = o_cust AND c_name LIKE 'A%') GROUP BY 1, 2""",
)
# Seconds one timed pass takes on the reference machine (best of several).
REFERENCE = 0.0


def measure(passes=4):
    """Best time of `passes` timed passes over the queries, after one warm-up pass."""
    import sqlglot
    from sqlglot.optimizer import optimize

    def one():
        for sql in QUERIES:
            for dialect in (None, "snowflake", "bigquery"):
                expression = sqlglot.parse_one(sql)
                optimize(expression, schema=SCHEMA).sql(pretty=True, dialect=dialect)

    one()
    best = None
    for _ in range(passes):
        started = time.process_time()
        one()
        took = time.process_time() - started
        if best is None or took < best:
            best = took
    return best
