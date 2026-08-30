#!/usr/bin/env python3
"""SessionStart hook: 停止宣言の規約をセッション開始時に知らせる。

`guard-idle-stop.py` は停止を既定で禁じ、末尾行が停止宣言のものだけを通す。宣言の存在を知る手段が
無ければ、エージェントは**必ず初回の停止でブロックされる**——全セッション・全リポジトリで1ターンが
確定的に失われる。deny メッセージが規約を教えるので回復はするが、**教わるために一度失敗する形は、
先に知らせれば消せる**。

**宣言の文字列は `guard-idle-stop.py` から読み込む**——判定する側と知らせる側で別々に持つと、片方が
古くなったときに「教わったとおりに書いたのに弾かれる」形になる。読み込みは通常のモジュール取り込みで
行い、あちらの内部構造(関数の並びや定義位置)に依存しない。

**説明文はここが正本**で、あちらの deny メッセージとは別に持つ。あちらは失敗した相手へ何をすべきかを
詳しく述べる場所で、ここは全セッションの開始時に必ず入る場所——長さの制約が違うので、同じ文言には
ならない。取り違えないよう、宣言の文字列だけを共有する。

Usage: configured as a SessionStart hook. Run with --selftest.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "guard-idle-stop.py"


def load_guard():
    """判定する側を通常のモジュールとして取り込む。`__main__` ガードが効くので `main()` は走らない。"""
    spec = importlib.util.spec_from_file_location("_guard_idle_stop", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_context():
    """停止宣言の規約文を組み立てる。宣言の文字列は判定する側から取る。"""
    guard = load_guard()
    return (
        "手番を返すときは理由を宣言する。末尾行を次のいずれかだけにする。\n"
        f"- {guard.DONE} — 作業が終わった\n"
        f"- {guard.DECISION} — ユーザーの判断が要る\n"
        f"- {guard.WAIT} — 完了を待つ(背景処理か予約済み起床が在るときのみ)"
    )


def main():
    # 符号化を固定する。ハーネスが渡す JSON は UTF-8 で、既定の符号化で読むと日本語が化ける。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if not isinstance(data, dict) or data.get("hook_event_name") != "SessionStart":
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": build_context(),
    }}))


def selftest():
    ok, cases = True, 0
    context = build_context()
    # 正本の宣言をそのまま持ってきていること
    # 宣言が追加されたとき、その宣言についてだけ「知る手段が無い」状態へ戻ることを防ぐ。
    # 説明文は自動生成できないので build_context は名指しで並べる——集合の一致はここで担保する。
    cases += 1
    missing = [m for m in load_guard().MARKERS if m not in context]
    if missing:
        ok = False
        print(f"FAIL 正本の宣言が文脈に無い: {missing}")
    markers = ("[停止: 完了]", "[停止: 要判断]", "[停止: 待機]")
    for marker in markers:
        cases += 1
        if marker not in context:
            ok = False
            print(f"FAIL 宣言が文脈に無い: {marker}")
        # 囲み記号を付けて提示すると、そのまま末尾行へ写した相手が判定側に弾かれる。
        cases += 1
        if f"`{marker}`" in context:
            ok = False
            print(f"FAIL 宣言を囲み記号で包んで提示している: {marker}")
    cases += 1
    if "末尾行" not in context:
        ok = False
        print("FAIL 末尾行の要求が文脈に無い")
    # ハーネスと同じ形で起動し、SessionStart の出力が返ること
    payload = json.dumps({"hook_event_name": "SessionStart"}, ensure_ascii=False).encode("utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=payload, capture_output=True, check=False,
    )
    try:
        out = json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        ok = False
        print(f"FAIL stdin roundtrip: 出力が JSON でない: {result.stdout[:200]!r}")
        out = {}
    cases += 1
    injected = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    if "[停止: 完了]" not in injected:
        ok = False
        print(f"FAIL stdin roundtrip: 文脈が注入されない: {out!r}")
    # SessionStart 以外では何も出さないこと
    other = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=json.dumps({"hook_event_name": "Stop"}).encode("utf-8"),
        capture_output=True, check=False,
    )
    cases += 1
    if other.stdout.strip():
        ok = False
        print(f"FAIL 非 SessionStart で出力した: {other.stdout[:200]!r}")
    print("ALL PASS" if ok else "SOME FAILED", f"({cases} cases)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
