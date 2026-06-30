## 2025-05-15 - [Efficient JSONL counting and tail reads]
**Learning:** Parsing large JSONL files (100k+ rows) into Python dictionaries just to get a count or the last few entries is a massive CPU and memory bottleneck. Binary newline counting and `collections.deque` on a file handle provide a ~48x speedup for status reports.
**Action:** Use `SensoriumStore.count_jsonl()` for file length and `SensoriumStore.read_jsonl(limit=N)` for recent history instead of full reads.
