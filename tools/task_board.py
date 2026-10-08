#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_board.py — 全部署 status.md のタスクをカード型ボードで一覧・編集・追加・完了。

「タスクリスト出して」用のローカルタスクボード。
  - http://127.0.0.1:8765 を開く
  - 部署見出し → PJ(セクション)ごとの色付きカード（「HC のタスク一覧」等）
  - 各行: ID(クリックで「タスクボードID:NNNNNN」をコピー) ｜ 優先度 ｜ タスク(✎編集) ｜ 期限(📅) ｜ ステータス(未着手/取組中/保留中) ｜ done(☑)
  - ツールバー: 🔍検索（動的）・PJ/優先度/状態/期限フィルタ・並び替え
  - 右上「保存 (N)」で 完了/編集/状態/追加 をまとめて反映し1コミット

使い方:
  py tools/task_board.py [--no-open] [--no-commit] [--dump] [--port 9000]

設計メモ:
  - 依存ライブラリなし（http.server / 標準ライブラリのみ）
  - 完了/編集の同定は (ファイル,行番号,行テキスト完全一致)。ズレたら安全にスキップ
  - 追加は見出し行番号(secline)で厳密同定し末尾へ挿入（in-place編集の後に処理）
  - 改行(CRLF/LF)・末尾改行を温存。非UTF-8は弾く。書換えは現存 company/**/status.md 限定
  - 入力の改行/パイプはサニタイズ。保存はロックで直列化。127.0.0.1 のみbind
  - ソート/フィルタ/検索は全てクライアント側（DOM操作）。サーバーは全件返すだけ
