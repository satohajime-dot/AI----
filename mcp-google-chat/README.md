# mcp-google-chat（Google Chat 読み取り MCP サーバー）

Claude Code から Google Chat のスペース・メッセージを**読める**ローカル MCP サーバー。
「あのチャットで何を約束したか」を秘書が自分で確認できるようになるため、言った言わないの取りこぼしが減る。

**依存パッケージはゼロ**（Node.js 18 以上の標準機能だけで動く）。`npm install` もネット接続も不要で、置いた時点で動く状態になっている。

## いちばん簡単なつなぎ方

```bash
node mcp-google-chat/setup.js
```

これ1回で、**鍵ファイルの取り込み → `.gitignore` への追記 → 認証（ブラウザで「許可」を押すだけ）→ 実際にスペース一覧を取って動作確認 → Claude Code への登録**まで済む。済んでいる所は飛ばすので、何度実行してもよい。秘書に「Google Chat と繋ぎたい、手伝って」と言えば、`google-chat-setup` スキルがこれを実行して伴走する。

- **鍵ファイル（`client_secret.json`）を組織から受け取っている場合**は、ダウンロードフォルダに置いておくだけでよい（`client_secret (1).json` のような名前でも自動で取り込む）。Google Cloud の作業は要らない。
- **受け取っていない場合**は、`TUTORIAL.html`（ブラウザで開く図解チュートリアル・約10分）で Google Cloud から発行する。`setup.js` は鍵が見つからないとき自動でこれを開く。
- どこで止まっているか分からないときは `node mcp-google-chat/setup.js --check`（何も変えずに5段の状態と、直す画面のリンクを表示する）。
- 別のアカウントに替える・認証が切れたときは `node mcp-google-chat/setup.js --reauth`。

> **Google 公式の Chat MCP（Developer Preview）は使わない。** 承認を受けた組織でないと動かない。

## 公開ツール

| ツール | 種別 | 内容 |
| --- | --- | --- |
| `chat_list_spaces` | 読み取り | 参加中のスペース・DM 一覧（`pageSize` / `pageToken` / `filter`） |
| `chat_list_messages` | 読み取り | 指定スペースのメッセージ一覧（新しい順・`spaceName` 必須） |
| `chat_send_message` | 書き込み | 指定スペースへ送信。**既定では公開されない**（読み取り専用として動く）。使うには `.mcp.json` の `env` に `GC_ENABLE_SEND=1` を入れ、かつ下記のスコープ追加が必要 |

## スコープ（既定は読み取り専用）

- `chat.spaces.readonly`
- `chat.messages.readonly`
- `openid` / `email`（v0.8〜。認証した本人の ID とメールアドレス。`setup.js` が「自分」を `people.json` に登録し、認証したアカウントを表示するのに使う）

## 発言者の名前（people.json）

Chat API は人間の発言者を `users/<数字ID>` でしか返さない。そのまま読むと**自分の発言を他人の発言と取り違える**ため、`chat_list_messages` は各メッセージに `senderLabel` を付けて返す。

- `自分` … 認証した本人（`setup.js` が自動で登録する）
- `山田さん` など … `people.json` に登録した人
- `未登録(users/123…)` … まだ分からない人。応答の `unknownSenders` / `notes` にも出る。**推測で誰かに割り当てない。** 本人に確認できたら追記する：

```json
{
  "people": {
    "users/123456789": { "name": "山田さん" }
  }
}
```

`people.json` は毎回読み直すので、追記した直後から名前で出る（再起動は不要）。

**送信は既定で二重に無効**。有効にするには次の2つを両方行う（片方だけでは送信できない）。

1. `.mcp.json` のこのサーバーのエントリに `"env": { "GC_ENABLE_SEND": "1" }` を追加する（ツール自体が公開されない）
2. `auth.js` の `SCOPES` に `https://www.googleapis.com/auth/chat.messages` を足して `node auth.js` を再実行する（ブラウザで再同意）

過去に書き込みスコープで認証した `token.json` が残っていても、1 を入れない限り送信ツールは現れない。まずは読み取りだけで運用し、送信が本当に必要になってから足すことを勧める。

## 手作業でつなぐ場合（参考）

通常は `setup.js` と `TUTORIAL.html` で足りる。以下は中で行っていることの説明。

### A. Google Cloud 側（初回のみ・ブラウザ作業）

