// Google Chat 読み取り MCP サーバー（stdio トランスポート）。
//
// 依存パッケージはゼロ。Node.js 18 以上の標準機能（fetch など）だけで動く。
// → `npm install` もネット接続も不要で、置いた時点で動く状態になる。
//
// 認証情報はこのファイルからの相対パスで読むため、起動元の cwd に依存しない。
// 認証がまだでもサーバー自体は正常に起動し、ツール呼び出し時に「何をすればよいか」を返す
// （起動時に落とすと MCP の接続エラーになるだけで、利用者に理由が伝わらないため）。

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLIENT_SECRET_PATH = path.join(__dirname, "client_secret.json");
const TOKEN_PATH = path.join(__dirname, "token.json");
const PEOPLE_PATH = path.join(__dirname, "people.json");

const TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";
const API_BASE = "https://chat.googleapis.com/v1/";
const SERVER_VERSION = "0.8.0";
const DEFAULT_PROTOCOL_VERSION = "2025-06-18";
const SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"];

if (typeof fetch !== "function") {
  console.error(
    "このMCPサーバーは Node.js 18 以上が必要です（fetch を使用）。`node --version` を確認し、Node.js を更新してください。"
  );
  process.exit(1);
}

// ---- 認証情報の読み込み（遅延・キャッシュ付き）-----------------------------

const SETUP_HINT =
  "セットアップがまだ終わっていません。ターミナルで `node mcp-google-chat/setup.js` を実行するか、Claude に「Google Chat と繋ぎたい、手伝って」と言ってください。";

function readJsonFile(file, label) {
  let raw;
  try {
    raw = fs.readFileSync(file, "utf8");
  } catch (err) {
    if (err.code === "ENOENT") {
      throw new Error(`${label} が見つかりません: ${file}\n${SETUP_HINT}`);
    }
    throw new Error(`${label} を読めませんでした: ${file}（${err.message}）`);
  }
  try {
    return JSON.parse(raw.replace(/^﻿/, ""));
  } catch (err) {
    throw new Error(`${label} の中身が JSON として壊れています: ${file}（${err.message}）`);
  }
}

let clientConfCache = null;
function loadClientConf() {
  if (clientConfCache) return clientConfCache;
  const creds = readJsonFile(CLIENT_SECRET_PATH, "client_secret.json");
  const conf = creds.installed || creds.web || creds;
  if (!conf.client_id) {
    throw new Error(
      `client_secret.json に client_id がありません: ${CLIENT_SECRET_PATH}\nGoogle Cloud からダウンロードした JSON をそのまま置いてください。`
    );
  }
  clientConfCache = conf;
  return conf;
}

function loadToken() {
  const token = readJsonFile(TOKEN_PATH, "token.json");
  if (!token.refresh_token) {
    throw new Error(
      `token.json に refresh_token がありません: ${TOKEN_PATH}\n\`node auth.js\` をもう一度実行して認証し直してください。`
    );
  }
  return token;
}

// ---- アクセストークン（メモリキャッシュ＋ファイル永続化）-------------------

let cachedAccessToken = null;
let cachedExpiresAt = 0;
let refreshInFlight = null;

/** token.json を壊さずに置き換える（別プロセスが切り詰め途中を読まないよう一時ファイル経由）。
 *  一時ファイル名に pid を入れるのは、auth.js と同時に走っても互いの一時ファイルを
 *  奪い合わないようにするため。 */
