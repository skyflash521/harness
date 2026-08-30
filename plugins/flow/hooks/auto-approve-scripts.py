#!/usr/bin/env python3
"""PreToolUse hook: auto-approve flow's bundled read-only script launches.

flow は読み取り専用のスクリプトを2つ同梱する。skills/codex-watchdog/watchdog.sh(codex のジョブ
状態ディレクトリに対する find/stat/grep/sleep)と scripts/wait.py(指定時刻か秒数まで待つだけ)で、
どちらもフックの誘導を受けて起動される。どちらも許可リストに載る形ではなく、
無害な読み取りでも呼び出し側にプロンプトが出る(Claude Code は `bash <script>` 形のコマンドを
許可パイプライン内で上書き不能な "ask" へ降格させることがある)。PreToolUse の allow 判定は
behavior:allow として直接返るのでこれを上書きできる。よってこのフックは、単一で連結の無い
この2つの起動だけを承認し、それ以外は何も出力せず通常の許可フローへ渡す(deny はしない)。

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


# 承認する「インタプリタ, 同梱スクリプトの位置」の組。これ以外は承認しない。
APPROVED_LAUNCHES = (
    ("bash", ("skills", "codex-watchdog", "watchdog.sh")),
    ("python3", ("scripts", "wait.py")),
)


def script_path(plugin_root, parts):
    return pathlib.PurePath(plugin_root, *parts).as_posix()


def is_approved_launch(cmd, plugin_root):
    """True only for a single, un-chained `<インタプリタ> <同梱スクリプト> [args...]` command."""
    if not isinstance(cmd, str) or not cmd.strip() or not plugin_root:
        return False
    # Any chaining/expansion/redirect could append a second command -> never approve.
    if "$" in cmd or "`" in cmd or "<(" in cmd or ">(" in cmd or re.search(r"[<>;|&\n]", cmd):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    if len(tokens) < 2:
        return False
    return any(
        tokens[0] == interpreter and _norm(tokens[1]) == _norm(script_path(plugin_root, parts))
        for interpreter, parts in APPROVED_LAUNCHES
    )


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    plugin_root = sys.argv[1] if len(sys.argv) > 1 else ""
    # 符号化を固定する。ハーネスが渡す JSON は UTF-8 で、既定の符号化で読むと非ASCII が化け、
    # 一致しないまま素通りする(復号例外で無出力に落ちる形もある)。出力側も同じ理由で固定する。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # pass-through on bad input
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = (data.get("tool_input") or {}).get("command")
    if is_approved_launch(cmd, plugin_root):
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
    wait = "python3 " + root + "/scripts/wait.py"
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
        (wait + ' "2026-08-29 01:47"', root, True),
        (wait + " 300", root, True),
        (wait + " --selftest", root, True),
        # インタプリタとスクリプトの組が入れ替わった形は承認しない
        (wait.replace("python3 ", "bash "), root, False),
        (launch.replace("bash ", "python3 "), root, False),
        (wait + " ; rm -rf x", root, False),
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
        got = is_approved_launch(cmd, plugin_root)
        if got != want:
            ok = False
            print(f"FAIL want={want} got={got} :: {cmd!r} root={plugin_root!r}")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
