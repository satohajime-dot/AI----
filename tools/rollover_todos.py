"""
未完了タスクの繰り越しスクリプト v2
直近3日分のTODOファイルから「純粋なタスク」のみ拾い、今日のファイルに追記する。
加えて、各部署 status.md から期限が近いタスク・期限超過タスクを浮上させる。
月曜日は全部署の status.md を棚卸しして全未完了タスクを報告する。

除外ルール (TODO繰り越し):
  - 時刻パターン（〜、:00）を含む → その日限りのスケジュールとみなす
  - 過去の期限が明示されていて今日より3日以上前 → 陳腐化とみなす

status.md 浮上ルール:
  - 月曜 or --all: 全部署の全未完了タスクを列挙（棚卸しモード）
  - それ以外: 期限が STATUS_URGENT_WINDOW 日以内 or 期限超過のみ
"""
import glob
import os
import re
import sys
from datetime import date, timedelta

# Windowsコンソール(cp932)でも絵文字・em-dash等を出力できるようにする
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODOS_DIR = os.path.join(BASE_DIR, "company", "secretary", "todos")
# 「鮮度ログ監視」はオーナー個人の機能。既定では無効（存在しないパス＝自動スキップ）。
# 使いたい場合は監視したいログの実パスを設定する。
TAAFT_LOG = os.path.join(BASE_DIR, ".freshness-log-disabled")

# 部署の表示名マッピング（任意）。ここに無い部署はフォルダ名がそのまま表示名になる。
# company/*/status.md は自動検出するので、部署を足しても列挙は不要。
DEPT_DISPLAY_NAMES = {}


def _discover_status_files() -> dict:
    """company/ 配下の status.md を再帰検出する（task_board.py の rglob と同じ範囲）。
    ネストした子PJ（例: sales/projectA/status.md）は「sales/projectA」のように表示する。"""
    company_dir = os.path.join(BASE_DIR, "company")
    found = {}
    if not os.path.isdir(company_dir):
        return found
    for path in sorted(glob.glob(os.path.join(company_dir, "**", "status.md"), recursive=True)):
        parts = os.path.relpath(path, company_dir).split(os.sep)[:-1]
        label = DEPT_DISPLAY_NAMES.get(parts[0], parts[0])
        if len(parts) > 1:
            label += "/" + "/".join(parts[1:])
        found[label] = path
    return found


DEPT_STATUS_FILES = _discover_status_files()

# status.md から浮上させる期限ウィンドウ（日）。この日数以内 or 超過なら浮上。
STATUS_URGENT_WINDOW = 7

# スケジュール行のパターン（時刻・場所・〜を含む行）
SCHEDULE_PATTERNS = [
    r"\d{1,2}:\d{2}",          # 時刻 HH:MM
    r"（\d{1,2}:\d{2}〜",       # （HH:MM〜
    r"\d{1,2}/\d{1,2}.*〜",     # M/D ...〜
    r"@[ぁ-ん一-龯ア-ン]",      # @場所名
]

DATE_PATTERN = re.compile(r"期限[:：]\s*(\d{4}-\d{2}-\d{2})")


def is_schedule(line: str) -> bool:
    for pat in SCHEDULE_PATTERNS:
        if re.search(pat, line):
            return True
    return False


def is_stale(line: str, today: date, threshold_days: int = 3) -> bool:
    m = DATE_PATTERN.search(line)
    if m:
        try:
            deadline = date.fromisoformat(m.group(1))
            if (today - deadline).days > threshold_days:
                return True
        except ValueError:
            pass
    return False


def _normalize(line: str) -> str:
    """重複判定用の正規化: ←繰越チェーンとMarkdown記号を除去した基底テキストを返す"""
    base = re.sub(r"\s*←.*$", "", line)
    # TAAFT系: 「— 最終記録: YYYY-MM-DD（X日未更新）」を除去して同一タスクと判定
    base = re.sub(r"\s*—\s*最終記録:.*$", "", base)
    return re.sub(r"[\*\[\]]", "", base).strip()


def get_rollover_tasks(days_back: int = 3) -> list:
    today = date.today()
    results = []
    seen = set()  # 正規化済みキーで重複排除

    for i in range(1, days_back + 1):
        target = today - timedelta(days=i)
        path = os.path.join(TODOS_DIR, f"{target}.md")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not re.match(r"\s*- \[ \]", line):
                    continue
                line = line.rstrip()
                # ←チェーンを除去して基底タスクのみで重複判定
                key = _normalize(line)
                if key in seen:
                    continue
                if is_schedule(line):
                    continue
                if is_stale(line, today):
                    continue
                seen.add(key)
                # 基底タスクのみ（チェーンなし）で保存し、後でfrom_dateを1つ付加する
                base_line = re.sub(r"\s*←.*$", "", line).rstrip()
                results.append((str(target), base_line))

    return results


