---
name: github-backup
description: 蓄積した記録をGitHubのprivateリポジトリへバックアップする設定を伴走する。「バックアップを取りたい」「PCが壊れたら消えるのが不安」「GitHubに上げたい」と言われたときに使う。設定後は変更のたびにpushして、未pushをセッション開始時に検知する。
---

# GitHub への private バックアップ

`git init` だけでは**巻き戻せるが、PCが壊れたら消える**。GitHub の private リポジトリへ
push しておくと、記録（メモ・議事録・台帳）がPCの外に残る。

## 🔴 先に確認を取る（勝手に進めない）

**これは社内の情報を外部ホスト（GitHub）に置く判断である。** 技術的に可能かどうかとは別の話なので、
**必ず本人に一度確認してから進める**。確認する内容：

> このフォルダの中身（メモ・議事録・タスク）を GitHub の**非公開**リポジトリに保管します。
> 社外に公開はされませんが、**社内の情報を外部のサービスに預けることになります**。
> 会社の情報管理の方針に照らして問題ないか、ご確認いただけますか？

「分からない」と言われたら、そこで止めて情報システム部門に確認するよう案内する。
**判断を代わりにしない。** 断られた場合は `git init` によるローカルの巻き戻しだけで運用し、
それでも十分な保護になることを伝える（外部に出ない代わりにPC故障には耐えられない、と正直に言う）。

## 手順

### ステップ1: 前提の確認

`gh`（GitHub公式CLI）が必要。`setup-prereqs` のチェッカーで `GH=missing` なら入れる。

```bash
winget install --id GitHub.cli -e --source winget   # Windows
brew install gh                                      # macOS
```

winget/brew が使えなければ <https://cli.github.com/> から導入する。
入れたあとは **Claude Code を開き直す**（PATH が反映されない）。

### ステップ2: GitHub アカウントと認証（利用者のブラウザ作業）

アカウントが無ければ <https://github.com/signup> で作る（無料。private リポジトリも無料で作れる）。

認証は次のコマンドで行う。**Claude が代行できるのはコマンドの実行までで、
ブラウザでの承認は利用者本人が行う。**

```bash
gh auth login
```

対話で聞かれる項目は、**この順で答えるよう案内する**（勝手に決めない）：

1. `GitHub.com` を選ぶ
2. プロトコルは **HTTPS** を選ぶ（SSH鍵の管理が要らない）
3. 「Authenticate Git with your GitHub credentials?」→ **Yes**
   （ここを Yes にしないと push のたびに認証を聞かれる）
4. 「Login with a web browser」を選び、表示された8桁のコードをブラウザに貼る

`gh auth status` が成功を返すまで次に進まない。

### ステップ3: 🔴 上げてはいけないものが除外されているか確認する（push の前に必ず）

**ここを飛ばすと認証情報が外部に出る。** しかも `.gitignore` に足すだけでは足りない——
**すでにコミット済みのものは履歴に残っており、push すればそのまま外部へ出る。**
だから「これから上がるもの」と「すでに履歴に入っているもの」の**両方**を見る。

**3-1. `.gitignore` を確認する**（無ければ作る。既にあれば不足分だけ追記する）：

```text
mcp-google-chat/client_secret.json
mcp-google-chat/token.json
**/client_secret.json
**/token.json
.env
.env.*
*.pem
*.key
id_rsa*
.claude/settings.local.json
local/
node_modules/
__pycache__/
```

**3-2. これから上がるものを機械で確認する**：

```bash
git add -A
git status --short
```

一覧を見て、認証情報・個人情報・容量の大きいファイルが混じっていないか目で確かめる。
混じっていたら `.gitignore` に足して `git reset` してやり直す。

**3-3. 🔴 すでに履歴に入っていないかを確認する（ここが本当の関門）**：

**ファイル名だけでなく中身も見る。** 普通の名前の設定ファイルにトークンが書かれていることも、
追加後のコミットで書き足されていることもあるので、名前の検査だけでは漏れる。

まず履歴の長さを見る（`git log --oneline | wc -l`）。導入直後なら数コミットなので全部見られる。

macOS / Linux：

