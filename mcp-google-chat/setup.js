// Google Chat 連携のセットアップを1コマンドで行う。
//
//   node mcp-google-chat/setup.js            足りない所だけ順に片付ける（済んでいる所は飛ばす）
//   node mcp-google-chat/setup.js --check    確認だけ（何も書き換えない・ブラウザも開かない）
//   node mcp-google-chat/setup.js --reauth   認証をやり直す（別のアカウントに替える・期限切れのとき）
//   node mcp-google-chat/setup.js --client <パス>   取り込む鍵ファイルを指定する（候補が複数見つかったとき）
//   node mcp-google-chat/setup.js --replace-mcp     .mcp.json の google-chat が別の設定のとき、正しい設定に置き換える
//
// やること:
//   1. Node.js の版
//   2. client_secret.json … 無ければダウンロードフォルダ等の client_secret*.json を探して取り込む。
//      それも無ければチュートリアル（TUTORIAL.html）を開いて止まる
//   3. .gitignore … 認証情報をコミットしない行を足す
//   4. 認証と動作確認 … token.json が無い／期限切れなら auth.js を走らせ、スペース一覧を実際に取得する。
//      失敗したら原因ごとに「どの画面で何を押すか」を出す。自分の Chat ID を people.json に登録する
//   5. Claude Code への登録 … プロジェクト直下の .mcp.json に google-chat を足す
//
// 依存パッケージはゼロ（Node.js 18 以上の標準機能だけで動く）。
// 終了コード: 0=完了 / 1=直す所がある / 2=client_secret.json が無い

import fs from "fs";
import os from "os";
import path from "path";
import { spawn, spawnSync } from "child_process";
import { fileURLToPath, pathToFileURL } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROJECT = path.dirname(HERE);
const DIR_NAME = path.basename(HERE);
const CLIENT_PATH = path.join(HERE, "client_secret.json");
const TOKEN_PATH = path.join(HERE, "token.json");
const PEOPLE_PATH = path.join(HERE, "people.json");
const TUTORIAL_PATH = path.join(HERE, "TUTORIAL.html");
const MCP_JSON_PATH = path.join(PROJECT, ".mcp.json");
const GITIGNORE_PATH = path.join(PROJECT, ".gitignore");
const LOCAL_SETTINGS_PATH = path.join(PROJECT, ".claude", "settings.local.json");
const TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";
const SERVER_NAME = "google-chat";

const rawArgs = process.argv.slice(2);
const argv = new Set(rawArgs);
const CHECK = argv.has("--check");
const REAUTH = argv.has("--reauth");
const REPLACE_MCP = argv.has("--replace-mcp");
const CLIENT_ARG = rawArgs.includes("--client") ? rawArgs[rawArgs.indexOf("--client") + 1] || "" : "";
const NET_TIMEOUT_MS = Number(process.env.GC_NET_TIMEOUT_MS) || 20000; // 通信1回の上限。プロキシや半断線で無言のまま待ち続けないため

const problems = [];
const say = (s = "") => console.log(s);
const ok = (s) => say(`  ✅ ${s}`);
const ng = (s) => {
  say(`  ❌ ${s}`);
  problems.push(s);
};
const info = (s) => say(`     ${s}`);
const stepHead = (n, s) => say(`\n[${n}/5] ${s}`);

const readJson = (file) => JSON.parse(fs.readFileSync(file, "utf8").replace(/^﻿/, ""));

function openInBrowser(target) {
  if (process.env.GC_NO_BROWSER === "1") return false;
  try {
    const [cmd, args] =
      process.platform === "win32"
        ? ["rundll32", ["url.dll,FileProtocolHandler", target]]
        : process.platform === "darwin"
          ? ["open", [target]]
          : ["xdg-open", [target]];
    const child = spawn(cmd, args, { detached: true, stdio: "ignore" });
    child.on("error", () => {});
    child.unref();
    return true;
  } catch (_) {
    return false;
  }
}

function consoleLinks(projectId) {
  const q = projectId ? `?project=${encodeURIComponent(projectId)}` : "";
  const amp = projectId ? `&project=${encodeURIComponent(projectId)}` : "";
  return {
    enable: `https://console.cloud.google.com/flows/enableapi?apiid=chat.googleapis.com${amp}`,
    configure: `https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat${q}`,
    audience: `https://console.cloud.google.com/auth/audience${q}`,
    clients: `https://console.cloud.google.com/auth/clients${q}`,
  };
}

