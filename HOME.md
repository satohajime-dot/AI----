---
tags: [home, dashboard]
related: ["[[company/secretary/CLAUDE]]"]
---

# 🏠 ホーム

ここがすべての入口です。迷ったらここに戻ってきてください。
秘書が新しい部署・案件・重要ノートを作るたびに、このページへリンクを追記していきます。

---

## 🗂 秘書室

日々のTODO・メモ・記録の置き場です。記録したいことがあれば、まず inbox に。

- 📋 **今日のTODO** → `company/secretary/todos/YYYY-MM-DD.md`（日付別。最新へのリンクは週次で張り替える）
- 📓 **ワークログ** → `company/secretary/worklog/YYYY-MM.md`（月別。やったことの記録）
- 📥 **受信箱（迷ったメモ）** → `company/secretary/inbox/`
- 📝 **メモ・議事録・意思決定** → `company/secretary/notes/`
- [[company/goals/README|🛡 スタンディングゴール（恒久検証）]] ← 再発防止策を毎セッション機械再検証

---

## 🏢 部署

領域ごとの作業場です。各部署の `status.md` がタスクの台帳（一次置き場）になります。

- [[company/general/status|📊 general（サンプル部署）]] ← 自分の領域に合わせて名前を変える／増やす

> 部署を増やしたら、**この一覧に必ず1行追記する**（`company/CLAUDE.md` の部署表とセットで更新）。
> 案件が多い部署は、部署の中に `projects.md`（案件一覧）を作ってここからリンクすると迷わない。

---

## 📚 書庫

URL・記事・議事録・学びの蓄積場所です。貯まるほど判断の精度が上がります。

- [[library/index|📖 書庫の索引]] ← 全記事一覧。保存したら必ずここに追記される
- 議事録・講演・研修メモ → `library/learnings/`

---

## 🛠 道具

- `python3 tools/rollover_todos.py` — 未完了タスクの繰り越し＋期限接近タスクの浮上
- `python3 tools/verify_goals.py` — スタンディングゴールの再検証
- `python3 tools/find_orphan_tasks.py` — 台帳の外に埋もれたタスクの点検（週次）
- `python3 tools/nippou.py` — 日報の材料集め（「日報」と言えば秘書が使う）
- 詳しくは `tools/README.md`

---

## 📌 このページの育て方

- 新しい部署・案件・重要ノートを作ったら**その場でここに追記する**（後回しにすると埋もれる）
- 日付入りのリンク（最新TODO・当月ワークログ）は週次で最新に張り替える
- 月に一度、リンク切れ・載っていない部署がないか点検する

秘書に「**ホームを最新にして**」と言えば、この点検と更新をまとめて行います。
