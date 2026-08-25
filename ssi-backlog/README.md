# SSI Phase 0 — Atomic Story Backlog

雙 VL53L7CX ToF 矩陣 + 骨傳導麥克風的無聲語音介面（**無 sEMG 版本**）

**93 個 atomic story** · 6 條軌道 · 總工時 188 h（23.6 人天）

---

## 快速開始

| 我想… | 看這個 |
|---|---|
| 知道怎麼平行開發 | [`PARALLEL_MAP.md`](PARALLEL_MAP.md) |
| 知道跨軌介面長怎樣 | [`CONTRACTS.md`](CONTRACTS.md) ← **先填這個** |
| 一個人的逐日排程 | [`schedules/solo.md`](schedules/solo.md) |
| 三個工作單元的排程 | [`schedules/parallel-3.md`](schedules/parallel-3.md) |
| 現在可以開工什麼 | `python3 tools/prompt.py --ready` |
| 把 story 交給 Claude Code agent | [`PROMPTS.md`](PROMPTS.md) |

### 立即可開工（零依賴）

- [`A04`](stories/A-firmware/A04.md) [1.5h] 🔧 修正 ToF 輪詢週期
- [`A10`](stories/A-firmware/A10.md) [3.0h] 🔧 esp-dsp 整合探針（FFT 正確性）
- [`T01`](stories/T-contracts/T01.md) [2.0h] 凍結協定 v2 規格
- [`T02`](stories/T-contracts/T02.md) [2.0h] 凍結 HDF5 session schema
- [`T03`](stories/T-contracts/T03.md) [1.5h] 凍結特徵向量規格
- [`T06`](stories/T-contracts/T06.md) [1.5h] 專案骨架與軌道目錄劃分

**建議先做 `T01` → `T04`。** T04 完成的那一刻，B/C/D 三軌共 136 h 的工作全部解鎖。

---

## 五種模式（側邊欄）

前端從單一畫面重構成五模式 SPA，側邊欄切換（數字鍵 `1`–`5`）：

```
┌──────────────────┐
│ ● 已連線  proto2 │  ← C04 全域狀態列
├──────────────────┤
│ ▣  監測      1   │  即時熱力圖 / Mel 瀑布圖 / 品質儀表 / PCA 軌跡
│ ⏺  錄製      2   │  session 設定 / trial 提示 / 進度 / 重錄
│ ◎  測驗      3   │  8 選項 / 三軌評分 / 融合滑桿 / 混淆矩陣
│ ⚗  驗證      4   │  實驗執行器 / 報告檢視
│ ▶  回放      5   │  session 重播（也是 Demo 備援）
├──────────────────┤
│ 掉幀   0.2%      │  ← 迷你品質摘要，五個模式都看得到
│ 對稱   8%        │
│ 30.1 Hz          │
├──────────────────┤
│ ⚙  設定          │
└──────────────────┘
```

### 架構關鍵：資料層與模式層分離（`C03`）

```
SSE ──► bus.js ──┬──► dataStore（環形緩衝，永遠在跑）
                 └──► 當前模式的 render()（只有可見的模式跑）
```

測驗模式需要即時 ToF 與 Mel，監測模式也需要。**如果資料層綁在模式上，
切換模式就會中斷資料流**，而品質指標的滑動窗需要連續資料。

所以：模式切換只切「誰在繪製」，不切「誰在收資料」。隱藏的模式停掉 `requestAnimationFrame`
但繼續累積狀態——切回來時基線、色階、統計都還在。

| Story | 內容 |
|---|---|
| [`C01`](stories/C-frontend/C01.md) | 側邊欄 + 主內容區骨架，`panel.html` 拆成多檔 |
| [`C02`](stories/C-frontend/C02.md) | 模式切換器（點擊 / 數字鍵 / 摺疊） |
| [`C03`](stories/C-frontend/C03.md) | 模式路由與跨模式狀態保存 |
| [`C04`](stories/C-frontend/C04.md) | 全域狀態列 |

