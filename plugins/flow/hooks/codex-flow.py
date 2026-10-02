#!/usr/bin/env python3
"""Codex の SessionStart・PreToolUse・Stop フック。

呼び出し形: python3 codex-flow.py
標準入力: Codex のフック JSON。標準出力: 同じイベントのフック応答 JSON または空。
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hook(name):
    return load(name.replace("-", "_"), ROOT / "hooks" / (name + ".py"))


def session_context():
    stop = hook("announce-stop-protocol")
    usage = hook("announce-usage-limit-response")
    execution = ROOT / "docs" / "guidance" / "codex-execution.md"
    adoption = ROOT / "docs" / "criteria" / "adoption.md"
    return (stop.build_context(codex=True) + "\n" + usage.build_context() + "\n"
            f"Codex の起動・監視・停止は {execution} に従う。"
            "待機中は実行ツールのセッションを監視し、手番を終了しない。"
            "自律進行の着手範囲は終端番号を含む n-m の形で申告する。"
            f"flow スキルに着手する前に {adoption} の Codex 用導入検査を実行する。")


def flow_entrypoint(command):
    parser = hook("codex-guard-git-write")
    try:
        words = parser.words(command)
    except ValueError:
        return False
    python = False
    for token, _ in words:
        if token in parser.OPERATORS:
            python = False
        name = token.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        if name in ("python", "python3", "py"):
            python = True
        if python and name in ("claude_review.py", "codex_review.py", "codex_consult.py", "codex_commit.py"):
            return True
    return False


def patch_documents(command, cwd):
    lines = command.splitlines()
    changes = []
    index = 0
    while index < len(lines):
        header = lines[index]
        kind = next((name for name in ("Add", "Update", "Delete") if header.startswith(f"*** {name} File: ")), None)
        if kind is None:
            index += 1
            continue
        path = Path(cwd) / header.split(": ", 1)[1]
        index += 1
        body = []
        while index < len(lines) and not lines[index].startswith(("*** Add File:", "*** Update File:",
                                                                  "*** Delete File:", "*** End Patch")):
            body.append(lines[index])
            index += 1
        if kind == "Delete":
            continue
        try:
            before = path.read_text(encoding="utf-8") if kind == "Update" else ""
        except OSError:
            continue
        destination = path
        if body[:1] and body[0].startswith("*** Move to: "):
            destination = Path(cwd) / body.pop(0).split(": ", 1)[1]
        if kind == "Add":
            after = "\n".join(line[1:] for line in body if line.startswith("+")) + "\n"
        else:
            after_lines = before.splitlines()
            old, new = [], []
            position = 0

            def apply_chunk(start, end=False):
                if not old:
                    after_lines.extend(new)
                    return len(after_lines)
                offsets = list(range(start, len(after_lines) - len(old) + 1))
                if end:
                    offsets.reverse()
                for normalize in (lambda value: value, str.rstrip, str.strip):
                    expected = [normalize(line) for line in old]
                    for offset in offsets:
                        if [normalize(line) for line in after_lines[offset:offset + len(old)]] == expected:
                            after_lines[offset:offset + len(old)] = new
                            return offset + len(new)
                raise ValueError("パッチの文脈が一致しない")

            try:
                for line in body + ["@@"]:
                    if line.startswith("@@") or line == "*** End of File":
                        if old or new:
                            position = apply_chunk(position, line == "*** End of File")
                            old, new = [], []
                        if line.startswith("@@ "):
                            target = line[3:]
                            for normalize in (lambda value: value, str.rstrip, str.strip):
                                offset = next((i for i in range(position, len(after_lines))
                                               if normalize(after_lines[i]) == normalize(target)), None)
                                if offset is not None:
                                    position = offset + 1
                                    break
                    elif line.startswith(" "):
                        old.append(line[1:])
                        new.append(line[1:])
                    elif line.startswith("-"):
                        old.append(line[1:])
                    elif line.startswith("+"):
                        new.append(line[1:])
            except ValueError:
                continue
            after = "\n".join(after_lines) + "\n"
        changes.append((destination, before if destination == path else "", after))
    return changes


def patch_reason(command, cwd):
    hygiene = hook("guard-artifact-hygiene")
    for path, before, after in patch_documents(command, cwd):
        if hygiene.applicable_atoms(str(path)):
            reason = hygiene.evaluate("Write", {"file_path": str(path), "content": after}, before)
            if reason:
                return reason
    return None


def decide(data):
    event = data.get("hook_event_name")
    if event == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": session_context()}}
    if event == "Stop":
        language = hook("guard-reply-language")
        reason = language.decide(data)
        if reason:
            reason = language.TAG + " " + reason
        idle = hook("guard-idle-stop")
        if reason is None:
            if isinstance(data.get("last_assistant_message"), str) and idle.last_line(data["last_assistant_message"]) == idle.WAIT:
                reason = f"待機は {ROOT / 'docs' / 'guidance' / 'codex-execution.md'} に従い、同じ実行セッションで続ける。"
            else:
                _, reason = idle.decide(data)
                if reason == idle.REASON_NO_MARKER:
                    reason = "末尾行を停止宣言だけにして報告する。\n" + session_context()
        if reason is None:
            reason = hook("guard-goal-completion").decide(data)
        return {"decision": "block", "reason": "[codex-flow] " + reason} if reason else None
    if event == "PreToolUse":
        language = hook("guard-reply-language")
        reason = language.decide(data)
        if reason:
            reason = language.TAG + " " + reason
        tool_input = data.get("tool_input")
        if reason is None and data.get("tool_name") == "Bash" and isinstance(tool_input, dict):
            command = tool_input.get("command")
            if isinstance(command, str) and flow_entrypoint(command):
                contract = load("check_adoption", ROOT / "contract" / "check_adoption.py")
                problems = contract.check(data.get("cwd") or Path.cwd(), host="codex")
                if problems:
                    reason = "導入契約に不足がある:\n" + "\n".join(problems) + f"\n{contract.ADOPTION_DOC} を確認する。"
        if reason is None and data.get("tool_name") == "apply_patch" and isinstance(tool_input, dict):
            command = tool_input.get("command")
            if isinstance(command, str):
                reason = patch_reason(command, data.get("cwd") or Path.cwd())
        if reason is None and data.get("tool_name", "").rsplit(".", 1)[-1] in (
            "request_user_input", "request_user_input_async", "AskUserQuestion",
        ):
            reason = hook("guard-autonomous-question").decide({**data, "tool_name": "AskUserQuestion"})
        if reason:
            return {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "deny",
                                           "permissionDecisionReason": "[codex-flow] " + reason}}
    return None


def main():
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, EOFError):
        return
    if not isinstance(data, dict):
        return
    sys.argv = [sys.argv[0], str(ROOT)]
    output = decide(data)
    if output:
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
