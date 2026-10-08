"""スタンディングゴール検証スクリプト v1

「一度検証したきりのゴールは、タイムスタンプ付きの思い込みにすぎない」——
完了した恒久対応・再発防止策を company/goals/ に「述語（機械チェック可能な条件）」として
登録し、セッション開始のたびに全件再検証する。違反（FAIL）はブリーフィング冒頭で報告する。

使い方:
  python3 tools/verify_goals.py            # 全ゴール検証（Windowsは py）
  python3 tools/verify_goals.py --list     # ゴール一覧のみ表示（検証しない）

ゴールファイル形式（company/goals/<name>.md）:
  ---
  name: obs-audio-device-refs
  description: 1行説明
  check: obs_audio_refs        ← 本ファイル内の CHECKS に登録された検証タイプ
  os: windows                  ← any / windows / darwin（該当OS以外はSKIP）
  status: active               ← active / retired（retiredは検証しない。削除はしない）
  on_violation: 違反時にどうするかの指示（報告に添えて表示）
  ...（check固有のパラメータ: file / pattern / exclude 等）
  ---

結果は company/goals/ledger.tsv に追記される（日時・OS・ゴール・結果・備考）。
終了コード: 0=全PASS（SKIP含む） / 1=FAILあり（ERROR併発時もFAIL優先で1）
           / 2=FAILは無いがERROR（ゴール定義・述語実行の異常）あり
"""
import glob
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta

# Windowsコンソール(cp932)でも絵文字・em-dash等を出力できるようにする
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOALS_DIR = os.path.join(BASE_DIR, "company", "goals")
LEDGER = os.path.join(GOALS_DIR, "ledger.tsv")
WORKLOG_DIR = os.path.join(BASE_DIR, "company", "secretary", "worklog")


# ---------------------------------------------------------------- goal parsing

def parse_goal(path: str) -> dict:
    """frontmatter（--- 〜 ---）の `key: value` 行を辞書にする。"""
    meta = {"_path": path}
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    if not lines or lines[0].strip() != "---":
        return meta
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta


# ---------------------------------------------------------------- checks
# 各チェックは (ok: bool, note: str) を返す。新しい述語タイプはここに追加する。

