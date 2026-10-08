"""今日のTODOファイルの繰り越しセクションを重複除去する"""
import re, sys, os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODOS_DIR = os.path.join(BASE_DIR, "company", "secretary", "todos")

from datetime import date
path = os.path.join(TODOS_DIR, f"{date.today()}.md")

if not os.path.isfile(path):
    # 導入直後や、その日まだTODOファイルを作っていない場合。異常ではないので静かに終わる
    print(f"今日のTODOファイルがまだありません（{os.path.relpath(path, BASE_DIR)}）。何もしませんでした。")
    sys.exit(0)

try:
    with open(path, encoding="utf-8") as f:
        content = f.read()
except OSError as e:
    print(f"TODOファイルを読めませんでした: {e}")
    sys.exit(1)

def normalize(line):
    base = re.sub(r"\s*←.*$", "", line)
    base = re.sub(r"\s*—\s*最終記録:.*$", "", base)
    return re.sub(r"[\*\[\]]", "", base).strip()

lines = content.splitlines()

cutoff = None
for i, line in enumerate(lines):
    if line.strip() == "## 繰り越しタスク":
        cutoff = i
        break

if cutoff is None:
    print("繰り越しセクションが見つかりませんでした")
    sys.exit(0)

header_part = lines[:cutoff]
rollover_part = lines[cutoff:]

existing_keys = set()
for line in header_part:
    if re.match(r"\s*- \[", line):
        existing_keys.add(normalize(line))

new_rollover = []
for line in rollover_part:
    if line.startswith("#"):
        new_rollover.append(line)
    elif re.match(r"\s*- \[", line):
        key = normalize(line)
        if key not in existing_keys:
            existing_keys.add(key)
            new_rollover.append(line)
        # else: 重複 → スキップ
    else:
        new_rollover.append(line)

result = "\n".join(header_part + new_rollover)
with open(path, "w", encoding="utf-8") as f:
    f.write(result)

orig = sum(1 for l in rollover_part if re.match(r"\s*- \[", l))
deduped = sum(1 for l in new_rollover if re.match(r"\s*- \[", l))
print(f"重複除去完了: {orig}件 -> {deduped}件")
