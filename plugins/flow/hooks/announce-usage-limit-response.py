#!/usr/bin/env python3
"""SessionStart フック: 使用量上限に当たったときに読む規約の在り処を知らせる。

使用量上限は作業のどこででも起きる——レビュー・調査・実行のどれでも、サブエージェントでも。
規約をスキルの中に置くと、そのスキルを読んでいる場面でしか効かない。かといって上限に当たって
から在り処を探すのでは、探す前に自分の判断で動いてしまう。

**規約の中身はここへ写さない。** SessionStart は毎セッション挿入されるので、渡すのは正本の絶対
パスと読む契機だけに切り詰める。

使い方: SessionStart フックとして登録する。--selftest で自己テスト。
"""
import json
import subprocess
import sys
from pathlib import Path

GUIDANCE = Path(__file__).resolve().parents[1] / "docs" / "guidance" / "usage-limit-response.md"


def build_context():
    return f"使用量上限で失敗したら {GUIDANCE} を読んでから動く。"


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
    if not GUIDANCE.is_file():
        ok = False
        print(f"FAIL 正本の規約が無い: {GUIDANCE}")

    cases += 1
    if str(GUIDANCE) not in context:
        ok = False
        print("FAIL 正本の絶対パスが文脈に無い")

    cases += 1
    if "使用量上限" not in context:
        ok = False
        print("FAIL 読む契機が文脈に無い")

    for leaked in ("Fable", "Codex", "取り直す。", "Sonnet"):
        cases += 1
        if leaked in context.replace(str(GUIDANCE), ""):
            ok = False
            print(f"FAIL 規約の中身を文脈へ写している: {leaked}")

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
    if GUIDANCE.name not in injected:
        ok = False
        print(f"FAIL stdin roundtrip: 文脈が注入されない: {out!r}")

    print("ALL PASS" if ok else "SOME FAILED", f"({cases} cases)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