"""
import sys
import re
import json
import html
import hashlib
import argparse
import datetime
import threading
import subprocess
import webbrowser
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
COMPANY = ROOT / "company"

CHECKBOX = re.compile(r"^(\s*)-\s*\[ \]\s*(.+?)\s*$")
HEADING = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
UPDATED = re.compile(r"^updated:\s*")
INDENT = re.compile(r"^(\s*)")
DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
PRI_OPTS = ["", "最高", "高", "通常", "中", "低"]
STATUS_OPTS = ["未着手", "取組中", "保留中"]
DEFAULT_STATUS = "未着手"

# 部署の表示名・並び順・色は任意設定。空でも動く（フォルダ名がそのまま表示名になり、
# 未指定の色は既定色になる）。自分の部署に合わせて埋めると見やすくなる。
# 例) DEPT_LABELS = {"sales": "営業", "dev": "開発"}
#     DEPT_ORDER  = ["営業", "開発"]
#     DEPT_COLORS = {"営業": "#69a7ff", "開発": "#5ad19a"}
DEPT_LABELS = {}
DEPT_ORDER = []
DEPT_COLORS = {}

_SAVE_LOCK = threading.Lock()


def _clean(s, allow_pipe=True):
    s = str(s).replace("\r", " ").replace("\n", " ").strip()
    if not allow_pipe:
        s = s.replace("|", "／").replace("｜", "／")
    return s


def A(s):
    return html.escape(str(s), quote=True)


def dept_of(status_path: Path) -> str:
    rel = status_path.relative_to(COMPANY)
    key = rel.parts[-2] if len(rel.parts) >= 3 else rel.parts[0]
    return DEPT_LABELS.get(key, key)


def read_lines(path: Path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    nl = "\r\n" if "\r\n" in text else "\n"
    return text, nl, text.split(nl)


def parse_task(raw_label: str):
    label = raw_label.replace("**", "")
    short = re.split(r"\s*[\|｜]\s*", label)[0].strip()
    pri = due = status = ""
    m = re.search(r"優先度:\s*([^\|｜]+)", label)
    if m:
        pri = m.group(1).strip()
    m = re.search(r"期限:\s*([^\|｜]+)", label)
    if m:
        due = m.group(1).strip()
    m = re.search(r"状態:\s*([^\|｜]+)", label)
    if m:
        status = m.group(1).strip()
    return short, pri, due, (status or DEFAULT_STATUS)


TASK_META = re.compile(r"^(優先度|期限|状態|完了):")


def task_id(content: str) -> str:
    """タスクボードID（6桁）。チェックボックス後の本文だけから決める。

    優先度・期限・状態・完了印・PJ移動・行番号では変わらない。本文を書き換えると変わる
    （旧IDからの引き当ては tools/task_id.py が git 履歴まで追う）。
    """
    segs = [s.strip() for s in re.split(r"\s*[\|｜]\s*", content.replace("**", ""))]
    body = " ".join(" | ".join(s for s in segs if s and not TASK_META.match(s)).split())
    return str(100000 + int(hashlib.sha1(body.encode("utf-8")).hexdigest(), 16) % 900000)


def split_for_edit(raw: str):
    m = CHECKBOX.match(raw)
    content = m.group(2) if m else raw.strip()
    body, pri, due, status = [], "", "", ""
    for s in re.split(r"\s*[\|｜]\s*", content):
        sm = re.match(r"^優先度:\s*(.+)$", s)
        dm = re.match(r"^期限:\s*(.+)$", s)
        tm = re.match(r"^状態:\s*(.+)$", s)
        if sm:
            pri = sm.group(1).strip()
        elif dm:
            due = dm.group(1).strip()
        elif tm:
            status = tm.group(1).strip()
        elif s.strip():
            body.append(s.strip())
    return " | ".join(body), pri, due, status


def build_line(indent, text, pri, due, status="", checked=False):
    line = f"{indent}{'- [x]' if checked else '- [ ]'} {text}".rstrip()
    if pri:
        line += f" | 優先度: {pri}"
    if due:
        line += f" | 期限: {due}"
    if status and status != DEFAULT_STATUS:
        line += f" | 状態: {status}"
    return line


def insert_in_section(lines, section_text, secline, new_line) -> bool:
    h_idx = None
    if isinstance(secline, int) and 1 <= secline <= len(lines):
        h = HEADING.match(lines[secline - 1])
        if h and h.group(2).strip() == section_text:
            h_idx = secline - 1
    if h_idx is None:
        for i, ln in enumerate(lines):
            h = HEADING.match(ln)
            if h and h.group(2).strip() == section_text:
                h_idx = i
                break
    if h_idx is None:
        return False
    end = len(lines)
    for j in range(h_idx + 1, len(lines)):
        if HEADING.match(lines[j]):
            end = j
            break
    ins = h_idx + 1
    for j in range(h_idx + 1, end):
        if lines[j].strip() != "":
            ins = j + 1
    lines.insert(ins, new_line)
    return True


def has_heading(lines, section_text):
    for l in lines:
        if l is None:
            continue
        h = HEADING.match(l)
        if h and h.group(2).strip() == section_text:
            return True
    return False


def collect():
    depts = {}
    for sf in sorted(COMPANY.rglob("status.md")):
        dept = dept_of(sf)
        try:
            _, _, lines = read_lines(sf)
        except (OSError, UnicodeDecodeError):
            continue
        section, sec_line = "", 0
        items = []
        for i, line in enumerate(lines, start=1):
            h = HEADING.match(line)
            if h:
                # PJ(カード)は大きな括り＝見出しレベル≦3(##/###)だけ。####以下(≧4)は親PJに畳む
                if len(h.group(1)) <= 3:
                    section = h.group(2).strip()
                    sec_line = i
                continue
            m = CHECKBOX.match(line)
            if not m:
                continue
            short, pri, due, status = parse_task(m.group(2))
            ebody, epri, edue, _ = split_for_edit(line)
            items.append({
                "file": str(sf.relative_to(ROOT)).replace("\\", "/"), "tid": task_id(m.group(2)),
                "lineno": i, "raw": line, "label": short, "pri": pri, "due": due, "status": status,
                "section": section, "secline": sec_line, "indent": len(m.group(1)),
                "ebody": ebody, "epri": epri, "edue": edue,
            })
        if items:
            depts.setdefault(dept, []).extend(items)
    ordered = {d: depts[d] for d in DEPT_ORDER if d in depts}
    for d in depts:
        ordered.setdefault(d, depts[d])
    return ordered


# ---------- 保存: 完了 / 編集・状態 / 追加 をまとめて適用し1コミット ----------

def apply_ops(ops, do_commit=True):
    with _SAVE_LOCK:
        return _apply_ops(ops, do_commit)


def _apply_ops(ops, do_commit=True):
    today = datetime.date.today().isoformat()
    valid = {str(sf.relative_to(ROOT)).replace("\\", "/") for sf in COMPANY.rglob("status.md")}
    company_res = COMPANY.resolve()

    files = {}  # f -> {"nl","lines","p","dirty"} or None（対象外）

    def load(f):
        if f in files:
            return files[f]
        if not isinstance(f, str) or f not in valid:
            files[f] = None
            return None
        p = ROOT / f
        try:
            ok = (company_res in p.resolve().parents) and p.exists()
        except OSError:
            ok = False
        if not ok:
            files[f] = None
            return None
        try:
            _, nl, lines = read_lines(p)
        except (OSError, UnicodeDecodeError):
            files[f] = None
            return None
        files[f] = {"nl": nl, "lines": lines, "p": p, "dirty": False}
        return files[f]

    applied, skipped = [], []
    counts = {"done": 0, "edit": 0, "add": 0, "move": 0}

    # Stage A: in-place（完了 / 編集・状態）— 行数不変なので行番号は有効
    for op in ops:
        t = op.get("type")
        if t not in ("done", "edit"):
            continue
        fo = load(op.get("file", ""))
        if not fo:
            skipped.append((op.get("text") or op.get("raw") or "?")[:50])
            continue
        lines = fo["lines"]
        ln = op.get("lineno", 0) - 1
        raw = op.get("raw", "")
        if 0 <= ln < len(lines) and lines[ln] == raw:
            if t == "done":
                lines[ln] = lines[ln].replace("- [ ]", "- [x]", 1) + f" | 完了: {today}"
                counts["done"] += 1
            else:
                indent = INDENT.match(raw).group(1)
                lines[ln] = build_line(indent, op.get("text", "").strip(), op.get("pri", "").strip(),
                                       op.get("due", "").strip(), status=op.get("status", DEFAULT_STATUS))
                counts["edit"] += 1
            fo["dirty"] = True
            applied.append((op.get("text") or raw)[:50])
        else:
            skipped.append((op.get("text") or raw or "?")[:50])

    # Stage B: 移動の「取り出し」— ソース行を None マークし内容を確保（行数はまだ変えない）
    moved = []
    for op in ops:
        if op.get("type") != "move":
            continue
        fo = load(op.get("file", ""))
        tfo = load(op.get("target_file", ""))
        if not fo or not tfo:
            skipped.append((op.get("raw") or "?")[:50])
            continue
        lines = fo["lines"]
        ln = op.get("lineno", 0) - 1
        raw = op.get("raw", "")
        tsec = op.get("target_section", "").strip()
        if 0 <= ln < len(lines) and lines[ln] == raw:
            if has_heading(tfo["lines"], tsec):  # 移動先見出しが在る時だけ取り出す＝消失防止
                moved.append({"file": op["target_file"], "section": tsec,
                              "secline": op.get("target_secline", 0), "content": raw.lstrip(), "kind": "move"})
                lines[ln] = None
                fo["dirty"] = True
            else:
                skipped.append("移動先PJ不明(消失回避): " + (raw or "?")[:40])
        else:
            skipped.append((op.get("raw") or "?")[:50])
    for fo in files.values():  # None を除去（コンパクション）
        if fo and any(l is None for l in fo["lines"]):
            fo["lines"] = [l for l in fo["lines"] if l is not None]

    # Stage C: 挿入（追加 ＋ 移動先）— 見出しは secline＋テキストで再同定（行ズレに強い）
    inserts = []
    for op in ops:
        if op.get("type") != "add":
            continue
        txt = op.get("text", "").strip()
        if not txt:
            skipped.append("(空の追加)")
            continue
        inserts.append({"file": op.get("file", ""), "section": op.get("section", "").strip(),
                        "secline": op.get("secline", 0), "kind": "add",
                        "content": build_line("", txt, op.get("pri", "").strip(), op.get("due", "").strip(),
                                              status=op.get("status", DEFAULT_STATUS))})
    inserts += moved
    for ins in inserts:
        fo = load(ins["file"])
        if not fo:
            skipped.append(ins["content"][:50])
            continue
        if insert_in_section(fo["lines"], ins["section"], ins["secline"], ins["content"]):
            counts[ins["kind"]] += 1
            applied.append(ins["content"][:50])
            fo["dirty"] = True
        else:
            skipped.append("挿入先不明: " + ins["content"][:30])

    changed = []
    for f, fo in files.items():
        if fo and fo["dirty"]:
            for idx, ln_text in enumerate(fo["lines"][:15]):
                if UPDATED.match(ln_text):
                    fo["lines"][idx] = f"updated: {today}"
                    break
            # Path.write_text(newline=...) は Python 3.10+ 専用。会社Mac(3.9)対応で open() を使う
            with open(fo["p"], "w", encoding="utf-8", newline="") as _wf:
                _wf.write(fo["nl"].join(fo["lines"]))
            changed.append(f)

    commit_msg, committed = "", False
    if do_commit and changed:
        try:
            subprocess.run(["git", "add", *changed], cwd=ROOT, check=True, capture_output=True)
            commit_msg = (f"chore(tasks): タスクボード — 完了 {counts['done']}/編集 {counts['edit']}/"
                          f"追加 {counts['add']}/移動 {counts['move']} ({today})")
            subprocess.run(["git", "commit", "-q", "-m", commit_msg, "--", *changed],
                           cwd=ROOT, check=True, capture_output=True)
            committed = True
        except subprocess.CalledProcessError as e:
            commit_msg = "git commit 失敗: " + (e.stderr or b"").decode("utf-8", "replace")[:300]
    return {"applied": applied, "skipped": skipped, "changed": changed,
            "committed": committed, "commit_msg": commit_msg, "counts": counts}


# ---------- HTML ----------

PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>タスクボード</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1d212b;--line:#2a2f3a;--txt:#e7e9ee;
--dim:#9aa3b2;--faint:#6b7280;--red:#ff5d5d;--amber:#ffb648;--green:#5ad19a;--blue:#69a7ff;--accent:#5ad19a;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font-family:-apple-system,"Segoe UI",Meiryo,system-ui,sans-serif;
line-height:1.5;padding-bottom:120px}
.topbar{position:sticky;top:0;z-index:50;background:rgba(15,17,21,.96);backdrop-filter:blur(8px);
border-bottom:1px solid var(--line)}
header{padding:12px 18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:18px;margin:0;letter-spacing:.3px}
header .meta{color:var(--dim);font-size:12px}
.spacer{flex:1}
#send{border:0;border-radius:10px;padding:11px 20px;font-size:15px;font-weight:700;cursor:pointer;
background:var(--accent);color:#06281a;transition:.12s;white-space:nowrap}
#send:disabled{background:#2a2f3a;color:var(--faint);cursor:not-allowed}
#send:not(:disabled):hover{filter:brightness(1.08)}
.toolbar{padding:0 18px 11px;display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.toolbar input,.toolbar select{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
border-radius:8px;padding:7px 10px;font-size:13px}
.toolbar .srch{min-width:200px;flex:1}
.toolbar .vcount{color:var(--faint);font-size:12px;margin-left:auto;white-space:nowrap}
.wrap{max-width:1136px;margin:0 auto;padding:14px 18px}
.dept{margin:30px 0 10px;font-size:22px;font-weight:800;display:flex;gap:10px;align-items:baseline}
.dept .c{font-size:12px;color:var(--faint);font-weight:500}
.card{border:1px solid var(--line);border-radius:13px;margin:12px 0;overflow:hidden;background:var(--panel)}
.card-h{display:flex;align-items:center;gap:12px;padding:13px 16px;border-left:6px solid var(--line)}
.card-title{font-size:24px;font-weight:800;line-height:1.1}
.card-sub{font-size:12px;color:var(--dim)}
.card-count{font-size:12px;color:var(--faint);background:var(--panel2);border:1px solid var(--line);
border-radius:99px;padding:2px 10px;margin-left:auto;font-weight:700}
.addbtn{background:transparent;border:1px dashed var(--line);color:var(--dim);border-radius:7px;
padding:3px 11px;cursor:pointer;font-size:12px}
.addbtn:hover{color:var(--accent);border-color:var(--accent)}
.addform-wrap{padding:8px 14px 0}
.addform-wrap[hidden]{display:none}
.rows{padding:2px 0 4px}
.row{display:grid;grid-template-columns:64px 96px minmax(0,1fr) 158px 132px 56px;align-items:center;
gap:0 12px;padding:8px 14px;border-bottom:1px solid var(--line)}
.rows .row:last-child{border-bottom:0}
.row.rowhead{color:var(--dim);font-size:11px;font-weight:700;background:#13161d;letter-spacing:.4px}
.row.task{cursor:grab}
.row.task:hover{background:var(--panel2)}
.row.task.on{background:rgba(90,209,154,.10)}
.row.task.sel{background:rgba(105,167,255,.20);outline:1px solid var(--blue);outline-offset:-1px}
.row.task.moving{border-left:3px solid var(--accent);background:rgba(90,209,154,.08)}
.mvtag{color:var(--accent);font-size:11px;font-weight:700;white-space:nowrap}
.card.drop{outline:2px dashed var(--accent);outline-offset:-3px}
#lasso{position:fixed;border:1px solid var(--blue);background:rgba(105,167,255,.12);z-index:60;
pointer-events:none;display:none;border-radius:3px}
.row>.editor{grid-column:1/-1;margin-top:8px}
.tid{background:transparent;border:1px solid var(--line);color:var(--faint);border-radius:6px;padding:2px 6px;
font:600 11.5px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.3px;cursor:copy}
.tid:hover{color:var(--blue);border-color:var(--blue)}
.tid.copied{color:var(--accent);border-color:var(--accent);background:rgba(90,209,154,.12)}
.c-pri{white-space:nowrap}
.c-task{min-width:0;display:flex;align-items:center;gap:6px}
.c-task .lbl{font-size:14px;overflow:hidden;text-overflow:ellipsis}
.row.sub .c-task{padding-left:18px}
.row.sub .lbl{color:var(--dim);font-size:13px}
.b{display:inline-block;font-size:10.5px;padding:1px 8px;border-radius:99px;font-weight:700;white-space:nowrap}
.b-high{background:rgba(255,93,93,.16);color:var(--red)}
.b-mid{background:rgba(255,182,72,.16);color:var(--amber)}
.b-low{background:rgba(123,132,148,.18);color:var(--dim)}
.edit{background:transparent;border:1px solid var(--line);color:var(--faint);border-radius:6px;
padding:1px 8px;cursor:pointer;font-size:12px;flex-shrink:0}
.edit:hover{color:var(--txt);border-color:var(--accent)}
.c-due{white-space:nowrap;color:var(--dim);font-size:12.5px;font-variant-numeric:tabular-nums}
.c-due.over{color:var(--red);font-weight:600}
.c-due .due-txt{display:inline-block;min-width:78px;vertical-align:middle}
.duecal{width:22px;overflow:hidden;background:transparent;border:0;color:var(--blue);cursor:pointer;
vertical-align:middle;padding:0;margin-right:8px}
.duecal::-webkit-calendar-picker-indicator{filter:invert(.75);cursor:pointer;margin:0}
.c-check{text-align:center}
.cb{appearance:none;width:20px;height:20px;border:2px solid #444b5a;border-radius:6px;cursor:pointer;
position:relative;vertical-align:middle}
.cb:checked{background:var(--accent);border-color:var(--accent)}
.cb:checked::after{content:"✓";position:absolute;inset:0;display:flex;align-items:center;
justify-content:center;color:#06281a;font-size:14px;font-weight:900}
.st{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:7px;
padding:5px 7px;font-size:12.5px;cursor:pointer}
.st.st-todo{color:var(--dim)}
.st.st-doing{border-color:var(--blue);color:var(--blue)}
.st.st-hold{border-color:var(--amber);color:var(--amber)}
.pri-sel{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:7px;padding:5px 5px;font-size:12px;cursor:pointer}
.pri-sel.pr-high{border-color:var(--red);color:var(--red)}
.pri-sel.pr-mid{border-color:var(--amber);color:var(--amber)}
.pri-sel.pr-low{color:var(--dim)}
.pri-sel.pr-none{color:var(--faint)}
.row.rowhead [data-sort]{cursor:pointer;user-select:none}
.row.rowhead [data-sort]:hover{color:var(--txt)}
.row.rowhead .sorted{color:var(--blue)}
.arr{font-size:10px}
.acc{background:transparent;border:0;color:var(--dim);cursor:pointer;font-size:13px;padding:0 5px;line-height:1}
.acc:hover{color:var(--txt)}
.card.collapsed .rows,.card.collapsed .addform-wrap{display:none}
.editor,.addform{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:9px;
border:1px dashed var(--line);border-radius:8px}
.editor[hidden]{display:none}
.editor input,.editor select,.addform input,.addform select{background:var(--panel2);
border:1px solid var(--line);color:var(--txt);border-radius:6px;padding:6px 8px;font-size:13px}
.e-text,.a-text{flex:1;min-width:220px}
.editnote{color:var(--faint);font-size:11px;flex-basis:100%}
.empty{color:var(--faint);padding:40px;text-align:center}
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(40px);opacity:0;
background:var(--panel2);border:1px solid var(--accent);color:var(--txt);padding:13px 20px;border-radius:10px;
font-size:13px;max-width:90vw;transition:.25s;z-index:80;box-shadow:0 8px 30px rgba(0,0,0,.5)}
#toast.show{transform:translateX(-50%) translateY(0);opacity:1}
.foot{color:var(--faint);font-size:11px;text-align:center;margin-top:30px}
</style></head><body>
<div class="topbar">
<header>
  <h1>🛒 タスクボード</h1>
  <span class="meta">全部署 status.md ／ 未完了 <b id="total">__TOTAL__</b> 件 ・ __NOW__</span>
  <span class="spacer"></span>
  <button id="send" disabled onclick="save()">保存 (0)</button>
</header>
<div class="toolbar">
  <input id="srch" class="srch" type="search" placeholder="🔍 検索（タスク名・ID・入力で動的に絞り込み）">
  __PJOPTS__
  <select id="f-pri"><option value="">優先度: 全て</option><option value="最高">最高</option><option value="高">高</option><option value="通常">通常</option><option value="中">中</option><option value="低">低</option></select>
  <select id="f-st"><option value="">状態: 全て</option><option value="未着手">未着手</option><option value="取組中">取組中</option><option value="保留中">保留中</option></select>
  <select id="f-due"><option value="">期限: 全て</option><option value="has">期限あり</option><option value="none">期限なし</option><option value="over">期限切れ</option><option value="week">今週以内</option></select>
  <select id="f-sort"><option value="default">並び: 既定</option><option value="pri">優先度順</option><option value="due">期限が近い順</option><option value="status">ステータス順</option></select>
  <span class="vcount" id="vcount"></span>
</div>
</div>
<div class="wrap">__BODY__</div>
<div id="toast"></div>
<div id="lasso"></div>
<div class="foot"><b>ID</b>クリックで「タスクボードID:NNNNNN」をコピー（秘書のチャットに貼るとその件の話ができる） ・ クリック/Ctrl/Shift/なげなわで選択→別PJカードへドラッグで移動 ・ ☑完了 ・ 状態 ・ 📅期限 ・ ✎編集 ・ ＋追加 → 「保存」で1コミット __COMMITNOTE__</div>
<script>
const TODAY="__TODAY__";
const all = s => [...document.querySelectorAll(s)];
const esc = s => { const d=document.createElement('div'); d.textContent=String(s==null?'':s); return d.innerHTML; };
const stcls = v => v==='取組中'?'st-doing':v==='保留中'?'st-hold':'st-todo';
const prcls = v => !v?'pr-none':(v.includes('高')?'pr-high':(v.includes('中')?'pr-mid':'pr-low'));
let sortKey='default', sortDir='asc';
function setDue(r,v){ if(!r) return; const ed=r.querySelector('.editor'); if(ed){const e=ed.querySelector('.e-due'); if(e) e.value=v;} const sp=r.querySelector('.due-txt'); if(sp) sp.textContent=v||'—'; const dc=r.querySelector('.duecal'); if(dc) dc.value=v; r.dataset.due=v||''; r.dataset.hasdue=v?'1':'0'; }
function toggleSort(key){ if(sortKey===key){ sortDir=(sortDir==='asc')?'desc':'asc'; } else { sortKey=key; sortDir='asc'; } const fs=document.getElementById('f-sort'); if(fs){ fs.value=[...fs.options].some(o=>o.value===key)?key:'default'; } applyView(); }
const prirank = p => ({'最高':0,'高':1,'通常':2,'中':3,'低':4})[p] ?? 5;
const strank = s => ({'取組中':0,'保留中':1,'未着手':2})[s] ?? 3;
function addDays(d,n){ const t=new Date(d+'T00:00:00'); t.setDate(t.getDate()+n); return t.toISOString().slice(0,10); }

function rowOp(r){
  const file=r.dataset.file, lineno=parseInt(r.dataset.lineno,10), raw=r.dataset.raw;
  const cb=r.querySelector('.cb');
  if(cb && cb.checked) return {type:'done', file, lineno, raw};
  if(r.dataset.mvSection) return {type:'move', file, lineno, raw,
    target_file:r.dataset.mvFile, target_section:r.dataset.mvSection, target_secline:parseInt(r.dataset.mvSecline,10)||0};
  const st=r.querySelector('.st'); const status=st.value;
  const priSel=r.querySelector('.pri-sel'); const pri=priSel?priSel.value:'';
  const ed=r.querySelector('.editor'); if(!ed) return null;
  const text=ed.querySelector('.e-text').value.trim();
  const due=ed.querySelector('.e-due').value.trim();
  const dirty = text!==(ed.dataset.otext||'') || due!==(ed.dataset.odue||'') ||
                pri!==(priSel?(priSel.dataset.opri||''):'') || status!==(st.dataset.ostatus||'未着手');
  if(dirty && text) return {type:'edit', file, lineno, raw, text, pri, due, status};
  return null;
}
function addOp(f){
  const text=f.querySelector('.a-text').value.trim(); if(!text) return null;
  return {type:'add', file:f.dataset.file, section:f.dataset.section, secline:parseInt(f.dataset.secline,10)||0,
          text, pri:f.querySelector('.a-pri').value, due:f.querySelector('.a-due').value.trim(),
          status:f.querySelector('.a-status').value};
}
function gather(){
  const ops=[];
  all('.row.task').forEach(r=>{ const o=rowOp(r); if(o) ops.push(o); });
  all('.addform').forEach(f=>{ const a=addOp(f); if(a) ops.push(a); });
  return ops;
}
function refresh(){
  const ops=gather(); const b=document.getElementById('send');
  b.textContent='保存 ('+ops.length+')'; b.disabled=ops.length===0;
  const dirty=new Set(); ops.forEach(o=>{ if(o.lineno) dirty.add(o.file+':'+o.lineno); });
  all('.row.task').forEach(r=>r.classList.toggle('on', dirty.has(r.dataset.file+':'+r.dataset.lineno)));
}
function applyView(){
  const q=(document.getElementById('srch').value||'').toLowerCase();
  const fpj=document.getElementById('f-pj').value, fpri=document.getElementById('f-pri').value,
        fst=document.getElementById('f-st').value, fdue=document.getElementById('f-due').value;
  const dir=sortDir==='desc'?-1:1;
  let shown=0;
  all('.card').forEach(card=>{
    const box=card.querySelector('.rows');
    const rows=[...card.querySelectorAll('.row.task')];
    let vis=0;
    rows.forEach(r=>{
      let ok=true;
      if(fpj && r.dataset.section!==fpj) ok=false;
      if(ok && fpri && r.dataset.pri!==fpri) ok=false;
      if(ok && fst && r.dataset.status!==fst) ok=false;
      if(ok && fdue){
        const d=r.dataset.due, has=r.dataset.hasdue==='1';
        if(fdue==='has') ok=has;
        else if(fdue==='none') ok=!has;
        else if(fdue==='over') ok=!!d && d<TODAY;
        else if(fdue==='week') ok=!!d && d>=TODAY && d<=addDays(TODAY,7);
      }
      if(ok && q && !(r.dataset.label||'').includes(q)) ok=false;
      r.style.display = ok?'':'none';
      if(ok) vis++;
    });
    let arr=rows.filter(r=>r.style.display!=='none');
    if(sortKey==='pri') arr.sort((a,b)=>(prirank(a.dataset.pri)-prirank(b.dataset.pri))*dir);
    else if(sortKey==='due') arr.sort((a,b)=>(((a.dataset.due||'9999-99-99')<(b.dataset.due||'9999-99-99'))?-1:1)*dir);
    else if(sortKey==='status') arr.sort((a,b)=>(strank(a.dataset.status)-strank(b.dataset.status))*dir);
    else if(sortKey==='label') arr.sort((a,b)=>(a.dataset.label||'').localeCompare(b.dataset.label||'')*dir);
    else arr.sort((a,b)=>(parseInt(a.dataset.idx,10))-(parseInt(b.dataset.idx,10)));
    arr.forEach(r=>box.appendChild(r));
    card.style.display = vis?'':'none';
    shown+=vis;
  });
  all('[data-sort]').forEach(h=>{ const on=h.dataset.sort===sortKey; h.classList.toggle('sorted',on);
    const a=h.querySelector('.arr'); if(a) a.textContent = on?(sortDir==='asc'?' ▲':' ▼'):''; });
  document.getElementById('vcount').textContent='表示 '+shown+' 件';
}
document.addEventListener('input', refresh);
document.addEventListener('change', e=>{
  const t=e.target;
  if(t.classList.contains('duepick')){
    const box=t.closest('.editor,.addform'); const due=box && box.querySelector('.e-due,.a-due');
    if(due && t.value) due.value=t.value;
  }
  if(t.classList.contains('duecal')){
    const r=t.closest('.row.task'); setDue(r, t.value);
    if(r && sel.has(r)) [...sel].forEach(x=>{ if(x!==r) setDue(x, t.value); });
  }
  if(t.classList.contains('st')){
    t.className='st '+stcls(t.value);
    const r=t.closest('.row.task');
    if(r && sel.has(r)) [...sel].forEach(x=>{ const s=x.querySelector('.st'); if(s){ s.value=t.value; s.className='st '+stcls(t.value); } });
  }
  if(t.classList.contains('pri-sel')){
    t.className='pri-sel '+prcls(t.value);
    const r=t.closest('.row.task');
    if(r && sel.has(r)) [...sel].forEach(x=>{ const s=x.querySelector('.pri-sel'); if(s){ s.value=t.value; s.className='pri-sel '+prcls(t.value); } });
  }
  refresh();
});
['srch','f-pj','f-pri','f-st','f-due'].forEach(id=>{
  const el=document.getElementById(id);
  el.addEventListener('input', applyView); el.addEventListener('change', applyView);
});
document.getElementById('f-sort').addEventListener('change', e=>{ sortKey=e.target.value; sortDir='asc'; applyView(); });
let sel=new Set(), lastClick=null;
function selStyle(){ all('.row.task').forEach(r=>r.classList.toggle('sel', sel.has(r))); refresh(); }
function selectRange(a,b){
  const card=a.closest('.card'); if(!card || b.closest('.card')!==card){ sel.add(b); return; }
  const rows=[...card.querySelectorAll('.row.task')]; let i=rows.indexOf(a), j=rows.indexOf(b);
  if(i<0||j<0){ sel.add(b); return; } if(i>j){ const k=i;i=j;j=k; }
  for(let x=i;x<=j;x++) sel.add(rows[x]);
}
async function copyTid(btn){
  const text='タスクボードID:'+btn.textContent.trim();
  let ok=false;
  try{ await navigator.clipboard.writeText(text); ok=true; }catch(_){}
  if(!ok){ try{ const ta=document.createElement('textarea'); ta.value=text; ta.style.cssText='position:fixed;top:0;opacity:0';
    document.body.appendChild(ta); ta.select(); ok=document.execCommand('copy'); document.body.removeChild(ta); }catch(_){} }
  if(ok){ btn.classList.add('copied'); setTimeout(()=>btn.classList.remove('copied'), 1400);
    toast('📋 <b>'+esc(text)+'</b> をコピーしました（秘書のチャットに貼り付け）'); }
  else toast('コピーできませんでした: <b>'+esc(text)+'</b>', false);
}
document.addEventListener('click', e=>{
  const t=e.target;
  if(t.classList.contains('tid')){ copyTid(t); return; }
  if(t.classList.contains('acc')){ const c=t.closest('.card'); if(c){ c.classList.toggle('collapsed'); t.textContent=c.classList.contains('collapsed')?'▸':'▾'; } return; }
  const hd=t.closest('[data-sort]'); if(hd){ toggleSort(hd.dataset.sort); return; }
  if(t.classList.contains('edit')){ const ed=t.closest('.row').querySelector('.editor'); if(ed) ed.hidden=!ed.hidden; return; }
  if(t.classList.contains('addbtn')){ const w=t.closest('.card').querySelector('.addform-wrap'); if(w) w.hidden=!w.hidden; return; }
  const row=t.closest('.row.task');
  if(row && !t.closest('.cb,.st,.pri-sel,.duecal,.editor')){
    if(e.shiftKey && lastClick){ selectRange(lastClick,row); }
    else if(e.ctrlKey||e.metaKey){ if(sel.has(row)) sel.delete(row); else sel.add(row); lastClick=row; }
    else { sel.clear(); sel.add(row); lastClick=row; }
    selStyle(); return;
  }
  if(!row && !t.closest('.toolbar,.card-h,#send')){ if(!(e.ctrlKey||e.metaKey||e.shiftKey)){ sel.clear(); selStyle(); } }
});
// なげなわ（ラバーバンド）選択
let lassoStart=null; const lassoEl=document.getElementById('lasso');
document.addEventListener('mousedown', e=>{
  if(e.button!==0) return; const t=e.target;
  if(t.closest('.row.task,.toolbar,.card-h,#send,.editor,.addform')) return;
  if(!t.closest('.wrap')) return;
  lassoStart={x:e.clientX,y:e.clientY};
  if(!(e.ctrlKey||e.metaKey||e.shiftKey)){ sel.clear(); selStyle(); }
});
document.addEventListener('mousemove', e=>{
  if(!lassoStart) return;
  const x1=Math.min(lassoStart.x,e.clientX), y1=Math.min(lassoStart.y,e.clientY),
        x2=Math.max(lassoStart.x,e.clientX), y2=Math.max(lassoStart.y,e.clientY);
  lassoEl.style.display='block'; lassoEl.style.left=x1+'px'; lassoEl.style.top=y1+'px';
  lassoEl.style.width=(x2-x1)+'px'; lassoEl.style.height=(y2-y1)+'px';
  all('.row.task').forEach(r=>{ if(r.style.display==='none') return;
    const b=r.getBoundingClientRect();
    if(!(b.right<x1||b.left>x2||b.bottom<y1||b.top>y2)) sel.add(r);
  });
  selStyle();
});
document.addEventListener('mouseup', ()=>{ if(lassoStart){ lassoStart=null; lassoEl.style.display='none'; } });
// ドラッグ&ドロップ移動（別PJカードへ）
document.addEventListener('dragstart', e=>{
  if(e.target.closest('.editor,.addform,.st,.duecal,.cb')){ e.preventDefault(); return; }
  const row=e.target.closest('.row.task'); if(!row){ e.preventDefault(); return; }
  if(!sel.has(row)){ sel.clear(); sel.add(row); selStyle(); }
  try{ e.dataTransfer.effectAllowed='move'; e.dataTransfer.setData('text/plain','move'); }catch(_){}
});
document.addEventListener('dragover', e=>{ const c=e.target.closest('.card'); if(c&&c.dataset.section) e.preventDefault(); });
document.addEventListener('dragenter', e=>{ const c=e.target.closest('.card'); if(c&&c.dataset.section){ all('.card').forEach(x=>x.classList.toggle('drop', x===c)); } });
document.addEventListener('dragend', ()=>{ all('.card').forEach(c=>c.classList.remove('drop')); });
document.addEventListener('drop', e=>{
  const card=e.target.closest('.card'); if(!card||!card.dataset.section) return;
  e.preventDefault();
  const tf=card.dataset.file, ts=card.dataset.section, tl=parseInt(card.dataset.secline,10)||0;
  const box=card.querySelector('.rows');
  [...sel].forEach(r=>{
    const tg=r.querySelector('.mvtag');
    if(tf===r.dataset.file && ts===r.dataset.section){
      delete r.dataset.mvFile; delete r.dataset.mvSection; delete r.dataset.mvSecline;
      r.classList.remove('moving'); if(tg) tg.textContent=''; box.appendChild(r); return;
    }
    r.dataset.mvFile=tf; r.dataset.mvSection=ts; r.dataset.mvSecline=tl;
    r.classList.add('moving'); if(tg) tg.textContent='↪ '+ts; box.appendChild(r);
  });
  all('.card').forEach(c=>c.classList.remove('drop'));
  sel.clear(); selStyle();
});
function toast(msg, ok=true){
  const t=document.getElementById('toast');
  t.style.borderColor = ok ? 'var(--accent)' : 'var(--red)';
  t.innerHTML=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 4600);
}
async function save(){
  const ops=gather(); if(!ops.length) return;
  const n={done:0,edit:0,add:0,move:0}; ops.forEach(o=>n[o.type]++);
  if(!confirm('保存します: 完了 '+n.done+' / 編集・状態 '+n.edit+' / 追加 '+n.add+' / 移動 '+n.move+'\\ngitコミットしてよろしいですか？')) return;
  const b=document.getElementById('send'); b.disabled=true; b.textContent='保存中…';
  try{
    const r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ops})});
    const j=await r.json(); const c=j.counts||{done:0,edit:0,add:0};
    let msg='✅ 完了 '+c.done+' / 編集・状態 '+c.edit+' / 追加 '+c.add+' / 移動 '+(c.move||0)+' 件を反映';
    if(j.committed) msg+='<br><span style="color:var(--dim)">'+esc(j.commit_msg)+'</span>';
    else if(j.changed&&j.changed.length) msg+='<br><span style="color:var(--amber)">'+esc(j.commit_msg||'コミットなし')+'</span>';
    if(j.skipped&&j.skipped.length) msg+='<br><span style="color:var(--amber)">⚠ '+j.skipped.length+'件スキップ（行ズレ/対象外）</span>';
    toast(msg, true); setTimeout(()=>location.reload(), 1700);
  }catch(err){ toast('❌ 保存失敗: '+esc(err), false); b.disabled=false; refresh(); }
}
applyView(); refresh();
</script></body></html>"""


