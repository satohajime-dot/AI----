#!/usr/bin/env python3
"""
Obsidian の related: frontmatter を一括付与するスクリプト（秘書メソッド同梱・汎用版）

フォルダ構成から関連リンクを自動導出するので、設定ファイルなしでそのまま使える。
  company/secretary/{notes,todos,worklog,inbox}/  -> [[company/secretary/CLAUDE]]
  company/<部署>/**                                -> [[company/<部署>/status]]
  library/**                                       -> [[library/index]]

自動導出で足りないとき（例: ある部署のノートのうち特定案件だけ案件ページにも紐づけたい）は、
プロジェクト直下に related-map.json を置くと、そちらが優先される。

  {
    "company/sales/notes/proj-a": ["[[company/sales/status]]", "[[company/sales/projects]]"],
    "library/AI/":                ["[[library/index]]"]
  }

キーは相対パスの部分一致（長いキーから順に照合）。値は related に入れるリンクの配列。

Usage:
  python3 tools/add_related_links.py --dry-run    # 変更内容の確認のみ（推奨・最初はこれ）
  python3 tools/add_related_links.py             # 実際に書き込む
  python3 tools/add_related_links.py --root /path/to/project
（Windows は python3 → py に読み替える）
"""
import argparse
import datetime
import json
import os
import re
import stat
import sys
import tempfile

MAP_FILENAME = "related-map.json"

SKIP_DIRS = [".git", ".obsidian", ".claude", "node_modules", "__pycache__", "_temp", "_work"]
SKIP_PATTERNS = ["library/index.md", "HOME.md", "WELCOME.md"]  # ハブ側はリンク元なので触らない
SKIP_FILENAMES = ["CLAUDE.md", "README.md", "MANIFEST.md", "VERSION"]

FENCE = "---"


class MapError(Exception):
    """related-map.json が壊れている（処理を続行してはいけない）。"""


def validate_link(link, where):
    """YAML のダブルクォート文字列に安全に入れられる値か検査する。"""
    if not isinstance(link, str) or not link.strip():
        raise MapError(f'{where}: 空または文字列でない値があります')
    if '"' in link or "\\" in link or "\n" in link or "\r" in link:
        raise MapError(f'{where}: リンクに使えない文字（" \\ 改行）が含まれています: {link!r}')
    return link


def load_override_map(root):
    """related-map.json があれば読む。壊れていれば MapError で中止する。"""
    for candidate in (os.path.join(root, MAP_FILENAME), os.path.join(root, "tools", MAP_FILENAME)):
        if not os.path.isfile(candidate):
            continue
        shown = os.path.relpath(candidate, root)
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise MapError(f"{shown} を読めません: {e}")
        if not isinstance(data, dict):
            raise MapError(f"{shown} はキー→配列のオブジェクトである必要があります")
        pairs = []
        for key, links in data.items():
            if isinstance(links, str):
                links = [links]
            if not isinstance(links, list):
                raise MapError(f'{shown} の "{key}" の値は文字列または文字列配列にしてください')
            pairs.append((key.replace("\\", "/"), [validate_link(x, f'{shown} の "{key}"') for x in links]))
        # 長いキーを先に照合（より具体的な指定を優先）
        pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
        return pairs, candidate
    return [], None


def derive_related(rel):
    """相対パスからフォルダ構成のルールで related を自動導出する。"""
    parts = rel.split("/")

    if rel.startswith("library/"):
        return ["[[library/index]]"]

    if parts[0] == "company" and len(parts) >= 3:
        dept = parts[1]
        if dept in ("secretary", "goals"):
            return ["[[company/secretary/CLAUDE]]"]
        # 部署の status.md 自身はハブ側なので、秘書へ紐づける
        if rel == f"company/{dept}/status.md":
            return ["[[company/secretary/CLAUDE]]"]
        return [f"[[company/{dept}/status]]"]

    return None


def get_related(rel, override_pairs):
    for pattern, links in override_pairs:
        if pattern in rel:
            return links
    return derive_related(rel)


def read_text(path):
    """改行コードと BOM を保ったまま読む。戻り値は (本文, BOMの有無)。"""
    with open(path, "rb") as f:
        raw = f.read()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    if has_bom:
        raw = raw[3:]
    return raw.decode("utf-8"), has_bom


def detect_newline(text):
    """そのファイルで最も多い改行コードを返す（新しく挿入する行に使う）。"""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    best = max((crlf, "\r\n"), (lf, "\n"), (cr, "\r"), key=lambda x: x[0])
    return best[1] if best[0] else "\n"


def resolve_target(path, root):
    """実際に書き込む先（シンボリックリンクなら実体）を返す。root の外を指すなら None。

    プロジェクト内のリンクが外部を指している場合に、対象範囲外のファイルを書き換えないため。
    """
    target = os.path.realpath(path)
    root_real = os.path.realpath(root)
    if os.path.commonpath([target, root_real]) != root_real:
        return None
    return target


