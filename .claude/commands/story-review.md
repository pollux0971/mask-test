---
description: 審查一個已完成的 ssi-backlog story（照驗收條件逐項檢查，不改程式碼）
argument-hint: <story-id 例如 A04>
allowed-tools: Bash(python3 ssi-backlog/tools/prompt.py:*)
---

!`python3 ssi-backlog/tools/prompt.py $1 --review`

---

以上是這個 story 的審查提示（由 tools/prompt.py 從 dag.json 產生）。

若上面沒有印出內容（$1 是空的或找不到這個 ID），改執行
`python3 ssi-backlog/tools/prompt.py --ready` 列出目前可開工的 story，
請使用者確認要審查哪一個，然後停在這裡。

否則，請扮演審查者（不是實作者），照提示指示進行：
1. 只讀不改：CONTRACTS.md、story 檔案、以及此 story 實際改動的檔案。
2. 照驗收條件表逐條標記「通過 / 未通過 / 無法驗證」，並附證據（檔案:行號或指令輸出）。
3. 額外檢查：有沒有碰到不屬於這個 track 的檔案、有沒有做了範圍排除的項目、
   有沒有偷做下一個 story、介面是否跟 CONTRACTS.md 完全一致、
   有沒有殘留的 TODO/print/註解掉的程式碼。
4. 最後照提示裡的報告格式輸出：判定（通過/需修正/不通過）、必須修正、建議。
5. 不要動手改程式碼——只回報，除非使用者明確要求你直接修。