function writeTokenAtomic(token) {
  const tmp = `${TOKEN_PATH}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(token, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, TOKEN_PATH);
}

async function getAccessToken(forceRefresh = false) {
  const now = Date.now();
  // 期限の60秒前に更新する（時計のずれと往復時間の余裕）
  if (!forceRefresh && cachedAccessToken && now < cachedExpiresAt - 60000) {
    return cachedAccessToken;
  }
  // 並行ツール呼び出しが同時に更新して token.json を奪い合わないよう1本に束ねる
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = refreshAccessToken().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

async function refreshAccessToken() {
  const now = Date.now();
  const conf = loadClientConf();
  const token = loadToken();

  const body = new URLSearchParams({
    client_id: conf.client_id,
    refresh_token: token.refresh_token,
    grant_type: "refresh_token",
  });
  if (conf.client_secret) body.set("client_secret", conf.client_secret);

  const res = await fetch(TOKEN_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  const data = await res.json().catch(() => null);

  if (!res.ok || !data || !data.access_token) {
    const detail = data ? JSON.stringify(data) : `HTTP ${res.status}`;
    let hint = "";
    if (data && data.error === "invalid_grant") {
      hint =
        "\n（認証の期限が切れています。同意画面が「外部・テスト中」だと7日で切れます。`node mcp-google-chat/setup.js` を実行すると、認証のやり直しと原因の確認まで行います。）";
    }
    throw new Error(`アクセストークンの更新に失敗しました: ${detail}${hint}`);
  }

  cachedAccessToken = data.access_token;
  cachedExpiresAt = now + (Number(data.expires_in) || 3600) * 1000;

  // 更新結果を token.json に反映しておく（refresh_token は消さない）
  try {
    // 直前に auth.js が走って refresh_token を更新している可能性があるので読み直してから合成する
    let latest = token;
    try {
      latest = readJsonFile(TOKEN_PATH, "token.json");
    } catch (_) {
      /* 読めなければ更新前の値で合成する */
    }
    const merged = { ...latest, ...data };
    if (!merged.refresh_token) merged.refresh_token = latest.refresh_token || token.refresh_token;
    writeTokenAtomic(merged);
  } catch (_) {
    // 書けなくてもこのセッションはメモリ上のトークンで動くため致命的ではない
  }

  return cachedAccessToken;
}

// ---- Google Chat REST 呼び出し --------------------------------------------

async function chatRequest(method, apiPath, options = {}, allowRetry = true) {
  const { query, body } = options;
  const url = new URL(apiPath, API_BASE);
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }

  const accessToken = await getAccessToken();
  const res = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${accessToken}`,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });

  // トークンが途中で失効した場合は一度だけ強制更新して再試行する
  if (res.status === 401 && allowRetry) {
    await getAccessToken(true);
    return chatRequest(method, apiPath, options, false);
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data ? JSON.stringify(data) : `HTTP ${res.status} ${res.statusText}`;
    let hint = "";
    if (res.status === 403) {
      hint =
        "\n（Google Cloud で Google Chat API が有効化されていない可能性があります。`node mcp-google-chat/setup.js --check` で原因と直す画面を表示できます。）";
    }
    throw new Error(`Google Chat API エラー: ${detail}${hint}`);
  }
  return data || {};
}

const SPACE_NAME_PATTERN = "^spaces/[A-Za-z0-9_-]+$";

// ---- 発言者の名前づけ ------------------------------------------------------
// Chat API は人間の発言者を `users/<数字ID>` でしか返さない（displayName が空）。ID のまま読むと、
// 自分の発言を他人の発言と取り違える事故が起きる。people.json（setup.js が自分の ID を登録する）で
// 名前に直し、分からない発言者は「未登録」とはっきり書いて返す。秘書が追記した直後から効くよう毎回読む。

function loadPeople() {
  try {
    const j = JSON.parse(fs.readFileSync(PEOPLE_PATH, "utf8").replace(/^\uFEFF/, ""));
    return (j && j.people) || {};
  } catch (_) {
    return {};
  }
}

