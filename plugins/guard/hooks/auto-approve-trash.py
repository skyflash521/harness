#!/usr/bin/env python3
"""PreToolUse フック: 同梱 trash.py の起動を無プロンプトで承認する。

guard-rm が rm を deny して誘導する、同梱 trash.py の起動を無プロンプトで通す。第1引数の
プラグインルートから同梱 trash.py の絶対パスを組み立て、連結・展開・リダイレクトの無い単一の
`python3 <その絶対パス> <引数...>` 呼び出しに一致した場合だけ allow を返す。絶対パス一致なので
同名の別ファイルへ許可が及ばない。

大文字小文字だけが異なる同名スクリプトは、それを区別する環境では別ファイルとして扱い承認しない。

承認するのは連結の無い単独の起動だけなので、承認が第2のコマンドを紛れ込ませることはない。該当しなければ
何も出力せず exit 0 で通すので、ここのバグはプロンプトを再発させるだけで誤って承認することはない。
deny は一切しない。

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


def is_trash(cmd, plugin_root):
    """連結の無い単独の `python3 <同梱trash.py> <引数...>` だけ True。"""
    if not isinstance(cmd, str) or not cmd.strip() or not plugin_root:
        return False
    # 連結・展開・リダイレクトは第2のコマンドを紛れ込ませうる。
    if "$" in cmd or "`" in cmd or "<(" in cmd or ">(" in cmd or re.search(r"[<>;|&\n]", cmd):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    if len(tokens) < 3 or tokens[0] != "python3":
        return False
    expected = pathlib.PurePath(plugin_root, "scripts", "trash.py").as_posix()
    return _norm(tokens[1]) == _norm(expected)


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
    if is_trash(cmd, plugin_root):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }}))
    sys.exit(0)


def _selftest():
    root = "C:/Users/user/.claude/plugins/cache/harness/guard/1.0.0"
    # CLAUDE_PLUGIN_ROOT は Windows でバックスラッシュ区切りになりうるが、bash のコマンド側は常にスラッシュ。
    win_root = root.replace("/", chr(92))
    launch = "python3 " + root + "/scripts/trash.py"
    spaced = "C:/Program Files/user/.claude/plugins/cache/harness/guard/1.0.0"
    Case = namedtuple("Case", "why cmd root want")
    cases = [
        Case("単一ファイルの起動", launch + " a.txt", root, True),
        Case("複数ファイルの起動", launch + " a.txt b.txt", root, True),
        Case("空白入りのプラグインルートを引用符で囲む誘導形",
             'python3 "' + spaced + '/scripts/trash.py" a.txt', spaced, True),
        Case("バックスラッシュ区切りのプラグインルート", launch + " a.txt", win_root, True),
        Case("ドライブ文字の大文字小文字",
             launch.replace("C:/Users", "c:/users") + " a.txt", root, os.name == "nt"),
        Case("大文字小文字だけ異なる別ディレクトリの同名スクリプト",
             launch.replace("/scripts/", "/Scripts/") + " a.txt", root, os.name == "nt"),
        Case("引数なしの起動", launch, root, False),
        Case("セミコロンでの連結", launch + " a.txt ; rm -rf x", root, False),
        Case("and での連結", launch + " a.txt && echo done", root, False),
        Case("パイプ", launch + " a.txt | cat", root, False),
        Case("リダイレクト", launch + " a.txt > out.txt", root, False),
        Case("コマンド置換", launch + " $(x)", root, False),
        Case("バッククォートのコマンド置換", launch + " `x`", root, False),
        Case("相対パスの trash.py", "python3 scripts/trash.py a.txt", root, False),
        Case("別の場所にある同名スクリプト", "python3 c:/elsewhere/trash.py a.txt", root, False),
        Case("python3 以外のインタプリタ",
             launch.replace("python3 ", "python ") + " a.txt", root, False),
        Case("無関係なコマンド", "cat README.md", root, False),
        Case("空のコマンド", "", root, False),
        Case("プラグインルート未指定", launch + " a.txt", "", False),
    ]
    ok = True
    for case in cases:
        got = is_trash(case.cmd, case.root)
        if got != case.want:
            ok = False
            print(f"FAIL {case.why}: want={case.want} got={got} :: {case.cmd!r}")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