/** client_secret.json の中身を見て、種類を返す（読めなければ null）。 */
function inspectClient(file) {
  try {
    const j = readJson(file);
    if (j.installed && j.installed.client_id) return { kind: "desktop", conf: j.installed };
    if (j.web && j.web.client_id) return { kind: "web", conf: j.web };
  } catch (_) {
    /* 壊れている・JSONでない */
  }
  return null;
}

/** ダウンロードした鍵ファイルを探す。`client_secret (1).json` のような名前も拾う。新しい順。 */
function findDownloadedClients() {
  const home = os.homedir();
  const dirs = [path.join(home, "Downloads"), path.join(home, "Desktop"), PROJECT, path.join(PROJECT, "temp")];
  const found = [];
  for (const dir of dirs) {
    let names;
    try {
      names = fs.readdirSync(dir);
    } catch (_) {
      continue;
    }
    for (const name of names) {
      if (!/^client_secret.*\.json$/i.test(name)) continue;
      const file = path.join(dir, name);
      const client = inspectClient(file);
      if (client) found.push({ file, client, mtime: fs.statSync(file).mtimeMs });
    }
  }
  return found.sort((a, b) => b.mtime - a.mtime);
}

function decodeIdToken(idToken) {
  try {
    return JSON.parse(Buffer.from(String(idToken).split(".")[1], "base64url").toString("utf8"));
  } catch (_) {
    return null;
  }
}

function runAuth() {
  say("\n     ブラウザで Google アカウントの確認画面が開きます。");
  say("     読みたい Google Chat が入っているアカウント（会社のチャットなら会社のアカウント）を選んで「許可」してください。\n");
  const r = spawnSync(process.execPath, [path.join(HERE, "auth.js")], {
    stdio: "inherit",
    env: { ...process.env, GC_FROM_SETUP: "1" },
  });
  return r.status === 0;
}

async function refreshAccess(conf) {
  let token;
  try {
    token = readJson(TOKEN_PATH);
  } catch (_) {
    return { error: "no_token" };
  }
  if (!token.refresh_token) return { error: "no_refresh_token" };
  const body = new URLSearchParams({
    client_id: conf.client_id,
    refresh_token: token.refresh_token,
    grant_type: "refresh_token",
  });
  if (conf.client_secret) body.set("client_secret", conf.client_secret);
  let res, data;
  try {
    res = await fetch(TOKEN_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
      signal: AbortSignal.timeout(NET_TIMEOUT_MS),
    });
    data = await res.json().catch(() => null);
  } catch (err) {
    return { error: "network", detail: err.message };
  }
  if (!res.ok || !data || !data.access_token) {
    return { error: (data && data.error) || `HTTP ${res.status}`, detail: data };
  }
  // token.json へは書き戻さない（MCPサーバーが同時に更新していても奪い合わない）
  return { access: data.access_token, idToken: data.id_token || token.id_token, scope: data.scope || token.scope };
}

async function listSpaces(access) {
  try {
    const res = await fetch("https://chat.googleapis.com/v1/spaces?pageSize=1000", {
      headers: { Authorization: `Bearer ${access}` },
      signal: AbortSignal.timeout(NET_TIMEOUT_MS),
    });
    return { status: res.status, data: await res.json().catch(() => null) };
  } catch (err) {
    return { status: 0, data: { error: { message: err.message } } };
  }
}

