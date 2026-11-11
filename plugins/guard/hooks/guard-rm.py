#!/usr/bin/env python3
"""PreToolUse フック: ファイルの削除を deny し、trash.py によるごみ箱送りへ誘導する。

一時文書・未追跡ファイルであっても、削除は取り消せない場合がある(git追跡外・エディタの
ローカル履歴にも残らない等)。

PowerShell ツールの発行も同じく見る。そちらは Remove-Item とその別名(rm・del・rd 等)が削除にあたり、
誘導先は Bash ツールでの trash.py になる——PowerShell から `python3` を同じ名前で引けるとは
限らないため。

使い方: プラグインルートを第1引数に渡す Bash・PowerShell の PreToolUse フックとして登録する。
Codex の Bash の PreToolUse フックとしては --codex を付けて登録する。
--selftest で自己テスト。
"""
import json
import pathlib
import sys

from hook_input import read_command
from shell_words import command_positions, head_name, segments

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
    """コマンド内のどこかで、命令の位置の語が names のいずれか(パス修飾・拡張子形含む)か。find -delete も含む。"""
    for tokens in segments(command) or []:
        for index in command_positions(tokens):
            name = head_name(tokens[index])
            if name in names or (name == "find" and "-delete" in tokens[index:]):
                return True
    return False


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
        ("; の後段の rm は拒否する", "echo hi; rm x"),
        ("&& の後段の rm は拒否する", "cat a && rm b"),
        ("パイプの前段の rm は拒否する", "rm a | cat"),
        ("パス修飾した rm は拒否する", "/bin/rm x"),
        ("拡張子付きの rm は拒否する", "rm.exe x"),
        ("コメント行の次の行の rm は拒否する", "echo prep # c\nrm x"),
        ("here-doc を伴う rm は拒否する", "rm -rf .scratch <<EOF\nx\nEOF"),
        ("here-doc が終わった後の rm は拒否する", "cat <<EOF\nx\nEOF\nrm y"),
        ("タブ字下げの区切り語で終わる here-doc の後の rm は拒否する", "cat <<-'EOF'\nx\n\tEOF\nrm y"),
        ("引用内の here-doc 記号の次の行の rm は拒否する", "echo '<<EOF'\nrm y"),
        ("find -exec で起動する rm は拒否する", "find . -exec rm {} +"),
        ("find -execdir で起動する rm は拒否する", "find . -execdir rm {} \\;"),
        ("find -ok で起動する rm は拒否する", "find . -ok rm {} ;"),
        ("find -okdir で起動する rm は拒否する", "find . -okdir rm {} ;"),
        ("find -delete は拒否する", "find . -name x -delete"),
        ("xargs で起動する rm は拒否する", "ls | xargs rm"),
        ("for の do の後の rm は拒否する", "for f in *.tmp; do rm \"$f\"; done"),
        ("then の後の rm は拒否する", "if true; then rm x; fi"),
        ("if の条件の rm は拒否する", "if rm x; then :; fi"),
        ("else の後の rm は拒否する", "if false; then :; else rm x; fi"),
        ("elif の条件の rm は拒否する", "if false; then :; elif rm x; then :; fi"),
        ("while の条件の rm は拒否する", "while rm x; do :; done"),
        ("until の条件の rm は拒否する", "until rm x; do :; done"),
        ("! で否定した rm は拒否する", "! rm x"),
        ("time で計る rm は拒否する", "time rm x"),
        ("sudo で起動する rm は拒否する", "sudo rm -rf x"),
        ("起動子のオプション値の後ろの rm は拒否する", "sudo -u www rm -rf x"),
        ("timeout で起動する rm は拒否する", "timeout -s KILL 5 rm x"),
        ("env で起動する rm は拒否する", "env rm x"),
        ("doas で起動する rm は拒否する", "doas rm x"),
        ("nohup で起動する rm は拒否する", "nohup rm x"),
        ("command で起動する rm は拒否する", "command rm x"),
        ("exec で起動する rm は拒否する", "exec rm x"),
        ("builtin で起動する rm は拒否する", "builtin rm x"),
        ("nice で起動する rm は拒否する", "nice rm x"),
        ("stdbuf で起動する rm は拒否する", "stdbuf -o0 rm x"),
        ("環境変数の代入の後ろの rm は拒否する", "LC_ALL=C rm x"),
        ("起動子の後ろの find -delete は拒否する", "sudo find . -delete"),
        ("サブシェルの中の rm は拒否する", "(rm x)"),
        ("グループの中の rm は拒否する", "{ rm x; }"),
        ("出力のリダイレクトを前置した rm は拒否する", ">&2 rm x"),
        ("入力のリダイレクトを前置した rm は拒否する", "<&0 rm x"),
        ("空白を挟まない && の後段の rm は拒否する", "true&&rm x"),
        ("空白を挟まないパイプの後段の rm は拒否する", "true|rm x"),
        ("case の分岐の rm は拒否する", "case x in x) rm y;; esac"),
    ]
    ps_deny_cases = [
        ("Remove-Item は拒否する", "Remove-Item foo.txt"),
        ("別名 rm は拒否する", "rm -Force foo.txt"),
        ("別名 ri は拒否する", "ri foo.txt"),
        ("別名 del は拒否する", "del foo.txt"),
        ("別名 erase は拒否する", "erase foo.txt"),
        ("別名 rd は拒否する", "rd /s .scratch"),
        ("別名 rmdir は拒否する", "rmdir .scratch"),
        ("パイプの後段の Remove-Item は拒否する", "Get-ChildItem | Remove-Item"),
    ]
    ps_pass_cases = [
        ("引用内の Remove-Item の言及は通す", "Write-Output 'Remove-Item x'"),
    ]
    pass_cases = [
        ("引数としての rm の言及は通す", "echo rm"),
        ("引用内の ; と rm は区切りと命令にならない", "echo '; rm x'"),
        ("find 以外の命令の -delete は通す", "echo x -delete"),
        ("解釈できない形は通す", "echo 'x"),
        ("コメント内の rm は通す", "true # x; rm y"),
        ("here-doc 本文の rm は通す", "cat <<EOF\nrm x\nEOF"),
        ("if の条件の命令の引数の rm は通す", "if grep -q rm f; then echo y; fi"),
        ("time で計る命令の引数の rm は通す", "time ls rm"),
        ("find の式の値の rm は通す", "find . -name rm"),
    ]
    ok = True
    if SANDBOX_EXCLUSION not in deny_reason():
        ok = False
        print("FAIL deny 文がサンドボックスの除外登録を案内しない")
    for why, case in deny_cases:
        if not has_rm(case):
            ok = False
            print(f"FAIL {why}: 拒否されない :: {case!r}")
    for why, case in pass_cases:
        if has_rm(case):
            ok = False
            print(f"FAIL {why}: 拒否された :: {case!r}")
    for why, case in ps_deny_cases:
        if not has_rm(case, PS_RM_NAMES):
            ok = False
            print(f"FAIL PowerShell {why}: 拒否されない :: {case!r}")
    for why, case in ps_pass_cases:
        if has_rm(case, PS_RM_NAMES):
            ok = False
            print(f"FAIL PowerShell {why}: 拒否された :: {case!r}")
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    if "--codex" in sys.argv:
        from codex_entry import run_bash_guards
        run_bash_guards(main)
    else:
        main()