def write_atomic(target, text, has_bom):
    """一時ファイルに書いてから置換する（中断しても元ファイルが壊れない）。

    - text には呼び出し側が行ごとの改行コードを保った状態で入れてあるので、バイト列で書く
      （テキストモードの newline 変換に任せると CRLF が \r\r\n に壊れる）
    - 元ファイルの権限を引き継ぐ（一時ファイル既定の 0600 のままにしない）
    """
    directory = os.path.dirname(target) or "."
    data = (b"\xef\xbb\xbf" if has_bom else b"") + text.encode("utf-8")
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".related-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)  # 失敗したら例外にする（0600 のまま置換しない）
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def strip_ending(line):
    return line.rstrip("\r\n")


def split_frontmatter(lines):
    """(開始index, 終了index) を返す。frontmatter が無い/閉じていなければ None。

    終端は「行頭から --- だけの行」に限る。字下げされた `  ---`（YAMLブロックスカラーの
    中身など）は終端として扱わない。
    """
    if not lines or strip_ending(lines[0]) != FENCE:
        return None
    for i in range(1, len(lines)):
        if strip_ending(lines[i]) == FENCE:
            return (0, i)
    return None  # 閉じていない = 触らない


def header_looks_sane(header_lines):
    """frontmatter の中身が「キー: 値」のマッピングとして読めるかを軽く検査する。

    PyYAML は使えない（標準ライブラリだけで動かす）ので完全な検証はしない。目的は
    「触ると余計に壊れるファイルを見分けて手を出さない」こと。次を異常とみなす:
      - `---xxx` のような、キーでも継続行でもない行が混じっている
      - ルートが配列（先頭の実質行が `- item`）＝ related: キーを足せない構造
    複数行のフロー配列（`related: [` … 行頭 `]`）は正常として通す。
    """
    depth = 0          # フロー collection ( [ や { ) のネスト深さ
    first_real = True
    for line in header_lines:
        s = strip_ending(line)
        if depth > 0:                     # 閉じ括弧待ちの途中はすべて許容
            depth += s.count("[") + s.count("{") - s.count("]") - s.count("}")
            depth = max(depth, 0)
            continue
        if not s.strip() or s.lstrip().startswith("#"):
            continue                      # 空行・コメント
        if first_real and re.match(r"^-\s", s):
            return False                  # ルートが配列 = マッピングではない
        first_real = False
        if re.match(r"^\s+\S", s) or re.match(r"^\s*-\s", s):
            pass                          # 字下げされた継続行・リスト項目
        elif re.match(r"^[^:#\s][^:]*\s*:", s):
            pass                          # キー: 値
        else:
            return False
        depth += s.count("[") + s.count("{") - s.count("]") - s.count("}")
        depth = max(depth, 0)
    return depth == 0                     # 閉じ忘れも異常扱い


def related_block_indices(header_lines):
    """header 内の related: キー（複数行の配列継続行も含む）の行番号集合を返す。"""
    marked = []
    i = 0
    while i < len(header_lines):
        if re.match(r"^related\s*:", header_lines[i]):
            marked.append(i)
            j = i + 1
            # 未閉鎖のフロー配列（related: [ … 行頭 ]）の継続行も回収する
            s = strip_ending(header_lines[i])
            depth = max(s.count("[") + s.count("{") - s.count("]") - s.count("}"), 0)
            while j < len(header_lines) and (
                depth > 0 or re.match(r"^(\s+\S|\s*-\s)", header_lines[j])
            ):
                cur = strip_ending(header_lines[j])
                depth = max(depth + cur.count("[") + cur.count("{")
                            - cur.count("]") - cur.count("}"), 0)
                marked.append(j)
                j += 1
            i = j
        else:
            i += 1
    return marked


def format_related(links):
    return "related: [" + ", ".join(f'"{x}"' for x in links) + "]"


def apply_related(lines, links, newline):
    """既存 frontmatter の related を1つに統一する（各行の改行コードは保つ）。"""
    span = split_frontmatter(lines)
    if span is None:
        return None

    start, end = span
    header = lines[start + 1:end]
    if not header_looks_sane(header):
        return "insane"  # 壊れた frontmatter は触らない
    marked = set(related_block_indices(header))
    desired_line = format_related(links) + newline

    if len(marked) == 1:
        only = sorted(marked)[0]
        if strip_ending(header[only]) == format_related(links):
            return lines  # すでに望む形

    insert_at = min(marked) if marked else len(header)
    new_header = [l for idx, l in enumerate(header) if idx not in marked]
    new_header.insert(min(insert_at, len(new_header)), desired_line)
    return [lines[start]] + new_header + lines[end:]


