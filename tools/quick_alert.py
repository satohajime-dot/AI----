#!/usr/bin/env python3
"""用件から始まったセッション用の「危険信号1行」。起動時の点検の代わりに、これだけを走らせる。

なぜ要るのか
------------
セッション開始時の点検（繰り越し・恒久ゴールの検証・未完了タスクの全件抽出と報告）を
**用件の返事より先に**走らせると、「ちょっとこれ見て」という相談ひとつでも、返事の前に
大量の再読と数十行の報告が挟まり、「いつまでも返事が来ない」状態になる。

そこで点検は「挨拶・状況確認・明示の指示」のときだけ行い（ルール `session-start`）、
用件から始まったセッションでは **このスクリプト1回だけ**を走らせる。

設計の制約（守ること）
--------------------
- **出力は最大1行**。何も無ければ何も出さない（無出力＝異常なし）
- **1秒以内**・読み取り専用。ゴール検証は再実行せず `company/goals/ledger.tsv` の
  最終回の結果を読むだけ（＝「前回検証時点」の事実として述べる。再検証は点検のときに行う）

使い方:
  python3 tools/quick_alert.py            # 例: ⚠ 期限超過3件・本日期限1件・ゴールFAIL1件（09-28 10:56時点）
  python3 tools/quick_alert.py --selftest

終了コード: 常に0（警報の有無で落とさない。落ちると用件の邪魔になる）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "tools"))

LEDGER = os.path.join(BASE_DIR, "company", "goals", "ledger.tsv")


def _status_deadline_counts(today: date | None = None) -> tuple:
    """各部署 status.md の未完了タスクから (期限超過件数, 本日期限件数) を数える。
    期限の読み取り・スケジュール行の除外は rollover_todos.py の実装を使い回す
    （判定規則が2箇所に分かれると、片方だけ直して食い違う）。"""
    import rollover_todos as rt

    today = today or date.today()
    overdue = due_today = 0
    for path in rt.DEPT_STATUS_FILES.values():
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not re.match(r"\s*- \[ \]", line):
                    continue
                line = line.rstrip()
                if rt.is_schedule(line):
                    continue
                m = rt.DATE_PATTERN.search(line)
                if not m:
                    continue
                try:
                    days = (date.fromisoformat(m.group(1)) - today).days
                except ValueError:
                    continue
                if days < 0:
                    overdue += 1
                elif days == 0:
                    due_today += 1
    return overdue, due_today


def _last_goal_fails() -> tuple:
    """ledger.tsv の「最終回の検証」だけを見て (FAIL件数, 日時ラベル, FAILゴール名リスト)。
    verify_goals.py は再実行しない（5秒かかるうえ、用件セッションでは要らない）。"""
    if not os.path.exists(LEDGER):
        return 0, "", []
    try:
        with open(LEDGER, encoding="utf-8") as f:
            rows = [ln.rstrip("\n").split("\t") for ln in f if ln.strip()]
    except OSError:
        return 0, "", []
    rows = [r for r in rows if len(r) >= 4 and r[0] not in ("datetime", "timestamp")]
    if not rows:
        return 0, "", []
    last_dt = rows[-1][0]
    batch = [r for r in rows if r[0] == last_dt]
    fails = [r[2] for r in batch if r[3] == "FAIL"]
    return len(fails), last_dt[5:], fails


def _unpushed() -> int:
    """未pushコミット数。バックアップ（GitHub）を設定していて、上げ忘れが溜まっていたら言う。"""
    try:
        proc = subprocess.run(
            ["git", "rev-list", "--count", "@{u}..HEAD"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=2,
        )
        return int(proc.stdout.strip()) if proc.returncode == 0 else 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def build_line(overdue: int, due_today: int, fails: int, fail_label: str, unpushed: int) -> str:
    """警報1行を組む。何も無ければ空文字（＝無出力）。"""
    parts = []
    if overdue:
        parts.append(f"期限超過{overdue}件")
    if due_today:
        parts.append(f"本日期限{due_today}件")
    if fails:
        parts.append(f"ゴールFAIL{fails}件（{fail_label}時点）" if fail_label else f"ゴールFAIL{fails}件")
    if unpushed:
        parts.append(f"未push{unpushed}件")
    if not parts:
        return ""
    return "⚠ " + "・".join(parts)


def selftest() -> int:
    """設計の制約（1行以内・0件で無出力・1秒以内）が生きていることを検査する。"""
    ng = []

    if build_line(0, 0, 0, "", 0) != "":
        ng.append("0件のとき無出力にならない")

    line = build_line(3, 1, 2, "09-07 15:59", 4)
    if "\n" in line:
        ng.append("警報が複数行になっている")
    if not ("期限超過3件" in line and "本日期限1件" in line
            and "ゴールFAIL2件" in line and "未push4件" in line):
        ng.append(f"警報の内訳が落ちている: {line}")

    t0 = time.perf_counter()
    real = _run(collect_only=True)
    elapsed = time.perf_counter() - t0
    if elapsed > 1.0:
        ng.append(f"1秒を超えた（{elapsed:.2f}秒）― 用件の返事を待たせる")
    if real.count("\n") > 0:
        ng.append("実データでの出力が1行を超えた")

    if ng:
        print("❌ quick_alert 自己検査 NG")
        for n in ng:
            print(f"  - {n}")
        return 1
    print(f"✅ quick_alert 自己検査 OK（1行以内・0件無出力・{elapsed:.2f}秒）")
    return 0


def _run(collect_only: bool = False) -> str:
    overdue, due_today = _status_deadline_counts()
    fails, fail_label, _names = _last_goal_fails()
    line = build_line(overdue, due_today, fails, fail_label, _unpushed())
    if not collect_only and line:
        print(line)
    return line


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        _run()
    except Exception:  # 警報のために用件を止めない（契約: 常に終了コード0）
        pass