def pri_badge(pri):
    if not pri:
        return '<span class="b b-low">—</span>'
    cls = "b-high" if ("高" in pri or "最高" in pri) else ("b-mid" if "中" in pri else "b-low")
    return f'<span class="b {cls}">{html.escape(pri)}</span>'


def pr_cls(pri):
    if not pri:
        return "pr-none"
    return "pr-high" if ("高" in pri or "最高" in pri) else ("pr-mid" if "中" in pri else "pr-low")


def pri_inline_select(current):
    opts = ["最高", "高", "通常", "中", "低", ""]
    if current and current not in opts:
        opts.insert(0, current)
    body = "".join(f'<option value="{A(o)}"{" selected" if o == current else ""}>{html.escape(o) if o else "—"}</option>'
                   for o in opts)
    return f'<select class="pri-sel {pr_cls(current)}" data-opri="{A(current)}">{body}</select>'


def status_cls(s):
    return {"取組中": "st-doing", "保留中": "st-hold"}.get(s, "st-todo")


def status_select(current):
    cur = current or DEFAULT_STATUS
    opts = list(STATUS_OPTS)
    if cur not in opts:
        opts.insert(0, cur)
    body = "".join(f'<option{" selected" if o == cur else ""}>{html.escape(o)}</option>' for o in opts)
    return f'<select class="st {status_cls(cur)}" data-ostatus="{A(cur)}">{body}</select>'


