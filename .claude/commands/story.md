---
description: 開工實作一個 ssi-backlog story（讀規格、依範圍限制直接開始寫）
argument-hint: <story-id 例如 A04>
allowed-tools: Bash(python3 ssi-backlog/tools/prompt.py:*)
---

## 派工提示

!`python3 ssi-backlog/tools/prompt.py "$ARGUMENTS" 2>&1 || python3 ssi-backlog/tools/prompt.py --ready`

---

以上是派工提示（由 `ssi-backlog/tools/prompt.py` 從 `dag.json` 產生）。

**如果上面印的是「可開工 (N):」清單**，代表沒給 story ID 或 ID 不存在。
請把清單顯示給使用者、請他選一個 ID，然後**停在這裡不要繼續**。

**否則，照派工提示直接開始實作：**

1. 依「讀取順序」把 `ssi-backlog/CONTRACTS.md`、story 檔案、你負責的檔案讀過一遍。
2. 只在「你只能修改這些路徑」內動手；「禁止讀取」的路徑不要碰。
3. 程式要**寫完整、能實際跑**，不要留空殼或 `pass`。沒有硬體無法驗證的部分，
   照實列進「需要人工驗證的項目」，**不要假裝已驗證，也不要捏造量測數字**。
4. 完成後照派工提示的「完成報告」格式回報。

## 平行開發鐵則（現在通常有多個 agent 同時在跑）

- `ssi-backlog/CONTRACTS.md` 是共用檔案。**一律用 Edit 做小範圍取代，
  絕對不要用 Write 覆寫整檔**，那會蓋掉別人剛寫的章節。每次 Edit 前先重新 Read。
- 只改屬於你這個 story 的章節，別人的章節看到也不要順手修。
- 不確定某個檔案是誰的，查 `ssi-backlog/CONTRACTS.md` 第 5 章「檔案所有權」。

## 版控

專案已是 git repo（remote：`https://github.com/pollux0971/mask-test.git`）。
**你不要自己 commit 或 push**，交給調度員統一處理，避免多個 agent 互相打架。

## 收尾

做完用 `SendMessage` 把完成回報傳給調度員 session **`esp-mask-test-ad`**。
（若該 session 不存在，就直接印在自己視窗給使用者看。）

## 遇到這些情況要停下來問調度員，不要自己決定

1. `CONTRACTS.md` 對你要用的介面沒有規定或規定不清
2. 你需要修改不屬於你的路徑才能完成
3. story 的驗收條件彼此矛盾，或與 `CONTRACTS.md` 衝突
4. 你發現這個 story 的前置其實沒做完

問法：用 `SendMessage` 傳給 `esp-mask-test-ad`，把選項和你的傾向一起寫出來，
然後停下來等回覆，不要自己猜著往下做。
