// 初回だけ実行する OAuth 認証スクリプト。使い方: `node auth.js`
//   読み取りだけ: node auth.js
//   書き込みも  : node auth.js --write
//
// 依存パッケージはゼロ。Node.js 18 以上の標準機能だけで動く。
// ローカルにリダイレクト受け取り用のサーバーを立て、認証コードを token.json に交換して保存する。
// PKCE（S256）を使うため、認証コードが漏れても第三者には交換できない。
//
// スコープ方針（既定は読み取り専用・2026-08-26 決定）:
//   - spreadsheets.readonly ... 値と数式の読み取り
//   - spreadsheets          ... 読み取り＋書き込み（--write を付けたときだけ要求する）
// Drive のスコープは要求しない。spreadsheets だけで index.js の全ツールが動くうえ、
// drive.readonly は「制限付きスコープ」で審査対象になり、必要も無い。
//
// 書き込みは --write で認証しただけでは使えない。MCP サーバー側も既定で書き込みツールを
// 隠しているので、.mcp.json の env に GS_ENABLE_WRITE=1 を入れて初めて有効になる（二重の鍵）。

import fs from "fs";
import http from "http";
import path from "path";
import crypto from "crypto";
import { spawn } from "child_process";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLIENT_SECRET_PATH = path.join(__dirname, "client_secret.json");
const TOKEN_PATH = path.join(__dirname, "token.json");

const AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";

// 3456 は mcp-google-chat の auth.js が使っているので 3457 を使う
const PORT = 3457;
// localhost ではなく 127.0.0.1 を使う。環境によって localhost が ::1（IPv6）に解決され、
// ループバック bind したサーバーに繋がらないことがある（Google もループバックIPを推奨）。
const REDIRECT_URI = `http://127.0.0.1:${PORT}`;

const WRITE = process.argv.includes("--write");
const SCOPES = WRITE
  ? ["https://www.googleapis.com/auth/spreadsheets"]
  : ["https://www.googleapis.com/auth/spreadsheets.readonly"];

if (typeof fetch !== "function") {
  console.error(
    "この認証スクリプトは Node.js 18 以上が必要です（fetch を使用）。`node --version` を確認し、Node.js を更新してください。"
  );
  process.exit(1);
}

// ---- client_secret.json ----------------------------------------------------

if (!fs.existsSync(CLIENT_SECRET_PATH)) {
  console.error(`client_secret.json が見つかりません: ${CLIENT_SECRET_PATH}`);
  console.error(
    "Google Cloud で発行した OAuth クライアントの JSON を、この名前で同じフォルダに置いてください。"
  );
  process.exit(1);
}

let conf;
try {
  const creds = JSON.parse(fs.readFileSync(CLIENT_SECRET_PATH, "utf8").replace(/^﻿/, ""));
  conf = creds.installed || creds.web || creds;
} catch (err) {
  console.error(`client_secret.json を JSON として読めませんでした: ${err.message}`);
  process.exit(1);
}
if (!conf.client_id) {
  console.error("client_secret.json に client_id がありません。ダウンロードした JSON をそのまま置いてください。");
  process.exit(1);
}

// ---- PKCE と state ---------------------------------------------------------

const base64url = (buf) => buf.toString("base64url");
const codeVerifier = base64url(crypto.randomBytes(32));
const codeChallenge = base64url(crypto.createHash("sha256").update(codeVerifier).digest());
const state = base64url(crypto.randomBytes(16));

const authUrl = new URL(AUTH_ENDPOINT);
authUrl.searchParams.set("client_id", conf.client_id);
authUrl.searchParams.set("redirect_uri", REDIRECT_URI);
authUrl.searchParams.set("response_type", "code");
authUrl.searchParams.set("scope", SCOPES.join(" "));
authUrl.searchParams.set("access_type", "offline"); // refresh_token を取得
authUrl.searchParams.set("prompt", "consent"); // 毎回同意 → refresh_token を確実に発行
authUrl.searchParams.set("code_challenge", codeChallenge);
authUrl.searchParams.set("code_challenge_method", "S256");
authUrl.searchParams.set("state", state);

