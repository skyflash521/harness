#!/usr/bin/env python3
"""PreToolUse フック: `sed -i` を deny し、インプレース編集を Edit ツールへ誘導する。

Edit ツールは対象を読んでからでないと編集できず、書き換えを差分として見せる。Bash の `sed -i` は
どちらも経ずにファイルを書き換えるので、deny して Edit ツールへ誘導する。

PowerShell ツールの発行も同じく見る。見るのは sed の呼び出しだけで、PowerShell 固有のインプレース編集
(Get-Content と Set-Content の組み合わせ等)は対象にしない。

使い方: Bash・PowerShell の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import json
import re
import sys

from hook_input import read_command
from shell_words import head_name, segments


def is_inplace_flag(flag):
    """sed のフラグが in-place 編集(-i・-i.bak・-ni・--in-place)か。"""
    if flag.startswith("--in-place"):
        return True
    if not flag.startswith("-") or flag.startswith("--"):
        return False
    return "i" in re.split("[efl]", flag[1:])[0]


def has_sed_inplace(command):
    """コマンド内のどこかで、コマンド先頭の sed が in-place 編集を行うか。"""
    return any(
        head_name(tokens[0]) == "sed" and any(is_inplace_flag(token) for token in tokens[1:])
        for tokens in segments(command) or []
    )


def main():
    # ハーネスが渡す JSON は UTF-8。既定の符号化で読むと非ASCII が化けて素通りする。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tool, command = read_command("--codex" in sys.argv)
    if tool is None:
        return
    if has_sed_inplace(command):
        editor = "apply_patch" if "--codex" in sys.argv else "Edit ツール"
        reason = (
            f"ファイルのインプレース書き換え(sed -i)は {editor} で行ってください。"
            "awk -i inplace・perl -i・python -c での読み書き等、別の手段で同じ書き換えを"
            "回避して実行しないこと。"
        )
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))


def selftest():
    deny_cases = [
        "sed -i 's/a/b/' f",
        "sed -i.bak 's/a/b/' f",
        "sed -ni 'p' f",
        "sed --in-place 's/a/b/' f",
        "cd t && sed -i x f",
        "sed -e 's/a/b/' -i f",
        "echo x\nsed -i 's/a/b/' f",
        "echo prep # c\nsed -i x f",
    ]
    pass_cases = [
        "sed -n '1,5p' f",
        "sed -e 's/time/date/' f",
        "sed -e's/time/date/' f",
        "sed -ffix.sed f",
        "grep sed -i",
        "git grep sed -i",
        "echo sed -i",
        "true # sed -i",
        "sed -n '1,5p' f; grep -i x f",
        'echo "a\nsed -i b"',
        "cat <<EOF\nsed -i x\nEOF",
    ]
    ok = True
    for case in deny_cases:
        if not has_sed_inplace(case):
            ok = False
            print("FAIL expected deny:", repr(case))
    for case in pass_cases:
        if has_sed_inplace(case):
            ok = False
            print("FAIL expected pass:", repr(case))
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    main()
