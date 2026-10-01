#!/usr/bin/env python3
"""Codex の PreToolUse フック。"""

import json
import sys
from pathlib import Path


def main():
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if not isinstance(data, dict) or data.get("tool_name") != "apply_patch":
        return
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not any(
        line.startswith("*** Delete File:") for line in command.splitlines()
    ):
        return
    trash = (Path(__file__).resolve().parents[1] / "scripts" / "trash.py").as_posix()
    reason = (
        "apply_patch によるファイル削除は deny します。"
        f'対象ファイルを python3 "{trash}" <path>... で OS のごみ箱へ送ってください。'
        "別の削除手段へ切り替えて回避しないこと。"
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    main()
