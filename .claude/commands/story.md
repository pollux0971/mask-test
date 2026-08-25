---
description: 開工實作一個 ssi-backlog story（讀規格、依範圍限制直接開始寫）
argument-hint: <story-id 例如 A04>
allowed-tools: Bash(python3 ssi-backlog/tools/prompt.py:*)
---

!`python3 ssi-backlog/tools/prompt.py $1`

---

以上是這個 story 的派工提示（由 tools/prompt.py 從 dag.json 產生）。

若上面沒有印出內容（$1 是空的或找不到這個 ID），改執行
`python3 ssi-backlog/tools/prompt.py --ready` 列出目前可開工的 story，
請使用者選一個 ID 重新下指令，然後停在這裡，不要繼續。

否則，請照派工提示的指示直接開始實作：
1. 依「讀取順序」把 CONTRACTS.md、story 檔案、負責的檔案讀過一遍。
2. 只在提示列出的「可修改路徑」內動手，FORBIDDEN 區塊列的路徑不要碰。
3. 完成後，照提示裡的「完成報告」格式回報（驗收條件逐項打勾、修改的檔案、
   需要人工驗證的項目、對 CONTRACTS.md 的疑問、注意到但沒動的問題）。
4. 遇到提示裡「應該停下來問」的情況（規格不清楚、需要動別人負責的檔案、
   驗收條件互相矛盾、前置條件沒做完），立刻停下來問使用者，不要自己猜。
