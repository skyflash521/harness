#!/usr/bin/env python3
"""PreToolUse フック: flow 同梱の読み取り専用スクリプトの起動を無プロンプトで承認する。

flow は読み取り専用のスクリプトを2つ同梱する。skills/codex-watchdog/watchdog.sh(codex のジョブ
状態ディレクトリに対する find/stat/grep/sleep)と scripts/wait.py(指定時刻か秒数まで待つだけ)で、
どちらもフックの誘導を受けて起動される。どちらも許可リストに載る形ではなく、
無害な読み取りでも呼び出し側にプロンプトが出る(Claude Code は `bash <script>` 形のコマンドを
許可パイプライン内で上書き不能な "ask" へ降格させることがある)。PreToolUse の allow 判定は
behavior:allow として直接返るのでこれを上書きできる。よってこのフックは、単一で連結の無い
この2つの起動だけを承認し、それ以外は何も出力せず通常の許可フローへ渡す(deny はしない)。

対象は第1引数で受け取ったプラグインルートから組み立てた絶対パスで特定する。大文字小文字だけが
異なる同名スクリプトは、それを区別する環境では別ファイルとして扱い承認しない。

連結・展開・リダイレクトを含む形は承認しないので、承認が第2のコマンドを紛れ込ませることはない。
該当しなければ何も出力せず通すので、ここのバグはプロンプトを再発させるだけで誤って承認することは
できない。

使い方: プラグインルートを第1引数に渡す Bash の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import json
import os
import pathlib
import re
import shlex
import sys
from collections import namedtuple


def _norm(path):
    # Windows はパス区切り・ドライブ文字・大文字小文字の表記が揺れ、normcase の同一視もそこだけ働く。
    return os.path.normcase(path.replace("\\", "/"))


APPROVED_LAUNCHES = (
    ("bash", ("skills", "codex-watchdog", "watchdog.sh")),
    ("python3", ("scripts", "wait.py")),
)


def script_path(plugin_root, parts):
    return pathlib.PurePath(plugin_root, *parts).as_posix()


def is_approved_launch(cmd, plugin_root):
    """連結の無い単独の `<インタプリタ> <同梱スクリプト> [引数...]` だけ True。"""
    if not isinstance(cmd, str) or not cmd.strip() or not plugin_root:
        return False
    # 連結・展開・リダイレクトは第2のコマンドを紛れ込ませうる。
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
    # ハーネスが渡す JSON は UTF-8。既定の符号化で読むと非ASCII が化けて素通りする。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    plugin_root = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
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
    # CLAUDE_PLUGIN_ROOT は Windows でバックスラッシュ区切りになりうるが、bash のコマンド側は常にスラッシュ。
    win_root = root.replace("/", chr(92))
    launch = "bash " + root + "/skills/codex-watchdog/watchdog.sh"
    wait = "python3 " + root + "/scripts/wait.py"
    spaced = "C:/Program Files/user/.claude/plugins/cache/harness/flow/1.0.0"
    Case = namedtuple("Case", "why cmd root want")
    cases = [
        Case("watchdog を全引数付きで起動", launch + ' 420 1200 "" 240 vprv0test9m2', root, True),
        Case("watchdog を引数なしで起動", launch, root, True),
        Case("状態ルートを空文字で渡す", launch + " 420 1200 '' 240 tok", root, True),
        Case("状態ルートをパスで渡す", launch + " 420 1200 /some/state 240 tok", root, True),
        Case("バックスラッシュ区切りのプラグインルート", launch, win_root, True),
        Case("ドライブ文字の大文字小文字",
             launch.replace("C:/Users", "c:/users"), root, os.name == "nt"),
        Case("大文字小文字だけ異なる別ディレクトリの同名スクリプト",
             launch.replace("/skills/", "/Skills/"), root, os.name == "nt"),
        Case("空白入りのプラグインルートを引用符で囲む誘導形",
             'bash "' + spaced + '/skills/codex-watchdog/watchdog.sh" 420 1200 "" 240 tok',
             spaced, True),
        Case("wait を目標時刻で起動", wait + ' "2026-08-29 01:47"', root, True),
        Case("wait を秒数で起動", wait + " 300", root, True),
        Case("wait の自己テスト", wait + " --selftest", root, True),
        Case("wait を bash で起動", wait.replace("python3 ", "bash "), root, False),
        Case("watchdog を python3 で起動", launch.replace("bash ", "python3 "), root, False),
        Case("wait のセミコロン連結", wait + " ; rm -rf x", root, False),
        Case("watchdog のセミコロン連結", launch + " ; rm -rf x", root, False),
        Case("and での連結", launch + " && echo done", root, False),
        Case("パイプ", launch + " | grep x", root, False),
        Case("リダイレクト", launch + " > out.txt", root, False),
        Case("コマンド置換", launch + " $(rm x)", root, False),
        Case("バッククォートのコマンド置換", launch + " `rm x`", root, False),
        Case("相対パスの watchdog", "bash skills/codex-watchdog/watchdog.sh", root, False),
        Case("別の場所にある同名スクリプト", "bash c:/elsewhere/watchdog.sh", root, False),
        Case("bash 以外のシェル", launch.replace("bash ", "sh "), root, False),
        Case("無関係なコマンド", "cat README.md", root, False),
        Case("空のコマンド", "", root, False),
        Case("プラグインルート未指定", launch, "", False),
    ]
    ok = True
    for case in cases:
        got = is_approved_launch(case.cmd, case.root)
        if got != case.want:
            ok = False
            print(f"FAIL {case.why}: want={case.want} got={got} :: {case.cmd!r}")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