/** API エラーを「何が起きていて、どこで何を押せば直るか」に言い換える。戻り値 true=再認証で直る。 */
function explainApiError(status, data, links) {
  const e = (data && data.error) || {};
  const msg = String(e.message || "");
  const reasons = JSON.stringify(e.details || []);
  if (status === 0) {
    ng("Google Chat に接続できませんでした（ネットワーク・プロキシ・VPN を確認してください）");
    info(`詳細: ${msg}`);
    return false;
  }
  if (status === 403 && /SERVICE_DISABLED|has not been used|is disabled/i.test(msg + reasons)) {
    ng("Google Chat API が有効になっていません（Google Cloud 側の設定）");
    info(`この画面で「有効にする」を押し、1〜2分待ってから、もう一度このコマンドを実行してください:`);
    info(links.enable);
    return false;
  }
  if (status === 403 && /SCOPE_INSUFFICIENT|insufficient/i.test(msg + reasons)) {
    ng("許可された権限が足りません（古い認証が残っています）");
    return true;
  }
  if (status === 404 && /Chat app not found/i.test(msg)) {
    ng("Google Cloud 側で Chat アプリの「構成」が求められました");
    info("Chat API の「構成」タブでアプリ名・アイコンURL・説明を入れて保存してから、再実行してください:");
    info(links.configure);
    return false;
  }
  if (status === 403) {
    ng("Google Chat API に拒否されました（組織の管理者が利用を制限している可能性があります）");
    info(`詳細: ${msg || JSON.stringify(data)}`);
    info("直せない場合は、社内の Google Workspace 管理者に「Google Chat API を読み取り専用で使いたい」と相談してください。");
    return false;
  }
  ng(`Google Chat API がエラーを返しました（HTTP ${status}）`);
  info(`詳細: ${msg || JSON.stringify(data)}`);
  return false;
}

// ---------------------------------------------------------------------------

say(`Google Chat 連携のセットアップ${CHECK ? "（確認だけ・何も変えません）" : ""}`);

if (DIR_NAME !== "mcp-google-chat") {
  say(`\n  ❌ このフォルダ名が ${DIR_NAME} です。プロジェクト直下の mcp-google-chat/ に置いてから実行してください。`);
  say("     （キットの導入手順で、この部品は プロジェクト/mcp-google-chat/ にコピーされます）");
  process.exit(1);
}

// 1. Node.js ------------------------------------------------------------------
stepHead(1, "Node.js");
const major = Number(process.versions.node.split(".")[0]);
if (major >= 18 && typeof fetch === "function") {
  ok(`Node.js ${process.versions.node}`);
} else {
  ng(`Node.js ${process.versions.node} は古すぎます（18 以上が必要）`);
  info("https://nodejs.org/ から LTS 版を入れ直し、ターミナル（または Claude Code）を開き直してください。");
  process.exit(1);
}

// 2. client_secret.json --------------------------------------------------------
stepHead(2, "鍵ファイル（client_secret.json）");
let client = fs.existsSync(CLIENT_PATH) ? inspectClient(CLIENT_PATH) : null;
if (fs.existsSync(CLIENT_PATH) && !client) {
  ng("client_secret.json が壊れているか、Google の鍵ファイルではありません");
  info("Google Cloud からダウンロードした JSON を、そのまま置き直してください。");
  process.exit(1);
}
if (!client) {
  let pick = null;
  let ambiguous = false;
  if (CLIENT_ARG) {
    const file = path.resolve(CLIENT_ARG);
    const c = inspectClient(file);
    if (!c) {
      ng(`指定された鍵ファイルを読めません（Google の鍵ファイルではないか、壊れています）: ${file}`);
      process.exit(1);
    }
    pick = { file, client: c };
  } else {
    const candidates = findDownloadedClients();
    const desktops = candidates.filter((c) => c.client.kind === "desktop");
    if (desktops.length > 1) {
      // 別アプリ・別組織の鍵を黙って取り込まないよう、複数あるときは選んでもらう
      ambiguous = true;
      ng(`鍵ファイルが ${desktops.length} 個見つかりました。どれを使うか選んでください`);
      for (const c of desktops) {
        const when = new Date(c.mtime).toLocaleString("ja-JP");
        info(`・${c.file}（プロジェクト: ${c.client.conf.project_id || "不明"}・${when}）`);
      }
      info(`使うものを指定して再実行: node ${DIR_NAME}/setup.js --client "<上のパス>"`);
      if (!CHECK) process.exit(1);
    } else {
      pick = desktops[0] || candidates[0] || null;
    }
  }
  if (ambiguous) {
    /* 選んでもらうまで取り込まない（--check のときは残りの段の確認を続ける） */
  } else if (pick && pick.client.kind === "desktop") {
    if (CHECK) {
      ng(`未配置（取り込める鍵が見つかりました: ${pick.file}）`);
    } else {
      fs.copyFileSync(pick.file, CLIENT_PATH);
      try {
        fs.chmodSync(CLIENT_PATH, 0o600);
      } catch (_) {
        /* Windows では効かない */
      }
      client = pick.client;
      ok(`取り込みました: ${pick.file} → ${DIR_NAME}/client_secret.json`);
      info("元のファイルは消して構いません。");
    }
  } else if (pick) {
    client = pick.client; // 種類の判定を下で行う
    info(`見つかった鍵: ${pick.file}`);
  } else {
    ng("まだありません（Google Cloud で発行する「鍵」のファイルです）");
    info("・組織から配られた client_secret.json があれば、ダウンロードフォルダに置いてもう一度実行してください（自動で取り込みます）");
    info("・自分で発行する場合は、チュートリアルに沿って約10分です:");
    info(pathToFileURL(TUTORIAL_PATH).href);
    if (!CHECK && openInBrowser(pathToFileURL(TUTORIAL_PATH).href)) info("（チュートリアルをブラウザで開きました）");
    process.exit(2);
  }
}
if (client && client.kind === "web") {
  ng("この鍵は「ウェブアプリケーション」型です。「デスクトップアプリ」型で作り直してください");
  info("ウェブ型のままだと認証の最後で redirect_uri_mismatch になります。作り直す画面:");
  info(consoleLinks(client.conf.project_id).clients);
  process.exit(1);
}
const projectId = (client && client.conf.project_id) || "";
const links = consoleLinks(projectId);
if (client && fs.existsSync(CLIENT_PATH)) ok(`デスクトップアプリ型の鍵${projectId ? `（プロジェクト: ${projectId}）` : ""}`);

