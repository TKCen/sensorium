## 2025-02-14 - Optimizing SQLite loops with ATTACH DATABASE

**Learning:** When querying multiple separate SQLite database files sequentially in a loop (an N+1 query pattern), significant overhead is incurred by repeatedly opening and closing database connections. We can optimize this by attaching up to 10 databases inside a single in-memory SQLite connection using `ATTACH DATABASE` and executing a unified `UNION ALL` query. In addition, SQLite's `ATTACH DATABASE` statement does not support parameterized bindings in older/standard configurations, necessitating manual string-literal formatting with single-quote escaping (`replace("'", "''")`) to prevent SQL errors and syntax exploits.

**Action:** For sequential SQLite query loops over dynamic database file paths:
1. When there are multiple paths (e.g. `1 < count <= 10`), attach them to a unified in-memory connection and process results in a single run.
2. Escape single quotes in the raw paths via `replace("'", "''")` rather than parameter binding.
3. Keep fallback handlers robust to catch SQLite limits or environment-specific failures and process them sequentially to guarantee correctness.