def add_status_select():
    body = "".join(f'<option{" selected" if o == DEFAULT_STATUS else ""}>{html.escape(o)}</option>' for o in STATUS_OPTS)
    return f'<select class="a-status">{body}</select>'


def pri_select(cls, current):
    opts = list(PRI_OPTS)
    if current and current not in opts:
        opts.append(current)
    body = "".join(
        f'<option value="{A(o)}"{" selected" if o == current else ""}>{html.escape(o) if o else "（優先度なし）"}</option>'
        for o in opts
    )
    return f'<select class="{cls}">{body}</select>'


def editor_html(it):
    return (
        f'<div class="editor" hidden data-otext="{A(it["ebody"])}" data-odue="{A(it["edue"])}">'
        f'<input class="e-text" type="text" value="{A(it["ebody"])}" placeholder="本文">'
        f'<input class="e-due" type="text" value="{A(it["edue"])}" placeholder="期限 例: 2026-06-30 / 締結後 / 空">'
        f'<input class="duepick" type="date" title="日付を選んで期限へ反映">'
        f'<span class="editnote">本文・期限を編集して「保存」。優先度／状態は行のプルダウン、期限は📅でも変更可。</span>'
        f'</div>'
    )


def addform_html(file_rel, section, secline):
    return (
        f'<div class="addform" data-file="{A(file_rel)}" data-section="{A(section)}" data-secline="{secline}">'
        f'<input class="a-text" type="text" placeholder="新しいタスク本文">'
        f'{pri_select("a-pri", "通常")}'
        f'<input class="a-due" type="text" placeholder="期限（任意）">'
        f'<input class="duepick" type="date" title="日付を選んで期限へ反映">'
        f'{add_status_select()}'
        f'</div>'
    )


