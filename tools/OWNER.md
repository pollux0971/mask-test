# OWNER: T 軌（契約層）

此目錄（`tools/**`）由 T 軌擁有，放置跨軌共用的工具程式（例如 mock device）。

- `compare_mel.py` 例外：由 **B 軌維護**（`B14` 產出），T 軌不要動這個檔案。
  比對邏輯本身在 `host/features/mel_compare.py`，這裡只是薄的 CLI 包裝。