// 3. .gitignore -----------------------------------------------------------------
stepHead(3, "認証情報をコミットしない設定（.gitignore）");
const secretFiles = ["client_secret.json", "token.json"];
let gitignore = "";
try {
  gitignore = fs.readFileSync(GITIGNORE_PATH, "utf8");
} catch (_) {
  /* 無ければ作る */
}
const giLines = gitignore.split(/\r?\n/).map((s) => s.trim());
const covered = (f) => [`${DIR_NAME}/${f}`, `/${DIR_NAME}/${f}`, `**/${f}`, f].some((p) => giLines.includes(p));
const missingIgnore = secretFiles.filter((f) => !covered(f));
if (!missingIgnore.length) {
  ok("設定済み");
} else if (CHECK) {
  ng(`.gitignore に足りない行: ${missingIgnore.map((f) => `${DIR_NAME}/${f}`).join(" ")}`);
} else {
  const nl = gitignore.includes("\r\n") ? "\r\n" : "\n";
  const head = gitignore && !gitignore.endsWith("\n") ? nl : "";
  const block = ["# Google Chat の認証情報（コミットしない）", ...missingIgnore.map((f) => `${DIR_NAME}/${f}`)].join(nl);
  fs.writeFileSync(GITIGNORE_PATH, gitignore + head + block + nl);
  ok(`.gitignore に追記しました: ${missingIgnore.map((f) => `${DIR_NAME}/${f}`).join(" ")}`);
}
const tracked = spawnSync("git", ["ls-files", "--", `${DIR_NAME}/client_secret.json`, `${DIR_NAME}/token.json`], {
  cwd: PROJECT,
  encoding: "utf8",
});
if (tracked.status === 0 && tracked.stdout.trim()) {
  ng(`すでに git に記録されています: ${tracked.stdout.trim().split(/\r?\n/).join(" ")}`);
  info(`記録から外す: git rm --cached ${DIR_NAME}/client_secret.json ${DIR_NAME}/token.json（ファイル自体は残ります）`);
}