def row_html(it, today):
    m = DATE.match(it["due"] or "")
    ddate = m.group(1) if m else ""
    over = " over" if (ddate and ddate < today) else ""
    hasdue = "1" if it["due"] else "0"
    sub = " sub" if it["indent"] > 0 else ""
    search = A((it["tid"] + " " + it["label"] + " " + it["ebody"]).lower())
    duetxt = html.escape(it["due"]) if it["due"] else "—"
    return (
        f'<div class="row task{sub}" draggable="true" data-file="{A(it["file"])}" data-lineno="{it["lineno"]}" '
        f'data-raw="{A(it["raw"])}" data-pri="{A(it["pri"])}" data-status="{A(it["status"])}" '
        f'data-section="{A(it["section"])}" data-due="{ddate}" data-hasdue="{hasdue}" '
        f'data-idx="{it["_idx"]}" data-tid="{it["tid"]}" data-label="{search}">'
        f'<div class="c-id"><button class="tid" title="クリックで「タスクボードID:{it["tid"]}」をコピー">'
        f'{it["tid"]}</button></div>'
        f'<div class="c-pri">{pri_inline_select(it["pri"])}</div>'
        f'<div class="c-task"><span class="lbl">{html.escape(it["label"])}</span>'
        f'<span class="mvtag"></span>'
        f'<button class="edit" title="編集">✎</button></div>'
        f'<div class="c-due{over}"><input type="date" class="duecal" value="{ddate}" title="期限の日付を変更">'
        f'<span class="due-txt">{duetxt}</span></div>'
        f'<div class="c-status">{status_select(it["status"])}</div>'
        f'<div class="c-check"><input type="checkbox" class="cb" title="チェックで完了"></div>'
        f'{editor_html(it)}'
        f'</div>'
    )