---

## 軌道總覽

| 軌 | 名稱 | Story | 工時 | 需要硬體 | 目錄 |
|---|---|---|---|---|---|
| **T** | 契約層 | 6 | 13 h | **0** | [`T-contracts/`](stories/T-contracts/) |
| **A** | 韌體 | 15 | 26 h | 15/15 | [`A-firmware/`](stories/A-firmware/) |
| **B** | 主機橋接 | 19 | 37 h | 2/19 | [`B-bridge/`](stories/B-bridge/) |
| **C** | 前端 | 25 | 58 h | **0** | [`C-frontend/`](stories/C-frontend/) |
| **D** | 分析引擎 | 20 | 42 h | **0** | [`D-analysis/`](stories/D-analysis/) |
| **E** | 硬體實驗 | 8 | 14 h | 8/8 | [`E-hardware/`](stories/E-hardware/) |

---

## 全部 Story

⚡ = 關鍵路徑　🔧 = 需要實體硬體

### T — 契約層（6 個, 13 h）

| ID | 標題 | 時 | 前置 | 標記 |
|---|---|---|---|---|
| [`T01`](stories/T-contracts/T01.md) | 凍結協定 v2 規格 | 2.0 | — | ⚡ |
| [`T02`](stories/T-contracts/T02.md) | 凍結 HDF5 session schema | 2.0 | — |  |
| [`T03`](stories/T-contracts/T03.md) | 凍結特徵向量規格 | 1.5 | — |  |
| [`T04`](stories/T-contracts/T04.md) | Mock Device：合成資料產生器 | 4.0 | `T01` | ⚡ |
| [`T05`](stories/T-contracts/T05.md) | Mock Device：真實 log 重播 | 2.0 | `T04` `E01` |  |
| [`T06`](stories/T-contracts/T06.md) | 專案骨架與軌道目錄劃分 | 1.5 | — |  |

### A — 韌體（15 個, 26 h）

| ID | 標題 | 時 | 前置 | 標記 |
|---|---|---|---|---|
| [`A01`](stories/A-firmware/A01.md) | $T 行：新標籤 + 序號 + 裝置時間戳 | 2.0 | `T01` | 🔧 |
| [`A02`](stories/A-firmware/A02.md) | $T 行：加入 signal_per_spad | 2.0 | `A01` | 🔧 |
| [`A03`](stories/A-firmware/A03.md) | $M 行：新標籤 + 序號 + 時間戳 | 1.0 | `T01` | 🔧 |
| [`A04`](stories/A-firmware/A04.md) | 修正 ToF 輪詢週期 | 1.5 | — | 🔧 |
| [`A05`](stories/A-firmware/A05.md) | 掉幀計數器 | 1.0 | `A01` `A04` | 🔧 |
| [`A06`](stories/A-firmware/A06.md) | $H 心跳行 | 1.5 | `A05` | 🔧 |
| [`A07`](stories/A-firmware/A07.md) | $STATUS 加入協定版本與韌體識別 | 1.0 | `T01` | 🔧 |
| [`A08`](stories/A-firmware/A08.md) | SENS 指令：感測器獨立開關 | 2.0 | `T01` | 🔧 |
| [`A09`](stories/A-firmware/A09.md) | PING 指令：主動校時取樣 | 0.5 | `A07` | 🔧 |
| [`A10`](stories/A-firmware/A10.md) | esp-dsp 整合探針（FFT 正確性） | 3.0 | — | 🔧 |
| [`A11`](stories/A-firmware/A11.md) | Mel 濾波器組靜態表 | 2.0 | `A10` `T03` | 🔧 |
| [`A12`](stories/A-firmware/A12.md) | log-Mel 計算與 $F 輸出 | 4.0 | `A11` `A03` | 🔧 |
| [`A13`](stories/A-firmware/A13.md) | MEL 指令：串流開關 | 0.5 | `A12` | 🔧 |
| [`A14`](stories/A-firmware/A14.md) | 音框 hop 改為 256（50% 重疊） | 2.0 | `A12` | 🔧 |
| [`A15`](stories/A-firmware/A15.md) | 韌體效能回歸測試 | 2.0 | `A06` `A12` | 🔧 |

