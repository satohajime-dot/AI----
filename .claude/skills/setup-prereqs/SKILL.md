---
name: setup-prereqs
description: 前提ソフト（Python・Node.js・git）が入っているか確認し、足りないものを導入まで伴走する。導入の最初（INSTALL.md の Step 0）で使う。あとから「スクリプトが動かない」「python が見つからないと出る」「タスクボードが開かない」と言われたときにも使う。
---

# 前提ソフトの確認と導入

この仕組みは外部ソフトに依存している。**入っていないと導入の途中で止まる**（実際に起きた）。

| ソフト | 何に使うか | 必要な版 | 区分 |
| --- | --- | --- | --- |
| **Python** | 繰り越し・ゴール検証・タスクボード・日報の各スクリプト | 3.9 以上 | **必須** |
| **git** | 「大きな変更の前に戻れる」ようにするため | 版は問わない | **必須** |
| **Node.js** | Google Chat 連携（MCPサーバー） | 18 以上 | 任意 |
| **gh**（GitHub CLI） | GitHub へのバックアップ | 版は問わない | 任意 |

**必須が2つ、任意が2つ。** 任意のものが無くても導入は先へ進められる（チェッカーも
`RESULT=ok` を返す）。その機能を使うときに入れればよい。

## 最初に必ず実測する（推測で進めない）

OS に合わせて**同梱のチェッカーを走らせる**。人に「入ってますか？」と聞くのではなく、機械で見る。

```bash
# macOS / Linux
sh tools/check_prereqs.sh

# Windows（PowerShell）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_prereqs.ps1
```

導入前でまだ `tools/` が無い場合は、解凍したキットの `payload/tools/` から直接走らせる。

出力は `KEY=VALUE` の並び（英語）。**そのまま利用者に見せず、日本語に直して伝える。**
読み方：

- `RESULT=ok` … **必須（Python・git）が揃っている。何も入れずに次へ進む**
- `RESULT=action_needed` … `MISSING=` に**必須で足りないもの**が並ぶ。ここを埋める
- `OPTIONAL_MISSING=` … 任意で足りないもの（`node` / `gh`）。**これだけなら先へ進んでよい。**
  「Google Chat を使うなら node、GitHubバックアップを使うなら gh が必要になります」と一言添えるだけにする
- `PYTHON=broken` / `NODE=broken` / `GIT=broken` … コマンドはあるが実行が失敗する（壊れた導入・PATHの残骸）。入れ直す
- `PYTHON=prerelease` / `NODE=prerelease` … 開発版（rc・beta）が入っている。安定版を入れる
- `RESULT=wrong_script` … Windows で `.sh` 側を走らせた。`.ps1` で取り直す
- `PYTHON=store_stub` … ⚠ Windows 特有。**Python は入っていない。** `python` が
  Microsoft Store のスタブに繋がっている状態（実行するとストアが開くだけ）
- `PYTHON=too_old` / `NODE=too_old` … 版が古い。入れ替えが必要
- `GIT=shim_only` … macOS で Xcode Command Line Tools の未導入シム。初回実行で
  GUI ダイアログが出る状態なので、先に CLT を入れる
- `PKGMGR=none` … パッケージマネージャが無い（後述の手動導入へ）
- `ADMIN=false` … 管理者権限なし。会社PCではこれが普通。詰まったら情報システム部門へ

## 入れる（OSごと）

**必ず1つずつ入れて、そのたびに実測で確認する。** まとめて入れて最後に確認すると、
どれが失敗したのか分からなくなる。

### Windows（`PKGMGR=winget` のとき）

```powershell
# 必須の2つ
winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements

# 任意（Google Chat 連携を使うときだけ）
winget install --id OpenJS.NodeJS.LTS -e --source winget --accept-package-agreements --accept-source-agreements
```

`MISSING=` に無いものは入れない（すでに入っているものを上書きしない）。

