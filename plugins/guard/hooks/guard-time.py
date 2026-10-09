#!/usr/bin/env python3
"""PreToolUse フック: `date` による時計の変更を deny する。

  * 時計を変える形(-s/--set、および位置引数による日時指定)-> deny
  * 読み取り専用の形(素の date・+FORMAT・-u/-R/-I・-d/--date <引数> など)-> 何も出力せず通す
  * 複合コマンド・展開・置換・リダイレクトを含む形 -> 何も出力せず通す

PowerShell ツールの発行では、Set-Date と w32tm を deny する。w32tm は引数を読まないので、状態を見る
だけの形も巻き込む。それ以外は通常の許可フローに委ねる。

使い方: Bash・PowerShell の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import json
import re
import shlex
import sys

from hook_input import read_command
from shell_words import command_positions, head_name, segments

# 次のトークンを引数として食う date のフラグ。いずれも読み取り専用。
TAKES_ARG = {"-d", "--date", "-r", "--reference", "-f", "--file"}
PS_CLOCK_SETTERS = ("set-date", "w32tm")
NOT_STANDALONE_STATIC = re.compile(r"[$`<>;&|\n(]")


def decide(cmd):
    """単独の静的な呼び出しで、命令の位置にある `date` が時計を変えるなら "deny"。それ以外は None(通す)。"""
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    if NOT_STANDALONE_STATIC.search(cmd):
        return None
    try:
        tokens = shlex.split(cmd, posix=True)
    except Exception:
        return None
    start = next((i for i in command_positions(tokens) if tokens[i] == "date"), None)
    if start is None:
        return None

    args = tokens[start + 1:]
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
    return None


def decide_powershell(cmd):
    """PowerShell の発行が時計を変えるなら "deny"。それ以外は None(通す)。"""
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    for tokens in segments(cmd) or []:
        if head_name(tokens[0]) in PS_CLOCK_SETTERS:
            return "deny"
    return None


def decide_cmd(cmd):
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    if decide_powershell(cmd) == "deny":
        return "deny"
    lexer = shlex.shlex(cmd.replace("\n", "\n;"), posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    parts = []
    current = []
    for token in tokens + [";"]:
        if token[:1] in ";&|":
            if current:
                parts.append(current)
            current = []
        else:
            current.append(token)
    for segment in parts:
        name = segment[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        if name in ("date", "time") and [arg.lower() for arg in segment[1:]] != ["/t"]:
            return "deny"
    return None


def main():
    # ハーネスが渡す JSON は UTF-8。既定の符号化で読むと非ASCII が化けて素通りする。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tool, command = read_command("--codex" in sys.argv)
    if tool is None:
        sys.exit(0)
    if tool == "CommandPrompt":
        decision = decide_cmd(command)
    elif tool == "PowerShell":
        decision = decide_powershell(command)
    else:
        decision = decide(command)
    if decision != "deny":
        sys.exit(0)

    reader = {"PowerShell": "Get-Date", "CommandPrompt": "date /t または time /t"}.get(tool, "date")
    breadth = "w32tm は状態を見るだけの形も deny する。" if tool == "PowerShell" else ""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"時計の変更は不可。現在時刻は読み取り専用の {reader} を使う。{breadth}"
            "PowerShellのSet-Date・w32tm・pythonのos/time経由での時刻変更等、"
            "別の手段で同じ変更を回避して実行しないこと。"
        ),
    }}))


def selftest():
    cases = [
        ("素の date", "date", None),
        ("書式指定", "date +%Y-%m-%d", None),
        ("UTC 表示", "date -u", None),
        ("RFC 表示", "date -R", None),
        ("値を連結した短フラグ", "date -Iseconds", None),
        ("値を別トークンで取る短フラグ", "date -d 20:03", None),
        ("長フラグの等号形", "date --date=now", None),
        ("参照ファイル", "date -r f", None),
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
    ps_cases = [
        ("時計を設定するコマンドレット", "Set-Date -Date '2030-01-01'", "deny"),
        ("パイプラインの後ろに置いた設定", "Get-Date | Set-Date", "deny"),
        ("時刻同期ツール", "w32tm /resync", "deny"),
        ("読み取りは通常の許可フローへ", "Get-Date", None),
        ("設定を言及しただけの形", "Write-Output 'Set-Date'", None),
        ("空のコマンド", "", None),
    ]
    ok = True
    for why, cmd, want in cases:
        got = decide(cmd)
        if got != want:
            ok = False
            print(f"FAIL {why}: want={want} got={got} :: {cmd!r}")
    for why, cmd, want in ps_cases:
        got = decide_powershell(cmd)
        if got != want:
            ok = False
            print(f"FAIL PowerShell {why}: want={want} got={got} :: {cmd!r}")
    print("ALL PASS" if ok else "SOME FAILED", f"({len(cases) + len(ps_cases)} cases)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
