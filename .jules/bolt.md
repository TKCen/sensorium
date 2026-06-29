## 2025-05-22 - [JSONL count and trailing read optimization]
**Learning:** For large append-only JSONL state stores, full O(N) reads for status counts or trailing views are a significant bottleneck. Standard JSON parsing dominates CPU and memory usage.
**Action:** Use binary newline counting (`count_jsonl`) for summary statistics and `collections.deque` for trailing reads (`limit=N`) to minimize JSON parsing and memory allocation.
