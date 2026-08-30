#!/usr/bin/env python3
"""PreToolUse フック: `date` の読み取りを無プロンプトで承認し、時計の変更を deny する。

  * 読み取り専用の形(素の date・+FORMAT・-u/-R/-I・-d/--date <引数> など)-> allow
  * 時計を変える形(-s/--set、および位置引数による日時指定)-> deny
  * 複合コマンド・展開・置換・リダイレクトを含む形 -> 何も出力せず通し、通常の許可フローに委ねる

使い方: Bash の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import json
import re
import shlex
import sys

# 次のトークンを引数として食う date のフラグ。いずれも読み取り専用。
TAKES_ARG = {"-d", "--date", "-r", "--reference", "-f", "--file"}
NOT_STANDALONE_STATIC = re.compile(r"[$`<>;&|\n(]")


def decide(cmd):
    """単独の静的な `date` に対する "allow"/"deny"。それ以外は None(通す)。"""
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    if NOT_STANDALONE_STATIC.search(cmd):
        return None
    try:
        tokens = shlex.split(cmd, posix=True)
    except Exception:
        return None
    if not tokens or tokens[0] != "date":
        return None

    args = tokens[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        # date で時計を変えるのは set だけ(-s・--set とその短縮形)。
        if arg.startswith(("-s", "--s")):
            return "deny"
        if arg in TAKES_ARG:
            i += 2
        elif arg.startswith("+") or arg.startswith("--"):
            i += 1
        elif arg.startswith("-") and (len(arg) == 2 or (len(arg) > 2 and arg[1] in "dfrI")):
            i += 1
        else:
            return "deny"
    return "allow"


def main():
    # ハーネスが渡す JSON は UTF-8。既定の符号化で読むと非ASCII が化けて素通りする。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if data.get("tool_name") != "Bash":
        sys.exit(0)

    command = (data.get("tool_input") or {}).get("command")
    decision = decide(command)
    if decision is None:
        sys.exit(0)

    out = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    if decision == "deny":
        out["permissionDecisionReason"] = (
            "date による時計変更は不可。現在時刻は読み取り専用の date を使う。"
            "PowerShellのSet-Date・w32tm・pythonのos/time経由での時刻変更等、"
            "別の手段で同じ変更を回避して実行しないこと。"
        )
    print(json.dumps({"hookSpecificOutput": out}))


def selftest():
    cases = [
        ("素の date", "date", "allow"),
        ("書式指定", "date +%Y-%m-%d", "allow"),
        ("UTC 表示", "date -u", "allow"),
        ("RFC 表示", "date -R", "allow"),
        ("値を連結した短フラグ", "date -Iseconds", "allow"),
        ("値を別トークンで取る短フラグ", "date -d 20:03", "allow"),
        ("長フラグの等号形", "date --date=now", "allow"),
        ("参照ファイル", "date -r f", "allow"),
        ("set の短形式", "date -s 2030-01-01", "deny"),
        ("set の長形式", "date --set=2030-01-01", "deny"),
        ("set の短縮形", "date --se 2030", "deny"),
        ("短フラグ束に紛れた set", "date -us2030", "deny"),
        ("読み取りフラグだけの束", "date -uR", "deny"),
        ("位置引数による日時指定", "date 010203", "deny"),
        ("複合コマンド", "date; ls", None),
        ("コマンド置換", "date -d $(x)", None),
        ("リダイレクト", "date > out", None),
        ("date でないコマンド", "ls", None),
        ("空のコマンド", "", None),
    ]
    ok = True
    for why, cmd, want in cases:
        got = decide(cmd)
        if got != want:
            ok = False
            print(f"FAIL {why}: want={want} got={got} :: {cmd!r}")
    print("ALL PASS" if ok else "SOME FAILED", f"({len(cases)} cases)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
