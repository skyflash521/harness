#!/usr/bin/env python3
"""PreToolUse フック: ファイルの削除を deny し、trash.py によるごみ箱送りへ誘導する。

一時文書・未追跡ファイルであっても、削除は取り消せない場合がある(git追跡外・エディタの
ローカル履歴にも残らない等)。rm はコマンド先頭がどこにあっても常に deny し、同じ引数で
同梱の trash.py(削除せずOS標準のごみ箱へ送る可逆な代替。同梱の auto-approve-trash フックが
無プロンプトで承認する)を使わせる。ユーザー確認を都度挟まず自律進行を止めないまま、誤削除を
可逆にする。

PowerShell ツールの発行も同じく見る。そちらは Remove-Item とその別名(rm・del・rd 等)が削除にあたり、
誘導先は Bash ツールでの trash.py になる——無プロンプトで承認する auto-approve-trash フックが Bash 専用で、
PowerShell から出した誘導形は許可プロンプトで止まるため。

使い方: プラグインルートを第1引数に渡す Bash・PowerShell の PreToolUse フックとして登録する。
--selftest で自己テスト。
"""
import json
import pathlib
import shlex
import sys

RM_NAMES = ("rm", "rm.exe")
# PowerShell では次がいずれも Remove-Item の別名で、同じ削除を行う。
PS_RM_NAMES = RM_NAMES + ("remove-item", "ri", "del", "erase", "rd", "rmdir")


def trash_script():
    """誘導先 trash.py の絶対パス。"""
    roots = [arg for arg in sys.argv[1:] if arg != "--selftest"]
    if not roots:
        return "<guard プラグイン同梱の scripts/trash.py>"
    return pathlib.PurePath(roots[0], "scripts", "trash.py").as_posix()


def has_rm(command, names=RM_NAMES):
    """コマンド内のどこかで、セグメント先頭が names のいずれか(パス修飾・拡張子形含む)か。"""
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
        if at_head and token.replace("\\", "/").rsplit("/", 1)[-1].lower() in names:
            return True
        at_head = False
    return False


def main():
    # ハーネスが渡す JSON は UTF-8。既定の符号化で読むと非ASCII が化けて素通りする。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    tool = data.get("tool_name")
    if tool not in ("Bash", "PowerShell"):
        return
    command = (data.get("tool_input") or {}).get("command") or ""
    if has_rm(command, PS_RM_NAMES if tool == "PowerShell" else RM_NAMES):
        # プラグインのキャッシュ先は空白を含みうる。引用の無いコマンドは分割されて起動に失敗する。
        via = "Bash ツールへ移り、" if tool == "PowerShell" else ""
        reason = (
            f'ファイルの削除は常に deny します。{via}同じ引数で python3 "{trash_script()}" <path>... を'
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
    ps_deny_cases = [
        "Remove-Item foo.txt",
        "remove-item -Recurse -Force .scratch",
        "rm -Force foo.txt",
        "del foo.txt",
        "rd /s .scratch",
        "Get-ChildItem | Remove-Item",
    ]
    ps_pass_cases = [
        "Get-ChildItem -Recurse",
        "Write-Output 'Remove-Item x'",
        "git status --short",
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
    for case in ps_deny_cases:
        if not has_rm(case, PS_RM_NAMES):
            ok = False
            print("FAIL expected deny (PowerShell):", repr(case))
    for case in ps_pass_cases:
        if has_rm(case, PS_RM_NAMES):
            ok = False
            print("FAIL expected pass (PowerShell):", repr(case))
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    main()