### B — 主機橋接（19 個, 37 h）

| ID | 標題 | 時 | 前置 | 標記 |
|---|---|---|---|---|
| [`B01`](stories/B-bridge/B01.md) | 解析協定 v2 行格式 | 2.0 | `T01` `T04` | ⚡ |
| [`B02`](stories/B-bridge/B02.md) | v1 / v2 雙協定相容 | 1.0 | `B01` `A07` |  |
| [`B03`](stories/B-bridge/B03.md) | 掉幀偵測（seq 跳號） | 1.5 | `B01` |  |
| [`B04`](stories/B-bridge/B04.md) | 時鐘對齊模型 | 3.0 | `B01` | ⚡ |
| [`B05`](stories/B-bridge/B05.md) | PING 主動校時 | 1.0 | `B04` `A09` | 🔧 |
| [`B06`](stories/B-bridge/B06.md) | 多模態時間對齊器 | 3.0 | `B01` `B04` | ⚡ |
| [`B07`](stories/B-bridge/B07.md) | HDF5 session writer | 3.0 | `T02` `B06` | ⚡ |
| [`B08`](stories/B-bridge/B08.md) | manifest.csv 產生與維護 | 1.5 | `B07` |  |
| [`B09`](stories/B-bridge/B09.md) | Session metadata API | 1.5 | `B07` | ⚡ |
| [`B10`](stories/B-bridge/B10.md) | Session baseline 自動錄製 | 1.5 | `B09` `B06` | ⚡ |
| [`B11`](stories/B-bridge/B11.md) | Trial 狀態機（後端） | 3.0 | `B06` `B07` |  |
| [`B12`](stories/B-bridge/B12.md) | Hold-to-Record 觸發 | 1.0 | `B11` |  |
| [`B13`](stories/B-bridge/B13.md) | Auto-VAD 觸發 | 2.0 | `B11` `B15` |  |
| [`B14`](stories/B-bridge/B14.md) | WAV → MFCC 管線（低風險備援路線） | 3.0 | `T03` |  |
| [`B15`](stories/B-bridge/B15.md) | 音訊 VAD 端點偵測 | 2.0 | `B10` |  |
| [`B16`](stories/B-bridge/B16.md) | ToF VAD 與唇動先行量測 | 2.0 | `B15` `B10` |  |
| [`B17`](stories/B-bridge/B17.md) | HDF5 session 回放模式 | 2.0 | `B07` `T05` |  |
| [`B18`](stories/B-bridge/B18.md) | SENS / MEL / 解析度控制端點 | 1.0 | `A08` `A13` | 🔧 |
| [`B19`](stories/B-bridge/B19.md) | SSE 事件擴充：品質與流程狀態 | 2.0 | `B03` `B04` `B11` |  |

### C — 前端（25 個, 58 h）