// 4. 認証と動作確認 ---------------------------------------------------------------
stepHead(4, "認証と動作確認");
if (!client || !fs.existsSync(CLIENT_PATH)) {
  info("（鍵ファイルを置いてから確認します）");
} else {
  const conf = client.conf;
  let people = { people: {} };
  try {
    people = readJson(PEOPLE_PATH);
    if (!people.people) people.people = {};
  } catch (_) {
    /* 無ければ作る */
  }
  const hasSelf = () => Object.values(people.people).some((p) => p && p.self);

  let authed = false;
  let spaces = null;
  let account = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    const needFirstAuth = !fs.existsSync(TOKEN_PATH) || (REAUTH && attempt === 0);
    if (needFirstAuth) {
      if (CHECK) {
        ng(fs.existsSync(TOKEN_PATH) ? "認証のやり直しが必要です" : "まだ認証していません");
        break;
      }
      if (!runAuth()) {
        ng("認証が完了しませんでした");
        info("よくある原因: 別のアカウントを選んだ（組織内向けの鍵は、その組織のアカウントでしか許可できない）／");
        info("「アクセスをブロック」と出た（管理者がアプリを制限している → 社内の Google Workspace 管理者へ相談）／途中で画面を閉じた");
        break;
      }
    }
    const r = await refreshAccess(conf);
    if (r.error) {
      if (r.error === "invalid_grant" || r.error === "no_refresh_token" || r.error === "no_token") {
        if (CHECK || attempt === 1) {
          ng("認証の期限が切れています（または取り消されています）");
          info("同意画面が「外部・テスト中」のままだと7日で切れます。社内だけで使うなら「内部」に:");
          info(links.audience);
          break;
        }
        info("認証の期限が切れているので、認証をやり直します。");
        if (!runAuth()) {
          ng("認証が完了しませんでした");
          break;
        }
        continue;
      }
      if (r.error === "invalid_client" || r.error === "unauthorized_client" || r.error === "deleted_client") {
        ng("鍵（client_secret.json）が無効です。発行元で削除されたか、別のプロジェクトの鍵です");
        info("新しい鍵を置き直してください（ダウンロードフォルダに置いて再実行すれば取り込みます）。");
        break;
      }
      if (r.error === "network") {
        ng("Google に接続できませんでした（ネットワーク・プロキシ・VPN を確認してください）");
        info(`詳細: ${r.detail}`);
        break;
      }
      ng(`アクセストークンを取得できませんでした: ${r.error}${r.detail ? ` ${JSON.stringify(r.detail)}` : ""}`);
      break;
    }
    const claims = r.idToken ? decodeIdToken(r.idToken) : null;
    account = claims && claims.email ? claims.email : null;
    const res = await listSpaces(r.access);
    if (res.status === 200) {
      authed = true;
      spaces = (res.data && res.data.spaces) || [];
      const more = res.data && res.data.nextPageToken ? "以上" : "";
      ok(`スペース・DM が ${spaces.length} 件${more}見えています${account ? `（アカウント: ${account}）` : ""}`);
      if (spaces.length === 0) info("0件なら、許可したアカウントが違う可能性があります → node mcp-google-chat/setup.js --reauth");
      if (claims && claims.sub) {
        const key = `users/${claims.sub}`;
        const cur = people.people[key];
        // 別のアカウントで認証し直した場合、前のアカウントを「自分」のまま残すと
        // 2人分の発言が「自分」と表示される。今の認証アカウントだけを「自分」にする。
        const staleSelf = Object.entries(people.people).filter(([k, v]) => k !== key && v && v.self);
        if (!cur || !cur.self || staleSelf.length) {
          if (CHECK) {
            ng(cur && cur.self ? "以前認証した別のアカウントも「自分」として登録されています" : "自分の ID が people.json に未登録です");
          } else {
            for (const [, v] of staleSelf) {
              delete v.self;
              if (!v.name || v.name === "自分") v.name = v.email ? `以前認証したアカウント（${v.email}）` : "以前認証したアカウント";
            }
            people.people[key] = { ...(cur || {}), name: (cur && cur.name) || "自分", self: true, ...(account ? { email: account } : {}) };
            people._説明 =
              "Google Chat の発言者ID（users/<数字>）と名前の対応表。Chat API は発言者を ID でしか返さないため、ここで名前に直す。追記例: \"users/123\": {\"name\": \"山田さん\"}";
            fs.writeFileSync(PEOPLE_PATH, JSON.stringify(people, null, 2) + "\n");
            ok("自分の発言を見分けられるよう、自分の ID を people.json に登録しました");
            if (staleSelf.length) info(`（以前認証した ${staleSelf.length} 件のアカウントは「自分」から外しました）`);
          }
        }
      } else if (!hasSelf()) {
        if (!CHECK && attempt === 0) {
          info("自分の発言を見分ける登録のため、一度だけ認証をやり直します（古い認証には自分の ID が入っていません）。");
          if (runAuth()) continue;
        }
        ng("自分の ID を取得できませんでした（node mcp-google-chat/setup.js --reauth で入ります）");
      }
      break;
    }
    const fixableByReauth = explainApiError(res.status, res.data, links);
    if (fixableByReauth && !CHECK && attempt === 0) {
      info("認証をやり直します。");
      if (runAuth()) continue;
    }
    break;
  }
  if (!authed && !problems.length) ng("動作確認ができませんでした");
}

