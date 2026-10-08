// Google スプレッドシート MCP サーバー（stdio トランスポート）。
//
// 依存パッケージはゼロ。Node.js 18 以上の標準機能（fetch など）だけで動く。
// → `npm install` もネット接続も不要で、置いた時点で動く状態になる。
//
// 叩く先は sheets.googleapis.com（枯れた通常の Sheets API v4）。
// Google 公式のリモート MCP（sheetsmcp.googleapis.com）は全ツールが 403 を返したため使わない。
// 経緯: company/dev/notes/2026-08-26-google-sheets-mcp-setup.md
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

const TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";
const API_BASE = "https://sheets.googleapis.com/v4/spreadsheets/";
const SERVER_VERSION = "0.9.0";
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
  "セットアップがまだ終わっていません。Claude に「スプレッドシートと繋ぎたい、手伝って」と言えば手順を案内します。";

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
        "\n（refresh_token が失効しています。Google アカウントでアクセス権を取り消した・パスワードを変えた等が原因です。`node auth.js` を再実行してください。）";
    }
    throw new Error(`アクセストークンの更新に失敗しました: ${detail}${hint}`);
  }

  cachedAccessToken = data.access_token;
  cachedExpiresAt = now + (Number(data.expires_in) || 3600) * 1000;

  // **token.json へ書き戻すのは refresh_token が入れ替わったときだけにする。**
  // 毎回のアクセストークン更新で書くと、利用者が `node auth.js --write` を実行した瞬間と競合し、
  // 取り直したばかりの refresh_token と書き込みスコープを古い値で上書きしてしまう
  // （README とスキルが「Claude Code を動かしたまま auth.js を走らせる」導線を案内しているので、
  //  これは理屈上の話ではなく現実に起きる。2026-08-26 Codex QA 指摘）。
  // アクセストークンはメモリに持っているので、書かないことによる不都合は
  // 「起動直後に一度だけ更新が走る」だけである。
  if (data.refresh_token && data.refresh_token !== token.refresh_token) {
    try {
      // Google が refresh_token を入れ替えてきた稀なケース。ここだけは書かないと次回起動で失効する
      const latest = readJsonFile(TOKEN_PATH, "token.json");
      writeTokenAtomic({ ...latest, ...data });
    } catch (_) {
      // 書けなくてもこのセッションはメモリ上のトークンで動くため致命的ではない
    }
  }

  return cachedAccessToken;
}

// ---- Google Sheets REST 呼び出し ------------------------------------------

async function sheetsRequest(method, apiPath, options = {}, allowRetry = true) {
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
    return sheetsRequest(method, apiPath, options, false);
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data ? JSON.stringify(data) : `HTTP ${res.status} ${res.statusText}`;
    let hint = "";
    if (res.status === 403) {
      hint =
        "\n（Google Cloud で Google Sheets API が有効化されていない／token.json のスコープが足りない可能性があります。" +
        "書き込みには spreadsheets スコープが必要です＝`node auth.js --write` で再認証してください。）";
    } else if (res.status === 404) {
      hint =
        "\n（spreadsheetId が違う／そのシートを見る権限が無い可能性があります。URL の /d/ と /edit の間の文字列か確認してください。）";
    } else if (res.status === 400) {
      hint = "\n（range のA1記法が正しいか確認してください。シート名が実在するかは sheets_get_spreadsheet で確認できます。）";
    }
    throw new Error(`Google Sheets API エラー: ${detail}${hint}`);
  }
  return data || {};
}

// スプレッドシートIDは URL の /d/ と /edit の間。記号は - と _ のみ
const SPREADSHEET_ID_PATTERN = "^[A-Za-z0-9_-]{20,}$";

// A1記法の range は「シート名!A1:D20」「シート名」「A1:D20」など幅が広く、
// シート名に日本語・空白・記号が入るため、形は縛らず非空だけ見る（誤りは API が 400 で教える）。

/** URL のパス要素として安全な形にする。
 *  encodeURIComponent は `.` を素通しするため、range が "." や ".." のときに
 *  URL の正規化でパス要素ごと消え、**別のエンドポイントを叩いてしまう**（2026-08-26 Codex QA 指摘）。
 *  ドットまで符号化しておけば、シート名に含まれるドット（例「2026.08 売上」）も安全に通る。 */
const encodePathSegment = (value) => encodeURIComponent(value).replace(/\./g, "%2E");

/** 上書きの取り消しは効かないので、1回で触れるセル数に上限を設ける（事故の規模を抑える）。 */
const MAX_WRITE_CELLS = 5000;

// ---- ツール定義 ------------------------------------------------------------

