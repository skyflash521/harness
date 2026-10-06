#!/usr/bin/env python3
"""PreToolUse フック: ファイルの削除を deny し、trash.py によるごみ箱送りへ誘導する。

一時文書・未追跡ファイルであっても、削除は取り消せない場合がある(git追跡外・エディタの
ローカル履歴にも残らない等)。rm はコマンド先頭がどこにあっても常に deny し、同じ引数で
同梱の trash.py(削除せずOS標準のごみ箱へ送る可逆な代替)を使わせる。ユーザー確認を都度挟まず
自律進行を止めないまま、誤削除を可逆にする。

PowerShell ツールの発行も同じく見る。そちらは Remove-Item とその別名(rm・del・rd 等)が削除にあたり、
誘導先は Bash ツールでの trash.py になる——PowerShell から `python3` を同じ名前で引けるとは
限らないため。

使い方: プラグインルートを第1引数に渡す Bash・PowerShell の PreToolUse フックとして登録する。
--selftest で自己テスト。
"""
import json
import pathlib
import sys

from hook_input import read_command
from shell_words import head_name, segments

RM_NAMES = ("rm", "rm.exe")
SANDBOX_EXCLUSION = 'python3 "*/guard/*scripts/trash.py"*'
# PowerShell では次がいずれも Remove-Item の別名で、同じ削除を行う。
PS_RM_NAMES = RM_NAMES + ("remove-item", "ri", "del", "erase", "rd", "rmdir")


def trash_script():
    """誘導先 trash.py の絶対パス。"""
    roots = [arg for arg in sys.argv[1:] if arg not in ("--selftest", "--codex")]
    if not roots:
        return "<guard プラグイン同梱の scripts/trash.py>"
    return pathlib.PurePath(roots[0], "scripts", "trash.py").as_posix()


def deny_reason(via="", codex=False):
    sandbox_guidance = (
        ""
        if codex else
        "macOS のサンドボックス内ではごみ箱へ送れず失敗する。サンドボックスを切らず、"
        f"{SANDBOX_EXCLUSION} を sandbox.excludedCommands へ登録するようユーザーに依頼すること。"
    )
    return (
        f'ファイルの削除は常に deny します。{via}同じ引数で python3 "{trash_script()}" <path>... を'
        "使ってください(削除でなくOS標準のごみ箱へ送る可逆な代替です)。"
        "os.remove/os.unlink/pathlib.Path.unlink・PowerShellのRemove-Item・find -delete等、"
        "別の手段で同じ削除を回避して実行しないこと。"
        f"{sandbox_guidance}"
    )


def has_rm(command, names=RM_NAMES):
    """コマンド内のどこかで、セグメント先頭が names のいずれか(パス修飾・拡張子形含む)か。"""
    return any(head_name(tokens[0]) in names for tokens in segments(command) or [])


def main():
    # ハーネスが渡す JSON は UTF-8。既定の符号化で読むと非ASCII が化けて素通りする。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tool, command = read_command("--codex" in sys.argv)
    if tool is None:
        return
    if has_rm(command, PS_RM_NAMES if tool in ("PowerShell", "CommandPrompt") else RM_NAMES):
        # プラグインのキャッシュ先は空白を含みうる。引用の無いコマンドは分割されて起動に失敗する。
        codex = "--codex" in sys.argv
        via = "Bash ツールへ移り、" if tool == "PowerShell" and not codex else ""
        reason = deny_reason(via, codex)
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
    if SANDBOX_EXCLUSION not in deny_reason():
        ok = False
        print("FAIL deny 文がサンドボックスの除外登録を案内しない")
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
