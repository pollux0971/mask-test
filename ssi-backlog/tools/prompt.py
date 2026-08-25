#!/usr/bin/env python3
"""
prompt.py — 為任一 story 產生可直接貼給 Claude Code agent 的派工提示。

用法:
    python3 tools/prompt.py B06              # 單一 story
    python3 tools/prompt.py B06 --review     # 驗收審查提示
    python3 tools/prompt.py --ready          # 列出當前所有「可開工」的 story
    python3 tools/prompt.py --wave 0         # 該波次全部的提示
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAG = ROOT / "dag.json"
STORIES = ROOT / "stories"

TRACK_DIR = {"T": "T-contracts", "A": "A-firmware", "B": "B-bridge",
             "C": "C-frontend", "D": "D-analysis", "E": "E-hardware"}

# ── 每軌的檔案所有權（T06）與驗證方式 ────────────────────────────
TRACK = {
    "T": {
        "name": "契約層",
        "own": ["CONTRACTS.md", "tools/", "config/"],
        "verify": "產出是文件或工具程式。文件要能讓另一條軌道照著實作而不需要問你；"
                  "工具程式要能實際跑起來。",
    },
    "A": {
        "name": "韌體",
        "own": ["vl53l7cx_test/main/"],
        "verify": "**你沒有實體板子，無法燒錄驗證。** 寫完程式碼後，"
                  "明確列出「需要人工上機驗證的項目」，不要聲稱驗收條件已通過。"
                  "可以做的是：`idf.py build` 確認編譯過、靜態檢查邏輯。",
    },
    "B": {
        "name": "主機橋接",
        "own": ["vl53l7cx_test/monitor/bridge_server.py", "host/"],
        "verify": "用 `tools/mock_device.py`（T04）產生假資料端到端測試。"
                  "純函式的部分寫 pytest。不要用真實硬體。",
    },
    "C": {
        "name": "前端",
        "own": ["vl53l7cx_test/monitor/panel/"],
        "verify": "用 mock device 餵資料，在瀏覽器實際開起來看。"
                  "**零建置**：不要引入 npm / webpack / vite，維持原生 ES module。"
                  "視覺類的驗收條件（『肉眼可見』『2 公尺外可讀』）你無法判斷，"
                  "請截圖或明確標記為「需人工確認」。",
    },
    "D": {
        "name": "分析引擎",
        "own": ["analysis/"],
        "verify": "全部是純函式，**必須寫 pytest**。用 `T02` 的假 HDF5 或人工合成資料測試。"
                  "數值類驗收條件要用具體案例驗證，不要只說「應該正確」。",
    },
    "E": {
        "name": "硬體實驗",
        "own": ["(無程式碼產出，是人工執行的實驗)"],
        "verify": "**這個 story 不能交給 agent。** 需要人本人戴上裝置操作。"
                  "agent 能做的只有：準備執行腳本、檢查清單、資料驗證程式。",
    },
}

FORBIDDEN = """## 禁止讀取（會燒光 context）

- `vl53l7cx_test/build/**` — 1589 個編譯產物
- `vl53l7cx_test/components/vl53l7cx_uld/include/vl53l7cx_buffers.h` — 22012 行的
  感測器韌體 blob，是一個巨大的 C 陣列，讀它沒有任何資訊價值
- `vl53l7cx_test/components/vl53l7cx_uld/**` — ST 官方 ULD 驅動，**唯讀，不要修改**。
  需要查 API 時只看 `include/vl53l7cx_api.h`，且用 grep 定位而非整檔讀取

如果你要搜尋整個 repo，一律加上排除：
```
--glob '!build/**' --glob '!**/vl53l7cx_buffers.h'
```
"""


def load():
    d = json.loads(DAG.read_text(encoding="utf-8"))
    return d, d["stories"]


def story_path(sid):
    return f"stories/{TRACK_DIR[sid[0]]}/{sid}.md"


def dispatch(sid, S, D):
    s = S[sid]
    t = TRACK[sid[0]]
    deps = s["deps"]
    blocks = sorted(x for x in S if sid in S[x]["deps"])

    dep_lines = "\n".join(
        f"- `{d}` {S[d]['title']} → `{story_path(d)}`" for d in deps
    ) or "- 無（可立即開始）"

    own = "\n".join(f"- `{p}`" for p in t["own"])

    cp = "\n> ⚡ **這個 story 在關鍵路徑上。** 它延遲一天，整個專案就延遲一天。\n" \
        if sid in D["critical_path"] else ""

    hw = ""
    if s["hardware"] and sid[0] != "E":
        hw = ("\n> 🔧 **這個 story 需要實體硬體驗證。** 你只能寫程式碼與編譯，"
              "最後的上機驗證必須由人執行。\n")
    if sid[0] == "E":
        hw = ("\n> 🛑 **這是人工執行的實驗 story，不適合完全交給 agent。** "
              "你能做的是準備腳本與檢查清單。\n")

    return f"""你要實作 **{sid} — {s['title']}**（{t['name']}軌，估時 {s['hours']} h）。
{cp}{hw}
## 讀取順序