// 書き込みツールは既定で公開しない。読むつもりで入れた MCP が、
// 取り違えで本番シートを上書きしてしまう事故を構造的に防ぐため。
// 必要になったら .mcp.json の env に GS_ENABLE_WRITE=1 を入れて明示的に有効化する。
const WRITE_ENABLED = process.env.GS_ENABLE_WRITE === "1";

const ALL_TOOLS = [
  {
    name: "sheets_get_spreadsheet",
    description:
      "スプレッドシートのメタデータ（タイトル・シート名・シートID・行列数）を取得する。範囲を指定する前にシート名を知るために使う。セルの値は返さない。",
    inputSchema: {
      type: "object",
      properties: {
        spreadsheetId: {
          type: "string",
          pattern: SPREADSHEET_ID_PATTERN,
          description: "URL の /d/ と /edit の間の文字列",
        },
      },
      required: ["spreadsheetId"],
      additionalProperties: false,
    },
    handler: async (args) => {
      const data = await sheetsRequest("GET", encodePathSegment(args.spreadsheetId), {
        query: {
          fields:
            "properties(title,locale,timeZone),sheets.properties(sheetId,title,index,gridProperties)",
        },
      });
      const props = data.properties || {};
      return {
        title: props.title,
        locale: props.locale,
        timeZone: props.timeZone,
        sheets: (data.sheets || []).map((s) => s.properties),
      };
    },
  },
  {
    name: "sheets_get_values",
    description:
      "指定範囲のセルを取得する。renderOption に FORMULA を渡すと、計算結果ではなく数式そのもの（=SUM(A1:A9) 等）が返る。",
    inputSchema: {
      type: "object",
      properties: {
        spreadsheetId: {
          type: "string",
          pattern: SPREADSHEET_ID_PATTERN,
          description: "スプレッドシートID",
        },
        range: {
          type: "string",
          minLength: 1,
          description: 'A1記法。例: "シート1!A1:D20"、シート全体なら "シート1"',
        },
        renderOption: {
          type: "string",
          enum: ["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"],
          description:
            "FORMATTED_VALUE=表示どおりの文字列（既定）／UNFORMATTED_VALUE=生の数値／FORMULA=数式そのもの",
        },
        majorDimension: {
          type: "string",
          enum: ["ROWS", "COLUMNS"],
          description: "ROWS=行の配列（既定）／COLUMNS=列の配列",
        },
      },
      required: ["spreadsheetId", "range"],
      additionalProperties: false,
    },
    handler: async (args) => {
      const data = await sheetsRequest(
        "GET",
        `${encodePathSegment(args.spreadsheetId)}/values/${encodePathSegment(args.range)}`,
        {
          query: {
            valueRenderOption: args.renderOption,
            majorDimension: args.majorDimension,
          },
        }
      );
      return {
        range: data.range,
        majorDimension: data.majorDimension || "ROWS",
        values: data.values || [],
      };
    },
  },
  {
    name: "sheets_update_values",
    requiresWriteEnabled: true,
    description:
      "指定範囲のセルを上書きする。inputOption=USER_ENTERED（既定）なら '=SUM(A1:A9)' のような文字列が数式として入る。" +
      "⚠ 既存の内容を壊すうえ取り消しが効かないので、書き込み前に必ず sheets_get_values で現在値を確認すること。",
    inputSchema: {
      type: "object",
      properties: {
        spreadsheetId: {
          type: "string",
          pattern: SPREADSHEET_ID_PATTERN,
          description: "スプレッドシートID",
        },
        range: {
          type: "string",
          minLength: 1,
          description: '書き込み先のA1記法。例: "シート1!B2:B10"',
        },
        values: {
          type: "array",
          minItems: 1,
          items: { type: "array", items: { type: ["string", "number", "boolean", "null"] } },
          description: "2次元配列。行の配列で、各行がセルの配列",
        },
        inputOption: {
          type: "string",
          enum: ["USER_ENTERED", "RAW"],
          description:
            "USER_ENTERED=UIで打ったのと同じ扱い（数式・日付が解釈される・既定）／RAW=文字列のまま入る",
        },
      },
      required: ["spreadsheetId", "range", "values"],
      additionalProperties: false,
    },
    handler: async (args) => {
      const cells = args.values.reduce((sum, row) => sum + row.length, 0);
      if (cells > MAX_WRITE_CELLS) {
        throw new Error(
          `1回に書き込めるセル数の上限（${MAX_WRITE_CELLS}）を超えています: ${cells}セル。範囲を分けてください。`
        );
      }
      const data = await sheetsRequest(
        "PUT",
        `${encodePathSegment(args.spreadsheetId)}/values/${encodePathSegment(args.range)}`,
        {
          query: { valueInputOption: args.inputOption || "USER_ENTERED" },
          body: { values: args.values },
        }
      );
      return {
        updatedRange: data.updatedRange,
        updatedRows: data.updatedRows || 0,
        updatedColumns: data.updatedColumns || 0,
        updatedCells: data.updatedCells || 0,
      };
    },
  },
];