| ID | 標題 | 時 | 前置 | 標記 |
|---|---|---|---|---|
| [`C01`](stories/C-frontend/C01.md) | 前端外殼：側邊欄 + 主內容區骨架 | 3.0 | `T04` `T06` |  |
| [`C02`](stories/C-frontend/C02.md) | 側邊欄模式切換器 | 2.5 | `C01` |  |
| [`C03`](stories/C-frontend/C03.md) | 模式路由與跨模式狀態保存 | 3.0 | `C02` |  |
| [`C04`](stories/C-frontend/C04.md) | 全域狀態列 | 2.0 | `C02` `B19` |  |
| [`C05`](stories/C-frontend/C05.md) | 監測模式：熱力圖遷移 | 2.0 | `C03` `T04` |  |
| [`C06`](stories/C-frontend/C06.md) | 監測模式：Δ 基線熱力圖 | 2.5 | `C05` `B10` |  |
| [`C07`](stories/C-frontend/C07.md) | 監測模式：signal rate 熱力圖 | 1.5 | `C05` `A02` |  |
| [`C08`](stories/C-frontend/C08.md) | 監測模式：Mel 頻譜瀑布圖 | 3.0 | `C03` `T05` |  |
| [`C09`](stories/C-frontend/C09.md) | 監測模式：訊號品質儀表板 | 3.0 | `C03` `B19` |  |
| [`C10`](stories/C-frontend/C10.md) | 監測模式：PCA 即時軌跡 | 3.0 | `C03` `D03` |  |
| [`C11`](stories/C-frontend/C11.md) | 錄製模式：session 設定表單 | 2.0 | `C03` `B09` `B10` | ⚡ |
| [`C12`](stories/C-frontend/C12.md) | 錄製模式：trial 提示卡與倒數 | 2.5 | `C11` `B11` `B12` | ⚡ |
| [`C13`](stories/C-frontend/C13.md) | 錄製模式：進度與 trial 清單 | 2.0 | `C12` | ⚡ |
| [`C14`](stories/C-frontend/C14.md) | 錄製模式：重錄與棄用 | 1.5 | `C13` |  |
| [`C15`](stories/C-frontend/C15.md) | 測驗模式：8 選項題目版面 | 2.5 | `C03` `B13` |  |
| [`C16`](stories/C-frontend/C16.md) | 測驗模式：三軌評分長條圖 | 3.0 | `C15` `D07` |  |
| [`C17`](stories/C-frontend/C17.md) | 測驗模式：融合權重滑桿 | 2.0 | `C16` |  |
| [`C18`](stories/C-frontend/C18.md) | 測驗模式：結果卡與信心度 | 2.0 | `C16` |  |
| [`C19`](stories/C-frontend/C19.md) | 測驗模式：輸入訊號縮圖 | 2.0 | `C15` `C08` `C10` |  |
| [`C20`](stories/C-frontend/C20.md) | 測驗模式：即時混淆矩陣 | 2.0 | `C15` `D08` |  |
| [`C21`](stories/C-frontend/C21.md) | 測驗模式：session 統計列 | 1.5 | `C20` |  |
| [`C22`](stories/C-frontend/C22.md) | 驗證模式：實驗執行器 | 3.0 | `C03` `B18` `D15` |  |
| [`C23`](stories/C-frontend/C23.md) | 驗證模式：報告檢視器 | 2.0 | `C22` `D15` |  |
| [`C24`](stories/C-frontend/C24.md) | 回放模式：播放控制列 | 2.0 | `C03` `B17` |  |
| [`C25`](stories/C-frontend/C25.md) | 設計 token 與跨模式視覺一致性 | 2.0 | `C05` `C12` `C16` `C22` `C24` |  |

### D — 分析引擎（20 個, 42 h）