def check_git_unpushed(goal: dict):
    """未pushのローカルコミットが無いこと（前セッションのpush忘れ検知）。"""
    try:
        out = subprocess.run(
            ["git", "rev-list", "--count", "@{u}..HEAD"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return True, f"git実行不可のため判定スキップ ({e})"
    if out.returncode != 0:
        # 2拠点同期の生命線なので、upstream未設定はPASS扱いにせず異常として報告
        return None, "upstream未設定（git branch --set-upstream-to で設定すること）"
    n = int(out.stdout.strip() or 0)
    if n > 0:
        return False, f"未pushコミットが {n} 件ある"
    return True, "未pushコミットなし"


def check_status_fields(goal: dict):
    """company/**/status.md 全件に必須フィールド3点が揃っていること。"""
    required = ["next_action:", "next_deadline:", "blocker:"]
    excludes = [x.strip() for x in goal.get("exclude", "").split(",") if x.strip()]
    bad = []
    pattern = os.path.join(BASE_DIR, "company", "**", "status.md")
    for path in sorted(glob.glob(pattern, recursive=True)):
        rel = os.path.relpath(path, BASE_DIR).replace(os.sep, "/")
        if any(ex in rel for ex in excludes):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        missing = [r for r in required if r not in text]
        if missing:
            bad.append(f"{rel}(欠:{'/'.join(m.rstrip(':') for m in missing)})")
    if bad:
        return False, f"必須フィールド欠落 {len(bad)} 件: " + " ; ".join(bad)
    return True, "全status.mdに next_action/next_deadline/blocker あり"


def check_weekly_briefing(goal: dict):
    """今週（直近の日曜以降）に週次ブリーフィング実施記録がworklogにあること。
    実施枠は日曜夜/月曜夜なので、日・月は猶予。火〜土のみ判定する。"""
    today = date.today()
    wd = today.weekday()  # Mon=0 ... Sun=6
    if wd in (6, 0):
        return True, "猶予期間（実施枠＝日曜夜/月曜夜）"
    window_start = today - timedelta(days=(wd + 1) % 7)  # 直近の日曜
    months = {today.strftime("%Y-%m"), window_start.strftime("%Y-%m")}
    found = []
    for m in sorted(months):
        path = os.path.join(WORKLOG_DIR, f"{m}.md")
        if not os.path.exists(path):
            continue
        current = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                h = re.match(r"^##\s*(\d{4}-\d{2}-\d{2})", line)
                if h:
                    current = date.fromisoformat(h.group(1))
                # 「未実施」の部分一致で偽PASSしないよう、除去してから判定する
                cleaned = line.replace("未実施", "")
                if "週次ブリーフィング" in cleaned and "実施" in cleaned and current:
                    found.append(current)
    if any(d >= window_start for d in found):
        return True, f"今週実施済み（{max(d for d in found if d >= window_start)}）"
    last = max(found).isoformat() if found else "記録なし"
    return False, f"今週（{window_start}〜）の実施記録なし。最終実施: {last}"


def check_regex_present(goal: dict):
    """指定ファイルに指定パターンが存在すること。params: file / pattern"""
    rel = goal.get("file", "")
    path = os.path.join(BASE_DIR, rel)
    if not os.path.exists(path):
        return False, f"{rel} が存在しない"
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if re.search(goal.get("pattern", r"$^"), text):
        return True, f"{rel} にパターンあり"
    return False, f"{rel} にパターン `{goal.get('pattern')}` が見つからない"


# OBSの音声デバイス参照。device_id は "{0.0.0.xxxx}.{endpoint-guid}" 形式
# （0.0.0=Render／0.0.1=Capture）。endpoint-guid がレジストリ MMDevices に
# 現存しない＝消滅済みデバイスへの固定参照＝無音インシデントの再発予備軍。
_DEVICE_ID_RE = re.compile(r'\{0\.0\.([01])\.[^}]*\}\.(\{[0-9a-fA-F-]+\})')


def _endpoint_alive(flow: str, guid: str) -> bool:
    # キー存在だけでなく DeviceState==ACTIVE(1) を見る。無効/未接続/非提示の
    # エンドポイントはキーが残っていても音を出せない＝「死」として扱う。
    import winreg
    path = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
            + "\\" + flow + "\\" + guid)
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
    except OSError:
        return False
    try:
        state, _ = winreg.QueryValueEx(k, "DeviceState")
        return state == 1  # DEVICE_STATE_ACTIVE
    except OSError:
        return True  # キーはあるがDeviceState不明 → 保守的に生存扱い
    finally:
        winreg.CloseKey(k)


def check_obs_audio_refs(goal: dict):
    """OBSの全シーンコレクション/プロファイルが参照する音声デバイスGUIDが
    すべて現存すること（2026-07-08 無音インシデントの再発防止）。"""
    obs_dir = os.path.join(os.environ.get("APPDATA", ""), "obs-studio", "basic")
    if not os.path.isdir(obs_dir):
        return True, "OBS設定フォルダなしのため判定スキップ"
    dead = {}  # 表示名 -> [guid...]

    def scan_text(label: str, text: str):
        for flow_bit, guid in set(_DEVICE_ID_RE.findall(text)):
            flow = "Render" if flow_bit == "0" else "Capture"
            if not _endpoint_alive(flow, guid):
                dead.setdefault(label, []).append(guid[:9] + "…}")

    for path in sorted(glob.glob(os.path.join(obs_dir, "scenes", "*.json"))):
        with open(path, encoding="utf-8", errors="replace") as f:
            scan_text("scenes/" + os.path.basename(path), f.read())
    # basic.ini は全文走査（DesktopDevice1/AuxDevice1/MonitoringDeviceId 等、
    # デバイスID形式の値はすべて音声参照。キー名を限定すると走査漏れする）
    for path in sorted(glob.glob(os.path.join(obs_dir, "profiles", "*", "basic.ini"))):
        prof = os.path.basename(os.path.dirname(path))
        with open(path, encoding="utf-8", errors="replace") as f:
            scan_text("profiles/" + prof, f.read())
    if dead:
        detail = " ; ".join(f"{k}: {len(v)}件({','.join(v)})" for k, v in dead.items())
        return False, f"消滅済みデバイスへの固定参照 {sum(len(v) for v in dead.values())} 件 — {detail}"
    return True, "全音声デバイス参照が現存（または既定）"


CHECKS = {
    "git_unpushed": check_git_unpushed,
    "status_fields": check_status_fields,
    "weekly_briefing": check_weekly_briefing,
    "regex_present": check_regex_present,
    "obs_audio_refs": check_obs_audio_refs,
}

OS_MAP = {"windows": "win32", "darwin": "darwin"}


# ---------------------------------------------------------------- runner

def log_ledger(rows):
    os.makedirs(GOALS_DIR, exist_ok=True)
    new = not os.path.exists(LEDGER)
    with open(LEDGER, "a", encoding="utf-8", newline="\n") as f:
        if new:
            f.write("datetime\tos\tgoal\tresult\tnote\n")
        for r in rows:
            f.write("\t".join(str(c).replace("\t", " ").replace("\n", " ") for c in r) + "\n")


def main():
    parsed = [parse_goal(p) for p in sorted(glob.glob(os.path.join(GOALS_DIR, "*.md")))
              if os.path.basename(p) != "README.md"]
    goals = [g for g in parsed if g.get("name")]
    broken = [os.path.basename(g["_path"]) for g in parsed if not g.get("name")]
    if "--list" in sys.argv:
        for g in goals:
            print(f"[{g.get('status', 'active')}] {g['name']} — {g.get('description', '')}")
        return 0
    if not goals:
        print("ゴール未登録（company/goals/ に述語ゴールを追加してください）")
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows, fails, errors = [], [], 0
    print(f"🛡 スタンディングゴール検証 — {now}")
    for fname in broken:  # frontmatter不備＝ゴールが未検証のまま隠れるのを防ぐ
        print(f"  ⚠ ERROR {fname}: frontmatter不備（`---` 区切りまたは name: が無い）")
        rows.append((now, sys.platform, fname, "ERROR", "frontmatter不備"))
        errors += 1
    for g in goals:
        name = g["name"]
        if g.get("status", "active") == "retired":
            continue
        want_os = g.get("os", "any")
        if want_os != "any" and want_os not in OS_MAP:
            print(f"  ⚠ ERROR {name}: 未知のos値 `{want_os}`（any/windows/darwin）")
            rows.append((now, sys.platform, name, "ERROR", f"未知のos: {want_os}"))
            errors += 1
            continue
        if want_os != "any" and sys.platform != OS_MAP.get(want_os):
            print(f"  ⏭ SKIP {name}（{want_os}専用）")
            rows.append((now, sys.platform, name, "SKIP", f"{want_os}専用"))
            continue
        fn = CHECKS.get(g.get("check", ""))
        if not fn:
            print(f"  ⚠ ERROR {name}: 未知のcheckタイプ `{g.get('check')}`")
            rows.append((now, sys.platform, name, "ERROR", f"未知のcheck: {g.get('check')}"))
            errors += 1
            continue
        try:
            ok, note = fn(g)
        except Exception as e:  # 述語の実行失敗は違反扱いにせず ERROR として報告
            ok, note = None, f"述語実行エラー: {e}"
        if ok is True:
            print(f"  ✅ PASS {name} — {note}")
            rows.append((now, sys.platform, name, "PASS", note))
        elif ok is False:
            print(f"  🔴 FAIL {name} — {note}")
            if g.get("on_violation"):
                print(f"       ↳ 対応: {g['on_violation']}")
            rows.append((now, sys.platform, name, "FAIL", note))
            fails.append(name)
        else:
            print(f"  ⚠ ERROR {name} — {note}")
            rows.append((now, sys.platform, name, "ERROR", note))
            errors += 1
    log_ledger(rows)
    if fails:
        print(f"→ 違反 {len(fails)} 件: {', '.join(fails)}（ブリーフィング冒頭で報告すること）")
        return 1
    if errors:
        print("→ 述語エラーあり（ゴール定義を確認）")
        return 2
    print("→ 全ゴール維持。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
