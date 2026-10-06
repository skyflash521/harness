#!/usr/bin/env python3
"""Codex の SessionStart・PreToolUse・Stop フック。

呼び出し形: python3 codex-flow.py
標準入力: Codex のフック JSON。標準出力: 同じイベントのフック応答 JSON または空。
--selftest で CLI 起動とフックイベントの判定を検査する。
"""

import importlib.util
import json
import math
import re
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
    execution = ROOT / "docs" / "guidance" / "execution.md"
    adoption = ROOT / "docs" / "criteria" / "adoption.md"
    return (stop.build_context(codex=True) + "\n" + usage.build_context() + "\n"
            f"Codex の起動・監視・停止は {execution} に従う。"
            "待機中は実行ツールのセッションを監視し、手番を終了しない。"
            "自律進行の着手範囲は終端番号を含む n-m の形で申告する。"
            f"flow スキルに着手する前に {adoption} の Codex 用導入検査を実行する。")


def flow_entrypoint(command):
    parser = hook("codex-guard-git-write")
    try:
        words = parser.words(command_text(command, parser))
    except ValueError:
        return False
    segment = []
    for token, quoted in words + [("\n", False)]:
        if quoted or token not in parser.OPERATORS:
            segment.append(token)
            continue
        for position in parser.command_positions(segment):
            args = segment[position:]
            name = parser.program(args[0]).removesuffix(".cmd").removesuffix(".ps1")
            if name in parser.SHELLS and any(flow_entrypoint(text) for text in parser.shell_commands(args)):
                return True
            if name in ("python", "python3", "py"):
                if any(Path(value).name == "codex_commit.py" for value in args[1:]):
                    return True
                if len(args) > 4 and Path(args[1]).name == "run_capped.py" and args[3] == "--":
                    args = args[4:]
                    name = parser.program(args[0]).removesuffix(".cmd").removesuffix(".ps1")
            if ((name == "claude" and any(value in ("-p", "--print") for value in args[1:]))
                    or (name == "codex" and "exec" in args[1:])):
                return True
        segment = []
    return False


def command_text(command, parser):
    output = []
    documents = []
    string_quote = None
    document = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")
    for line in command.splitlines(keepends=True):
        if string_quote:
            if line.startswith(string_quote + "@"):
                string_quote = None
                output.append(line[2:])
            continue
        if documents:
            delimiter, tabs = documents[0]
            if (line.lstrip("\t") if tabs else line).rstrip("\r\n") == delimiter:
                documents.pop(0)
            continue
        string = re.search(r"@(['\"])\s*$", line)
        if string:
            string_quote = string.group(1)
            output.append(line[:string.start()] + '""\n')
            continue
        matches = []
        for match in document.finditer(line):
            try:
                parser.words(line[:match.start()])
            except ValueError:
                continue
            matches.append(match)
        documents.extend((match.group(3), bool(match.group(1))) for match in matches)
        for match in reversed(matches):
            line = line[:match.start()] + line[match.end():]
        output.append(line)
    return "".join(output)


