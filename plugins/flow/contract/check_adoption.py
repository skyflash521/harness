#!/usr/bin/env python3
"""導入契約の必須条項を機械確認する。

flow スキルの起動を受けるフックがこれを起動し、欠けた条項があれば起動を deny して adoption.md へ案内する。
確認をスクリプトへ寄せるのは、各スキルが個別に手順を書くと判定が食い違い、条項が増えたときに
追随漏れが出るため。

確認する条項:

    1. 検証手順書が存在すること
    2. スクラッチ置き場が除外設定に入っていること
    3. 必須エントリの定義が持つエントリが設定に登録されていること

使い方: python3 <このスクリプトの絶対パス> [対象リポジトリのルート]
       ルートを省いた場合は CLAUDE_PROJECT_DIR、それも無ければカレントディレクトリを使う。
終了コード: 全条項を満たせば 0、1つでも欠ければ 1。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIRED_SETTINGS = HERE / "required-settings.json"
ADOPTION_DOC = HERE.parent / "docs" / "rules" / "adoption.md"

VERIFICATION_DOC = "docs/conventions/verification.md"
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")
USER_SETTINGS = Path.home() / ".claude" / "settings.json"
SCRATCH_DIR = ".scratch"


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


def registered_entries(root, user_settings=None):
    """リポジトリとユーザーの設定が持つエントリを合算した辞書と、在るのに読めなかった設定の一覧を返す。"""
    merged = {}
    unreadable = []
    paths = [root / relative for relative in SETTINGS_FILES]
    paths.append(Path(user_settings) if user_settings else USER_SETTINGS)
    for path in paths:
        settings = load_json(path)
        if settings is None:
            if path.is_file():
                unreadable.append(path)
            continue
        for outer, inner in (("permissions", "allow"), ("sandbox", "excludedCommands")):
            have = ((settings.get(outer) or {}).get(inner)) or []
            merged.setdefault(outer, {}).setdefault(inner, []).extend(have)
    return merged, unreadable


def check(root):
    """欠けている条項の説明を並べて返す。空なら全条項を満たす。"""
    root = Path(root).resolve()
    problems = []

    if not (root / VERIFICATION_DOC).is_file():
        problems.append(f"条項1: 検証手順書 {VERIFICATION_DOC} が無い")

    if not ignored(root, SCRATCH_DIR):
        problems.append(f"条項2: スクラッチ置き場 {SCRATCH_DIR}/ が除外設定に入っていない")

    registered, unreadable = registered_entries(root)
    note = ""
    if unreadable:
        note = "\n      JSON として読めず数えられなかった設定: " + "、".join(str(p) for p in unreadable)

    required = load_json(REQUIRED_SETTINGS)
    if required is None:
        problems.append(f"条項3: 必須エントリの定義 {REQUIRED_SETTINGS} を読めない")
    else:
        for label, lacking in missing_entries(registered, required).items():
            listed = "\n      ".join(lacking)
            problems.append(f"条項3: {label} に不足がある\n      {listed}{note}")
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
    print("導入契約 OK(条項1・2・3)")
    return 0


def _selftest():
    import tempfile

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

    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        (root / ".claude").mkdir()
        first, second = required["permissions"]["allow"]
        (root / SETTINGS_FILES[0]).write_text(
            json.dumps({"permissions": {"allow": [first]}}), encoding="utf-8")
        user = root / "user-settings.json"
        user.write_text(json.dumps({"sandbox": required["sandbox"]}), encoding="utf-8")

        registered, unreadable = registered_entries(root, user_settings=user)
        got = missing_entries(registered, required)
        if got != {"permissions.allow": [second]} or unreadable:
            ok = False
            print(f"FAIL 設定の合算: got={got} unreadable={unreadable}")

        (root / SETTINGS_FILES[1]).write_text("{壊れた JSON", encoding="utf-8")
        _, unreadable = registered_entries(root, user_settings=user)
        if [path.name for path in unreadable] != [Path(SETTINGS_FILES[1]).name]:
            ok = False
            print(f"FAIL 読めない設定の報告: {unreadable}")

    if load_json(REQUIRED_SETTINGS) is None:
        ok = False
        print(f"FAIL 同梱の {REQUIRED_SETTINGS.name} を読めない")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