def card_html(color, section, secline, file_rel, group, today):
    rows = "".join(row_html(it, today) for it in group)
    rowhead = ('<div class="row rowhead">'
               '<div>ID</div>'
               '<div data-sort="pri">優先度<span class="arr"></span></div>'
               '<div data-sort="label">タスク<span class="arr"></span></div>'
               '<div data-sort="due">期限<span class="arr"></span></div>'
               '<div data-sort="status">ステータス<span class="arr"></span></div>'
               '<div>done</div></div>')
    title = html.escape(section) if section else "その他"
    addbtn = '<button class="addbtn" title="このPJにタスク追加">＋追加</button>' if section else ""
    addwrap = (f'<div class="addform-wrap" hidden>{addform_html(file_rel, section, secline)}</div>'
               if section else "")
    head = (f'<div class="card-h" style="border-left-color:{color};background:{color}14">'
            f'<button class="acc" title="開閉">▾</button>'
            f'<span class="card-title" style="color:{color}">{title}</span>'
            f'<span class="card-sub">のタスク一覧</span>'
            f'<span class="card-count">{len(group)}</span>{addbtn}</div>')
    data = (f' data-section="{A(section)}" data-secline="{secline}" data-file="{A(file_rel)}"'
            if section else "")
    return f'<div class="card"{data}>{head}{addwrap}<div class="rows">{rowhead}{rows}</div></div>'


