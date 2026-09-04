#!/usr/bin/env python3
"""SessionStart フック: 停止宣言の規約をセッション開始時に知らせる。

知らせなければ、エージェントは必ず初回の停止で `guard-idle-stop.py` にブロックされる。deny
メッセージが規約を教えるので回復はするが、教わるために1ターンを確実に失う。

知らせるのは、**宣言の形、選べる値の名前、選び分けるのに要る一行の説明、宣言に先立つ必須の動作**に
限る。その値を選んでよいかの基準——区分外に当たらないか、待機を裏取りできているか——と、それを
確かめた旨の申告、失敗したときの手立て(時間待ちの手段)は、その停止を弾く deny が渡す。

宣言の文字列はあちらから読み込む——別々に持つと、片方が古くなったときに「教わったとおりに書いた
のに弾かれる」形になる。説明文はここが正本(deny メッセージとは長さの制約が違うので、同じ文言に
ならない)。

使い方: プラグインルートを第1引数に渡す SessionStart フックとして登録する。--selftest で自己テスト。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "guard-idle-stop.py"


def load_guard():
    """判定する側を取り込む。`__main__` ガードが効くので `main()` は走らない。"""
    spec = importlib.util.spec_from_file_location("_guard_idle_stop", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_context():
    guard = load_guard()
    return (
        "手番を返すときは理由を宣言する。末尾行を次のいずれかだけにする。\n"
        f"- {guard.DONE} — このセッションで受けた指示の全部が済んだ\n"
        f"- {guard.DECISION} — ユーザーの判断が要る。末尾行の前に"
        f"「{guard.DECISION_FIELD}: <区分>」の1行を置く"
        f"(区分は {'・'.join(guard.DECISION_KINDS)} のいずれか)\n"
        f"- {guard.WAIT} — 完了を待つ\n"
        f"- {guard.RESPOND} — 問われたことに答えたが、まだ済んでいない指示が残っている。"
        f"末尾行の前に「{guard.RESPOND_FIELD}: <何が残っているか>」の1行を置く\n"
        "完了・要判断・応答は、宣言の前に PushNotification を送る。"
    )


def main():
    # UTF-8 を明示する。既定の符号化で読むと日本語が化ける。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": build_context(),
    }}))


def selftest():
    ok, cases = True, 0
    context = build_context()
    cases += 1
    missing = [m for m in load_guard().MARKERS if m not in context]
    if missing:
        ok = False
        print(f"FAIL 正本の宣言が文脈に無い: {missing}")
    markers = ("[停止: 完了]", "[停止: 要判断]", "[停止: 待機]", "[停止: 応答]")
    for marker in markers:
        cases += 1
        if marker not in context:
            ok = False
            print(f"FAIL 宣言が文脈に無い: {marker}")
        cases += 1
        if f"`{marker}`" in context:
            ok = False
            print(f"FAIL 宣言を囲み記号で包んで提示している: {marker}")
    guard = load_guard()
    for phrase in (guard.DECISION_FIELD, guard.RESPOND_FIELD, *guard.DECISION_KINDS):
        cases += 1
        if phrase not in context:
            ok = False
            print(f"FAIL 区分の宣言の形が文脈に無い: {phrase}")
    for phrase in ("PushNotification", "完了・要判断・応答", "宣言の前", "指示の全部", "残っている"):
        cases += 1
        if phrase not in context:
            ok = False
            print(f"FAIL 手順の要点が文脈に無い: {phrase}")
    cases += 1
    if "末尾行" not in context:
        ok = False
        print("FAIL 末尾行の要求が文脈に無い")
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
    print("ALL PASS" if ok else "SOME FAILED", f"({cases} cases)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
