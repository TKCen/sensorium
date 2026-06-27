# Bolt's Journal - Critical Learnings

## 2025-05-27 - [JSONL read/count bottleneck]
**Learning:** `SensoriumStore.read_jsonl` parsed the entire file as JSON even when only the count or the last entry was needed. For files with ~20k entries, newline counting is ~60x faster than full JSON parsing, and binary-seek tail reading is ~300x faster for limit=1.
**Action:** Use `count_jsonl` for counts and binary-seek `read_jsonl(limit=N)` for retrieving the latest entries. This avoids linear scaling with file size for common status/latest-decision checks.
