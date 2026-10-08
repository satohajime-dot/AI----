# tools/（秘書メソッド同梱スクリプト）

コマンド例は `python3` 表記（Mac/Linux）。Windows では `py` に読み替える。

**前提**: これらは秘書システムのフォルダ構成（`company/secretary/todos/` と `company/*/status.md`）を前提にしています。cc-company などで秘書システムを構築済みなら、その構成がそのまま使えます。構成が無い場合はスクリプトは何もせず終了します（安全）。

| スクリプト | 目的 | 備考 |
|---|---|---|
| `check_prereqs.ps1` | **Windows用**：Python 3.9+／Node.js 18+／git が入っているかを実測（+ winget・管理者権限・実行ポリシー） | 出力は `KEY=VALUE` の英語。`RESULT=ok` なら揃っている。**Pythonが無い環境で走らせる道具なのでPythonでは書けない**（PowerShellのみ）。ASCII固定＝日本語入り.ps1がcp932誤読で落ちるのを回避 |
| `check_prereqs.sh` | **macOS/Linux用**：同じ項目を実測（+ Homebrew・Xcode CLT・sudo） | Git Bash等のWindows上シェルで走らせると `RESULT=wrong_script` を返して.ps1へ誘導する（誤った判定を出さないため） |
| `rollover_todos.py` | 直近のTODOから未完了タスクを今日のファイルへ繰り越し＋ `company/*/status.md`（自動検出）から期限近・超過タスクを浮上 | 月曜=全件棚卸し / `--all`=任意全件 / `--dry`=書き込みなし確認。セッション開始時に実行推奨 |
| `verify_goals.py` | スタンディングゴール（`company/goals/*.md` の述語）を全件再検証。FAILは冒頭で報告 | セッション開始時に毎回実行 / `--list`=一覧のみ / 結果は `company/goals/ledger.tsv` に追記 |
| `find_orphan_tasks.py` | status.md 外に埋もれた未完了タスクの監査 | 週次棚卸しで実行。`--days 30` で ⚠STALE 判定 |
| `dedup_today.py` | 今日のTODOファイルの繰り越し重複除去 | rollover が重複を作った時の応急ツール |
| `task_board.py` | 全 status.md をカード型ボードで閲覧・編集（`127.0.0.1:8765`・保存で status.md へ書き戻し） | 「タスクボードを出して」で起動（`task-board` スキル） |
| `task_id.py` | タスクボードID（行頭の6桁）→ status.md の該当行を引く。本文が書き換わっていればgit履歴から今の行まで辿る | 「タスクボードID:123456」をそのまま渡せる（`task-board` スキル） |
| `add_related_links.py` | Obsidian `related:` frontmatter の一括付与（ファイル同士のつながりを張る） | **まず `--dry-run` で確認**してから本実行。フォルダ構成から自動導出。例外は `related-map.json` で上書き |
| `nippou.py` | 日報の材料集め（当日の完了タスク・ワークログ・gitコミットを収集） | 読み取り専用。要約・提出文は秘書が作る（`daily-report` スキル） |
| `speaker_id.py` | 録音の文字起こし＋話者判別（`enroll` で声紋登録 → `transcribe` → `assign`） | **別途 ffmpeg と `pip install numpy sherpa-onnx faster-whisper` が要る**。初回は `--download-models`。完全オフライン（`voice-minutes` スキル） |
| `mail_timeline.py` | メール／チャットのやりとりを日時の昇順に並べ替え、「最後に投げたのは誰か」を出す（`--json`=スレッド取得API出力／`--text`=引用チェーン本文） | 「自分」の判定は `--me` → 環境変数 `SECRETARY_ME` → `company/secretary/me.txt` の順。事実関係を述べる前に通す |
| `quick_alert.py` | 用件から始まったセッション用の危険信号1行（期限超過・本日期限・前回のゴールFAIL・未push）。1秒以内・異常なしなら無出力 | ルール `session-start` が使う。`--selftest` あり |

備考: `rollover_todos.py` の「鮮度ログ監視」機能は既定で無効化されています（オーナー個人向け機能のため）。
