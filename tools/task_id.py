#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_id.py — タスクボードID（6桁）から status.md の該当行を引く。

ボードの行頭IDをクリックすると「タスクボードID:123456」がクリップボードに入る。
それがチャットに貼られたら、これで行を特定してから話す（推測で当てない）。

  python3 tools/task_id.py 123456 [654321 ...]    # 「タスクボードID:123456」をそのまま渡してもよい
  python3 tools/task_id.py 123456 --days 180      # git履歴をさかのぼる日数（既定90）

探す順:
  1. 現在の company/**/status*.md（未完了・完了済み・status-archive.md）
  2. 無ければ git 履歴。IDは本文から決まる（task_board.task_id）ので、本文を書き換えると変わる。
     書き換えを差分で追いかけ、今の行と今のIDまで辿る
"""
import re
import sys
import argparse
import subprocess
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("tb", ROOT / "tools" / "task_board.py")
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)

TASK = re.compile(r"^(\s*)-\s*\[( |x|X)\]\s*(.+?)\s*$")
SECTION = re.compile(r"^#{2,3}\s+(.+?)\s*$")  # ボードのカード見出しと同じ粒度（##／###）
CLIP = 160


def tid_of(line):
    m = TASK.match(line)
    return tb.task_id(m.group(3)) if m else None


def find_current(tid):
    hits = []
    for sf in sorted(tb.COMPANY.rglob("status*.md")):
        try:
            _, _, lines = tb.read_lines(sf)
        except (OSError, UnicodeDecodeError):
            continue
        sec = ""
        for i, line in enumerate(lines, start=1):
            h = SECTION.match(line)
            if h:
                sec = h.group(1)
            elif tid_of(line) == tid:
                done = TASK.match(line).group(2) != " "
                hits.append((str(sf.relative_to(ROOT)).replace("\\", "/"), i, sec, done, line.strip()))
    return hits


def history(days):
    """古い順のコミット列。各要素は (「hash 日付 件名」, [(path, 消えたタスク行[], 入ったタスク行[]), ...])。"""
    out = subprocess.run(
        ["git", "log", "--reverse", f"--since={days}.days", "-p", "-U0", "--no-color", "--no-renames",
         "--format=\x01%h %cs %s", "--", "company/*status*.md"],
        cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace").stdout
    commits, path, rem, add = [], None, [], []

    def flush():
        if commits and path and (rem or add):
            commits[-1][1].append((path, rem[:], add[:]))
        rem.clear()
        add.clear()

    for line in out.splitlines():
        if line.startswith("\x01"):
            flush()
            commits.append((line[1:], []))
            path = None
        elif line.startswith("+++ "):
            flush()
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("--- ") or line.startswith("@@"):
            flush()
        elif line[:1] == "-" and TASK.match(line[1:]):
            rem.append(line[1:])
        elif line[:1] == "+" and TASK.match(line[1:]):
            add.append(line[1:])
    flush()
    return commits


def trace(tid, commits):
    """本文の書き換えを追う。戻り値: (経緯[(meta, path, 旧行, 新行 or None, 候補[])], 今のID or None)。"""
    steps, cur = [], tid
    for meta, hunks in commits:
        hit = None
        for path, rem, add in hunks:
            rt = [tid_of(l) for l in rem]
            if cur in rt:
                hit = (path, rem, add, rt.index(cur))
                break
        if not hit:
            continue
        if any(cur in [tid_of(l) for l in a] for _, _, a in hunks):
            continue  # 同じ本文のまま（完了印・状態・期限の変更、PJ間の移動、archive送り）
        path, rem, add, k = hit
        if len(rem) == len(add):
            steps.append((meta, path, rem[k], add[k], []))
            cur = tid_of(add[k])
        else:
            steps.append((meta, path, rem[k], None, add))
            return steps, None
    return steps, cur


def clip(s):
    s = s.strip()
    return s if len(s) <= CLIP else s[:CLIP] + "…"


def show(hits, indent="  "):
    if len(hits) > 1:
        print(f"{indent}（{len(hits)}行が一致＝同じ本文の行が複数ある）")
    for f, ln, sec, done, line in hits:
        print(f"{indent}{'完了' if done else '未完了'}  {f}:{ln}  【{sec or '見出しなし'}】")
        print(f"{indent}  {line}")


def main():
    ap = argparse.ArgumentParser(description="タスクボードID → status.md の行")
    ap.add_argument("ids", nargs="+", help="6桁のID（「タスクボードID:123456」のままでも可）")
    ap.add_argument("--days", type=int, default=90, help="git履歴をさかのぼる日数（既定90）")
    a = ap.parse_args()
    ids = list(dict.fromkeys(re.findall(r"(?<!\d)\d{6}(?!\d)", " ".join(a.ids))))
    if not ids:
        print("6桁のIDが見つかりません")
        return 2

    commits, rc = None, 0
    for tid in ids:
        print(f"■ タスクボードID:{tid}")
        hits = find_current(tid)
        if hits:
            show(hits)
            continue
        if commits is None:
            commits = history(a.days)
        steps, cur = trace(tid, commits)
        if not steps:
            print(f"  見つかりません（現在の status*.md にも直近{a.days}日の履歴にも無い。--days を延ばす）")
            rc = 1
            continue
        print("  今の status.md には無い。本文が書き換わっている（IDは本文から決まるため変わった）:")
        for meta, path, old, new, cands in steps:
            print(f"  - {meta[:80]}  ({path})")
            print(f"      旧: {clip(old)}")
            if new:
                print(f"      新: {clip(new)}")
            elif cands:
                print(f"      新（候補・特定不可）: " + " ／ ".join(clip(c) for c in cands))
            else:
                print("      削除された")
        if cur:
            now = find_current(cur)
            if now:
                print(f"  ▶ 今の行（今のID {cur}）:")
                show(now, indent="    ")
            else:
                print(f"  ▶ 今のID {cur} の行は見つからない")
                rc = 1
        else:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
