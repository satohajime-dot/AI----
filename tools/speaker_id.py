#!/usr/bin/env python3
"""録音の文字起こしに「誰が話したか」を付ける（話者判別）。

なぜ必要か:
  ボイスメモの文字起こしには話者の区別がない。懇親会・立ち話のような雑音環境では
  pyannote 系の話者分離（クラスタリング）が使えなかった（2026-08-04 実測）。
    - 閾値クラスタリング → クラスタが数百に過分割
    - 話者数を2に固定 → 相手の発話が本人と同じクラスタに 4/6 件混入
  一方、あらかじめ声紋を持っている人との照合（speaker verification）なら解ける。
  本ツールはクラスタリングではなく、この照合を土台にしている。

方式（2026-08-04 に検証。保留アンカーでの分離は **AUC 1.00**）:
  1. 内容から本人しか言い得ない発話を「アンカー」に指定する（--anchor）
     → その区間の音声から話者embeddingを取り、平均して本人の声紋を作る
  2. 相手側は**人ごとに参照発話**を登録する（--neg）
     相手が複数人いる場合、平均して1つの声紋にすると弱くなるため個別に持つ
  3. 各発話のスコア = （本人の声紋との類似度）−（相手の誰かとの最大類似度）
     符号で振り分ける。単一声紋との類似度だけで見る方式は AUC 0.83〜0.94 で
     頭打ちだったが、この差分にすると完全分離した
  4. アンカーの半分を検証用に取り置いてAUCと「空白帯」を実測し、閾値を決める
  5. 目的が本人の発言を落とすことなら、**判定が微妙な帯は本人側に寄せる**

モデル（sherpa-onnx・HuggingFaceのトークン不要・完全オフライン・CPU）:
  pyannote.audio 3.1 はゲート付きモデルの規約同意（ブラウザ操作）が必要で自動化できない。
  sherpa-onnx は同等のモデルをGitHubリリースからトークンなしで配布している。
  単体でのアンカー分離（窓分割なし・全区間embedding）は次のとおりだった。
    campplus_zh 0.92-0.94 ／ titanet_large 0.90-0.94 ／ eres2net_vox 0.82-0.88
    wespeaker_en_voxceleb_resnet34_LM は全発話が0.93付近に張り付き使えず（0.59）
  **1.5〜3秒の窓に刻むと悪化する**（0.67〜0.88）。発話区間まるごとで取るのが良い。

準備（Windows / Mac / Linux 共通・初回のみ）:
    pip install numpy sherpa-onnx faster-whisper      # Apple Silicon なら mlx-whisper でもよい
    python3 tools/speaker_id.py --download-models     # 声紋モデル2種を取得（約100MB・トークン不要）
    ffmpeg も要る（Windows: winget install Gyan.FFmpeg ／ Mac: brew install ffmpeg）

使い方A（推奨・声紋を先に登録する）:
    # 0) 参加者ごとに1回だけ。本人だけが30秒以上しゃべっている音声を渡す
    python3 tools/speaker_id.py enroll 佐野 temp/sano-30sec.m4a
    python3 tools/speaker_id.py enroll 安藤 temp/ando-30sec.m4a
    # 1) 会議の録音を文字起こし
    python3 tools/speaker_id.py transcribe temp/録音.m4a --out local/speaker_id
    # 2) 登録済みの声紋で振り分ける（会話の中身を知らなくても実行できる）
    python3 tools/speaker_id.py assign local/speaker_id/録音 --profile 佐野 --profile 安藤

使い方B（従来・その場のアンカーで振り分ける）:
    声紋を登録していない過去の録音向け。会話の中身を知っている人しか使えない。
    python3 tools/speaker_id.py assign local/speaker_id/録音 \
        --speaker "佐野:ドーナツ全体のAI推進" --speaker "安藤:基本的に全員使ってる"
    # 「自分か否か」の二値でよければ judge（アンカー＋相手の参照発話）

チャネル適応（assign が --profile 使用時に自動で行う）:
  登録済み声紋は別の日・別のマイクで録ったものなので、当日の音響条件とずれる。
  1回目で確信度の高かった区間から重心を録音側へ寄せ直し、もう一度振り分ける。
  2026-08-28 実測（8/19の声紋 → 8/26の録音）でアンカー方式との一致が
  89.9% → 94.0%、「不明」が 86件 → 72件。切るなら --no-adapt。

出力:
  <name>.assigned.txt … [mm:ss] 名前(スコア,d=差) 本文
  <name>.judged.txt  … [mm:ss] ラベル(スコア) 本文   ラベル = 本人 / 相手 / 不明 / 短
  いずれも .json の機械可読版が並んで出る

注意:
  - **声紋は個人情報**。既定の保存先 local/voiceprints/ は gitignore 配下。録音・登録は
    本人に断ってから行うこと
  - 判定は補助。採否は必ず会話の順番（質問→回答）と内容で最終確認する
  - アンカー方式（使い方B）では、アンカー自体を取り違えることがある（2026-08-04 に実際に発生）。
    「相手」と思った発話が本人側に強く出たら、まず**自分の思い込みを疑う**
  - `--neg` は**相手の人数分**（各人1件以上）。登録漏れの人の発話は本人側に倒れる
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import wave

import numpy as np

# Windows の既定コンソールは cp932 なので、日本語や記号を print した瞬間に
# UnicodeEncodeError で落ちる。出力の符号化はここで固定する（2026-08-22）。
# 述語: tools/check_stdout_utf8.py / company/goals/tools-force-utf8-stdout.md
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SR = 16000
MODEL_DIR = os.path.expanduser("~/.cache/sherpa-diarize")
BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"
MODELS = {
    "campplus": "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
    "titanet": "nemo_en_titanet_large.onnx",
}


def download_models():
    os.makedirs(MODEL_DIR, exist_ok=True)
    for fn in MODELS.values():
        p = os.path.join(MODEL_DIR, fn)
        if os.path.exists(p):
            print("skip", fn)
            continue
        print("download", fn, flush=True)
        subprocess.run(["curl", "-sSL", "-o", p, f"{BASE}/{fn}"], check=True)
    print("done ->", MODEL_DIR)


def load_wav(path):
    with wave.open(path) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1, "16kHz・モノラルにすること"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def to_wav(src, dst):
    """任意の音声 → 16kHz モノラル WAV。

    ffmpeg があればそれを使う（Windows/Mac/Linux 共通）。無い場合だけ macOS 同梱の
    afconvert に落とす。以前は afconvert 決め打ちで、macOS 以外では動かなかった。
    """
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", src, "-vn",
                        "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", dst],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("afconvert"):
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{SR}", "-c", "1",
                        src, dst], check=True)
    else:
        sys.exit("ffmpeg が要る（Windows: winget install Gyan.FFmpeg ／ Mac: brew install ffmpeg）")


def run_whisper(wav, model, engine):
    """文字起こし。mlx（Apple Silicon 専用）と faster-whisper（どこでも動く）を選ぶ。

    engine="auto" は mlx を試し、入っていなければ faster-whisper に落ちる。
    戻り値は [{"start","end","text"}, ...]。
    """
    if engine in ("auto", "mlx"):
        try:
            import mlx_whisper
        except ImportError:
            if engine == "mlx":
                sys.exit("mlx_whisper が入っていない（Apple Silicon 専用。--engine faster を使うこと）")
        else:
            r = mlx_whisper.transcribe(load_wav(wav), language="ja",
                                       path_or_hf_repo=model or "mlx-community/whisper-large-v3-turbo",
                                       condition_on_previous_text=False, verbose=False)
            return [{"start": x["start"], "end": x["end"], "text": x["text"].strip()}
                    for x in r["segments"]]
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper が入っていない（pip install faster-whisper）")
    m = WhisperModel(model or "large-v3-turbo", device="auto", compute_type="int8")
    it, _ = m.transcribe(wav, language="ja", condition_on_previous_text=False, vad_filter=True)
    return [{"start": x.start, "end": x.end, "text": x.text.strip()} for x in it]


def cmd_transcribe(args):
    os.makedirs(args.out, exist_ok=True)
    name = os.path.splitext(os.path.basename(args.audio))[0]
    wav = os.path.join(args.out, f"{name}.wav")
    if not os.path.exists(wav):
        to_wav(args.audio, wav)
    segs = run_whisper(wav, args.model, args.engine)
    json.dump(segs, open(os.path.join(args.out, f"{name}.segments.json"), "w", encoding="utf-8"), ensure_ascii=False)
    with open(os.path.join(args.out, f"{name}.txt"), "w", encoding="utf-8") as f:
        for s in segs:
            m, sec = divmod(int(s["start"]), 60)
            f.write(f"[{m:02d}:{sec:02d}] {s['text']}\n")
    print(f"{len(segs)} segments -> {args.out}/{name}.segments.json")


DEFAULT_PROFILE_DIR = os.path.join("local", "voiceprints")


def make_extractor(model_name, threads):
    import sherpa_onnx
    path = os.path.join(MODEL_DIR, MODELS[model_name])
    if not os.path.exists(path):
        sys.exit(f"モデルがない: {path}（--download-models を実行）")
    return sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=path, num_threads=threads))


def embed_span(ex, audio, a, b):
    """[a, b) サンプルの区間から正規化済み embedding を1本取る（取れなければ None）"""
    st = ex.create_stream()
    st.accept_waveform(SR, audio[a:b])
    st.input_finished()
    if not ex.is_ready(st):
        return None
    v = np.array(ex.compute(st), dtype=np.float32)
    n = np.linalg.norm(v)
    return None if n == 0 else v / n


def energy_chunks(audio, min_sec=1.0, max_sec=8.0, gap_sec=0.3, pad_sec=0.15):
    """登録用クリップを「発話のかたまり」に切る（無音で区切るだけの簡易VAD）。

    判定側は1〜数秒の発話区間ごとに embedding を取る。登録側だけ30秒まるごと1本で
    取ると長さの分布がずれて類似度が下がるため、同じくらいの長さに刻んでから平均する。
    """
    fl = int(0.02 * SR)
    n = len(audio) // fl
    if n < 2:
        return []
    rms = np.sqrt(np.mean(audio[:n * fl].reshape(n, fl) ** 2, axis=1))
    floor, peak = np.percentile(rms, 20), np.percentile(rms, 95)
    thr = max(floor + 0.15 * (peak - floor), 1e-4)
    voiced = rms > thr

    runs, start, gap = [], None, 0
    for i, v in enumerate(list(voiced) + [False]):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > gap_sec / 0.02 or i == n:
                runs.append((start, i - gap + 1))
                start, gap = None, 0

    pad, out = int(pad_sec * SR), []
    for a, b in runs:
        a, b = max(0, a * fl - pad), min(len(audio), b * fl + pad)
        step = int(max_sec * SR)
        for c in range(a, b, step):
            d = min(c + step, b)
            if d - c >= min_sec * SR:
                out.append((c, d))
    return out                       # 1つも取れない＝無音。呼び出し側で弾く


def cmd_enroll(args):
    """本人の声を1回だけ録って、名前つきの声紋として保存する。

    これがあると assign で --anchor / --speaker を指定しなくてよくなる。
    アンカー方式は「会話の中身を知っている人」しか使えなかったので、配布の障壁だった。
    """
    os.makedirs(args.profile_dir, exist_ok=True)
    out = os.path.join(args.profile_dir, f"{args.name}.npz")
    if os.path.exists(out) and not args.force:
        sys.exit(f"すでにある: {out}（録り直すなら --force）")

    wav = os.path.join(args.profile_dir, f"{args.name}.enroll.wav")
    to_wav(args.audio, wav)
    audio = load_wav(wav)
    chunks = energy_chunks(audio)
    total = sum(b - a for a, b in chunks) / SR
    print(f"{args.name}: 音声 {len(audio)/SR:.1f}秒 → 発話 {len(chunks)}個 / 計 {total:.1f}秒")
    if not chunks:
        sys.exit("発話が1つも見つからない（無音・音量が極端に小さい・録音に失敗している）。"
                 "音声を再生して声が入っているか確かめること")
    if total < args.min_sec:
        sys.exit(f"発話が {total:.1f}秒 しかない（{args.min_sec:.0f}秒以上を推奨）。"
                 "静かな場所で、本人だけが続けて話している音声を使うこと")

    vecs = {}
    for mname in MODELS:
        ex = make_extractor(mname, args.threads)
        vs = [v for v in (embed_span(ex, audio, a, b) for a, b in chunks) if v is not None]
        if not vs:
            sys.exit(f"{mname}: embedding が1本も取れなかった")
        m = np.mean(vs, axis=0)
        vecs[mname] = m / np.linalg.norm(m)
        cons = float(np.mean([v @ vecs[mname] for v in vs]))
        print(f"  {mname}: {len(vs)}本を平均（まとまり {cons:.3f}）")
        if cons < args.min_consistency:
            print(f"  ※まとまりが低い。別人の声や雑音が混ざっている可能性がある")

    meta = {"name": args.name, "seconds": round(total, 1), "chunks": len(chunks),
            "source": os.path.basename(args.audio)}
    np.savez(out, models=np.array(sorted(vecs)), meta=np.array(json.dumps(meta, ensure_ascii=False)),
             **{f"v_{k}": v for k, v in vecs.items()})
    print(f"-> {out}")


def load_profiles(reqs, pdir, names):
    """登録済み声紋を読み、assign が使う重心と同じ形 {モデル名: ベクトル} で返す"""
    out = {}
    for r in reqs:
        path = r if r.endswith(".npz") else os.path.join(pdir, f"{r}.npz")
        who = os.path.splitext(os.path.basename(path))[0]
        if not os.path.exists(path):
            sys.exit(f"声紋がない: {path}（先に enroll すること）")
        d = np.load(path)
        got = sorted(str(x) for x in d["models"])
        if got != sorted(names):
            sys.exit(f"{who} の声紋はモデル構成が違う（{'・'.join(got)}）。enroll し直すこと")
        out[who] = {n: d[f"v_{n}"] for n in names}
    return out


def cmd_profiles(args):
    if not os.path.isdir(args.profile_dir):
        print(f"まだ1件も登録されていない: {args.profile_dir}")
        return
    found = sorted(f for f in os.listdir(args.profile_dir) if f.endswith(".npz"))
    if not found:
        print(f"まだ1件も登録されていない: {args.profile_dir}")
        return
    for f in found:
        d = np.load(os.path.join(args.profile_dir, f))
        m = json.loads(str(d["meta"])) if "meta" in d else {}
        print(f"  {f[:-4]:<16} {m.get('seconds','?')}秒 / 発話{m.get('chunks','?')}個 / 元: {m.get('source','?')}")


def embed_all(stem, segs, audio, threads):
    """全発話のembeddingを計算してキャッシュする（アンカーを変えても再計算しない）"""
    import sherpa_onnx
    out = {}
    for name, fn in MODELS.items():
        cache = f"{stem}.emb.{name}.npz"
        if os.path.exists(cache):
            d = np.load(cache)
            out[name] = {int(i): v for i, v in zip(d["idx"], d["vecs"])}
            print(f"  {name}: キャッシュを使用（{len(out[name])}件）")
            continue
        path = os.path.join(MODEL_DIR, fn)
        if not os.path.exists(path):
            sys.exit(f"モデルがない: {path}（--download-models を実行）")
        ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=path, num_threads=threads))
        idx, vecs = [], []
        for i, sg in enumerate(segs):
            a, b = sg["start"], min(sg["end"], len(audio) / SR)
            if b - a < 0.8:                      # 0.8秒未満は判定に使わない
                continue
            st = ex.create_stream()
            st.accept_waveform(SR, audio[int(a * SR):int(b * SR)])
            st.input_finished()
            if not ex.is_ready(st):
                continue
            v = np.array(ex.compute(st), dtype=np.float32)
            n = np.linalg.norm(v)
            if n == 0:
                continue
            idx.append(i)
            vecs.append(v / n)
        np.savez(cache, idx=np.array(idx), vecs=np.array(vecs))
        out[name] = {i: v for i, v in zip(idx, vecs)}
        print(f"  {name}: {len(idx)}/{len(segs)} 件を計算 -> {cache}", flush=True)
    return out


def cmd_assign(args):
    """複数話者を、人ごとのアンカーで振り分ける（judge の多値版）。

    judge は「本人か否か」の二値だが、会議録音では出席者ごとに分けたい。
    各人のアンカーから重心を作り、発話ごとに最も近い人を選ぶ。
    1位と2位の差が --margin 未満なら「不明」に落とす（無理に割り当てない）。
    """
    stem = args.stem
    audio = load_wav(f"{stem}.wav")
    segs = json.load(open(f"{stem}.segments.json", encoding="utf-8"))
    emb = embed_all(stem, segs, audio, args.threads)
    names = list(emb)
    common = set.intersection(*[set(emb[n]) for n in names])

    spk = {}
    for item in args.speaker or []:
        if ":" not in item:
            sys.exit(f'--speaker は "名前:発話の一部" の形式で指定すること: {item}')
        who, key = item.split(":", 1)
        for i, sg in enumerate(segs):
            if key in sg["text"]:
                if i in common:
                    spk.setdefault(who, []).append(i)
                else:
                    print(f"  ※{who} のアンカーが短くて使えない: {key}")
                break
        else:
            print(f"  ※{who} のアンカーが文字起こしに見つからない: {key}")
    prof = load_profiles(args.profile or [], args.profile_dir, names)
    dup = sorted(set(prof) & set(spk))
    if dup:
        sys.exit(f"同じ名前が --profile と --speaker の両方にある: {'・'.join(dup)}")
    if len(spk) + len(prof) < 2:
        sys.exit("話者を2人以上、--profile（登録済み声紋）か --speaker（アンカー）で指定すること")

    def centroid(idxs):
        c = {}
        for n in names:
            v = np.mean([emb[n][i] for i in idxs], axis=0)
            c[n] = v / np.linalg.norm(v)
        return c

    def sim(i, cent):
        return float(np.mean([emb[n][i] @ cent[n] for n in names]))

    for who in prof:
        print(f"  {who}: 登録済み声紋")
    for who, idxs in spk.items():
        print(f"  {who}: アンカー {len(idxs)} 件")

    # 交差検証（2件以上ある人のアンカーを1件ずつ抜いて当てる）
    hold = {w: v[1::2] for w, v in spk.items() if len(v) >= 2}
    if hold and sum(len(v) for v in hold.values()) >= 2:
        tr = {w: (v[0::2] if len(v) >= 2 else v) for w, v in spk.items()}
        cents = {**prof, **{w: centroid(v) for w, v in tr.items() if v}}
        ok = tot = 0
        for who, idxs in hold.items():
            for i in idxs:
                pred = max(cents, key=lambda w: sim(i, cents[w]))
                ok += (pred == who)
                tot += 1
        print(f"\n検証: 保留アンカー {tot} 件の的中 {ok}/{tot}")

    def label_all(cents):
        out = []
        for i, sg in enumerate(segs):
            if i not in common:
                out.append({**sg, "speaker": "短", "score": None, "margin": None})
                continue
            ss = sorted(((sim(i, c), w) for w, c in cents.items()), reverse=True)
            top, second = ss[0], (ss[1] if len(ss) > 1 else (0.0, None))
            gap = top[0] - second[0]
            who = top[1] if gap >= args.margin else "不明"
            out.append({**sg, "speaker": who, "score": round(top[0], 3), "margin": round(gap, 3)})
        return out

    cents = {**prof, **{w: centroid(v) for w, v in spk.items()}}
    rows = label_all(cents)

    # チャネル適応: 登録済み声紋は「別の日・別のマイク」で録ったものなので、そのままだと
    # 当日の音響条件とずれる。1回目で確信度の高かった区間だけを使って重心をこの録音側へ
    # 寄せ直し、もう一度振り分ける。2026-08-28 実測（8/19の声紋→8/26の録音）で
    # アンカー方式との一致が 89.9% → 94.0%、「不明」も 86件 → 72件 に改善した。
    if prof and not args.no_adapt:
        moved, ad = [], {}
        for w, c in cents.items():
            idx = [i for i, r in enumerate(rows)
                   if r["speaker"] == w and r["margin"] and r["margin"] >= args.adapt_threshold]
            if len(idx) < max(1, args.adapt_min):
                ad[w] = c
                continue
            ad[w] = centroid(idx)
            moved.append(f"{w} {len(idx)}件")
        if moved:
            print("チャネル適応:", "・".join(moved), "で重心を録音側へ寄せ直した")
            rows = label_all(ad)

    with open(f"{stem}.assigned.txt", "w", encoding="utf-8") as f:
        for r in rows:
            m, sec = divmod(int(r["start"]), 60)
            f.write(f"[{m:02d}:{sec:02d}] {r['speaker']}({r['score']},d={r['margin']}) {r['text']}\n")
    json.dump(rows, open(f"{stem}.assigned.json", "w", encoding="utf-8"), ensure_ascii=False)
    from collections import Counter
    print("内訳:", dict(Counter(r["speaker"] for r in rows)), "->", f"{stem}.assigned.txt")
    if prof and not spk:
        print("※登録済み声紋だけで判定した。抜き取りで2〜3か所、実際の発言者と照合すること")


def cmd_judge(args):
    stem = args.stem
    audio = load_wav(f"{stem}.wav")
    segs = json.load(open(f"{stem}.segments.json", encoding="utf-8"))
    emb = embed_all(stem, segs, audio, args.threads)
    names = list(emb)
    common = set.intersection(*[set(emb[n]) for n in names])

    def find(keys, label):
        out = []
        for k in keys or []:
            for i, s in enumerate(segs):
                if k in s["text"]:
                    if i in common:
                        out.append(i)
                    else:
                        print(f"  ※{label}が短くて使えない: {k}")
                    break
            else:
                print(f"  ※{label}が文字起こしに見つからない: {k}")
        return out

    pos = find(args.anchor, "アンカー")
    neg = find(args.neg, "相手の参照")
    if len(pos) < 3:
        sys.exit(f"アンカーが {len(pos)} 件しかない。文字起こしの表記に合わせて増やすこと")
    if not neg:
        sys.exit("--neg（相手と確定できる発話）を人数分指定すること")

    def centroid(idxs):
        c = {}
        for n in names:
            v = np.mean([emb[n][i] for i in idxs], axis=0)
            c[n] = v / np.linalg.norm(v)
        return c

    def score(i, cent, refs):
        a = float(np.mean([emb[n][i] @ cent[n] for n in names]))
        b = max(float(np.mean([emb[n][i] @ emb[n][j] for n in names])) for j in refs)
        return a - b

    # 交差検証（アンカーを2分割）で分離性能と空白帯を測る
    lo, hi = args.lo, args.hi
    if len(pos) >= 6:
        tr, he = pos[0::2], pos[1::2]
        c = centroid(tr)
        ps = [score(i, c, neg) for i in he]
        ns = [score(i, c, neg) for i in neg]
        a = sum((p > n) + 0.5 * (p == n) for p in ps for n in ns) / (len(ps) * len(ns))
        print(f"\n検証: 本人(保留){len(ps)}件 vs 相手{len(ns)}件 → AUC {a:.2f}")
        print("  本人:", " ".join(f"{x:+.3f}" for x in sorted(ps)))
        print("  相手:", " ".join(f"{x:+.3f}" for x in sorted(ns)))
        if lo is None:
            lo = max(ns) - args.margin      # 相手と言い切る側は余裕を持たせる
        if hi is None:
            hi = min(ps)
    if lo is None or hi is None:
        lo, hi = -0.10, 0.00
    print(f"閾値: 相手<= {lo:+.3f} / 不明 / 本人>= {hi:+.3f}")

    cent = centroid(pos)
    rows = []
    for i, sg in enumerate(segs):
        s = score(i, cent, neg) if i in common else None
        lab = "短" if s is None else ("本人" if s >= hi else "相手" if s <= lo else "不明")
        rows.append({**sg, "score": None if s is None else round(s, 3), "label": lab})
    with open(f"{stem}.judged.txt", "w", encoding="utf-8") as f:
        for r in rows:
            m, sec = divmod(int(r["start"]), 60)
            f.write(f"[{m:02d}:{sec:02d}] {r['label']}({r['score']}) {r['text']}\n")
    json.dump(rows, open(f"{stem}.judged.json", "w", encoding="utf-8"), ensure_ascii=False)
    from collections import Counter
    print("内訳:", dict(Counter(r["label"] for r in rows)), "->", f"{stem}.judged.txt")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--download-models", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    t = sub.add_parser("transcribe", help="m4a等→wav＋セグメントJSON")
    t.add_argument("audio")
    t.add_argument("--out", default="local/speaker_id")
    t.add_argument("--model", default=None, help="既定: mlx=whisper-large-v3-turbo / faster=large-v3-turbo")
    t.add_argument("--engine", choices=["auto", "mlx", "faster"], default="auto",
                   help="auto=mlxがあれば使い、無ければfaster-whisper")
    t.set_defaults(func=cmd_transcribe)

    j = sub.add_parser("judge", help="声紋照合で本人/相手を判定")
    j.add_argument("stem", help="<dir>/<name>（.wav と .segments.json の共通部分）")
    j.add_argument("--anchor", action="append", help="本人しか言い得ない発話の一部（10件前後）")
    j.add_argument("--neg", action="append", help="相手と確定できる発話の一部（人数分）")
    j.add_argument("--margin", type=float, default=0.07, help="相手側の閾値に持たせる余裕")
    j.add_argument("--lo", type=float, default=None)
    j.add_argument("--hi", type=float, default=None)
    j.add_argument("--threads", type=int, default=8)
    j.set_defaults(func=cmd_judge)

    g = sub.add_parser("assign", help="複数話者を人ごとのアンカーで振り分ける")
    g.add_argument("stem", help="<dir>/<name>（.wav と .segments.json の共通部分）")
    g.add_argument("--speaker", action="append", help='"名前:発話の一部"。1人につき2件以上あると検証が効く')
    g.add_argument("--profile", action="append", help="登録済み声紋の名前（enroll で作ったもの）")
    g.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR)
    g.add_argument("--margin", type=float, default=0.03, help="1位と2位の差がこれ未満なら不明にする")
    g.add_argument("--no-adapt", action="store_true", help="チャネル適応（2回目の振り分け）をしない")
    g.add_argument("--adapt-threshold", type=float, default=0.15, help="適応に使う区間の最低margin")
    g.add_argument("--adapt-min", type=int, default=10, help="適応に必要な区間数")
    g.add_argument("--threads", type=int, default=8)
    g.set_defaults(func=cmd_assign)

    e = sub.add_parser("enroll", help="本人の声を録って名前つきの声紋として登録する")
    e.add_argument("name", help="話者名（ファイル名になる）")
    e.add_argument("audio", help="その人だけが話している音声（30秒以上を推奨）")
    e.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR)
    e.add_argument("--min-sec", type=float, default=15.0, help="必要な実発話の秒数")
    e.add_argument("--min-consistency", type=float, default=0.55, help="これ未満なら警告を出す")
    e.add_argument("--force", action="store_true", help="既存の声紋を上書きする")
    e.add_argument("--threads", type=int, default=8)
    e.set_defaults(func=cmd_enroll)

    pl = sub.add_parser("profiles", help="登録済みの声紋を一覧する")
    pl.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR)
    pl.set_defaults(func=cmd_profiles)

    a = ap.parse_args()
    if a.download_models:
        download_models()
        return
    if not getattr(a, "func", None):
        ap.print_help()
        return
    a.func(a)


if __name__ == "__main__":
    main()
