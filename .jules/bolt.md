## 2025-05-14 - Optimized JSONL counting and trailing reads

**Learning:** Reading and parsing entire JSONL files into memory just to determine their length or to retrieve the most recent entry becomes a massive bottleneck as the dataset grows. In this codebase, the status tool and heartbeats were frequently re-parsing 100k+ signal records, taking ~1.4s per call.

**Action:** Use binary newline counting (`count_jsonl`) with a large buffer for fast O(n) counts without UTF-8 or JSON overhead. For tail reads, use `collections.deque` with `maxlen` directly on the file object to stream and keep only the last N lines, skipping parsing for the preceding bulk.
