#!/usr/bin/env python3
"""guard の自己テスト。

使い方: python3 plugins/guard/contract/check_codex_hooks.py --selftest
OS 分岐は sys.platform を模擬し、コマンド自体は実行しない。
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"
RUNNER = (
    "import os,runpy,sys; platform,path,*args=sys.argv[1:]; "
    "sys.path.insert(0,os.path.dirname(path)); "
    "sys.platform=platform; sys.argv=[path,*args]; "
    "runpy.run_path(path,run_name='__main__')"
)


def invoke(script, data, *, platform="win32", codex=True, decision="deny"):
    args = [str(ROOT)] if script == "guard-rm.py" else []
    if codex and script != "guard-delete-patch.py":
        args.append("--codex")
    result = subprocess.run(
        [sys.executable, "-c", RUNNER, platform, str(HOOKS / script), *args],
        input=json.dumps(data, ensure_ascii=False), capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (script, result.stderr)
    if not result.stdout.strip():
        return None
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == decision
    return output["permissionDecisionReason"]


def command_input(command, shell=None, tool="Bash"):
    data = {"tool_name": tool, "tool_input": {"command": command}}
    if shell is not None:
        data["tool_input"]["shell"] = shell
    return data


def selftest():
    cases = 0
    checks = [
        (f"{row}: {script} は拒否し Codex 向けの案内だけを出す", platform, shell, script, denied, guidance)
        for row, platform, shell, delete, setter, reader in (
            ("Codex の Windows PowerShell 絶対パス", "win32",
             "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
             "Remove-Item 検査.txt", "Set-Date -Date '2030-01-01'", "Get-Date"),
            ("Codex の pwsh", "win32", "pwsh.exe", "del 検査.txt", "w32tm /resync", "Get-Date"),
            ("Codex の cmd.exe", "win32", "cmd.exe", "del 検査.txt", "date 2030-01-01", "date /t"),
            ("Codex の macOS の zsh", "darwin", "/bin/zsh", "rm 検査.txt", "date 010203002030", "date"),
            ("Codex の Linux の bash", "linux", "/bin/bash", "rm 検査.txt", "date --set=2030-01-01", "date"),
            ("Codex の Windows の bash", "win32", "/bin/bash", "rm 検査.txt", "date -s 2030", "date"),
            ("Codex の Windows の shell 指定なしは PowerShell", "win32", None,
             "Remove-Item 検査.txt", "Set-Date", "Get-Date"),
            ("Codex の macOS の shell 指定なしは Bash", "darwin", None, "rm 検査.txt", "date 010203", "date"),
        )
        for script, denied, guidance in (
            ("guard-rm.py", delete, "trash.py"),
            ("guard-time.py", setter, reader),
        )
    ] + [
        ("Codex のルート起点の rg --files は拒否する", "win32", None, "guard-root-scan.py", "rg --files /",
         "rg --files"),
        ("Codex の here-doc を伴う rm は拒否する", "win32", None, "guard-rm.py", "rm -rf 検査 <<EOF\nx\nEOF",
         "trash.py"),
    ]
    passes = [
        (f"{row}: {script} は {passed!r} を通す", platform, shell, script, passed)
        for row, platform, shell, reader in (
            ("Codex の Windows PowerShell 絶対パス", "win32",
             "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "Get-Date"),
            ("Codex の pwsh", "win32", "pwsh.exe", "Get-Date"),
            ("Codex の cmd.exe", "win32", "cmd.exe", "date /t"),
            ("Codex の macOS の zsh", "darwin", "/bin/zsh", "date"),
            ("Codex の Linux の bash", "linux", "/bin/bash", "date"),
            ("Codex の Windows の bash", "win32", "/bin/bash", "date"),
            ("Codex の Windows の shell 指定なしは PowerShell", "win32", None, "Get-Date"),
            ("Codex の macOS の shell 指定なしは Bash", "darwin", None, "date"),
        )
        for script, passed in (("guard-rm.py", "git status --short"), ("guard-time.py", reader))
    ] + [
        ("Codex のリポジトリ配下の rg --files は通す", "win32", None, "guard-root-scan.py", "rg --files plugins"),
        ("Codex の here-doc 本文の rm は通す", "win32", None, "guard-rm.py", "cat <<EOF\nrm x\nEOF"),
    ]
    for name, platform, shell, script, denied, guidance in checks:
        reason = invoke(script, command_input(denied, shell), platform=platform)
        assert reason and guidance in reason, (name, reason)
        assert "Glob ツール" not in reason and "sandbox.excludedCommands" not in reason, name
        cases += 1
    for name, platform, shell, script, passed in passes:
        assert invoke(script, command_input(passed, shell), platform=platform) is None, name
        cases += 1
    for name, denied in (
        ("cmd.exe の time による時刻の設定は拒否する", "time 12:00"),
        ("cmd.exe の w32tm は拒否する", "w32tm /resync"),
        ("cmd.exe の & の後段の date は拒否する", "echo x & date 2030-01-01"),
        ("cmd.exe の大文字の DATE による設定は拒否する", "DATE 2030-01-01"),
    ):
        assert invoke("guard-time.py", command_input(denied, "cmd.exe")), name
        cases += 1
    assert invoke("guard-time.py", command_input("DATE /T", "cmd.exe")) is None, "cmd.exe の大文字の DATE /T は通す"
    cases += 1
    for name, platform, shell in (
        ("Codex の shell が空文字なら Windows では PowerShell とみなす", "win32", ""),
        ("Codex の shell が文字列でなければ Windows では PowerShell とみなす", "win32", 123),
        ("Codex の shell 名は大文字とバックスラッシュのパスでも PowerShell と見分ける", "linux",
         "C:\\Windows\\PowerShell.EXE"),
    ):
        assert invoke("guard-rm.py", command_input("Remove-Item 検査.txt", shell), platform=platform), name
        cases += 1
    for script, blocked in (
        ("guard-rm.py", "rm x"),
        ("guard-time.py", "Set-Date"),
        ("guard-root-scan.py", "rg --files /"),
        ("guard-gh-write.py", "gh pr merge 1"),
    ):
        for name, malformed in (
            ("JSON が辞書でない入力は通す", []),
            ("tool_input が辞書でない入力は通す", {"tool_name": "Bash", "tool_input": []}),
            ("command が文字列でない入力は通す", command_input(123)),
            ("対象外のツールの入力は通す", command_input(blocked, tool="Read")),
        ):
            assert invoke(script, malformed) is None, f"{script}: {name}"
            cases += 1
    for name, malformed in (
        ("JSON が辞書でない入力は通す", []),
        ("tool_input が辞書でない入力は通す", {"tool_name": "apply_patch", "tool_input": []}),
        ("command が文字列でない入力は通す", command_input(123, tool="apply_patch")),
        ("apply_patch 以外のツールの入力は通す",
         command_input("*** Begin Patch\n*** Delete File: 検査.txt\n*** End Patch")),
    ):
        assert invoke("guard-delete-patch.py", malformed) is None, f"guard-delete-patch.py: {name}"
        cases += 1
    for name, patch, denied in (
        ("ファイルを削除するパッチは拒否し trash.py へ誘導する",
         "*** Begin Patch\n*** Delete File: 検査.txt\n*** End Patch", True),
        ("ファイルを更新するパッチは通す",
         "*** Begin Patch\n*** Update File: 検査.txt\n@@\n-a\n+b\n*** End Patch", False),
        ("追加するファイルの本文にある削除の行は通す",
         "*** Begin Patch\n*** Add File: 検査.txt\n+*** Delete File: x\n*** End Patch", False),
    ):
        reason = invoke("guard-delete-patch.py", command_input(patch, tool="apply_patch"))
        assert bool(reason) == denied, name
        if denied:
            assert "trash.py" in reason, name
        cases += 1
    for script, denied, guidance in (
        ("guard-rm.py", "rm 検査.txt", "sandbox.excludedCommands"),
        ("guard-time.py", "date -s 2030", "date"),
        ("guard-root-scan.py", "rg --files /", "Glob ツール"),
    ):
        reason = invoke(script, command_input(denied), codex=False)
        assert reason and guidance in reason, f"Claude Code の Bash: {script} は拒否し {guidance} を案内する"
        assert invoke(script, command_input("git status --short"), codex=False) is None, \
            f"Claude Code の Bash: {script} は git status を通す"
        cases += 2
    for script, denied, guidance in (
        ("guard-rm.py", "Remove-Item 検査.txt", "Bash ツールへ移り"),
        ("guard-time.py", "Set-Date", "Get-Date"),
        ("guard-root-scan.py", "rg --files /", "Glob ツール"),
    ):
        reason = invoke(script, command_input(denied, tool="PowerShell"), codex=False)
        assert reason and guidance in reason, f"Claude Code の PowerShell: {script} は拒否し {guidance} を案内する"
        cases += 1
    assert invoke("guard-time.py", command_input("Get-Date", tool="PowerShell"), codex=False) is None, \
        "Claude Code の PowerShell: guard-time.py は Get-Date を通す"
    cases += 1
    reason = invoke("guard-rm.py", command_input("rm 検査.txt"), codex=False)
    assert (ROOT / "scripts" / "trash.py").as_posix() in reason, "rm の拒否文は第1引数のプラグインルート配下の trash.py を示す"
    assert "Bash ツールへ移り" not in reason, "Claude Code の Bash での rm の拒否文は Bash ツールへの移動を案内しない"
    assert "Bash ツールへ移り" not in invoke("guard-rm.py", command_input("Remove-Item 検査.txt")), \
        "Codex の PowerShell での削除の拒否文は Bash ツールへの移動を案内しない"
    cases += 3
    breadth = "w32tm は状態を見るだけ"
    assert breadth in invoke("guard-time.py", command_input("Set-Date", tool="PowerShell"), codex=False), \
        "PowerShell での時計の拒否文は w32tm を巻き込むことを注記する"
    assert breadth not in invoke("guard-time.py", command_input("date -s 2030"), codex=False), \
        "Bash での時計の拒否文は w32tm の注記を出さない"
    cases += 2
    for tool in ("Bash", "PowerShell"):
        reason = invoke("guard-gh-write.py", command_input("gh pr merge 1", tool=tool),
                        codex=False, decision="ask")
        assert reason and "gh pr merge" in reason, f"Claude Code の {tool}: gh の書き込みは確認を求める"
        cases += 1
    assert invoke("guard-gh-write.py", command_input("gh pr view 1"), codex=False) is None, \
        "Claude Code の Bash: gh の読み取りは通す"
    cases += 1
    claude = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    codex = json.loads((HOOKS / "codex-hooks.json").read_text(encoding="utf-8"))
    claude_expected = ["guard-rm.py", "guard-time.py", "guard-root-scan.py", "guard-gh-write.py"]
    assert len(claude["hooks"]["PreToolUse"]) == 2
    for entry, matcher in zip(claude["hooks"]["PreToolUse"], ("Bash", "PowerShell")):
        assert entry["matcher"] == matcher
        assert len(entry["hooks"]) == len(claude_expected)
        for hook, script in zip(entry["hooks"], claude_expected):
            suffix = ' "${CLAUDE_PLUGIN_ROOT}"' if script == "guard-rm.py" else ""
            assert hook == {"type": "command", "shell": "bash",
                            "command": f'python3 "${{CLAUDE_PLUGIN_ROOT}}/hooks/{script}"{suffix}'}
    assert codex == {"hooks": {"PreToolUse": [
        {"matcher": "^Bash$", "hooks": [{"type": "command",
                                         "command": 'python3 "${PLUGIN_ROOT}/hooks/guard-rm.py" "${PLUGIN_ROOT}" --codex'}]},
        {"matcher": "^apply_patch$", "hooks": [{"type": "command",
                                                "command": 'python3 "${PLUGIN_ROOT}/hooks/guard-delete-patch.py"'}]},
    ]}}, "Codex は信頼を登録の位置と定義に結び付けるので、登録を変えない。ガードの増減は codex_entry.py で行う"
    for name, denied, guidance in (
        ("時計の変更", "date -s 2030", "date"),
        ("ルート起点の再帰探索", "rg --files /", "rg --files"),
    ):
        reason = invoke("guard-rm.py", command_input(denied, "/bin/bash"))
        assert reason and guidance in reason, f"Codex の Bash の入口 guard-rm.py は{name}を拒否する"
        cases += 1
    print(f"guard JSON 入出力 OK({cases}件)、製品別登録 OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if sys.argv[1:] != ["--selftest"]:
        raise SystemExit("--selftest を指定してください")
    selftest()
