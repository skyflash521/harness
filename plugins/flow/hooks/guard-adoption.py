#!/usr/bin/env python3
"""PreToolUse フック: 導入契約を満たさないリポジトリでの flow スキル起動を deny する。

契約の確認をスキルの本文に書くと、スキルごとに毎回 Bash 呼び出しが1往復増える。呼び出しは
ネストで重なり(自律開発 → レビューループ → コミット)、確認の結果は同じリポジトリで変わらない
のに会話へ積み上がる。ここで確認すればモデルの往復は増えず、スキル側の指示も要らなくなる。

判定は同梱の contract/check_adoption.py に委ねる。条項の正本を2箇所に持つと食い違うため、
このフックは起動の絞り込みと deny の出力だけを行う。

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
    if data.get("tool_name") != "Skill":
        return None
    name = (data.get("tool_input") or {}).get("skill")
    return name if isinstance(name, str) else None


def reason_for(name, root):
    """deny すべきなら理由文、通してよいなら None。"""
    if not name or not name.startswith(SKILL_PREFIX):
        return None
    contract = _load_contract()
    problems = contract.check(root)
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

        repo = Path(__file__).resolve().parents[3]
        if reason_for("flow:commit", repo) is not None:
            ok = False
            print("FAIL 契約を満たすリポジトリで deny になる")

        roundtrips = [
            ("deny の往復", unmet, {"tool_name": "Skill", "tool_input": {"skill": "flow:commit"}}, True),
            ("通過の往復", str(repo), {"tool_name": "Skill", "tool_input": {"skill": "flow:commit"}}, False),
            ("別ツールの往復", unmet, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, False),
        ]
        for why, root, payload, want_deny in roundtrips:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve())],
                input=json.dumps(payload), capture_output=True, text=True,
                env={**os.environ, "CLAUDE_PROJECT_DIR": root}, check=False,
            )
            out = result.stdout.strip()
            if not want_deny:
                if out:
                    ok = False
                    print(f"FAIL {why}: 通すべき起動で出力がある: {out!r}")
                continue
            try:
                decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
            except (json.JSONDecodeError, KeyError, TypeError):
                ok = False
                print(f"FAIL {why}: deny の JSON が読めない: {out!r}")
                continue
            if decision != "deny":
                ok = False
                print(f"FAIL {why}: permissionDecision={decision!r}")

    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