| ID | 標題 | 時 | 前置 | 標記 |
|---|---|---|---|---|
| [`D01`](stories/D-analysis/D01.md) | ToF 特徵：z-score 與活躍 zone 篩選 | 2.5 | `T02` `T03` |  |
| [`D02`](stories/D-analysis/D02.md) | 音訊特徵：CMN 與長度正規化 | 2.0 | `T03` `B14` |  |
| [`D03`](stories/D-analysis/D03.md) | 特徵組裝與固定長度重採樣 | 2.0 | `D01` `D02` |  |
| [`D04`](stories/D-analysis/D04.md) | 餘弦距離基準 | 1.5 | `D03` |  |
| [`D05`](stories/D-analysis/D05.md) | DTW 距離（Sakoe-Chiba band） | 3.0 | `D04` |  |
| [`D06`](stories/D-analysis/D06.md) | 距離轉分數與拒識閾值 | 2.0 | `D04` |  |
| [`D07`](stories/D-analysis/D07.md) | 三軌評分與融合 | 2.0 | `D06` |  |
| [`D08`](stories/D-analysis/D08.md) | Enrollment 樣板管理與 LOOCV | 2.5 | `D06` `B11` |  |
| [`D09`](stories/D-analysis/D09.md) | 即時辨識服務 | 2.5 | `D07` `D08` `B06` |  |
| [`D10`](stories/D-analysis/D10.md) | 實驗 C₀：Crosstalk 分析 | 2.0 | `T02` `B07` |  |
| [`D11`](stories/D-analysis/D11.md) | 實驗 A：逐 zone SNR 分析 | 2.5 | `D01` |  |
| [`D12`](stories/D-analysis/D12.md) | 實驗 B：CV（同次戴 vs 跨次戴） | 2.0 | `B08` |  |
| [`D13`](stories/D-analysis/D13.md) | 實驗 C：Silhouette 多模態比較 | 2.5 | `D01` `D02` `D03` |  |
| [`D14`](stories/D-analysis/D14.md) | 實驗 E：Viseme 敏感度熱力圖 | 1.5 | `D13` `B16` |  |
| [`D15`](stories/D-analysis/D15.md) | 驗證報告產生器 | 2.5 | `D10` `D11` `D12` `D13` `D14` |  |
| [`D16`](stories/D-analysis/D16.md) | 互信息分析 | 1.5 | `D13` |  |
| [`D17`](stories/D-analysis/D17.md) | t-SNE 視覺化 | 2.0 | `D13` |  |
| [`D18`](stories/D-analysis/D18.md) | Permutation Test | 1.5 | `D13` |  |
| [`D19`](stories/D-analysis/D19.md) | 消融實驗套件 | 2.0 | `D16` `D18` |  |
| [`D20`](stories/D-analysis/D20.md) | 圖表樣式統一 | 1.5 | `D15` |  |

### E — 硬體實驗（8 個, 14 h）

| ID | 標題 | 時 | 前置 | 標記 |
|---|---|---|---|---|
| [`E01`](stories/E-hardware/E01.md) | Bring-up 冒煙測試（協定 v2 上機） | 2.0 | `A04` `A06` `B01` | 🔧 |
| [`E02`](stories/E-hardware/E02.md) | Crosstalk 資料蒐集 | 1.0 | `E01` `A08` `C22` | 🔧 |
| [`E03`](stories/E-hardware/E03.md) | SNR 資料蒐集 | 1.0 | `E01` `C11` `C12` | 🔧 |
| [`E04`](stories/E-hardware/E04.md) | CV 資料蒐集（20 次戴脫） | 1.5 | `E03` | 🔧 |
| [`E05`](stories/E-hardware/E05.md) | 主資料集蒐集 | 4.0 | `E03` `C13` `B08` | ⚡🔧 |
| [`E06`](stories/E-hardware/E06.md) | Enrollment 與 LOOCV 驗收 | 1.0 | `E05` `D08` `C13` | ⚡🔧 |
| [`E07`](stories/E-hardware/E07.md) | Demo 排練 ×3 | 2.0 | `E06` `C21` | ⚡🔧 |
| [`E08`](stories/E-hardware/E08.md) | 失敗備援演練 | 1.0 | `E07` `B17` `C24` | ⚡🔧 |

---

## 工期

| 人力 | 工期 | 效率 |
|---|---|---|
| 1 | 23.6 天 | 100% |
| 2 | 14.1 天 | 83% |
| 3 | 10.7 天 | 73% |
| 4 | 9.9 天 | 59% |

關鍵路徑 34.5 h（4.3 天）是硬下限，加人也不會更快。

---

## 「開發完成」不等於「有真實結果」