// ---- ブラウザを開く（失敗しても URL を表示するので致命的ではない）---------

function openBrowser(url) {
  // GS_NO_BROWSER=1 を付けると自動で開かない（SSH 越し・検証時用）
  if (process.env.GS_NO_BROWSER === "1") return false;
  try {
    const [cmd, args] =
      process.platform === "win32"
        ? ["rundll32", ["url.dll,FileProtocolHandler", url]]
        : process.platform === "darwin"
          ? ["open", [url]]
          : ["xdg-open", [url]];
    const child = spawn(cmd, args, { detached: true, stdio: "ignore" });
    child.on("error", () => {});
    child.unref();
    return true;
  } catch (_) {
    return false;
  }
}

// ---- 認証コード → トークン交換 ---------------------------------------------

async function exchangeCodeForToken(code) {
  const body = new URLSearchParams({
    client_id: conf.client_id,
    code,
    code_verifier: codeVerifier,
    grant_type: "authorization_code",
    redirect_uri: REDIRECT_URI,
  });
  if (conf.client_secret) body.set("client_secret", conf.client_secret);

  const res = await fetch(TOKEN_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok || !data || !data.access_token) {
    const detail = data ? JSON.stringify(data) : `HTTP ${res.status} ${res.statusText}`;
    throw new Error(detail);
  }
  return data;
}

const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const html = (title, body) =>
  `<!doctype html><meta charset="utf-8"><title>${title}</title>` +
  `<div style="font-family:system-ui,sans-serif;padding:3rem;line-height:1.7">${body}</div>`;

/** 応答を書き切ってから（flush を待って）次の処理へ進む。 */
function finish(res, status, title, body, afterSent) {
  res.writeHead(status, { "Content-Type": "text/html; charset=utf-8" });
  res.end(html(title, body), () => {
    if (afterSent) afterSent();
  });
}

/** ログを出し、サーバーを閉じてから終了する（出力とHTTP応答を取りこぼさない）。 */
function shutdown(exitCode, logBeforeExit) {
  if (logBeforeExit) logBeforeExit();
  server.close(() => process.exit(exitCode));
  // close のコールバックが来ない場合の保険
  setTimeout(() => process.exit(exitCode), 2000).unref();
}