def cli_reason(command):
    parser = hook("codex-guard-git-write")
    command = command_text(command, parser)
    try:
        tokens = parser.words(command)
    except ValueError:
        if re.search(r"\bclaude(?:\.exe|\.cmd)?\s+.*(?:-p|--print)|\bcodex(?:\.exe|\.cmd)?\s+exec\b", command):
            return "CLI の起動引数を確認できない。引用符を閉じて起動する。"
        return None
    segment = []
    for word, quoted in tokens + [("\n", False)]:
        if quoted or word not in parser.OPERATORS:
            segment.append(word)
            continue
        for position in parser.command_positions(segment):
            args = segment[position:]
            name = parser.program(args[0]).removesuffix(".cmd").removesuffix(".ps1")
            if name in parser.SHELLS:
                for nested in parser.shell_commands(args):
                    if reason := cli_reason(nested):
                        return reason
            capped = False
            if name in ("python", "python3", "py") and len(args) > 4:
                runner = Path(args[1]).resolve()
                if runner == ROOT / "skills" / "run-and-bench" / "run_capped.py" and args[3] == "--":
                    try:
                        cap = float(args[2])
                        capped = math.isfinite(cap) and 0 < cap <= hook("guard-idle-stop").WAIT_CAP_SECS
                    except ValueError:
                        pass
                    args = args[4:]
                    name = parser.program(args[0]).removesuffix(".cmd").removesuffix(".ps1")
            claude = name == "claude" and any(value in ("-p", "--print") for value in args[1:])
            codex = name == "codex" and "exec" in args[1:]
            if not (claude or codex):
                continue
            if not capped:
                return "レビュー・相談の CLI は同梱 run_capped.py の配下で、スキルの時間上限以内に起動する。"
            if claude:
                values = {}
                key = None
                for value in args[1:]:
                    if value.startswith("--"):
                        key, _, inline = value.partition("=")
                        values[key] = [inline] if inline else []
                    elif key:
                        values[key].append(value)
                tools = {item for value in values.get("--tools", []) for item in value.split(",")}
                allowed = {item for value in values.get("--allowedTools", []) for item in value.split(",")}
                denied = {item for value in values.get("--disallowedTools", []) for item in value.split(",")}
                if (values.get("--model") not in (["opus"], ["fable"])
                        or tools != {"Read", "Grep", "Glob"}
                        or allowed != {"Read", "Grep", "Glob"}
                        or not {"Edit", "Write", "NotebookEdit", "Bash"} <= denied
                        or values.get("--permission-mode") != ["dontAsk"]):
                    return "Claude レビューは opus または fable を指定し、Read・Grep・Glob のみを許可し、書き込みツールを拒否する。"
            else:
                modes = [value.split("=", 1)[1].strip('"\'') for value in args if value.startswith("sandbox_mode=")]
                modes.extend(args[i + 1] for i, value in enumerate(args[:-1]) if value in ("-s", "--sandbox"))
                modes.extend(value.split("=", 1)[1] for value in args if value.startswith("--sandbox="))
                if any(value in args for value in ("--dangerously-bypass-approvals-and-sandbox", "--approve-for-me")):
                    return "レビュー・相談の sandbox_mode を迂回するオプションは使えない。"
                if ("review" in args or "resume" in args) and (not modes or set(modes) != {"read-only"}):
                    return "Codex レビューは sandbox_mode=read-only で起動する。"
                if not modes or not set(modes) <= {"read-only", "workspace-write"}:
                    return "Codex 相談は sandbox_mode を明示して起動する。"
        segment = []
    return None


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
                reason = f"待機は {ROOT / 'docs' / 'guidance' / 'execution.md'} に従い、同じ実行セッションで続ける。"
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
            if isinstance(command, str):
                reason = cli_reason(command)
            if isinstance(command, str) and reason is None and flow_entrypoint(command):
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