def render(depts):
    today = datetime.date.today().isoformat()
    total = sum(len(v) for v in depts.values())
    # 連番（既定ソート用）
    idx = 0
    pj_opts, seen = [], set()
    for items in depts.values():
        for it in items:
            it["_idx"] = idx
            idx += 1
            s = it["section"]
            if s and s not in seen:
                seen.add(s)
                pj_opts.append(s)
    pj_select = ('<select id="f-pj"><option value="">PJ: 全て</option>'
                 + "".join(f'<option value="{A(s)}">{html.escape(s)}</option>' for s in pj_opts)
                 + '</select>')

    if not total:
        body = '<div class="empty">未完了タスクはありません 🎉</div>'
    else:
        parts = []
        for dept, items in depts.items():
            color = DEPT_COLORS.get(dept, "#9aa3b2")
            parts.append(f'<div class="dept" style="color:{color}">{html.escape(dept)}'
                         f'<span class="c">{len(items)}件</span></div>')
            i = 0
            while i < len(items):
                sec = items[i]["section"]
                secline = items[i]["secline"]
                file_rel = items[i]["file"]
                grp = []
                while i < len(items) and items[i]["section"] == sec:
                    grp.append(items[i])
                    i += 1
                parts.append(card_html(color, sec, secline, file_rel, grp, today))
        body = "\n".join(parts)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return (PAGE.replace("__BODY__", body)
                .replace("__TOTAL__", str(total))
                .replace("__NOW__", now)
                .replace("__TODAY__", today)
                .replace("__PJOPTS__", pj_select)
                .replace("__COMMITNOTE__", "" if HANDLER_COMMIT else "（--no-commit: コミットなし）"))