function labelMessages(messages) {
  const people = loadPeople();
  const unknown = new Set();
  const labeled = messages.map((m) => {
    const s = m.sender || {};
    const who = people[s.name];
    let label;
    if (who && who.self) label = who.name && who.name !== "自分" ? `自分（${who.name}）` : "自分";
    else if (who && who.name) label = who.name;
    else if (s.type === "BOT") label = `${s.displayName || "アプリ"}（アプリ）`;
    else if (s.displayName) label = s.displayName;
    else {
      label = `未登録(${s.name || "不明"})`;
      if (s.name) unknown.add(s.name);
    }
    return { senderLabel: label, ...m };
  });
  const notes = [];
  if (!Object.values(people).some((p) => p && p.self)) {
    notes.push("自分の ID が未登録です。どれが自分の発言か判別できません（`node mcp-google-chat/setup.js` を実行すると登録されます）。");
  }
  if (unknown.size) {
    notes.push(
      "未登録の発言者がいます。誰の発言かを推測で決めないこと。本人に確認できたら mcp-google-chat/people.json の people に {\"users/…\": {\"name\": \"…\"}} を追記する。"
    );
  }
  return { labeled, unknown: [...unknown], notes };
}

// ---- ツール定義 ------------------------------------------------------------

// 送信ツールは既定で公開しない。「読み取り専用」を名乗る以上、
// 書き込みスコープ付きの token.json が残っていても送信できない状態が既定であるべき。
// 必要になったら .mcp.json の env に GC_ENABLE_SEND=1 を入れて明示的に有効化する。
const SEND_ENABLED = process.env.GC_ENABLE_SEND === "1";

const ALL_TOOLS = [
  {
    name: "chat_list_spaces",
    description:
      "自分が参加している Google Chat のスペース・DM 一覧を取得する。nextPageToken でページネーション可能。",
    inputSchema: {
      type: "object",
      properties: {
        pageSize: {
          type: "integer",
          minimum: 1,
          maximum: 1000,
          description: "1回で取得する件数（1〜1000）",
        },
        pageToken: { type: "string", description: "前回の応答の nextPageToken" },
        filter: {
          type: "string",
          description: '例: spaceType = "SPACE" や spaceType = "DIRECT_MESSAGE"',
        },
      },
      additionalProperties: false,
    },
    handler: async (args) => {
      const data = await chatRequest("GET", "spaces", {
        query: { pageSize: args.pageSize, pageToken: args.pageToken, filter: args.filter },
      });
      return { spaces: data.spaces || [], nextPageToken: data.nextPageToken || null };
    },
  },
  {
    name: "chat_list_messages",
    description:
      "指定スペースのメッセージ一覧を新しい順（createTime desc）で取得する。nextPageToken でページネーション可能。各メッセージの senderLabel が発言者（mcp-google-chat/people.json で ID を名前に直したもの。「（自分）」は認証した本人）。「未登録(users/…)」の発言者を推測で誰かに割り当てないこと。",
    inputSchema: {
      type: "object",
      properties: {
        spaceName: {
          type: "string",
          pattern: SPACE_NAME_PATTERN,
          description: 'スペースのリソース名。例: "spaces/AAAAxxxx"',
        },
        filter: { type: "string", description: '例: createTime > "2026-07-01T00:00:00Z"' },
        pageSize: {
          type: "integer",
          minimum: 1,
          maximum: 1000,
          description: "1回で取得する件数（1〜1000）",
        },
        pageToken: { type: "string", description: "前回の応答の nextPageToken" },
      },
      required: ["spaceName"],
      additionalProperties: false,
    },
    handler: async (args) => {
      const data = await chatRequest("GET", `${args.spaceName}/messages`, {
        query: {
          orderBy: "createTime desc",
          filter: args.filter,
          pageSize: args.pageSize,
          pageToken: args.pageToken,
        },
      });
      const { labeled, unknown, notes } = labelMessages(data.messages || []);
      const out = { messages: labeled, nextPageToken: data.nextPageToken || null };
      if (unknown.length) out.unknownSenders = unknown;
      if (notes.length) out.notes = notes;
      return out;
    },
  },
  {
    name: "chat_send_message",
    requiresSendEnabled: true,
    description:
      "指定スペースにテキストメッセージを送信する。token.json のスコープに chat.messages が含まれていないと失敗する（既定は読み取り専用）。",
    inputSchema: {
      type: "object",
      properties: {
        spaceName: {
          type: "string",
          pattern: SPACE_NAME_PATTERN,
          description: '送信先スペースのリソース名。例: "spaces/AAAAxxxx"',
        },
        text: { type: "string", description: "送信するテキスト本文" },
      },
      required: ["spaceName", "text"],
      additionalProperties: false,
    },
    handler: async (args) => {
      const data = await chatRequest("POST", `${args.spaceName}/messages`, {
        body: { text: args.text },
      });
      return { sent: true, message: data };
    },
  },
];