1. `CONTRACTS.md` — 跨軌介面的單一事實來源。**先讀它，不要憑猜測決定介面。**
2. `{story_path(sid)}` — 這個 story 的完整規格
3. 你要改的檔案（見下方所有權）

### 前置 story（應已完成，讀它們了解上下文）

{dep_lines}

## 你只能修改這些路徑

{own}

其他目錄屬於別的軌道，**現在可能有另一個 agent 正在改**。碰了就是衝突。

{FORBIDDEN}

## 範圍紀律

- **只做 {sid} 這一個 story。** 不要「順便」做 {', '.join(f'`{b}`' for b in blocks[:3]) if blocks else '下一個'}
  ——它們的其他前置可能還沒完成。
- story 檔案裡的「不包含」章節是硬性邊界，不是建議。
- 看到相鄰的程式碼寫得不好，**不要重構**。記下來回報，不要動手。

## 驗證方式

{t['verify']}

## 完成後回報格式

```
## {sid} 完成回報

### 驗收條件
- [x] <逐條列出 story 裡的驗收條件，標明通過/未通過/需人工確認>
- [ ] <未通過的要說明原因>

### 修改的檔案
- <路徑>：<一句話說明改了什麼>

### 需要人工驗證的項目
- <你無法自行驗證的，明確列出，附上驗證方法>

### CONTRACTS.md 的疑問或建議變更
- <若無則寫「無」>

### 我注意到但沒有動的問題
- <相鄰程式碼的問題、發現的 bug，只回報不修>
```

## 遇到這些情況要停下來問我，不要自己決定

1. `CONTRACTS.md` 對你要用的介面**沒有規定或規定不清**
   → 兩個 agent 對同一個歧義做出不同決定，就是整合爆炸的來源
2. 你需要修改**不屬於你的路徑**才能完成
3. story 的驗收條件彼此矛盾，或與 `CONTRACTS.md` 衝突
4. 你發現這個 story 的前置其實沒做完
"""


def review(sid, S, D):
    s = S[sid]
    t = TRACK[sid[0]]
    return f"""你要審查 **{sid} — {s['title']}** 的實作是否真的完成。

你是審查者，不是實作者。**不要修改任何程式碼。**

## 讀取

1. `{story_path(sid)}` — 驗收條件在這裡
2. `CONTRACTS.md` — 確認實作沒有偏離契約
3. 實作者宣稱修改的檔案

{FORBIDDEN}

## 逐條核對

對 story 裡的**每一條**驗收條件：

| 條件 | 判定 | 證據 |
|---|---|---|
| <條件原文> | 通過 / 未通過 / **無法驗證** | <你在程式碼或測試裡看到的具體依據> |

「無法驗證」是合法答案——例如需要實體硬體、或需要肉眼判斷視覺效果。
**但不要把「看起來應該沒問題」當成通過。**

## 額外檢查

- [ ] 有沒有動到不屬於這條軌道的檔案？（見 `{story_path(sid)}` 的軌道與 T06 所有權）
- [ ] 有沒有做了 story「不包含」章節明確排除的事？
- [ ] 有沒有偷偷順手做了下一個 story？
- [ ] 介面是否與 `CONTRACTS.md` 完全一致（欄位名、型別、單位）？
- [ ] 錯誤處理：畸形輸入會拋例外還是優雅降級？
- [ ] 有沒有留下 TODO、註解掉的程式碼、除錯用 print？

## 回報

```
## {sid} 審查結果

判定：通過 / 需修正 / 不通過

### 驗收條件核對
<上面的表格>

### 必須修正
1. <具體到檔案與行號>

### 建議（不阻擋合併）
1. <...>
```
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sid", nargs="?", help="Story ID，例如 B06")
    ap.add_argument("--review", action="store_true", help="產生審查提示而非派工提示")
    ap.add_argument("--ready", action="store_true", help="列出零依賴、可立即開工的 story")
    ap.add_argument("--wave", type=int, help="產生某一波次全部的派工提示")
    ap.add_argument("--done", default="", help="逗號分隔的已完成 story，用於 --ready")
    a = ap.parse_args()

    D, S = load()

    if a.ready:
        done = {x.strip().upper() for x in a.done.split(",") if x.strip()}
        ready = sorted(s for s in S
                       if s not in done and all(d in done for d in S[s]["deps"]))
        print(f"可開工 ({len(ready)}):\n")
        for s in ready:
            mark = "⚡" if s in D["critical_path"] else " "
            hw = "🔧" if S[s]["hardware"] else "  "
            print(f"  {mark}{hw} {s} [{S[s]['hours']:>4.1f}h] {S[s]['title']}")
        return

    if a.wave is not None:
        ids = sorted(s for s in S if D["level"][s] == a.wave)
        for s in ids:
            print(f"\n{'='*72}\n{s}\n{'='*72}\n")
            print(dispatch(s, S, D))
        return

    if not a.sid:
        ap.error("需要 story ID，或用 --ready / --wave")
    sid = a.sid.upper()
    if sid not in S:
        sys.exit(f"找不到 story: {sid}")
    print(review(sid, S, D) if a.review else dispatch(sid, S, D))


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass   # 被 head / less 截斷是正常的
