#!/usr/bin/env python3
"""メール／チャットのやりとりを、日時の昇順に並べ替えて「最後に投げたのは誰か」を出す。

なぜ要るのか
------------
やりとりの状態（「相手が未回答」「こちらが未対応」）を、読んだ順・見た順で判断すると
逆の結論になることがある。原因は主に2つ。

  1. **検索結果は部分リストであることが多い。** メール検索・チャット検索の類は
     スレッドの全件を返すとは限らない（新しい/古い一部だけのことがある）。
     部分リストに見当たらないことを「送っていない証拠」として使うと誤る。
     判断には全件（スレッド全体を取得する API・エクスポート）を使うこと。

  2. **引用チェーンは入れ子の外側が新しい。** メールクライアントの引用は
     「新しい本文の下に、引用された古いメールが入れ子で続く」形が多く、
     上から読んで最初に出会ったメッセージを最新と誤認しやすい。

人の目で入れ子を追う限り再発する。だから判断の前に、このツールへ通して
タイムスタンプで並べ替えてから「相手が未回答／こちらが未対応」を述べる。

「自分」の判定
--------------
どのアドレス／名前を「自分」とみなすかは、以下の優先順で決める。

  1. `--me` 引数（複数指定可・カンマ区切りも可。例: `--me a@example.com,b@example.com`）
  2. 環境変数 `SECRETARY_ME`（カンマ区切り可）
  3. `company/secretary/me.txt`（プロジェクトルート相対。1行に1つ・# で始まる行は注記）
  4. どれも無ければエラー終了（`--me` を指定するか `company/secretary/me.txt` を作ること、と案内する）

アドレスの一致判定は大小文字を区別しない部分一致で行う。

使い方
------
  # 推奨: メールAPI等のスレッド取得結果（JSON）を保存して渡す（全件が入る）
  python3 tools/mail_timeline.py --json <thread.json>

  # 引用チェーンの本文テキストからでも組める（日本語の引用ヘッダを拾う）
  # 例: 「2026年8月25日(火) 19:14 山田太郎 <taro@example.co.jp>:」
  python3 tools/mail_timeline.py --text <chain.txt>

  # 自分側の判定に使う文字列（省略時は SECRETARY_ME → company/secretary/me.txt）
  python3 tools/mail_timeline.py --json <f> --me you@example.com

  # 自己検査（standing goal 等から呼ばれる想定）
  python3 tools/mail_timeline.py --selftest

終了コード: 0=正常 / 1=日時が1件も取れなかった・自己検査失敗・自分の判定に失敗
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME_FILE = os.path.join(BASE_DIR, "company", "secretary", "me.txt")

# 日本語の引用ヘッダ: 「2026年8月25日(火) 19:14 山田太郎 <taro@example.co.jp>:」
QUOTE_HEADER = re.compile(
    r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日"
    r"(?:\([^)]*\))?\s*"
    r"(\d{1,2}):(\d{2})\s*"
    r"(.{0,60}?)\s*<([^>\s]+@[^>\s]+)>"
)


class Msg:
    __slots__ = ("when", "who", "addr", "head")

    def __init__(self, when: dt.datetime, who: str, addr: str, head: str = ""):
        self.when, self.who, self.addr, self.head = when, who, addr, head


def parse_text(body: str) -> list[Msg]:
    """引用チェーンの本文から (日時, 差出人, アドレス) を拾う。入れ子順は信用しない。"""
    out = []
    for m in QUOTE_HEADER.finditer(body):
        y, mo, d, hh, mm = (int(m.group(i)) for i in range(1, 6))
        name = re.sub(r"^[>\s]+", "", m.group(6)).strip()
        out.append(Msg(dt.datetime(y, mo, d, hh, mm), name, m.group(7).strip()))
    return out


def parse_json(raw: str) -> list[Msg]:
    """スレッド取得APIの出力（{"messages":[{"date":...,"sender":...}, ...]}）から組む。
    検索API形式（{"threads":[{"messages":[...]}]}）にも対応する。"""
    data = json.loads(raw)
    msgs = data.get("messages")
    if msgs is None:  # 検索結果形式（threads[].messages[]）
        msgs = [m for t in data.get("threads", []) for m in t.get("messages", [])]
    out = []
    for m in msgs:
        iso = (m.get("date") or "").replace("Z", "+00:00")
        if not iso:
            continue
        when = dt.datetime.fromisoformat(iso)
        # 日本時間に寄せる（表示のみ。並べ替えは絶対時刻で行うので結論は変わらない）
        when = (when.astimezone(dt.timezone(dt.timedelta(hours=9))) if when.tzinfo else when).replace(tzinfo=None)
        sender = (m.get("sender") or "").strip()
        out.append(Msg(when, sender, sender, (m.get("snippet") or "")[:40]))
    return out


def resolve_me(cli_me: list[str] | None) -> list[str]:
    """「自分」と判定する文字列の一覧を返す。--me → SECRETARY_ME → me.txt の順。
    どれも取れなければ ValueError（呼び出し側でエラー終了する）。"""
    if cli_me:
        values = []
        for v in cli_me:
            values += [x.strip() for x in v.split(",") if x.strip()]
        if values:
            return values

    env = os.environ.get("SECRETARY_ME", "")
    if env.strip():
        return [x.strip() for x in env.split(",") if x.strip()]

    if os.path.isfile(ME_FILE):
        # 1行に1つ（アドレスを複数持つ人がいる）。カンマ区切りも受ける。# で始まる行は注記
        with open(ME_FILE, encoding="utf-8") as f:
            values = [x.strip() for line in f if not line.strip().startswith("#")
                      for x in line.split(",") if x.strip()]
        if values:
            return values

    raise ValueError(
        "「自分」と判定するアドレスが指定されていません。\n"
        "  次のいずれかで指定してください。\n"
        "    1) --me you@example.com\n"
        "    2) 環境変数 SECRETARY_ME=you@example.com\n"
        f"    3) {ME_FILE} にアドレスを書く（1行に1つ）\n"
    )


_ADDR = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def _is_me(addr: str, me_list: list[str]) -> bool:
    """差出人（「山田 <taro@example.co.jp>」の形でもよい）が自分か。**アドレスの完全一致**で判定する。
    部分一致にすると notaro@example.co.jp や taro@example.co.jp.evil.test まで自分扱いになり、
    「相手が未回答／こちらが未回答」の結論が逆転する。"""
    found = {x.lower() for x in _ADDR.findall(addr or "")}
    if not found:
        return False
    return any(me.strip().lower() in found for me in me_list if me and me.strip())


def report(msgs: list[Msg], me_list: list[str], today: dt.datetime | None = None) -> int:
    if not msgs:
        print("日時が1件も取れませんでした。入力の形式を確認してください。")
        return 1
    msgs = sorted(msgs, key=lambda x: x.when)  # ← 読み順ではなく必ず日時で並べる
    print(f"通数 {len(msgs)} 件（日時の昇順・古い順）")
    print("-" * 72)
    for m in msgs:
        arrow = "→送信" if _is_me(m.addr, me_list) else "←受信"
        head = f"  {m.head}" if m.head else ""
        print(f"  {m.when:%Y-%m-%d %H:%M}  {arrow}  {m.who}{head}")
    print("-" * 72)

    last = msgs[-1]
    mine = _is_me(last.addr, me_list)
    now = today or dt.datetime.now()
    elapsed = (now.date() - last.when.date()).days
    print(f"最後の発言者: {last.who}（{last.when:%Y-%m-%d %H:%M}・{elapsed}日前）")
    print("判定: " + ("相手が未回答（こちらが最後に投げている）" if mine
                      else "こちらが未回答（相手が最後に投げている）"))

    # 各方向の最終発言も出す。「相手からも来ているが、その後こちらが投げた」を見落とさないため。
    for label, cond in (("こちら側の最終送信", True), ("相手側の最終受信", False)):
        hit = [m for m in msgs if _is_me(m.addr, me_list) is cond]
        if hit:
            print(f"  {label}: {hit[-1].when:%Y-%m-%d %H:%M}  {hit[-1].who}")
        else:
            print(f"  {label}: なし")
    if not msgs[0].head:
        print("※ --text は引用ヘッダのない最外側（＝最新）の1通を拾えません。手で足すこと。")
    return 0


def selftest() -> int:
    """入れ子の逆順・部分リスト・壊れた入力で、意図どおり振る舞うかを確かめる。

    「道具を作ったら、わざと壊して落ちることを確かめる」（fix-discipline の考え方）。
    フィクションの登場人物のみを使い、me.txt / SECRETARY_ME には依存しない
    （selftest 内では「自分」を明示的に渡す）。
    """
    fails = []
    ME = "taro@example.co.jp"

    # 1. 引用チェーン（新しいものが上＝入れ子の外側）を渡して、昇順に直せるか。
    chain = (
        "2026年8月25日(火) 19:14 山田太郎 <taro@example.co.jp>:\n"
        "> 質問2本お願いします\n"
        "> 2026年8月25日(火) 16:30 鈴木花子 <hanako@example.com>:\n"
        ">> お申込み書類は送付いただけておりましたでしょうか\n"
        ">> 2026年8月5日(水) 16:33 鈴木花子 <hanako@example.com>:\n"
        ">>> 4点回答いたします\n"
    )
    got = sorted(parse_text(chain), key=lambda x: x.when)
    if len(got) != 3:
        fails.append(f"- 引用チェーンの抽出件数が3件でない（{len(got)}件）")
    elif [m.when.strftime("%m-%d %H:%M") for m in got] != ["08-05 16:33", "08-25 16:30", "08-25 19:14"]:
        fails.append("- 昇順に並べ替えられていない")
    elif got[-1].addr != ME:
        fails.append(f"- 最後の発言者を取り違えている（{got[-1].addr}）")

    # 2. 読み順のまま（＝並べ替えなし）だと結論が逆になることを確かめる。
    #    これが崩れたら、そもそもこの道具が守っている性質が消えている。
    raw = parse_text(chain)
    if raw and raw[-1].addr == ME:
        fails.append("- 読み順の末尾が既に正しい＝逆順チェーンの再現になっていない")

    # 3. JSON（スレッド取得形式・順不同）を渡して昇順になるか。
    js = json.dumps({"messages": [
        {"date": "2026-08-25T10:46:32Z", "sender": "hanako@example.com"},
        {"date": "2026-08-04T09:50:00Z", "sender": "taro@example.co.jp"},
        {"date": "2026-08-25T10:14:00Z", "sender": "taro@example.co.jp"},
    ]})
    gotj = sorted(parse_json(js), key=lambda x: x.when)
    if [m.when.strftime("%m-%d") for m in gotj] != ["08-04", "08-25", "08-25"]:
        fails.append("- JSON入力を昇順に並べ替えられていない")

    # 3b. 「自分」はアドレスの完全一致。似たアドレスを自分扱いしないこと。
    for other in ("notaro@example.co.jp", "taro@example.co.jp.evil.test", "Taro <xtaro@example.co.jp>"):
        if _is_me(other, [ME]):
            fails.append(f"- 別人のアドレスを自分と判定している（{other}）")
    if not _is_me("山田太郎 <TARO@example.co.jp>", [ME]):
        fails.append("- 表示名つき・大文字のアドレスを自分と判定できていない")

    # 4. 壊れた入力・空入力で、黙って通さないこと。
    if parse_text("日時のない本文だけ") != []:
        fails.append("- 日時のない本文から偽の日時を作っている")
    with contextlib.redirect_stdout(io.StringIO()):  # 検査中の警告文を混ぜない
        empty_rc = report([], [ME])
    if empty_rc != 1:
        fails.append("- 空入力で終了コード1を返していない")
    try:
        parse_json("{ これはJSONではない")
    except json.JSONDecodeError:
        pass
    else:
        fails.append("- 壊れたJSONを例外にせず通している")

    # 5. 「自分」未指定（--me も SECRETARY_ME も me.txt も無い）ならエラーにすること。
    env_bak = os.environ.pop("SECRETARY_ME", None)
    me_file_bak_exists = os.path.isfile(ME_FILE)
    try:
        if not me_file_bak_exists:
            try:
                resolve_me(None)
            except ValueError:
                pass
            else:
                fails.append("- 自分の判定が無いのにエラーにしていない")
    finally:
        if env_bak is not None:
            os.environ["SECRETARY_ME"] = env_bak

    if fails:
        print("自己検査 FAIL")
        for f in fails:
            print(f)
        return 1
    print("自己検査 PASS")
    print("mail_timeline.py OK（引用の逆順・JSON順不同・壊れた入力・自分未指定の5系統）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="やりとりを日時の昇順に並べ、最後の発言者を出す")
    ap.add_argument("--json", help="スレッド取得APIの出力JSONファイル")
    ap.add_argument("--text", help="引用チェーンの本文テキストファイル")
    ap.add_argument(
        "--me",
        action="append",
        help="自分側と判定する文字列（複数指定可・カンマ区切り可）。"
             "省略時は環境変数 SECRETARY_ME、次に company/secretary/me.txt を見る",
    )
    ap.add_argument("--selftest", action="store_true", help="自己検査のみ行う")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if not a.json and not a.text:
        ap.error("--json か --text のいずれかを指定してください（--selftest も可）")

    try:
        me_list = resolve_me(a.me)
    except ValueError as e:
        print(str(e))
        return 1

    msgs: list[Msg] = []
    if a.json:
        with open(a.json, encoding="utf-8") as f:
            msgs += parse_json(f.read())
    if a.text:
        with open(a.text, encoding="utf-8") as f:
            msgs += parse_text(f.read())
    return report(msgs, me_list)


if __name__ == "__main__":
    sys.exit(main())