def get_status_tasks(full_scan: bool = False) -> list:
    """
    各部署 status.md から未完了タスクを抽出する。
    戻り値: (dept_name, section_name, task_line) のリスト

    full_scan=True  → 全件（月曜棚卸しモード・--all フラグ時）
    full_scan=False → 期限が STATUS_URGENT_WINDOW 日以内 or 期限超過のみ
    """
    today = date.today()
    results = []

    for dept_name, path in DEPT_STATUS_FILES.items():
        if not os.path.exists(path):
            continue
        current_section = ""
        with open(path, encoding="utf-8") as f:
            for line in f:
                # セクションヘッダーを追跡（部署内の所属コンテキストとして使う）
                header_match = re.match(r"^#{2,4}\s+(.+)", line)
                if header_match:
                    current_section = header_match.group(1).strip()
                    continue

                if not re.match(r"\s*- \[ \]", line):
                    continue
                line = line.rstrip()
                if is_schedule(line):
                    continue

                if full_scan:
                    results.append((dept_name, current_section, line))
                else:
                    m = DATE_PATTERN.search(line)
                    if m:
                        try:
                            deadline = date.fromisoformat(m.group(1))
                            days_until = (deadline - today).days
                            if days_until <= STATUS_URGENT_WINDOW:
                                results.append((dept_name, current_section, line))
                        except ValueError:
                            pass

    return results


def check_taaft_staleness(threshold_days: int = 3):
    """TAAFTログの最終記録日を確認し、滞っていればTODO文字列を返す"""
    if not os.path.exists(TAAFT_LOG):
        return None
    with open(TAAFT_LOG, encoding="utf-8") as f:
        content = f.read()
    dates = re.findall(r"### (\d{4}-\d{2}-\d{2})", content)
    if not dates:
        return None
    last_date = date.fromisoformat(max(dates))
    gap = (date.today() - last_date).days
    if gap >= threshold_days:
        return (f"- [ ] **TAAFTログ更新** — 最終記録: {last_date}（{gap}日未更新）"
                f" | 優先度: 高 | ai-suishinshitsu.md に追記すること")
    return None


def rollover(dry_run: bool = False, force_all: bool = False) -> None:
    today = date.today()
    today_path = os.path.join(TODOS_DIR, f"{today}.md")
    is_monday = today.weekday() == 0
    full_scan = is_monday or force_all

    # 既存の今日ファイルを読む（重複排除用）
    existing = ""
    if os.path.exists(today_path):
        with open(today_path, encoding="utf-8") as f:
            existing = f.read()

    # ---- 1. 日次繰り越し（直近 todos/ ファイルから） ----
    tasks = get_rollover_tasks()
    taaft_task = check_taaft_staleness()
    if taaft_task:
        tasks = [("定期チェック", taaft_task)] + tasks

    # 既存ファイルのタスク行を正規化してセットに変換（重複チェック用）
    existing_keys = set()
    for ex_line in existing.splitlines():
        if re.match(r"\s*- \[", ex_line):
            existing_keys.add(_normalize(ex_line))

    new_tasks = []
    for from_date, task in tasks:
        if _normalize(task) not in existing_keys:
            new_tasks.append((from_date, task))

    # ---- 2. status.md 浮上タスク ----
    status_tasks = get_status_tasks(full_scan=full_scan)
    new_status_tasks = []
    for dept, section, task in status_tasks:
        if _normalize(task) not in existing_keys:
            new_status_tasks.append((dept, section, task))

    # ---- 出力 ----
    total = len(new_tasks) + len(new_status_tasks)
    if total == 0:
        print("繰り越すタスクはありませんでした。")
        return

    mode_label = "棚卸し（全件）" if full_scan else f"期限{STATUS_URGENT_WINDOW}日以内"
    print(f"{'[DRY RUN] ' if dry_run else ''}繰り越し {len(new_tasks)}件 / status浮上[{mode_label}] {len(new_status_tasks)}件:")
    for from_date, task in new_tasks:
        print(f"  [繰越:{from_date}] {task.strip()}")
    for dept, section, task in new_status_tasks:
        label = f"{dept}/{section}" if section else dept
        print(f"  [status:{label}] {task.strip()}")

    if dry_run:
        return

    sections = ""

    # 繰り越しセクション
    if new_tasks:
        sections += "\n\n## 繰り越しタスク\n\n"
        for from_date, task in new_tasks:
            sections += f"{task} ← {from_date}より繰越\n"

    # status.md 浮上セクション
    if new_status_tasks:
        if full_scan:
            sections += "\n\n## 全部署 status.md 棚卸し\n\n"
            if is_monday:
                sections += "> 月曜棚卸し：全部署の未完了タスクを全件表示\n\n"
            else:
                sections += "> --all フラグによる全件表示\n\n"
        else:
            sections += f"\n\n## status.md 浮上タスク（期限{STATUS_URGENT_WINDOW}日以内・期限超過）\n\n"

        current_label = None
        for dept, section, task in new_status_tasks:
            label = f"{dept} / {section}" if section else dept
            if label != current_label:
                sections += f"### {label}\n"
                current_label = label
            sections += f"{task} ← status.md\n"

    with open(today_path, "a", encoding="utf-8") as f:
        f.write(sections)
    print(f"✅ {today_path} に追記しました。")


if __name__ == "__main__":
    import sys
    dry = "--dry" in sys.argv or "--dry-run" in sys.argv
    force_all = "--all" in sys.argv
    rollover(dry_run=dry, force_all=force_all)