HANDLER_COMMIT = True


def _validate_ops(raw):
    ops = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        t, f = it.get("type"), it.get("file")
        if not isinstance(f, str):
            continue
        if t in ("done", "edit"):
            if not isinstance(it.get("lineno"), int) or not isinstance(it.get("raw"), str):
                continue
            op = {"type": t, "file": f, "lineno": it["lineno"], "raw": it["raw"]}
            if t == "edit":
                text = _clean(it["text"]) if isinstance(it.get("text"), str) else ""
                if not text:
                    continue
                op["text"] = text
                op["pri"] = _clean(it["pri"], allow_pipe=False) if isinstance(it.get("pri"), str) else ""
                op["due"] = _clean(it["due"], allow_pipe=False) if isinstance(it.get("due"), str) else ""
                op["status"] = it["status"] if it.get("status") in STATUS_OPTS else DEFAULT_STATUS
            ops.append(op)
        elif t == "add":
            text = _clean(it["text"]) if isinstance(it.get("text"), str) else ""
            section = _clean(it["section"]) if isinstance(it.get("section"), str) else ""
            if not text or not section:
                continue
            ops.append({"type": "add", "file": f, "section": section, "text": text,
                        "secline": it["secline"] if isinstance(it.get("secline"), int) else 0,
                        "pri": _clean(it["pri"], allow_pipe=False) if isinstance(it.get("pri"), str) else "",
                        "due": _clean(it["due"], allow_pipe=False) if isinstance(it.get("due"), str) else "",
                        "status": it["status"] if it.get("status") in STATUS_OPTS else DEFAULT_STATUS})
        elif t == "move":
            if not isinstance(it.get("lineno"), int) or not isinstance(it.get("raw"), str):
                continue
            tf = it.get("target_file")
            ts = it.get("target_section")
            if not isinstance(tf, str) or not isinstance(ts, str) or not ts.strip():
                continue
            ops.append({"type": "move", "file": f, "lineno": it["lineno"], "raw": it["raw"],
                        "target_file": tf, "target_section": _clean(ts),
                        "target_secline": it["target_secline"] if isinstance(it.get("target_secline"), int) else 0})
    return ops


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        if self.path.startswith("/favicon"):
            self._send(204, b"")
            return
        self._send(200, render(collect()))

    def do_POST(self):
        if self.path not in ("/save", "/done"):
            self._send(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > 2_000_000:
            self._json(400, {"error": "invalid length"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/done":
                raw = [{**it, "type": "done"} for it in payload.get("items", []) if isinstance(it, dict)]
            else:
                raw = payload.get("ops", [])
            if not isinstance(raw, list) or len(raw) > 4000:
                raise ValueError("ops must be a list (<=4000)")
            result = apply_ops(_validate_ops(raw), do_commit=HANDLER_COMMIT)
            self._json(200, result)
        except Exception as e:  # noqa
            self._json(500, {"error": str(e)})


def serve(port, open_browser, do_commit):
    global HANDLER_COMMIT
    HANDLER_COMMIT = do_commit
    url = f"http://127.0.0.1:{port}"
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    total = sum(len(v) for v in collect().values())
    print(f"タスクボード起動: {url}  （未完了 {total}件 / コミット={'ON' if do_commit else 'OFF'}）")
    print("停止: Ctrl+C")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")
        srv.shutdown()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--dump", action="store_true", help="収集結果を出力して終了（動作確認用）")
    args = ap.parse_args()

    if args.dump:
        depts = collect()
        for d, items in depts.items():
            print(f"\n■ {d}  ({len(items)}件)")
            for it in items:
                tag = " ".join(x for x in (f"[{it['pri']}]" if it['pri'] else "",
                                           f"<{it['status']}>" if it['status'] != DEFAULT_STATUS else "") if x)
                print(f"  {it['file']}:{it['lineno']}  {it['label']} {tag}")
        print(f"\n合計 {sum(len(v) for v in depts.values())}件")
        return 0

    serve(args.port, not args.no_open, not args.no_commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