const TOOLS = ALL_TOOLS.filter((tool) => SEND_ENABLED || !tool.requiresSendEnabled);
const TOOLS_BY_NAME = new Map(TOOLS.map((tool) => [tool.name, tool]));
const DISABLED_TOOL_NAMES = new Set(
  ALL_TOOLS.filter((tool) => !TOOLS_BY_NAME.has(tool.name)).map((tool) => tool.name)
);

// ---- 引数の検証（zod を使わない手書きバリデータ）---------------------------

function validateArgs(tool, rawArgs) {
  const schema = tool.inputSchema;
  const props = schema.properties || {};
  const required = schema.required || [];
  // arguments 自体の未指定・null は「引数なし」として受ける（クライアント実装差の吸収）。
  // 必須引数があるツールでは下の required チェックで弾かれるので、緩めても穴にはならない。
  // 一方、個々の引数に明示的な null が入ってきた場合はスキーマと合わないのでエラーにする（後述）。
  const args = rawArgs === undefined || rawArgs === null ? {} : rawArgs;

  if (typeof args !== "object" || Array.isArray(args)) {
    throw new Error("arguments はオブジェクトで渡してください。");
  }
  for (const key of Object.keys(args)) {
    if (!props[key]) {
      const allowed = Object.keys(props).join(", ") || "なし";
      throw new Error(`使えない引数が渡されました: ${key}（使えるのは: ${allowed}）`);
    }
  }
  for (const key of required) {
    const value = args[key];
    if (value === undefined || value === null || value === "") {
      throw new Error(`必須の引数がありません: ${key}`);
    }
  }

  // 明示的に null を渡された引数は、公開しているスキーマ（string / integer）と合わないのでエラーにする
  for (const key of Object.keys(props)) {
    if (Object.prototype.hasOwnProperty.call(args, key) && args[key] === null) {
      throw new Error(`${key} に null は渡せません（省略するか値を入れてください）。`);
    }
  }

  const out = {};
  for (const [key, spec] of Object.entries(props)) {
    const value = args[key];
    if (value === undefined || value === null) continue;
    if (spec.type === "integer") {
      if (typeof value !== "number" || !Number.isInteger(value)) {
        throw new Error(`${key} は整数で渡してください（受け取った値: ${JSON.stringify(value)}）`);
      }
      if (spec.minimum !== undefined && value < spec.minimum) {
        throw new Error(`${key} は ${spec.minimum} 以上にしてください。`);
      }
      if (spec.maximum !== undefined && value > spec.maximum) {
        throw new Error(`${key} は ${spec.maximum} 以下にしてください。`);
      }
    } else if (spec.type === "string") {
      if (typeof value !== "string") {
        throw new Error(`${key} は文字列で渡してください（受け取った値: ${JSON.stringify(value)}）`);
      }
      if (spec.pattern && !new RegExp(spec.pattern).test(value)) {
        throw new Error(
          `${key} の形式が違います: ${JSON.stringify(value)}（期待する形: ${spec.pattern}）`
        );
      }
    }
    out[key] = value;
  }
  return out;
}

// ---- JSON-RPC / MCP プロトコル ---------------------------------------------