1. [Google Cloud Console](https://console.cloud.google.com/) にログインし、**プロジェクトを新規作成**する（既存プロジェクトでもよい）。
2. 「APIとサービス」→「ライブラリ」で **Google Chat API** を検索し、**有効にする**。
3. 同意画面（「Google Auth Platform」→「ブランディング」。以前の名前は「OAuth 同意画面」）を設定する。対象は組織内なら「内部」。外部の場合は自分をテストユーザーに追加し、続けて使うなら「アプリを公開」する（テスト中のままだと7日で認証が切れる）。
4. 「APIとサービス」→「認証情報」→「認証情報を作成」→ **OAuth クライアント ID**。
   - アプリケーションの種類：**デスクトップアプリ**
5. 作成後に表示される **JSON をダウンロード**する。

> 会社アカウントの場合、組織のポリシーでプロジェクト作成や API 有効化が制限されていることがある。
> その場合は情報システム部門に「Google Chat API を使うOAuthクライアントを発行したい」と相談する。

### B. このフォルダ側（初回のみ・ターミナル作業）

1. ダウンロードした JSON を **`client_secret.json`** という名前でこのフォルダに置く（すでに入っている場合はこの手順は不要）。
2. 認証する（Node.js 18 以上が必要。Windows でも `node` はそのまま使える。**`npm install` は不要**）：

   ```bash
   node mcp-google-chat/auth.js
   ```

   ブラウザが自動で開く（開かない場合は表示された URL を手動で開く）。**読みたい Google Chat が入っているアカウント**で許可する。
   ターミナルに「token.json を保存しました」と出れば完了。
   SSH 越しなど自動で開かせたくない場合は `GC_NO_BROWSER=1` を付けて実行する。

3. プロジェクト直下の `.mcp.json` にサーバーを登録する（無ければ作る）：

   ```json
   {
     "mcpServers": {
       "google-chat": {
         "command": "node",
         "args": ["mcp-google-chat/index.js"]
       }
     }
   }
   ```

4. **Claude Code を再起動**し、「参加しているチャットのスペースを一覧して」と頼んで動作を確認する。

## 秘密情報の扱い（重要）

- `client_secret.json` と `token.json` は**認証情報そのもの**。`.gitignore` に必ず追加し、リポジトリにコミットしない。
- ファイルは `0600` で書くが、**Windows ではこの権限指定は効かない**（ACLは変わらない）。同じPCの他ユーザーからは読めるので、PC自体のログイン保護で守ること。
- git で複数台のPCを同期している場合、**この2つは同期されない**（＝各PCで認証を1回ずつ行う必要がある）。共有されるのはコード（`index.js` / `auth.js` / `package.json`）だけ。
- 認証情報を他人に渡さない。渡すとそのアカウントのチャットが読める状態になる。

## 構成

- `index.js` … MCP サーバー本体（stdio・JSON-RPC を直接実装）。認証情報はこのファイルからの相対パスで読むため、実行時のカレントディレクトリに依存しない
- `setup.js` … 1コマンドのセットアップ（確認・鍵の取り込み・認証・動作確認・登録）。`--check` で確認だけ、`--reauth` で認証のやり直し
- `TUTORIAL.html` … Google Cloud で鍵を発行する図解チュートリアル（ブラウザで開く。各画面への直リンク付き）
- `people.json` … 発言者IDと名前の対応表（`setup.js` が作る。認証情報ではないが、社内の人名が入る）
- `auth.js` … 初回 OAuth 認証（PKCE + `state` 検証。`http://127.0.0.1:3456` を**ループバックのみで**待ち受けてリダイレクトを受信する。10分で自動終了）
- `package.json` … `type: module` の宣言のみ。**dependencies は無い**
- ポート 3456 が使われている場合は `auth.js` の `PORT` を変更して再実行する
- **認証が未完了でもサーバーは正常に起動する。** ツールを呼んだときに「何をすればよいか」を返す設計（起動時に落とすと MCP の接続エラーになるだけで理由が伝わらないため）

## うまくいかないとき

| 症状 | 確認すること |
| --- | --- |
| `client_secret.json が見つかりません` | ファイル名が正確か（`client_secret (1).json` などになっていないか）・置き場所がこのフォルダか |
| ブラウザで「このアプリは確認されていません」 | 自分が作ったクライアントなので「詳細」→続行で進んでよい。外部種別ならテストユーザーに自分を追加 |
| 認証は通るがスペースが0件 | 許可したアカウントを間違えている可能性。`node mcp-google-chat/setup.js --reauth` |
| API が 403 で拒否される | Chat API が有効になっていない可能性が高い。`node mcp-google-chat/setup.js --check` が原因と直す画面を表示する（読み取りだけなら Chat API の「構成」タブの設定は不要） |
| 7日ほどで急に読めなくなる | 同意画面が「外部・テスト中」。「内部」にするか「アプリを公開」してから `setup.js` を再実行 |
| Claude Code にツールが出てこない | `.mcp.json` の登録内容とパス、そして**再起動**したか |
| `Node.js 18 以上が必要です` と出る | `node --version` を確認する。18 未満なら Node.js を更新する（このサーバーは標準の `fetch` を使う） |
| `chat_send_message` が出てこない／失敗する | 既定で無効。`GC_ENABLE_SEND=1` とスコープ追加の**両方**が必要（上記） |
