#!/usr/bin/env python3
"""PreToolUse hook: deny Bash `rm`(削除でなく trash.py によるごみ箱送りへ誘導する)。

一時文書・未追跡ファイルであっても、削除は取り消せない場合がある(git追跡外・エディタの
ローカル履歴にも残らない等)。rm はコマンド先頭がどこにあっても常に deny し、同じ引数で
同梱の trash.py(削除せずOS標準のごみ箱へ送る可逆な代替。同梱の auto-approve-trash フックが
無プロンプトで承認する)を使わせる。ユーザー確認を都度挟まず自律進行を止めないまま、誤削除を
可逆にする。

誘導先は第1引数で受け取ったプラグインルートから組み立てた絶対パスで示す。相対パスで示すと、
消費リポジトリ側に同名のスクリプトが無い限り誘導どおりの実行が成立しないため。

Usage: configured as a Bash PreToolUse hook with the plugin root as first argument.
Run with --selftest.
"""
import json
import pathlib
import shlex
import sys


def trash_script():
    """誘導先 trash.py の絶対パス。第1引数(プラグインルート)から組み立てる。"""
    roots = [arg for arg in sys.argv[1:] if arg != "--selftest"]
    if not roots:
        return "<guard プラグイン同梱の scripts/trash.py>"
    return pathlib.PurePath(roots[0], "scripts", "trash.py").as_posix()


def has_rm(command):
    """コマンド内のどこかで、セグメント先頭が rm(パス修飾・拡張子形含む)か。"""
    if "<<" in command:  # here-doc 本文は安全に切り出せないので対象外
        return False
    lexer = shlex.shlex(command.replace("\n", "\n;"), posix=True, punctuation_chars=";()<>|&")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    at_head = True
    for token in tokens:
        if token[:1] in ";|&<>(){}":
            at_head = True
            continue
        if at_head and token.replace("\\", "/").rsplit("/", 1)[-1] in ("rm", "rm.exe"):
            return True
        at_head = False
    return False


def main():
    # 符号化を固定する。ハーネスが渡す JSON は UTF-8 で、既定の符号化で読むと非ASCII が化け、
    # 一致しないまま素通りする(復号例外で無出力に落ちる形もある)。出力側も同じ理由で固定する。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if data.get("tool_name") != "Bash":
        return
    command = (data.get("tool_input") or {}).get("command") or ""
    if has_rm(command):
        # 誘導先のパスは引用符で囲む。プラグインのキャッシュ先が空白を含むパスに置かれると、
        # 引用の無いコマンドは分割されて起動に失敗するため。
        reason = (
            f'rm は常に deny します。同じ引数で python3 "{trash_script()}" <path>... を'
            "使ってください(削除でなくOS標準のごみ箱へ送る可逆な代替です)。"
            "os.remove/os.unlink/pathlib.Path.unlink・PowerShellのRemove-Item・find -delete等、"
            "別の手段で同じ削除を回避して実行しないこと。"
        )
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))


def selftest():
    deny_cases = [
        "rm foo.txt",
        "rm -rf /tmp/x",
        "echo hi; rm x",
        "cat a && rm b",
        "rm a | cat",
        "/bin/rm x",
        "rm.exe x",
        "cd t && rm x",
        "echo prep # c\nrm x",
    ]
    pass_cases = [
        "echo rm",
        "echo 'rm x'",
        "cat rm.txt",
        "ls -la",
        "grep rm f",
        "true # rm x",
        "cat <<EOF\nrm x\nEOF",
    ]
    ok = True
    for case in deny_cases:
        if not has_rm(case):
            ok = False
            print("FAIL expected deny:", repr(case))
    for case in pass_cases:
        if has_rm(case):
            ok = False
            print("FAIL expected pass:", repr(case))
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    main()
