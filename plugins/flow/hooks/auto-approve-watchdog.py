#!/usr/bin/env python3
"""PreToolUse hook: auto-approve the codex-watchdog watchdog.sh launch.

watchdog.sh は同梱の読み取り専用スクリプト(codex のジョブ状態ディレクトリに対する find/stat/
grep/sleep)で、codex:codex-rescue を起動するどのスキルからも共有される。Claude Code は
`bash <script>` 形のコマンドを許可パイプライン内で上書き不能な "ask" へ降格させることがあり、
無害な読み取りでも呼び出し側にプロンプトが出る。PreToolUse の allow 判定は behavior:allow として
直接返るのでこれを上書きできる。よってこのフックは、単一で連結の無い watchdog 起動だけを承認し、
それ以外は何も出力せず通常の許可フローへ渡す(deny はしない)。

対象は第1引数で受け取ったプラグインルートから組み立てた絶対パスで特定する。パスの照合は、双方の
区切りをスラッシュへ統一したうえで `os.path.normcase` にかけて比較する。Windows はパス区切りと
ドライブ文字の表記が揺れ、文字列完全一致では表記差でフックが素通りし許可プロンプトが再発する
ため。大文字小文字の同一視を `normcase` に委ねるのは、それが大文字小文字を区別しない Windows で
だけ働き、区別する環境(Linux)では働かないから。無条件に小文字化すると、そうした環境で大小文字
だけ異なる別ファイルを同梱スクリプトと誤認して承認してしまう。

Safety: 連結・展開・リダイレクトを含む形は承認しないので、承認が第2のコマンドを紛れ込ませることは
ない。該当しなければ何も出力せず exit 0(pass-through)なので、ここのバグはプロンプトを再発させる
ことしかできず、誤って承認することはない。

Usage: configured as a Bash PreToolUse hook with the plugin root as first argument.
Run with --selftest.
"""
import json
import os
import pathlib
import re
import shlex
import sys


def _norm(path):
    return os.path.normcase(path.replace("\\", "/"))


def watchdog_path(plugin_root):
    return pathlib.PurePath(plugin_root, "skills", "codex-watchdog", "watchdog.sh").as_posix()


def is_watchdog(cmd, plugin_root):
    """True only for a single, un-chained `bash <同梱watchdog.sh> [args...]` command."""
    if not isinstance(cmd, str) or not cmd.strip() or not plugin_root:
        return False
    # Any chaining/expansion/redirect could append a second command -> never approve.
    if "$" in cmd or "`" in cmd or "<(" in cmd or ">(" in cmd or re.search(r"[<>;|&\n]", cmd):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    return (
        len(tokens) >= 2
        and tokens[0] == "bash"
        and _norm(tokens[1]) == _norm(watchdog_path(plugin_root))
    )


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    plugin_root = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # pass-through on bad input
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = (data.get("tool_input") or {}).get("command")
    if is_watchdog(cmd, plugin_root):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }}))
    sys.exit(0)


def _selftest():
    root = "C:/Users/user/.claude/plugins/cache/harness/flow/1.0.0"
    # CLAUDE_PLUGIN_ROOT は Windows でバックスラッシュ区切りになりうる。コマンド側は常に
    # スラッシュ区切り(bash ではバックスラッシュがエスケープ文字として食われる)。
    win_root = root.replace("/", chr(92))
    launch = "bash " + root + "/skills/codex-watchdog/watchdog.sh"
    spaced = "C:/Program Files/user/.claude/plugins/cache/harness/flow/1.0.0"
    cases = [
        (launch + ' 420 1200 "" 240 vprv0test9m2', root, True),
        (launch, root, True),
        (launch + " 420 1200 '' 240 tok", root, True),
        (launch + " 420 1200 /some/state 240 tok", root, True),
        (launch, win_root, True),
        # ドライブ文字の大文字小文字は Windows でのみ同一視される
        (launch.replace("C:/Users", "c:/users"), root, os.name == "nt"),
        # 大文字小文字だけ異なる別ディレクトリの同名スクリプト。区別する環境では別ファイルなので
        # 承認してはならない
        (launch.replace("/skills/", "/Skills/"), root, os.name == "nt"),
        # 呼び出し元が発する形(空白入りのプラグインルートを引用符で囲む)
        ('bash "' + spaced + '/skills/codex-watchdog/watchdog.sh" 420 1200 "" 240 tok',
         spaced, True),
        (launch + " ; rm -rf x", root, False),
        (launch + " && echo done", root, False),
        (launch + " | grep x", root, False),
        (launch + " > out.txt", root, False),
        (launch + " $(rm x)", root, False),
        (launch + " `rm x`", root, False),
        ("bash skills/codex-watchdog/watchdog.sh", root, False),
        ("bash c:/elsewhere/watchdog.sh", root, False),
        (launch.replace("bash ", "sh "), root, False),
        ("cat README.md", root, False),
        ("", root, False),
        (launch, "", False),
    ]
    ok = True
    for cmd, plugin_root, want in cases:
        got = is_watchdog(cmd, plugin_root)
        if got != want:
            ok = False
            print(f"FAIL want={want} got={got} :: {cmd!r} root={plugin_root!r}")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
