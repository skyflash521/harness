#!/usr/bin/env python3
"""PreToolUse フック: 起動する flow スキルに要る導入契約の区分を満たさないリポジトリで、その起動を deny する。

見るのは `flow:` で始まるスキルの `Skill` 起動だけで、他は何も出力せず通す。

使い方: `Skill` の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

CONTRACT = Path(__file__).resolve().parent.parent / "contract" / "check_adoption.py"
SKILL_PREFIX = "flow:"


def _load_contract():
    spec = importlib.util.spec_from_file_location("check_adoption", CONTRACT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def skill_name(data):
    """Skill 起動なら起動対象のスキル名、それ以外なら None。"""
    if not isinstance(data, dict) or data.get("tool_name") != "Skill":
        return None
    tool_input = data.get("tool_input")
    name = tool_input.get("skill") if isinstance(tool_input, dict) else None
    return name if isinstance(name, str) else None


def reason_for(name, root):
    """deny すべきなら理由文、通してよいなら None。"""
    if not name or not name.startswith(SKILL_PREFIX):
        return None
    contract = _load_contract()
    problems = contract.check(root, tier=contract.skill_tier(name))
    if not problems:
        return None
    listed = "\n".join(f"  - {problem}" for problem in problems)
    return (f"導入契約を満たしていない条項がある:\n{listed}\n\n"
            f"満たし方は {contract.ADOPTION_DOC} を参照。この不足はリポジトリ側のものなので、"
            "スキルを起動し直しても解けない。ユーザーへ不足をそのまま示して止まれ。")


def main():
    # ハーネスが渡す JSON は UTF-8 で、既定の符号化では復号できずに落ちる。
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    reason = reason_for(skill_name(data), root)
    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))


def selftest():
    import subprocess
    import tempfile

    ok = True
    name_cases = [
        ("flow スキル", {"tool_name": "Skill", "tool_input": {"skill": "flow:commit"}}, "flow:commit"),
        ("他プラグインのスキル", {"tool_name": "Skill", "tool_input": {"skill": "codex:rescue"}}, "codex:rescue"),
        ("別ツール", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, None),
        ("スキル名が無い", {"tool_name": "Skill", "tool_input": {}}, None),
        ("スキル名が文字列でない", {"tool_name": "Skill", "tool_input": {"skill": 1}}, None),
        ("tool_input が無い", {"tool_name": "Skill"}, None),
    ]
    for why, data, want in name_cases:
        got = skill_name(data)
        if got != want:
            ok = False
            print(f"FAIL {why}: want={want!r} got={got!r}")

    with tempfile.TemporaryDirectory() as unmet:
        reason_cases = [
            ("flow スキルは deny", "flow:commit", True),
            ("他プラグインのスキルは通す", "codex:rescue", False),
            ("スキルでない起動は通す", None, False),
        ]
        for why, name, want_deny in reason_cases:
            got = reason_for(name, unmet)
            if bool(got) != want_deny:
                ok = False
                print(f"FAIL {why}: want_deny={want_deny} got={got!r}")

        repo = Path(unmet, "satisfied")
        (repo / "docs" / "conventions").mkdir(parents=True)
        (repo / "docs" / "conventions" / "verification.md").write_text("x\n", encoding="utf-8")
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(
            Path(__file__).resolve().parents[1].joinpath("contract", "required-settings.json")
            .read_text(encoding="utf-8"), encoding="utf-8")
        (repo / ".gitignore").write_text(".scratch/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        skills = sorted(f"flow:{path.parent.name}"
                        for path in Path(__file__).resolve().parents[1].glob("skills/*/SKILL.md"))
        for name in skills:
            if reason_for(name, repo) is not None:
                ok = False
                print(f"FAIL 契約を満たすリポジトリで deny になる: {name}")

        outside = Path(unmet, "outside")
        (outside / ".claude").mkdir(parents=True)
        (outside / ".claude" / "settings.json").write_text(
            (repo / ".claude" / "settings.json").read_text(encoding="utf-8"), encoding="utf-8")
        for name, want_deny in (("flow:run-and-bench", False), ("flow:codex-consult", False),
                                ("flow:codex-watchdog", False), ("flow:commit", True), ("flow:plan", True),
                                ("flow:codex-review-loop", True), ("flow:autonomous-dev", True)):
            got = reason_for(name, outside)
            if bool(got) != want_deny or (want_deny and "git 管理外" not in got):
                ok = False
                print(f"FAIL git 管理外のルートでの {name}: want_deny={want_deny} got={got!r}")

        roundtrips = [
            ("deny の往復", unmet, {"tool_name": "Skill", "tool_input": {"skill": "flow:commit"}}, True),
            ("通過の往復", str(repo), {"tool_name": "Skill", "tool_input": {"skill": "flow:commit"}}, False),
            ("git 管理外で基本条項のスキルが通る往復", str(outside),
             {"tool_name": "Skill", "tool_input": {"skill": "flow:run-and-bench"}}, False),
            ("git 管理外で開発条項のスキルを deny する往復", str(outside),
             {"tool_name": "Skill", "tool_input": {"skill": "flow:commit"}}, "git 管理外"),
            ("別ツールの往復", unmet, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, False),
            ("辞書でない入力の往復", unmet, [], False),
            ("tool_name が null の往復", unmet, {"tool_name": None}, False),
            ("tool_input が辞書でない往復", unmet, {"tool_name": "Skill", "tool_input": [1]}, False),
        ]
        for why, root, payload, want_deny in roundtrips:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve())],
                input=json.dumps(payload), capture_output=True, text=True,
                env={**os.environ, "CLAUDE_PROJECT_DIR": root}, check=False,
            )
            out = result.stdout.strip()
            if not want_deny:
                if out or result.returncode:
                    ok = False
                    print(f"FAIL {why}: 通すべき起動で出力がある: exit {result.returncode} {out!r}")
                continue
            try:
                output = json.loads(out)["hookSpecificOutput"]
                decision, reason = output["permissionDecision"], output["permissionDecisionReason"]
            except (json.JSONDecodeError, KeyError, TypeError):
                ok = False
                print(f"FAIL {why}: deny の JSON が読めない: {out!r}")
                continue
            if decision != "deny":
                ok = False
                print(f"FAIL {why}: permissionDecision={decision!r}")
            if isinstance(want_deny, str) and want_deny not in reason:
                ok = False
                print(f"FAIL {why}: 理由に {want_deny!r} が無い: {reason!r}")

    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