const TOOLS = ALL_TOOLS.filter((tool) => WRITE_ENABLED || !tool.requiresWriteEnabled);
const TOOLS_BY_NAME = new Map(TOOLS.map((tool) => [tool.name, tool]));
const DISABLED_TOOL_NAMES = new Set(
  ALL_TOOLS.filter((tool) => !TOOLS_BY_NAME.has(tool.name)).map((tool) => tool.name)
);

// ---- 引数の検証（zod を使わない手書きバリデータ）---------------------------

const CELL_TYPES = new Set(["string", "number", "boolean"]);

/** values（2次元配列）の中身を検証する。ここを緩めると、意図しない形のまま
 *  API に投げて分かりにくい 400 を受け取ることになる。 */
function validateCellMatrix(key, value, spec) {
  if (!Array.isArray(value)) {
    throw new Error(`${key} は2次元配列（行の配列）で渡してください。`);
  }
  const minItems = spec && spec.minItems !== undefined ? spec.minItems : 0;
  if (value.length < minItems) {
    throw new Error(`${key} が空です。書き込む行を${minItems}つ以上入れてください。`);
  }
  value.forEach((row, i) => {
    if (!Array.isArray(row)) {
      throw new Error(`${key}[${i}] は配列（1行分のセル）で渡してください。`);
    }
    row.forEach((cell, j) => {
      if (cell === null) return; // null は「このセルは飛ばす」の意味で API が受ける
      if (!CELL_TYPES.has(typeof cell)) {
        throw new Error(
          `${key}[${i}][${j}] に使えない型があります（文字列・数値・真偽値・null のみ）: ${JSON.stringify(cell)}`
        );
      }
    });
  });
}

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
    // 空配列は「値がある」ように見えて中身が無いので、型ごとの検証側で弾く
    if (value === undefined || value === null || value === "") {
      throw new Error(`必須の引数がありません: ${key}`);
    }
  }

  // 明示的に null を渡された引数は、公開しているスキーマと合わないのでエラーにする
  for (const key of Object.keys(props)) {
    if (Object.prototype.hasOwnProperty.call(args, key) && args[key] === null) {
      throw new Error(`${key} に null は渡せません（省略するか値を入れてください）。`);
    }
  }

  const out = {};
  for (const [key, spec] of Object.entries(props)) {
    const value = args[key];
    if (value === undefined || value === null) continue;
    if (spec.type === "array") {
      validateCellMatrix(key, value, spec);
    } else if (spec.type === "integer") {
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
      if (spec.minLength !== undefined && value.length < spec.minLength) {
        throw new Error(`${key} は${spec.minLength}文字以上で渡してください。`);
      }
      if (spec.pattern && !new RegExp(spec.pattern).test(value)) {
        throw new Error(
          `${key} の形式が違います: ${JSON.stringify(value)}（期待する形: ${spec.pattern}）`
        );
      }
      if (spec.enum && !spec.enum.includes(value)) {
        throw new Error(
          `${key} に使えない値です: ${JSON.stringify(value)}（使えるのは: ${spec.enum.join(", ")}）`
        );
      }
    }
    out[key] = value;
  }
  return out;
}

// ---- JSON-RPC / MCP プロトコル ---------------------------------------------

/** 応答の出口。通常は stdout へ1行ずつ書くが、バッチ処理中は配列へ集める。
 *  出口を引数で渡すのは、**バッチの応答が他のメッセージの応答と混ざらないようにする**ため
 *  （モジュール変数で切り替えると、並行して届いた単発メッセージがバッチ側へ紛れ込む）。 */
const stdoutSink = (message) => process.stdout.write(JSON.stringify(message) + "\n");

function sendResult(sink, id, result) {
  sink({ jsonrpc: "2.0", id, result });
}

function sendError(sink, id, code, message) {
  sink({ jsonrpc: "2.0", id, error: { code, message } });
}

function negotiateProtocolVersion(requested) {
  if (typeof requested === "string" && SUPPORTED_PROTOCOL_VERSIONS.includes(requested)) {
    return requested;
  }
  return DEFAULT_PROTOCOL_VERSION;
}

