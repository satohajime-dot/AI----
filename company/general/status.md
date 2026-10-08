---
updated: 2026-01-01
next_action: この部署の最初のタスクを決める（不要ならこのフォルダごと削除してよい）
next_deadline: 未定
blocker: なし
---
# general（サンプル部署）

これは `status.md`（タスク台帳）の見本です。各部署に1つ置きます。
`tools/rollover_todos.py` と `find_orphan_tasks.py` はこの形式を読み取ります。

## タスク

- [ ] サンプルタスク（このまま消して自分のタスクを書く） | 優先度: 通常 | 期限: 未定

## 使い方メモ
- 冒頭の frontmatter 3点（`next_action` / `next_deadline` / `blocker`）は必須。欠けると「管理不能」とみなす。
- 完了タスク（`- [x]`）が溜まったら `status-archive.md` へ退避する。
- 部署をコピーして増やすときは、このフォルダを複製して名前を変えるのが早い。