```bash
# ① 追加されたファイル名
git log --all --pretty=format: --name-only --diff-filter=A | sort -u | grep -Ei "client_secret|token\.json|\.env|secret|password|credential|\.pem$|\.key$|id_rsa"
# ② 履歴の中身
git log -p --all | grep -Ein "client_secret|refresh_token|access_token|api[_-]?key|BEGIN [A-Z ]*PRIVATE KEY|password[[:space:]]*[:=]"
```

Windows（PowerShell。**`grep` は無いので `Select-String` を使う**）：

```powershell
git log --all --pretty=format: --name-only --diff-filter=A | Sort-Object -Unique | Select-String -Pattern "client_secret|token\.json|\.env|secret|password|credential|\.pem$|\.key$|id_rsa"
git log -p --all | Select-String -Pattern "client_secret|refresh_token|access_token|api[_-]?key|BEGIN [A-Z ]*PRIVATE KEY|password\s*[:=]"
```

- **どちらも何も出なければ**そのまま次へ進む。
- 履歴が長くて②を流し切れない場合は、**そのことを正直に伝えて本人に判断を仰ぐ**
  （「全部は見切れていません」と言う。見たふりをしない）。
- **1件でも出たら、そこで止める。** 履歴に入ったものは `git rm --cached` では消えない。
  この時点の履歴は導入の初期コミットだけのはずなので、**作り直すのが最も安全**：

  ```bash
  # 記録そのものは消えない。git の履歴だけを捨ててやり直す
  rm -rf .git        # Windows: Remove-Item -Recurse -Force .git
  ```

  そのうえで 3-1 から順にやり直す（`.gitignore` を先に置いてから `git init`）。
  **履歴を書き換える方法（filter-branch 等）は勧めない**——手順が重く、失敗すると分かりにくい。
  もし過去の履歴に価値がある場合は、そこで止めて本人に判断を仰ぐ。

### ステップ4: private リポジトリを作って push

```bash
git init                       # まだなら
git add -A
git commit -m "初期化: 業務記録のバックアップ"
gh repo create <リポジトリ名> --private --source=. --remote=origin --push
```

`--private` を必ず付ける。リポジトリ名は他人に見えないが、分かる名前にする（例: `my-work-notes`）。

### ステップ5: 本当に private になっているか実測して確認する

**「--private を付けたから private のはず」で終わらせない。**

```bash
gh repo view --json visibility,name,url
```

`"visibility": "PRIVATE"` を目で確認してから「完了しました」と言う。
`PUBLIC` だった場合は即座に `gh repo edit --visibility private --accept-visibility-change-consequences` で直す。

### ステップ6: 未pushを毎朝検知するようにする

push を忘れると「バックアップしているつもり」の状態になる。同梱の恒久検証に載せる。

`company/goals/git-pushed.md` を次の内容で作る：

```markdown
---
name: git-pushed
description: 未pushのローカルコミットが残っていない（バックアップ漏れの検知）
check: git_unpushed
os: any
status: active
born: <今日の日付>
source: github-backup スキルによる設定
on_violation: git push する（rejectedなら git pull --no-rebase で取り込んでから）
related: ["[[company/secretary/CLAUDE]]"]
---
# git未push検知

GitHubのprivateリポジトリがバックアップ経路。push を忘れると、その分は
PCの中にしか無い状態になる。セッション開始時の検証で未pushが出たら、その場で push する。
```

作ったら `<Pythonコマンド> tools/verify_goals.py` を実行し、**このゴールが PASS すること**を確認する。

## 設定後の運用（利用者に伝えること）

- **変更したら push する。** 「バックアップして」と言えばコミットとpushをまとめて行う
- セッション開始時に未pushがあれば報告が出る（上のゴール）
- 巻き戻したいときは「◯◯の変更を戻して」と言えばよい。GitHub 上の操作は不要

## つまずきやすいところ

| 症状 | 対処 |
| --- | --- |
| `gh: command not found` | 入れた直後は PATH が未反映。Claude Code を開き直す |
| push のたびに認証を聞かれる | ステップ2の3番を Yes にしていない。`gh auth setup-git` で直す |
| `push` が rejected | 別のPCが先に push している。`git pull --no-rebase` で取り込んでから push |
| upstream 未設定と出る | `git push -u origin main` で一度紐づける |
| 大きなファイルで失敗 | GitHub は100MB超を拒否する。`.gitignore` に足して `git rm --cached` で外す |

<!-- secretary-method-skill: github-backup v0.8 -->