// 5. Claude Code への登録 ----------------------------------------------------------
stepHead(5, "Claude Code への登録（.mcp.json）");
let mcp = {};
let mcpBroken = false;
if (fs.existsSync(MCP_JSON_PATH)) {
  try {
    mcp = readJson(MCP_JSON_PATH);
  } catch (_) {
    mcpBroken = true;
  }
}
const entry = { command: "node", args: [`${DIR_NAME}/index.js`] };
if (mcpBroken) {
  ng(".mcp.json が JSON として壊れているため、書き換えませんでした。次を mcpServers に足してください:");
  info(JSON.stringify({ [SERVER_NAME]: entry }));
} else {
  mcp.mcpServers = mcp.mcpServers || {};
  const cur = mcp.mcpServers[SERVER_NAME];
  // 登録があっても、このフォルダの index.js を node で起動する形でなければ繋がらない
  const pointsHere = (e) => {
    if (!e || typeof e.command !== "string" || !/(^|[\\/])node(\.exe)?$/i.test(e.command.trim())) return false;
    const target = path.join(HERE, "index.js");
    return (e.args || []).some((a) => typeof a === "string" && path.resolve(PROJECT, a) === target);
  };
  if (cur && pointsHere(cur)) {
    ok("登録済み");
  } else if (cur && (!REPLACE_MCP || CHECK)) {
    ng("google-chat は登録されていますが、このフォルダのサーバーを指していません（このままでは繋がりません）");
    info(`いまの設定: ${JSON.stringify(cur)}`);
    info(`正しい設定: ${JSON.stringify(entry)}`);
    info(`置き換えるには: node ${DIR_NAME}/setup.js --replace-mcp`);
  } else if (cur && REPLACE_MCP && !CHECK) {
    mcp.mcpServers[SERVER_NAME] = { ...cur, command: entry.command, args: entry.args };
    fs.writeFileSync(MCP_JSON_PATH, JSON.stringify(mcp, null, 2) + "\n");
    ok(`.mcp.json の google-chat を置き換えました（前の設定: ${JSON.stringify(cur)}）`);
  } else if (CHECK) {
    ng(".mcp.json に google-chat が登録されていません");
  } else {
    mcp.mcpServers[SERVER_NAME] = entry;
    fs.writeFileSync(MCP_JSON_PATH, JSON.stringify(mcp, null, 2) + "\n");
    ok(".mcp.json に登録しました");
  }
}
// 以前「使わない」を選んだ記録があると、登録しても読み込まれない
try {
  const local = readJson(LOCAL_SETTINGS_PATH);
  const disabled = Array.isArray(local.disabledMcpjsonServers) ? local.disabledMcpjsonServers : [];
  if (disabled.includes(SERVER_NAME)) {
    if (CHECK) {
      ng("以前 google-chat を「使わない」と選んだ記録が残っています（.claude/settings.local.json）");
    } else {
      local.disabledMcpjsonServers = disabled.filter((n) => n !== SERVER_NAME);
      fs.writeFileSync(LOCAL_SETTINGS_PATH, JSON.stringify(local, null, 2) + "\n");
      ok("以前「使わない」を選んだ記録を外しました");
    }
  }
} catch (_) {
  /* 設定ファイルが無い＝記録なし */
}

// まとめ -------------------------------------------------------------------------
say("\n" + "━".repeat(60));
if (problems.length) {
  say(`直す所が ${problems.length} 件あります（上の ❌）。直したら、もう一度このコマンドを実行してください:`);
  say(`  node ${DIR_NAME}/setup.js`);
  process.exit(1);
}
if (CHECK) {
  say("✅ すべて整っています。");
} else {
  say("✅ セットアップ完了。あと2つだけです:");
  say("  1. Claude Code を再起動する（ターミナル: /exit してから claude ／ VS Code: コマンドパレット →「Developer: Reload Window」）");
  say("     起動時に「google-chat を使いますか」と聞かれたら「使う」を選ぶ");
  say("  2. 「Google Chat のスペース一覧を見せて」と話しかける");
}
process.exit(0);