D 軌的實驗分析 story（`D10`–`D19`）標記為**不需要硬體**——因為它們可以用
`T02` 的假 HDF5 開發並測試。但它們要產出**真實結論**，仍然需要 E 軌先蒐集資料：

| 分析 story | 開發需要 | 真實結果需要 |
|---|---|---|
| `D10` Crosstalk | 假資料 | `E02` |
| `D11` SNR | 假資料 | `E03` |
| `D12` CV | 假資料 | `E04`（20 次戴脫） |
| `D13` Silhouette | 假資料 | `E05`（4 h 資料蒐集） |
| `D18` Permutation | 假資料 | `E05` |

這個區分讓平行化成立：**D 軌可以在 E 軌還沒開始前就全部寫完並測過**，
等資料一到，跑一個指令就有報告。

---

## 三個絕對不能省的 Story

| Story | 為什麼 |
|---|---|
| [`A01`](stories/A-firmware/A01.md) 時間戳 | 目前全系統沒有任何時間戳。沒有它，SNR / CV / Silhouette 都是在對齊誤差上做統計 |
| [`A04`](stories/A-firmware/A04.md) 取樣率修正 | `vTaskDelay(50ms)` 是 20 Hz 輪詢 vs 30 Hz 感測器 → **4×4 模式正在不規則地丟掉三分之一的幀** |
| [`C17`](stories/C-frontend/C17.md) 融合權重滑桿 | 沒有它，你的 Demo 和一支麥克風沒有區別 |

### 為什麼 C17 這麼重要

少了 sEMG 之後，剩下兩個模態裡有一個是麥克風——而麥克風本身就能做語音辨識。
如果 Demo 準確率 90% 卻全部來自麥克風，你證明的是「麥克風可以聽聲音」，
不是「ToF 可以讀唇」。

`C16` 的三軌並列 + `C17` 的權重滑桿讓這個問題**在 Demo 現場就有答案**：
把滑桿推到純 ToF，再念一次「五」——仍然對。

---

## Demo 腳本（`E07`）

| 步驟 | 操作 | 展示什麼 |
|---|---|---|
| 1 | `w=0.5` 念「五」→ 三軌都對 | 系統認得出來 |
| 2 | 滑桿推到 `w=1.0`（純 ToF）→ 再念「五」→ 仍然對 | **而且不靠聲音** |
| 3 | 用氣音念「五」→ Mel 軌崩潰、ToF 軌仍對 | **全場最強的一擊** |
| 4 | 純 ToF 念「四」→ 認不出來 | ToF 的能力邊界，所以需要多模態 |

**第 4 步主動展示失敗，反而大幅提升可信度。**
委員最怕的是看到一個永遠成功的 Demo。

---

## 目錄結構

```
ssi-backlog/
├── README.md              ← 你在這裡
├── PARALLEL_MAP.md        平行開發地圖（波次、關鍵路徑、檔案所有權）
├── CONTRACTS.md           跨軌介面契約（T01–T03 填寫並凍結）
├── schedules/
│   ├── solo.md            單人逐日排程
│   ├── parallel-2.md
│   ├── parallel-3.md      ← 建議
│   └── parallel-4.md
├── PROMPTS.md             派工提示登錄簿（開場／審查／契約變更／整合／升級）
├── tools/prompt.py        產生任一 story 的派工與審查提示
├── dag.json               依賴圖（prompt.py 讀它）
└── stories/
    ├── T-contracts/       T01–T06
    ├── A-firmware/        A01–A15
    ├── B-bridge/          B01–B19
    ├── C-frontend/        C01–C25
    ├── D-analysis/        D01–D20
    └── E-hardware/        E01–E08
```

每個 story 檔案含：軌道 / Epic / 估時 / 前置 / 阻擋 / 是否需要硬體 / 可平行對象 ·
Story 敘述 · 為什麼是這個順序 · 範圍（含**明確排除**）· 實作提示 · 驗收條件 · 完成定義