def build_new_frontmatter(links, filepath, newline):
    basename = os.path.basename(filepath)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", basename)
    if m:
        date = m.group(1)
    else:
        # 日付がファイル名に無ければ最終更新日を使う（固定値を埋め込まない）
        date = datetime.date.fromtimestamp(os.path.getmtime(filepath)).isoformat()
    head = [FENCE, f"date: {date}", format_related(links), FENCE, ""]
    return [l + newline for l in head]


def process_file(filepath, rel, override_pairs, dry_run, root):
    links = get_related(rel, override_pairs)
    if links is None:
        return "skip_no_rule", None

    try:
        text, has_bom = read_text(filepath)
    except (OSError, UnicodeDecodeError) as e:
        print(f"⚠ 読めないので飛ばします: {rel}（{e}）")
        return "skip_unreadable", None

    newline = detect_newline(text)
    lines = text.splitlines(keepends=True)  # 各行の改行コードを保持する

    if split_frontmatter(lines) is None:
        if lines and strip_ending(lines[0]) == FENCE:
            # 開いたまま閉じていない frontmatter = 壊すリスクがあるので触らない
            print(f"⚠ frontmatter が閉じていないので飛ばします: {rel}")
            return "skip_unreadable", None
        new_lines = build_new_frontmatter(links, filepath, newline) + lines
        mode = "new"
    else:
        new_lines = apply_related(lines, links, newline)
        if new_lines == "insane":
            print(f"⚠ frontmatter の形が想定外なので飛ばします（手で直してください）: {rel}")
            return "skip_unreadable", None
        if new_lines == lines:
            return "skip_uptodate", links
        mode = "update"

    new_text = "".join(new_lines)
    if new_text == text:
        return "skip_uptodate", links

    target = resolve_target(filepath, root)
    if target is None:
        print(f"⚠ プロジェクト外を指すリンクなので飛ばします: {rel}")
        return "skip_unreadable", None

    if not dry_run:
        try:
            write_atomic(target, new_text, has_bom)
        except OSError as e:
            print(f"❌ 書き込めませんでした（元ファイルは無傷）: {rel}（{e}）")
            return "failed", links
    return mode, links


def collect_files(root):
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            if not filename.endswith(".md") or filename in SKIP_FILENAMES:
                continue
            filepath = os.path.join(dirpath, filename)
            rel = os.path.relpath(filepath, root).replace("\\", "/")
            if any(p in rel for p in SKIP_PATTERNS):
                continue
            result.append((filepath, rel))
    return sorted(result, key=lambda x: x[1])


def main():
    parser = argparse.ArgumentParser(
        description="Obsidian の related: frontmatter を一括付与する（秘書メソッド同梱）",
        epilog="未知のオプションはエラーになります（打ち間違いで意図せず書き込むのを防ぐため）。",
    )
    parser.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                        help="書き込まずに変更内容だけ表示する（最初はこれで確認する）")
    parser.add_argument("--root", default=None, help="対象プロジェクトのルート（既定: カレントディレクトリ）")
    args = parser.parse_args()

    root = os.path.abspath(args.root) if args.root else os.getcwd()
    if not os.path.isdir(root):
        print(f"エラー: フォルダが見つかりません: {root}")
        return 1

    try:
        override_pairs, map_path = load_override_map(root)
    except MapError as e:
        print(f"❌ {MAP_FILENAME} が正しくありません。修正してから再実行してください。\n   {e}")
        return 1

    print("=== DRY RUN（書き込みません） ===" if args.dry_run else "=== 実行（書き込みます） ===")
    if map_path:
        print(f"上書きマップ: {os.path.relpath(map_path, root)}（{len(override_pairs)}件）")
    else:
        print(f"上書きマップ: なし（フォルダ構成から自動導出。{MAP_FILENAME} を置けば上書き可）")

    counts = {"new": 0, "update": 0, "skip_uptodate": 0, "skip_no_rule": 0,
              "skip_unreadable": 0, "failed": 0}
    for filepath, rel in collect_files(root):
        result, links = process_file(filepath, rel, override_pairs, args.dry_run, root)
        counts[result] += 1
        if result in ("new", "update"):
            tag = "🆕" if result == "new" else "➕"
            print(f"{tag} {rel}\n   → {', '.join(links)}")

    print("\n=== 完了 ===")
    print(f"  🆕 frontmatter 新規追加: {counts['new']}件")
    print(f"  ➕ related 更新:         {counts['update']}件")
    print(f"  ⏭  すでに最新:           {counts['skip_uptodate']}件")
    print(f"  ❓ 対象ルールなし:       {counts['skip_no_rule']}件")
    if counts["skip_unreadable"]:
        print(f"  ⚠ 読めない/未閉鎖で飛ばした: {counts['skip_unreadable']}件")
    if counts["failed"]:
        print(f"  ❌ 書き込み失敗:         {counts['failed']}件")
    if args.dry_run:
        print("\n※ --dry-run のため書き込んでいません。内容に納得したら --dry-run を外して再実行してください。")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