const server = http.createServer(async (req, res) => {
  const reqUrl = new URL(req.url, REDIRECT_URI);

  // ブラウザの favicon リクエスト等は無視する
  if (reqUrl.pathname !== "/") {
    res.writeHead(204);
    res.end();
    return;
  }

  const error = reqUrl.searchParams.get("error");
  const code = reqUrl.searchParams.get("code");
  const returnedState = reqUrl.searchParams.get("state");

  // **state の検証を最優先で行う。**
  // 自分が出した認証リクエスト以外（別タブの古い認証・第三者に踏ませられたURL）を
  // 受け付けないため。ここで終了させないのも意図的で、外から `/?error=...` を叩くだけで
  // 認証待ちを打ち切られないようにしている。
  if (returnedState !== state) {
    finish(res, 400, "エラー", "<h1>この認証リクエストのものではありません</h1><p>ブラウザに表示されている認証URLから開き直してください。</p>");
    console.error("state が一致しない応答を無視しました（認証待ちは続行中）。");
    return;
  }

  if (error) {
    finish(res, 400, "認証が拒否されました", `<h1>認証が拒否されました</h1><p>${escapeHtml(error)}</p>`, () =>
      shutdown(1, () => console.error("認証が拒否されました:", error))
    );
    return;
  }

  if (!code) {
    finish(res, 400, "エラー", "<h1>code パラメータがありません</h1>");
    return;
  }

  try {
    const tokens = await exchangeCodeForToken(code);

    // 応答に refresh_token が無い場合、既存の有効な refresh_token を消してはいけない
    // （消すと以降アクセストークンを更新できず、回復にはブラウザ作業が必要になる）
    let merged = tokens;
    if (!tokens.refresh_token && fs.existsSync(TOKEN_PATH)) {
      try {
        const existing = JSON.parse(fs.readFileSync(TOKEN_PATH, "utf8").replace(/^﻿/, ""));
        if (existing.refresh_token) {
          merged = { ...tokens, refresh_token: existing.refresh_token };
          console.warn("⚠ 応答に refresh_token が無かったため、既存の refresh_token を引き継ぎました。");
        }
      } catch (_) {
        /* 既存ファイルが壊れていれば引き継がない */
      }
    }

    // 一時ファイル名に pid を入れる（MCPサーバー側の更新と同時に走っても奪い合わない）
    const tmp = `${TOKEN_PATH}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(merged, null, 2), { mode: 0o600 });
    fs.renameSync(tmp, TOKEN_PATH);

    finish(res, 200, "認証完了", "<h1>認証完了！</h1><p>このタブを閉じて、ターミナルに戻ってください。</p>", () =>
      shutdown(0, () => {
        console.log("\n✅ token.json を保存しました:", TOKEN_PATH);
        if (!merged.refresh_token) {
          console.warn(
            "⚠ refresh_token がありません。Google アカウントのアクセス権を一度取り消してから再実行すると確実です。"
          );
        }
        console.log(
          WRITE
            ? "この後 Claude Code を再起動すると、スプレッドシートを読み書きできるようになります（書き込みは .mcp.json の GS_ENABLE_WRITE=1 も必要）。"
            : "この後 Claude Code を再起動すると、スプレッドシートが読めるようになります。"
        );
      })
    );
  } catch (err) {
    finish(res, 500, "エラー", `<h1>トークン取得に失敗</h1><pre>${escapeHtml(String((err && err.message) || err))}</pre>`, () =>
      shutdown(1, () => console.error("トークン取得に失敗:", (err && err.message) || err))
    );
  }
});

server.on("error", (err) => {
  if (err.code === "EADDRINUSE") {
    console.error(
      `ポート ${PORT} が使用中です。前回の認証スクリプトが残っている可能性があります。そのプロセスを終了してから再実行してください。`
    );
  } else {
    console.error("サーバーエラー:", err);
  }
  process.exit(1);
});

// 放置されたときにプロセスが残り続けないよう、待機に上限を設ける
const WAIT_LIMIT_MS = 10 * 60 * 1000;
const waitTimer = setTimeout(() => {
  console.error(`\n${WAIT_LIMIT_MS / 60000}分待っても認証が完了しなかったので終了します。もう一度 node auth.js を実行してください。`);
  process.exit(1);
}, WAIT_LIMIT_MS);
waitTimer.unref();

// **ループバックだけで待ち受ける。** 既定の全インターフェース bind だと、同一LANや
// ローカルの別サイトからリダイレクト受信口を叩けてしまう（Windowsのファイアウォール警告も出る）。
server.listen(PORT, "127.0.0.1", () => {
  const opened = openBrowser(authUrl.toString());
  console.log("=".repeat(70));
  console.log(WRITE ? "要求するスコープ: 読み取り＋書き込み（spreadsheets）" : "要求するスコープ: 読み取り専用（spreadsheets.readonly）");
  if (opened) {
    console.log("ブラウザを開きました。読み書きしたいスプレッドシートが入っているアカウントを選んで許可してください。");
    console.log("開かなかった場合は、以下の URL を手動で開いてください:\n");
  } else {
    console.log("以下の URL をブラウザで開き、Google アカウントで許可してください:");
    console.log("(読み書きしたいスプレッドシートが入っているアカウントを選択してください)\n");
  }
  console.log(authUrl.toString());
  console.log("\n許可後、このターミナルに「token.json を保存しました」と出れば完了です。");
  console.log(`(リダイレクト待機中: ${REDIRECT_URI})`);
  console.log("=".repeat(70));
});
