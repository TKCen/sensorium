## 2025-05-15 - JSONL Count and Tail Read Optimization
**Learning:** In append-only JSONL systems, loading and parsing the entire history to get a count or the latest entry creates O(N) performance degradation as logs grow. Parsing 50k lines took ~0.9s, while binary newline counting took <0.02s.
**Action:** Always prefer binary chunk-based newline counting for counts and `collections.deque` with a limit for tail reads to maintain constant memory pressure and high throughput on status/dashboard cold paths.