### Windows（`PKGMGR=none` のとき＝winget が無い／ブロックされている）

winget が使えない会社PCは珍しくない。この場合は**利用者にブラウザでダウンロードしてもらう**
（Claude は代行できない）。1つずつ、URL とクリックする場所を伝える。

- Python（必須）: <https://www.python.org/downloads/windows/> の「Windows installer (64-bit)」
  → インストーラで **「Add python.exe to PATH」に必ずチェック**（ここを外すと後で必ず詰まる）
- Git（必須）: <https://git-scm.com/download/win>
- Node.js（任意・Google Chat 連携用）: <https://nodejs.org/> の LTS 版 Windows Installer (.msi)

### macOS（`PKGMGR=brew` のとき）

```bash
# 必須の2つ
brew install python@3.12
brew install git

# 任意（Google Chat 連携を使うときだけ）
brew install node
```

`MISSING=` に無いものは入れない（すでに入っているものを上書きしない）。

### macOS（`PKGMGR=none` のとき＝Homebrew が無い）

Homebrew から入れる。先に Xcode Command Line Tools が必要（`XCODE_CLT=missing` なら）。

```bash
xcode-select --install                 # GUIダイアログが出る。利用者がクリックする
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Homebrew の導入には**管理者パスワードの入力が要る**（利用者本人が打つ）。
Apple Silicon では導入後に `eval "$(/opt/homebrew/bin/brew shellenv)"` を
`~/.zprofile` に足す案内が出るので、その指示に従う。

> macOS は `python3` が最初から入っていることも多い。チェッカーが `PYTHON=ok` なら
> Homebrew で入れ直す必要はない。

## 🔴 入れた直後に必ず引っかかること：PATH は新しいシェルにしか反映されない

**インストールしても、いま動いている Claude Code からはまだ見えない。** これが
「入れたのに動かない」の最大の原因。

1. インストールが終わったら、**Claude Code を一度終了して開き直すよう伝える**
   （Windows はターミナル自体を開き直すほうが確実）
2. 開き直したあと、**もう一度チェッカーを走らせて `RESULT=ok` を確認してから**
   「入りました」と報告する。インストーラが成功を出したことを根拠にしない

## Windows での Python の呼び方（取り違え注意）

**Windows では `python3` ではなく `py` を使う。** `python3` は Microsoft Store の
スタブに繋がっていることがあり、その場合ストアが開くだけで何も動かない。

チェッカーの `PYTHON_CMD=` に、この環境で実際に動くコマンドが入っている。導入が済んだら
**プロジェクトの `CLAUDE.md` の「パーソナライズ」欄に1行書き残す**：

```markdown
- このPCでは Python を `py` で呼ぶ（`python3` はスタブに繋がるため使わない）
```

こう書いておけば、以降のセッションで毎回取り違えずに済む。

## 権限で詰まったとき（無理に回避しない）

管理者権限が無くてインストールが弾かれたら、**そこで止めて情報システム部門への依頼文を作る**。
勝手にユーザー領域へ入れる回避策を試すと、あとで別の場所が壊れる。

依頼文には次を入れる：目的（業務効率化ツールの動作環境）／必要なソフトと版
（Python 3.9以上・Node.js 18以上・Git）／いずれも公式配布元の一般的な開発ツールであること／
インストール先のPCと利用者名。

## 完了の判定（希望的観測で言わない）

- [ ] チェッカーを**開き直したあとに**もう一度走らせて `RESULT=ok`
- [ ] `PYTHON_CMD` の値でスクリプトが実際に動く（例: `py tools/verify_goals.py --list`）
- [ ] Google Chat を使うなら `node --version` が 18 以上（使わないならここは空でよい）
- [ ] `GIT_IDENTITY=unset` なら `git config --global user.name` / `user.email` を設定する
      （未設定だと最初のコミットで失敗する）

<!-- secretary-method-skill: setup-prereqs v0.8 -->