def selftest():
    import os
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import patch

    runner = ROOT / "skills" / "run-and-bench" / "run_capped.py"
    prefix = f'python3 "{runner}" 900 -- '
    valid = ('claude -p --model opus --permission-mode dontAsk --tools Read,Grep,Glob '
             '--allowedTools Read Grep Glob --disallowedTools Edit Write NotebookEdit Bash')
    cases = [(prefix + valid, False), (valid, True), (prefix + valid.replace("opus", "fable"), False),
             (prefix + valid.replace("opus", "sonnet"), True),
             (prefix + valid.replace("Read,Grep,Glob", "Read,Grep,Glob,Bash"), True),
             (prefix + valid.replace("NotebookEdit Bash", "NotebookEdit"), True),
             (prefix.replace("900", "nan") + valid, True),
             (prefix.replace("900", "901") + valid, True),
             (prefix + 'codex exec review --json -c sandbox_mode=read-only -', False),
             (prefix + 'codex exec review --json -', True),
             (prefix + 'codex exec review -c sandbox_mode=read-only -s danger-full-access -', True),
             ('codex exec --json -c sandbox_mode=read-only -', True),
             ('echo "claude -p"', False),
             ("printf '%s' \"Claude's", False),
             (prefix + valid + " <<'EOF'\nClaude's\ncodex exec を調べる\nEOF\n", False),
             ("$prompt=@'\nClaude's\ncodex exec を調べる\n'@\n" + prefix + valid, False),
             ("cat <<EOF\ncodex exec を調べる\nEOF\ncodex exec -\n", True)]
    failures = [command for command, denied in cases if bool(cli_reason(command)) != denied]
    for shell in ("bash -lc", "eval", "powershell", "powershell -ExecutionPolicy Bypass -Command"):
        if cli_reason(shell + " '" + prefix + valid + "'") is not None:
            failures.append(f"入れ子の正規 CLI を拒否: {shell}")
        if cli_reason(shell + " 'claude -p --model sonnet --tools Bash'") is None:
            failures.append(f"入れ子の無保護 CLI を通す: {shell}")
    for shell in ("powershell -ExecutionPolicy Bypass -Command ", "powershell -exec bypass -c ",
                  "powershell -exec bypass -com ", "powershell -exec bypass "):
        if cli_reason(shell + prefix + valid) is not None:
            failures.append("オプション値の後の正規 CLI を拒否する")
        if cli_reason(shell + "claude -p --model sonnet --tools Bash") is None:
            failures.append("オプション値の後の無保護 CLI を通す")
    if flow_entrypoint("echo pwsh -Command 'python3 scripts/codex_commit.py --help'"):
        failures.append("シェル名を出力するだけの命令を起動と誤認する")
    for event in ("SessionStart", "PreToolUse", "Stop"):
        registered = json.loads((ROOT / "hooks" / "codex-hooks.json").read_text(encoding="utf-8"))["hooks"]
        if not any("codex-flow.py" in item["command"] for group in registered.get(event, [])
                   for item in group.get("hooks", [])):
            failures.append(f"{event}: フックが登録されていない")

    context = decide({"hook_event_name": "SessionStart"})["hookSpecificOutput"]
    if context["hookEventName"] != "SessionStart" or any(
            clause not in context["additionalContext"] for clause in (
                "[停止: 完了]", "着手範囲", "execution.md", "adoption.md", "手番を終了しない")):
        failures.append("SessionStart: Codex の実行・停止・導入の文脈が不足する")

    for message, expected in (
        ("確認しました。\n[停止: 完了]", None),
        ("確認しました。", "末尾行"),
        ("結果を待ちます。\n[停止: 待機]", "同じ実行セッション"),
        ("I have completed the task.\n[停止: 完了]", "guard-reply-language"),
    ):
        result = decide({"hook_event_name": "Stop", "last_assistant_message": message})
        if (expected is None and result is not None) or (expected is not None and (
                not result or result.get("decision") != "block" or expected not in result.get("reason", ""))):
            failures.append(f"Stop: {message}")

    actual_load = load
    for problems in ([], ["条項1: 検証手順書が無い"], ["条項2: 除外されていない"],
                     ["条項3(Codex): flow が無効"]):
        received = []

        def check(root, host):
            received.append((root, host))
            return problems

        contract = SimpleNamespace(check=check, ADOPTION_DOC=ROOT / "docs" / "criteria" / "adoption.md")
        for command in (prefix + valid, 'pwsh -Command "python3 scripts/codex_commit.py --help"',
                        'bash -c "python3 scripts/codex_commit.py --help"',
                        'cmd /c "python3 scripts/codex_commit.py --help"',
                        'bash -lc "python3 scripts/codex_commit.py --help"',
                        'eval "python3 scripts/codex_commit.py --help"',
                        'powershell "python3 scripts/codex_commit.py --help"',
                        'powershell -ExecutionPolicy Bypass -Command python3 scripts/codex_commit.py --help',
                        'powershell -exec bypass -Command python3 scripts/codex_commit.py --help',
                        'powershell -exec bypass -com python3 scripts/codex_commit.py --help',
                        'powershell -exec bypass python3 scripts/codex_commit.py --help'):
            received.clear()
            with patch(__name__ + ".load", side_effect=lambda name, path:
                       contract if name == "check_adoption" else actual_load(name, path)):
                result = decide({"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(ROOT),
                                 "tool_input": {"command": command}})
            if received != [(str(ROOT), "codex")]:
                failures.append("PreToolUse: Codex の導入検査に cwd と host を渡していない")
            if (not problems and result is not None) or (problems and (
                    not result or result["hookSpecificOutput"].get("permissionDecision") != "deny"
                    or problems[0] not in result["hookSpecificOutput"].get("permissionDecisionReason", ""))):
                failures.append("PreToolUse: 導入検査の結果を許可・拒否へ接続していない")

    for content, denied in (("確認できます。", False), ("今回の変更です。", True)):
        command = f"*** Begin Patch\n*** Add File: docs/sample.md\n+{content}\n*** End Patch"
        result = decide({"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "cwd": str(ROOT.parents[1]),
                         "tool_input": {"command": command}})
        if bool(result) != denied:
            failures.append("PreToolUse: apply_patch の成果物衛生")

    for payload, expected in (("{", None), ("[]", None), ("{}", None),
                              (json.dumps({"hook_event_name": "SessionStart"}), "additionalContext"),
                              (json.dumps({"hook_event_name": "Stop", "last_assistant_message": "確認しました。"}), "block")):
        result = subprocess.run([sys.executable, __file__], input=payload.encode("utf-8"),
                                capture_output=True, env={**os.environ, "FLOW_UNATTENDED": "1"})
        if result.returncode or (expected is None and result.stdout.strip()) or (
                expected is not None and expected not in result.stdout.decode("utf-8")):
            failures.append("フック JSON 入出力の往復")
    for command in failures:
        print("FAIL " + command)
    print("SOME FAILED" if failures else "ALL PASS")
    return bool(failures)


if __name__ == "__main__":
    sys.exit(selftest()) if sys.argv[1:] == ["--selftest"] else main()
