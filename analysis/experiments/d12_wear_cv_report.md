# D12 — 實驗 B：同次戴 vs 跨次戴 CV 分析報告

> ⚠️ **本報告使用合成資料，數字不是真實結論。**真實結論待 `E04`（20 次戴脫資料蒐集）完成後，用真實錄音重跑本模組。

## 分模態 CV（within / between）
- `tof_L_distance`：**PASS**（within=0.0%，between=0.0%，門檻 30%，between/within 比值=2.35）
- `tof_R_distance`：**PASS**（within=0.0%，between=0.1%，門檻 30%，between/within 比值=2.19）
- `signal_rate`：**PASS**（within=0.3%，between=0.0%，門檻 30%，between/within 比值=0.06）
- `mel_total_energy`：**PASS**（within=0.1%，between=0.0%，門檻 30%，between/within 比值=0.50）

## 距離比值法（組內 vs 組間，D04/D05 距離函式）
- within-wear 平均距離：0.0000
- between-wear 平均距離：0.0000
- 比值（between/within）：5.37

## 改進建議
至少一個模態 `between/within > 1.5`，戴法重複性可能是瓶頸，建議：
- 骨架與臉部的接觸點增加（三點支撐 → 更穩定）
- 加定位標記（讓每次戴的位置一致）
- SOP 加入「戴上後先看 C09 的對稱性指標，調到綠燈才開始」
