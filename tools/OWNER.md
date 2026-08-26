# OWNER: T 軌（契約層）

此目錄（`tools/**`）由 T 軌擁有，放置跨軌共用的工具程式（例如 mock device）。

- `compare_mel.py` 例外：由 **B 軌維護**（`B14` 產出），T 軌不要動這個檔案。
  比對邏輯本身在 `host/features/mel_compare.py`，這裡只是薄的 CLI 包裝。
- `fw_regression.py` 例外：由 **A 軌維護**（`A15` 產出，2026-08-26
  調度員 SendMessage 授權，比照 `compare_mel.py` 的模式），T 軌不要動這個
  檔案。解析邏輯重用 `host/capture/protocol.py`/`dropwatch.py`，這裡只是
  跑一段時間、彙整成 `reports/A15_perf.md` 表格數字的薄外殼。
