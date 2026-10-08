#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""find_orphan_tasks.py — status.md 外に埋もれた未完了タスクを検出する監査ツール。

背景:
  rollover_todos.py と朝のブリーフィングは status.md と直近 todos しか見ない。
  そのため notes/議事録/jobs/inbox に書かれたアクションアイテム(`- [ ]`)が
  status.md に昇格されないまま放置される「漏れ」が起きる（2026-06-19 に大量発覚）。
  本ツールは status.md・CLAUDE.md・日次todos 以外の *.md に残る未完了 `- [ ]` を
  ファイル別に一覧化し、週次棚卸しで拾い上げられるようにする。

使い方:
  py tools/find_orphan_tasks.py            # company/ 配下を監査
  py tools/find_orphan_tasks.py --days 14  # 14日以上未更新を STALE 表示にする閾値
"""
import sys
import re
import argparse
import datetime
from pathlib import Path

# Windowsのcp932でem-dash等が落ちないようUTF-8で出力する（rollover_todos.pyの教訓）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent          # リポジトリルート
COMPANY = ROOT / "company"
CHECKBOX = re.compile(r"^\s*-\s*\[ \]\s*(.+?)\s*$")     # 未完了チェックボックス（インデント可）

# 除外対象: 既に棚卸し済み(status.md)・テンプレ記入例(CLAUDE.md)・繰り越し複製(todos/)・依存物
EXCLUDE_NAMES = {"status.md", "CLAUDE.md"}
EXCLUDE_SEGMENTS = ("todos", "vendor", "node_modules", "__pycache__", "assets", "meetings")


def is_excluded(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return True
    # パスの「セグメント」単位で判定（部分一致で無関係なファイルを巻き込まない）
    segs = {part.lower() for part in path.parts}
    return any(seg in segs for seg in EXCLUDE_SEGMENTS)


def main() -> int:
    ap = argparse.ArgumentParser(description="status.md外の埋もれた未完了タスクを監査する")
    ap.add_argument("--days", type=int, default=30,
                    help="この日数以上更新されていないファイルを STALE(過去案件の可能性) と表示")
    args = ap.parse_args()

    today = datetime.date.today()
    findings = []  # (相対パス, 経過日数, [タスク...])
    for md in sorted(COMPANY.rglob("*.md")):
        if is_excluded(md):
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tasks = []
        for i, line in enumerate(text.splitlines(), start=1):
            m = CHECKBOX.match(line)
            if m:
                tasks.append((i, m.group(1)))
        if not tasks:
            continue
        age = (today - datetime.date.fromtimestamp(md.stat().st_mtime)).days
        findings.append((md.relative_to(ROOT), age, tasks))

    total = sum(len(t) for _, _, t in findings)
    print(f"# 埋もれタスク監査  ({today})")
    print(f"# status.md / CLAUDE.md / todos を除く {len(findings)} ファイルに "
          f"未完了 `- [ ]` が {total} 件\n")

    # 新しく触ったファイル順（＝生きている可能性が高い順）に出す
    for rel, age, tasks in sorted(findings, key=lambda x: x[1]):
        flag = "   ⚠STALE(過去案件かも)" if age >= args.days else ""
        print(f"## {rel}  —  {age}日前更新{flag}  ({len(tasks)}件)")
        for lineno, t in tasks:
            print(f"  L{lineno}: - [ ] {t}")
        print()

    print("※ 生きているタスクは該当部署の status.md へ転記し、")
    print("   過去案件のチェックリストは元ファイルでクローズ([x]/[-])してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
