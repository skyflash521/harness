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


def invoke(script, data, *, platform="win32", codex=True):
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
    assert output["permissionDecision"] == "deny"
    return output["permissionDecisionReason"]


def command_input(command, shell=None, tool="Bash"):
    data = {"tool_name": tool, "tool_input": {"command": command}}
    if shell is not None:
        data["tool_input"]["shell"] = shell
    return data


def selftest():
    cases = 0
    for platform, shell, delete, setter, reader in (
        ("win32", "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
         "Remove-Item 検査.txt", "Set-Date -Date '2030-01-01'", "Get-Date"),
        ("win32", "pwsh.exe", "del 検査.txt", "w32tm /resync", "Get-Date"),
        ("win32", "cmd.exe", "del 検査.txt", "date 2030-01-01", "date /t"),
        ("darwin", "/bin/zsh", "rm 検査.txt", "date 010203002030", "date"),
        ("linux", "/bin/bash", "rm 検査.txt", "date --set=2030-01-01", "date"),
        ("win32", "/bin/bash", "rm 検査.txt", "date -s 2030", "date"),
        ("win32", None, "Remove-Item 検査.txt", "Set-Date", "Get-Date"),
        ("darwin", None, "rm 検査.txt", "date 010203", "date"),
        ("linux", None, "rm 検査.txt", "date -s 2030", "date"),
    ):
        for script, denied, passed, guidance in (
            ("guard-rm.py", delete, "git status --short", "trash.py"),
            ("guard-time.py", setter, reader, reader),
            ("guard-root-scan.py", "rg --files /", "rg --files plugins", "rg --files"),
        ):
            reason = invoke(script, command_input(denied, shell), platform=platform)
            assert reason and guidance in reason, (platform, shell, script, reason)
            assert "Edit ツール" not in reason and "sandbox.excludedCommands" not in reason
            assert invoke(script, command_input(passed, shell), platform=platform) is None
            cases += 2
    for script in ("guard-rm.py", "guard-time.py", "guard-root-scan.py"):
        for malformed in (None, [], {}, {"tool_name": "Bash", "tool_input": []},
                          command_input(123), command_input("rm x", tool="Read")):
            assert invoke(script, malformed) is None
            cases += 1
    for platform in ("win32", "darwin", "linux"):
        for patch, denied in (
            ("*** Begin Patch\n*** Delete File: 検査.txt\n*** End Patch", True),
            ("*** Begin Patch\n*** Update File: 検査.txt\n@@\n-a\n+b\n*** End Patch", False),
            ("*** Begin Patch\n*** Add File: 検査.txt\n+*** Delete File: x\n*** End Patch", False),
        ):
            reason = invoke("guard-delete-patch.py", command_input(patch, tool="apply_patch"),
                            platform=platform)
            assert bool(reason) == denied
            if denied:
                assert "trash.py" in reason
            cases += 1
    for script, denied, guidance in (
        ("guard-rm.py", "rm 検査.txt", "sandbox.excludedCommands"),
        ("guard-time.py", "date -s 2030", "date"),
        ("guard-root-scan.py", "rg --files /", "Glob ツール"),
    ):
        reason = invoke(script, command_input(denied), codex=False)
        assert reason and guidance in reason
        assert invoke(script, command_input("git status --short"), codex=False) is None
        cases += 2
    for script, denied, guidance in (
        ("guard-rm.py", "Remove-Item 検査.txt", "Bash ツールへ移り"),
        ("guard-time.py", "Set-Date", "Get-Date"),
        ("guard-root-scan.py", "rg --files /", "Glob ツール"),
    ):
        reason = invoke(script, command_input(denied, tool="PowerShell"), codex=False)
        assert reason and guidance in reason
        assert invoke(script, command_input("Get-Date", tool="PowerShell"), codex=False) is None
        cases += 2
    claude = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    codex = json.loads((HOOKS / "codex-hooks.json").read_text(encoding="utf-8"))
    expected = ["guard-rm.py", "guard-time.py", "guard-root-scan.py"]
    assert len(claude["hooks"]["PreToolUse"]) == 2
    for entry, matcher in zip(claude["hooks"]["PreToolUse"], ("Bash", "PowerShell")):
        assert entry["matcher"] == matcher
        assert len(entry["hooks"]) == len(expected)
        for hook, script in zip(entry["hooks"], expected):
            suffix = ' "${CLAUDE_PLUGIN_ROOT}"' if script == "guard-rm.py" else ""
            assert hook == {"type": "command", "shell": "bash",
                            "command": f'python3 "${{CLAUDE_PLUGIN_ROOT}}/hooks/{script}"{suffix}'}
    shell_entry, patch_entry = codex["hooks"]["PreToolUse"]
    assert shell_entry["matcher"] == "^Bash$"
    assert len(shell_entry["hooks"]) == len(expected)
    for hook, script in zip(shell_entry["hooks"], expected):
        suffix = ' "${PLUGIN_ROOT}"' if script == "guard-rm.py" else ""
        assert hook == {"type": "command",
                        "command": f'python3 "${{PLUGIN_ROOT}}/hooks/{script}"{suffix} --codex'}
    assert patch_entry == {"matcher": "^apply_patch$", "hooks": [{"type": "command",
                           "command": 'python3 "${PLUGIN_ROOT}/hooks/guard-delete-patch.py"'}]}
    print(f"guard JSON 入出力 OK({cases}件)、製品別登録 OK")


if __name__ == "__main__":
    if sys.argv[1:] != ["--selftest"]:
        raise SystemExit("--selftest を指定してください")
    selftest()
