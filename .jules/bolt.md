# Bolt's Journal - Performance Learnings

This journal documents critical performance learnings for the Agent Sensorium project.

## 2026-01-26 - Optimized JSONL Counting and Tail Reading
**Learning:** status reporting was reading and parsing hundreds of thousands of JSON lines just to get a count, causing O(N) CPU and memory pressure. Binary newline counting is ~100x faster for large files.
**Action:** Use `count_jsonl` for counts and `collections.deque` for tail reads in append-only JSONL stores.
