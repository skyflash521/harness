"""guard の PreToolUse フックが共有する入力モジュール。"""

import json
import sys


def read_command(codex=False):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    tool = data.get("tool_name")
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None, None
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None, None
    if codex:
        if tool != "Bash":
            return None, None
        shell = tool_input.get("shell")
        if isinstance(shell, str) and shell:
            name = shell.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
            if name in ("powershell", "pwsh"):
                tool = "PowerShell"
            elif name == "cmd":
                tool = "CommandPrompt"
            else:
                tool = "Bash"
        else:
            tool = "PowerShell" if sys.platform == "win32" else "Bash"
    elif tool not in ("Bash", "PowerShell"):
        return None, None
    return tool, command
