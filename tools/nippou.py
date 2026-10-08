#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nippou.py — 日報の「材料」を集めるだけの読み取り専用コレクター（秘書メソッド同梱）

日報本文の要約・仕事とプライベートの切り分け・次回出社日の判定は秘書（メイン）が行う。
このスクリプトは判断をせず、その日の一次情報を機械的に拾って構造化して出すだけ。
（役割分担: スクリプト=機械的収集 / 秘書=要約・判断・カレンダー参照・テンプレ埋め）

読むもの:
  company/secretary/todos/YYYY-MM-DD.md   完了タスク（- [x]）
  company/secretary/worklog/YYYY-MM.md    当日の見出し（## YYYY-MM-DD）配下
  git log                                 当日のコミット（定型を除く）

使い方:
    python3 tools/nippou.py            # 今日ぶん
    python3 tools/nippou.py 2026-07-16 # 日付指定
（Windows は python3 → py に読み替える）
"""
import os
import re
import sys
import subprocess
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def target_date(argv):
    if len(argv) > 1:
        try:
            return datetime.strptime(argv[1], "%Y-%m-%d").date()
        except ValueError:
            print(f"日付の形式が正しくありません: {argv[1]}（例: 2026-07-16）")
            sys.exit(1)
    return date.today()


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def completed_todos(d):
    """その日のTODOファイルから完了(- [x])を抜く。"""
    txt = read(os.path.join(ROOT, "company", "secretary", "todos", f"{d:%Y-%m-%d}.md"))
    out = []
    for line in txt.splitlines():
        if re.match(r"^\s*-\s*\[x\]", line, re.IGNORECASE):
            item = re.sub(r"^\s*-\s*\[x\]\s*", "", line, flags=re.IGNORECASE)
            item = item.split("←")[0].strip()
            out.append(item)
    return out


def worklog_sections(d):
    """worklog/YYYY-MM.md から当日(## YYYY-MM-DD…)のセクション本文を抜く。"""
    txt = read(os.path.join(ROOT, "company", "secretary", "worklog", f"{d:%Y-%m}.md"))
    if not txt:
        return []
    lines = txt.splitlines()
    heads = [(i, l) for i, l in enumerate(lines) if l.startswith("## ")]
    sections = []
    for idx, (i, head) in enumerate(heads):
        if head.startswith(f"## {d:%Y-%m-%d}"):
            end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
            sections.append("\n".join(lines[i:end]).strip())
    return sections


def git_commits(d):
    """当日のコミット。定型の自動コミット・マージはノイズなので除外。"""
    try:
        out = subprocess.check_output(
            ["git", "log",
             f"--since={d:%Y-%m-%d} 00:00:00", f"--until={d:%Y-%m-%d} 23:59:59",
             "--pretty=format:%s"],
            cwd=ROOT, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    # git の出力は UTF-8。Windows の既定コードページで復号すると日本語で落ちるため明示する
    out = out.decode("utf-8", errors="replace")
    msgs = [m for m in out.splitlines() if m.strip()]
    msgs = [m for m in msgs if not m.startswith("chore(tasks)")]
    msgs = [m for m in msgs if not m.startswith("Merge ")]
    return msgs


def main():
    d = target_date(sys.argv)
    todos = completed_todos(d)
    logs = worklog_sections(d)
    commits = git_commits(d)

    print(f"===== 日報材料 {d:%Y-%m-%d} =====\n")

    print(f"【完了タスク（todos）: {len(todos)}件】")
    for t in todos:
        print(f"  - {t}")
    if not todos:
        print("  （なし）")

    print(f"\n【worklog 当日エントリ: {len(logs)}件】")
    for s in logs:
        print(s)
        print("  " + "-" * 40)
    if not logs:
        print("  （なし）")

    print(f"\n【gitコミット（定型除く）: {len(commits)}件】")
    for c in commits:
        print(f"  - {c}")
    if not commits:
        print("  （なし）")

    print("\n===== ここまで（要約・仕事/私用の切り分け・提出先の判断は秘書が実施） =====")


if __name__ == "__main__":
    main()