async function handleToolCall(sink, id, params) {
  const name = params && params.name;
  const tool = TOOLS_BY_NAME.get(name);
  if (!tool) {
    if (DISABLED_TOOL_NAMES.has(name)) {
      sendError(
        sink,
        id,
        -32602,
        `${name} は既定で無効です（このサーバーは読み取り専用として動いています）。有効にするには .mcp.json の env に GS_ENABLE_WRITE=1 を追加し、あわせて \`node auth.js --write\` で spreadsheets スコープを取り直してください。`
      );
      return;
    }
    sendError(sink, id, -32602, `そのツールはありません: ${name}`);
    return;
  }

  // ツールの実行エラーは JSON-RPC エラーではなく isError 付きの結果で返す（MCP の作法）
  try {
    const args = validateArgs(tool, params && params.arguments);
    const data = await tool.handler(args);
    sendResult(sink, id, {
      content: [{ type: "text", text: JSON.stringify(data, null, 2) }],
    });
  } catch (err) {
    sendResult(sink, id, {
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

async function handleMessage(msg, sink) {
  if (Array.isArray(msg)) {
    // バッチの入れ子は JSON-RPC 2.0 では不正（要素はオブジェクトでなければならない）
    sendError(sink, null, -32600, "バッチの中にバッチは入れられません。");
    return;
  }
  if (!msg || typeof msg !== "object") {
    sendError(sink, null, -32600, "JSON-RPC のメッセージはオブジェクトで送ってください。");
    return;
  }
  if (typeof msg.method !== "string") {
    // result / error を持つものは自分が出した要求への応答。このサーバーは要求を出さないので無視する
    if (!has(msg, "result") && !has(msg, "error")) {
      sendError(sink, isValidRequestId(msg.id) ? msg.id : null, -32600, "method がありません。");
    }
    return;
  }
  if (msg.jsonrpc !== "2.0") {
    sendError(
      sink,
      isValidRequestId(msg.id) ? msg.id : null,
      -32600,
      'jsonrpc は "2.0" である必要があります。'
    );
    return;
  }
  // 通知（id フィールドそのものが無い）には応答してはいけない。notifications/initialized 等
  if (!has(msg, "id")) return;
  if (!isValidRequestId(msg.id)) {
    sendError(sink, null, -32600, "id は文字列または整数である必要があります（null は不可）。");
    return;
  }

  const { id, method, params } = msg;

  switch (method) {
    case "initialize":
      sendResult(sink, id, {
        protocolVersion: negotiateProtocolVersion(params && params.protocolVersion),
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "google-sheets", version: SERVER_VERSION },
      });
      return;
    case "ping":
      sendResult(sink, id, {});
      return;
    case "tools/list":
      sendResult(sink, id, {
        tools: TOOLS.map((tool) => ({
          name: tool.name,
          description: tool.description,
          inputSchema: tool.inputSchema,
        })),
      });
      return;
    case "tools/call":
      await handleToolCall(sink, id, params);
      return;
    default:
      sendError(sink, id, -32601, `未対応のメソッドです: ${method}`);
      return;
  }
}

/** 届いた1件（単発またはバッチ）を処理する。
 *  バッチは **応答も1つの配列で返す**のが JSON-RPC 2.0 の要求で、個別に複数行返すのは不正。
 *  プロトコル 2025-03-26 を受け付ける宣言をしているためこの経路も仕様どおりに扱う
 *  （バッチは 2025-06-18 で廃止された。2026-08-26 Codex QA 指摘）。 */
async function handleIncoming(msg) {
  if (!Array.isArray(msg)) {
    await handleMessage(msg, stdoutSink);
    return;
  }
  if (msg.length === 0) {
    sendError(stdoutSink, null, -32600, "空のバッチは送れません。");
    return;
  }
  const collected = [];
  const collect = (message) => collected.push(message);
  for (const one of msg) await handleMessage(one, collect);
  // 中身が全部「通知」だった場合、応答は返さない（仕様）
  if (collected.length > 0) stdoutSink(collected);
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
      sendError(stdoutSink, null, -32700, `JSON として読めませんでした: ${err.message}`);
      continue;
    }
    handleIncoming(msg).catch((err) => {
      console.error("メッセージ処理中の想定外エラー:", (err && err.message) || err);
    });
  }
});

process.stdin.on("end", () => process.exit(0));

// stdout は MCP の通信路なので、ログは必ず stderr に出す
console.error(
  `google-sheets MCP サーバーを起動しました（stdio・依存ゼロ版 v${SERVER_VERSION}・書き込み${WRITE_ENABLED ? "有効" : "無効"}）。`
);

// secretary-method-mcp: google-sheets v0.9
