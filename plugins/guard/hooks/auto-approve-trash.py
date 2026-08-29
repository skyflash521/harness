#!/usr/bin/env python3
"""PreToolUse hook: auto-approve the plugin-bundled trash.py launch.

guard-rm が rm を deny して誘導する、同梱 trash.py の起動を無プロンプトで通す。第1引数の
プラグインルートから同梱 trash.py の絶対パスを組み立て、連結・展開・リダイレクトの無い単一の
`python3 <その絶対パス> <引数...>` 呼び出しに一致した場合だけ allow を返す。絶対パス一致なので
同名の別ファイルへ許可が及ばない。

パスの照合は、双方の区切りをスラッシュへ統一したうえで `os.path.normcase` にかけて比較する。
Windows はパス区切りとドライブ文字の表記が揺れ、文字列完全一致では表記差でフックが素通りし許可
プロンプトが再発するため。大文字小文字の同一視を `normcase` に委ねるのは、それが大文字小文字を
区別しない Windows でだけ働き、区別する環境(Linux)では働かないから。無条件に小文字化すると、
そうした環境で大小文字だけ異なる別ファイル(`Scripts/trash.py` 等)を同梱スクリプトと誤認して
承認してしまう。

Safety: only a lone, un-chained invocation is approved, so the approval cannot smuggle a
second command. Anything else prints nothing and exits 0 (pass-through), so a bug here can
only re-introduce a prompt, never wrongly approve. It never denies.

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


def is_trash(cmd, plugin_root):
    """True only for a single, un-chained `python3 <同梱trash.py> <args...>` command."""
    if not isinstance(cmd, str) or not cmd.strip() or not plugin_root:
        return False
    # Any chaining/expansion/redirect could append a second command -> never approve.
    if "$" in cmd or "`" in cmd or "<(" in cmd or ">(" in cmd or re.search(r"[<>;|&\n]", cmd):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    if len(tokens) < 3 or tokens[0] != "python3":
        return False  # 引数なしの起動は削除対象を伴わないので承認しない
    expected = pathlib.PurePath(plugin_root, "scripts", "trash.py").as_posix()
    return _norm(tokens[1]) == _norm(expected)


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
    if is_trash(cmd, plugin_root):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }}))
    sys.exit(0)


def _selftest():
    root = "C:/Users/user/.claude/plugins/cache/harness/guard/1.0.0"
    # CLAUDE_PLUGIN_ROOT は Windows でバックスラッシュ区切りになりうる。コマンド側は常に
    # スラッシュ区切り(bash ではバックスラッシュがエスケープ文字として食われる)。
    win_root = root.replace("/", chr(92))
    launch = "python3 " + root + "/scripts/trash.py"
    spaced = "C:/Program Files/user/.claude/plugins/cache/harness/guard/1.0.0"
    cases = [
        (launch + " a.txt", root, True),
        (launch + " a.txt b.txt", root, True),
        # guard-rm が発する誘導形(パスを引用符で囲む)。空白入りのプラグインルートでも
        # tokens[1] が分割されず承認されることを固定する
        ('python3 "' + spaced + '/scripts/trash.py" a.txt', spaced, True),
        (launch + " a.txt", win_root, True),
        # ドライブ文字の大文字小文字は Windows でのみ同一視される
        (launch.replace("C:/Users", "c:/users") + " a.txt", root, os.name == "nt"),
        # 大文字小文字だけ異なる別ディレクトリの同名スクリプト。Windows では同一ファイルを指すが、
        # 区別する環境では別ファイルなので承認してはならない
        (launch.replace("/scripts/", "/Scripts/") + " a.txt", root, os.name == "nt"),
        (launch, root, False),
        (launch + " a.txt ; rm -rf x", root, False),
        (launch + " a.txt && echo done", root, False),
        (launch + " a.txt | cat", root, False),
        (launch + " a.txt > out.txt", root, False),
        (launch + " $(x)", root, False),
        (launch + " `x`", root, False),
        ("python3 scripts/trash.py a.txt", root, False),
        ("python3 c:/elsewhere/trash.py a.txt", root, False),
        (launch.replace("python3 ", "python ") + " a.txt", root, False),
        ("cat README.md", root, False),
        ("", root, False),
        (launch + " a.txt", "", False),
    ]
    ok = True
    for cmd, plugin_root, want in cases:
        got = is_trash(cmd, plugin_root)
        if got != want:
            ok = False
            print(f"FAIL want={want} got={got} :: {cmd!r} root={plugin_root!r}")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
