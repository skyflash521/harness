#!/usr/bin/env python3
"""導入契約の必須条項を機械確認する。

flow のスキルは実行開始時にこれを起動し、欠けた条項があれば停止して adoption.md へ案内する。
確認をスクリプトへ寄せるのは、各スキルが個別に手順を書くと判定が食い違い、条項が増えたときに
追随漏れが出るため。

確認する条項:

    1. 検証手順書が存在すること
    2. スクラッチ置き場が除外設定に入っていること
    4. 必要な permissions・sandbox エントリが設定に登録されていること

条項3(有効化)は、このスクリプトを呼ぶスキルが起動できている時点で満たされているため見ない。

Usage: python3 <このスクリプトの絶対パス> [対象リポジトリのルート]
       ルートを省いた場合は CLAUDE_PROJECT_DIR、それも無ければカレントディレクトリを使う。
Exit code: 全条項を満たせば 0、1つでも欠ければ 1。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIRED_SETTINGS = HERE / "required-settings.json"
ADOPTION_DOC = HERE.parent / "docs" / "adoption.md"

VERIFICATION_DOC = "docs/conventions/verification.md"
SETTINGS_FILE = ".claude/settings.json"
SCRATCH_DIRS = (".scratch", "docs/.scratch")


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def missing_entries(settings, required):
    """設定に足りない必須エントリを {キーの説明: [エントリ]} で返す。空なら充足。"""
    settings = settings or {}
    missing = {}
    pairs = [
        ("permissions.allow", ("permissions", "allow")),
        ("sandbox.excludedCommands", ("sandbox", "excludedCommands")),
    ]
    for label, (outer, inner) in pairs:
        want = ((required.get(outer) or {}).get(inner)) or []
        have = set(((settings.get(outer) or {}).get(inner)) or [])
        lacking = [entry for entry in want if entry not in have]
        if lacking:
            missing[label] = lacking
    return missing


def ignored(root, relative):
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", f"{relative}/probe"],
        capture_output=True, check=False,
    )
    return result.returncode == 0


def check(root):
    """欠けている条項の説明を並べて返す。空なら全条項を満たす。"""
    root = Path(root).resolve()
    problems = []

    if not (root / VERIFICATION_DOC).is_file():
        problems.append(f"条項1: 検証手順書 {VERIFICATION_DOC} が無い")

    not_ignored = [d for d in SCRATCH_DIRS if not ignored(root, d)]
    if not_ignored:
        problems.append("条項2: 除外設定に入っていないスクラッチ置き場: " + "、".join(not_ignored))

    required = load_json(REQUIRED_SETTINGS)
    if required is None:
        problems.append(f"条項4: 必須エントリの定義 {REQUIRED_SETTINGS} を読めない")
    else:
        settings = load_json(root / SETTINGS_FILE)
        if settings is None:
            problems.append(f"条項4: {SETTINGS_FILE} が無い、または JSON として読めない")
        else:
            for label, lacking in missing_entries(settings, required).items():
                listed = "\n      ".join(lacking)
                problems.append(f"条項4: {label} に不足がある\n      {listed}")
    return problems


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    root = argv[0] if argv else os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    problems = check(root)
    if problems:
        print("導入契約を満たしていない条項がある:")
        for problem in problems:
            print(f"  - {problem}")
        print(f"\n満たし方は {ADOPTION_DOC} を参照。")
        return 1
    print("導入契約 OK(条項1・2・4)")
    return 0


def _selftest():
    required = {
        "permissions": {"allow": ["Bash(git add *)", "Bash(git commit *)"]},
        "sandbox": {"excludedCommands": ["node \"*x.mjs\"*"]},
    }
    cases = [
        ("充足", {"permissions": {"allow": ["Bash(git add *)", "Bash(git commit *)", "Bash(ls)"]},
                  "sandbox": {"excludedCommands": ["node \"*x.mjs\"*"]}}, {}),
        ("allow 不足", {"permissions": {"allow": ["Bash(git add *)"]},
                        "sandbox": {"excludedCommands": ["node \"*x.mjs\"*"]}},
         {"permissions.allow": ["Bash(git commit *)"]}),
        ("sandbox 不足", {"permissions": {"allow": ["Bash(git add *)", "Bash(git commit *)"]}},
         {"sandbox.excludedCommands": ["node \"*x.mjs\"*"]}),
        ("空の設定", {}, {"permissions.allow": ["Bash(git add *)", "Bash(git commit *)"],
                          "sandbox.excludedCommands": ["node \"*x.mjs\"*"]}),
        ("None", None, {"permissions.allow": ["Bash(git add *)", "Bash(git commit *)"],
                        "sandbox.excludedCommands": ["node \"*x.mjs\"*"]}),
    ]
    ok = True
    for name, settings, want in cases:
        got = missing_entries(settings, required)
        if got != want:
            ok = False
            print(f"FAIL {name}: want={want} got={got}")
    if load_json(REQUIRED_SETTINGS) is None:
        ok = False
        print(f"FAIL 同梱の {REQUIRED_SETTINGS.name} を読めない")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
