---
name: knowledge-capture
description: URL が貼られたとき、または議事録・文字起こし・研修メモ・学びを整理したときに使う。内容を取得・分類し、書庫（library/）に決まったフォーマットで保存して索引に追記する。後回しにせずその場で保存する。
---

# 書庫（library/）へのナレッジ蓄積

URL が貼られたら、または学び・議事録を整理したら、**その場で**書庫に保存する（後回し禁止）。未来の自分が「あの時◯◯さんが言っていたことを引用したい」に応えられる形で残す。

## URL・記事の保存
- 保存先: `library/<カテゴリ>/YYYY-MM-DD-<スラッグ>.md`
- 保存後は `library/index.md` の一覧に必ず追記する。
- ログイン必須ページは、概要を一言もらってから登録する。

```markdown
---
tags: [カテゴリ, キーワード1, キーワード2]
url: https://...
date: YYYY-MM-DD
source: サイト名
related: ["[[library/index]]"]
---
# タイトル

## 概要

## メモ
```

## 議事録・講演・研修メモ（learnings）
- 対象: カンファレンス・講演の文字起こし／社外 MTG・面談の議事録／インタビュー・対談録／自分が受けた研修のメモ。
- 保存先: `library/learnings/YYYY-MM-DD-<スラッグ>.md`
- タイミング: 整理したその場で保存する。

```markdown
---
tags: [カテゴリ, 人名, キーワード]
date: YYYY-MM-DD
source: イベント名・会社名
speaker: 登壇者名
related: ["[[library/index]]"]
---
# タイトル

## エッセンス（1〜3行で言えること）

## キーメッセージ（引用に使いやすい形で）

## 詳細メモ

## 自分への応用メモ
```

## 原則
- 新規ファイルには `related:` frontmatter を付け、関連する索引・台帳へリンクする。
- 事実ベースで書く（`grasp-to-report` スキルの原則に従う）。

<!-- secretary-method-skill: knowledge-capture v0.4 -->
