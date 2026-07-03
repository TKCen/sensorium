## 2026-05-19 - Optimized JSONL counting and trailing reads

**Learning:** Loading and parsing massive append-only JSONL files just to get a line count or the last record is a significant O(N) bottleneck in both CPU and memory.
**Action:** Implement binary chunk-based newline counting for O(N) counts with minimal overhead, and use collections.deque for O(Limit) trailing reads to bypass parsing early records.