function send(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function sendResult(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function sendError(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

function negotiateProtocolVersion(requested) {
  if (typeof requested === "string" && SUPPORTED_PROTOCOL_VERSIONS.includes(requested)) {
    return requested;
  }
  return DEFAULT_PROTOCOL_VERSION;
}

async function handleToolCall(id, params) {
  const name = params && params.name;
  const tool = TOOLS_BY_NAME.get(name);
  if (!tool) {
    if (DISABLED_TOOL_NAMES.has(name)) {
      sendError(
        id,
        -32602,
        `${name} は既定で無効です（このサーバーは読み取り専用として動いています）。有効にするには .mcp.json の env に GC_ENABLE_SEND=1 を追加し、あわせて token.json のスコープに chat.messages を足してください。`
      );
      return;
    }
    sendError(id, -32602, `そのツールはありません: ${name}`);
    return;
  }

  // ツールの実行エラーは JSON-RPC エラーではなく isError 付きの結果で返す（MCP の作法）
  try {
    const args = validateArgs(tool, params && params.arguments);
    const data = await tool.handler(args);
    sendResult(id, {
      content: [{ type: "text", text: JSON.stringify(data, null, 2) }],
    });
  } catch (err) {
    sendResult(id, {
      content: [{ type: "text", text: (err && err.message) || String(err) }],
      isError: true,
    });
  }
}

/** JSON-RPC / MCP の要求 ID は文字列か整数のみ（null は仕様上不可）。 */
function isValidRequestId(id) {
  return typeof id === "string" || (typeof id === "number" && Number.isInteger(id));
}

const has = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

async function handleMessage(msg) {
  if (Array.isArray(msg)) {
    for (const one of msg) await handleMessage(one);
    return;
  }
  if (!msg || typeof msg !== "object") {
    sendError(null, -32600, "JSON-RPC のメッセージはオブジェクトで送ってください。");
    return;
  }
  if (typeof msg.method !== "string") {
    // result / error を持つものは自分が出した要求への応答。このサーバーは要求を出さないので無視する
    if (!has(msg, "result") && !has(msg, "error")) {
      sendError(isValidRequestId(msg.id) ? msg.id : null, -32600, "method がありません。");
    }
    return;
  }
  if (msg.jsonrpc !== "2.0") {
    sendError(
      isValidRequestId(msg.id) ? msg.id : null,
      -32600,
      'jsonrpc は "2.0" である必要があります。'
    );
    return;
  }
  // 通知（id フィールドそのものが無い）には応答してはいけない。notifications/initialized 等
  if (!has(msg, "id")) return;
  if (!isValidRequestId(msg.id)) {
    sendError(null, -32600, "id は文字列または整数である必要があります（null は不可）。");
    return;
  }

  const { id, method, params } = msg;

  switch (method) {
    case "initialize":
      sendResult(id, {
        protocolVersion: negotiateProtocolVersion(params && params.protocolVersion),
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "google-chat", version: SERVER_VERSION },
      });
      return;
    case "ping":
      sendResult(id, {});
      return;
    case "tools/list":
      sendResult(id, {
        tools: TOOLS.map((tool) => ({
          name: tool.name,
          description: tool.description,
          inputSchema: tool.inputSchema,
        })),
      });
      return;
    case "tools/call":
      await handleToolCall(id, params);
      return;
    default:
      sendError(id, -32601, `未対応のメソッドです: ${method}`);
      return;
  }
}

// 改行区切りの JSON-RPC を stdin から読む
let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let index;
  while ((index = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, index).trim();
    buffer = buffer.slice(index + 1);
    if (!line) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch (err) {
      sendError(null, -32700, `JSON として読めませんでした: ${err.message}`);
      continue;
    }
    handleMessage(msg).catch((err) => {
      console.error("メッセージ処理中の想定外エラー:", (err && err.message) || err);
    });
  }
});
process.stdin.on("end", () => process.exit(0));

// stdout は MCP の通信路なので、ログは必ず stderr に出す
console.error(`google-chat MCP サーバーを起動しました（stdio・依存ゼロ版 v${SERVER_VERSION}）。`);

// secretary-method-mcp: google-chat v0.8
