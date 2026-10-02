#!/usr/bin/env python3
"""導入契約の必須条項を機械確認する。

フックまたはスキルの入口から起動し、欠けた条項を報告する。
確認をスクリプトへ寄せるのは、各スキルが個別に手順を書くと判定が食い違い、条項が増えたときに
追随漏れが出るため。

確認する条項:

    1. 検証手順書が存在すること
    2. スクラッチ置き場が除外設定に入っていること
    3. Claude Code は必須設定の登録、Codex は flow の導入・有効化
       (Claude Code のネイティブ Windows では sandbox.excludedCommands を確認しない)

使い方: python3 <このスクリプトの絶対パス> [--host claude|codex] [対象リポジトリのルート]
       ルート省略時は Claude Code のみ CLAUDE_PROJECT_DIR を使い、未設定または Codex は現在のディレクトリ。
       Codex の一覧取得は、公式フック既定上限600秒の半分300秒を実行に割り当てる。
       残りは判定とプロセス停止に残す。https://learn.chatgpt.com/docs/hooks
終了コード: 全条項を満たせば 0、1つでも欠ければ 1。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIRED_SETTINGS = HERE / "required-settings.json"
ADOPTION_DOC = HERE.parent / "docs" / "criteria" / "adoption.md"

VERIFICATION_DOC = "docs/conventions/verification.md"
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")
USER_SETTINGS = Path.home() / ".claude" / "settings.json"
SCRATCH_DIR = ".scratch"
CODEX_SETTINGS_TIMEOUT = 600 / 2


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def missing_entries(settings, required, sandboxed=True):
    """設定に足りない必須エントリを {キーの説明: [エントリ]} で返す。空なら充足。"""
    settings = settings or {}
    missing = {}
    pairs = [("permissions.allow", ("permissions", "allow"))]
    if sandboxed:
        pairs.append(("sandbox.excludedCommands", ("sandbox", "excludedCommands")))
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


def codex_settings(root):
    codex = shutil.which("codex")
    if codex is None:
        return ["条項3(Codex): codex コマンドが見つからない"]
    try:
        spec = importlib.util.spec_from_file_location("_review_process", HERE.parent / "scripts" / "review_process.py")
        process = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(process)
        result = process.run_review([codex, "plugin", "list", "--marketplace", "harness", "--json"],
                                    cwd=root, input_text="", timeout=CODEX_SETTINGS_TIMEOUT)
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        return [f"条項3(Codex): プラグイン設定を確認できない: {error}"]
    if result.returncode != 0 or not isinstance(payload, dict):
        return ["条項3(Codex): プラグイン設定の取得が失敗した: " + result.stderr.strip()]
    installed = payload.get("installed")
    if not isinstance(installed, list):
        return ["条項3(Codex): 導入済みプラグインの一覧が無い"]
    for plugin in installed:
        if isinstance(plugin, dict) and plugin.get("pluginId") == "flow@harness":
            if plugin.get("installed") is True and plugin.get("enabled") is True:
                return []
    return ["条項3(Codex): 対象リポジトリで flow@harness が導入・有効化されていない"]


def check(root, host="claude"):
    """欠けている条項の説明を並べて返す。空なら全条項を満たす。"""
    root = Path(root).resolve()
    problems = []

    if not (root / VERIFICATION_DOC).is_file():
        problems.append(f"条項1: 検証手順書 {VERIFICATION_DOC} が無い")

    if not ignored(root, SCRATCH_DIR):
        problems.append(f"条項2: スクラッチ置き場 {SCRATCH_DIR}/ が除外設定に入っていない")

    if host == "codex":
        return problems + codex_settings(root)

    registered, unreadable = registered_entries(root)
    note = ""
    if unreadable:
        note = "\n      JSON として読めず数えられなかった設定: " + "、".join(str(p) for p in unreadable)

    required = load_json(REQUIRED_SETTINGS)
    if required is None:
        problems.append(f"条項3: 必須エントリの定義 {REQUIRED_SETTINGS} を読めない")
    else:
        for label, lacking in missing_entries(
                registered, required, sandboxed=sys.platform != "win32").items():
            listed = "\n      ".join(lacking)
            problems.append(f"条項3: {label} に不足がある\n      {listed}{note}")
    return problems


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    host = "claude"
    if argv[:1] == ["--host"]:
        if len(argv) < 2 or argv[1] not in ("claude", "codex"):
            print("--host は claude または codex を指定する")
            return 1
        host, argv = argv[1], argv[2:]
    if len(argv) > 1:
        print("対象リポジトリは1つだけ指定する")
        return 1
    root = argv[0] if argv else (os.environ.get("CLAUDE_PROJECT_DIR") if host == "claude" else None) or os.getcwd()
    problems = check(root, host=host)
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
    from unittest.mock import Mock, patch

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
    got = missing_entries({}, required, sandboxed=False)
    if got != {"permissions.allow": ["Bash(git add *)", "Bash(git commit *)"]}:
        ok = False
        print(f"FAIL サンドボックスの無い環境は sandbox を確認しない: got={got}")

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

    plugin = {"pluginId": "flow@harness", "installed": True, "enabled": True}
    for payload, returncode, accepted in (
        ({"installed": [plugin]}, 0, True),
        ({"installed": [{**plugin, "enabled": False}]}, 0, False),
        ({"installed": [{**plugin, "installed": False}]}, 0, False),
        ({"installed": [{**plugin, "pluginId": "guard@harness"}]}, 0, False),
        ({"installed": []}, 0, False),
        ({"installed": None}, 0, False),
        ([], 0, False),
        ({"installed": [plugin]}, 1, False),
    ):
        process = Mock(returncode=returncode)
        process.communicate.return_value = (json.dumps(payload).encode("utf-8"), b"failure")
        with patch("shutil.which", return_value="codex"), patch("subprocess.Popen", return_value=process):
            problems = codex_settings(HERE)
        if bool(problems) == accepted:
            ok = False
            print(f"FAIL Codex の導入・有効化: payload={payload} returncode={returncode}")
    process = Mock(returncode=0)
    process.communicate.return_value = (b"{", b"")
    with patch("shutil.which", return_value="codex"), patch("subprocess.Popen", return_value=process):
        if not codex_settings(HERE):
            ok = False
            print("FAIL Codex の一覧が不正 JSON でも通す")
    with patch("shutil.which", return_value=None):
        if not codex_settings(HERE):
            ok = False
            print("FAIL Codex CLI が無くても通す")
    with patch("shutil.which", return_value="codex"), patch("subprocess.Popen", side_effect=OSError("fixture")):
        if not codex_settings(HERE):
            ok = False
            print("FAIL Codex CLI の起動失敗でも通す")

    if load_json(REQUIRED_SETTINGS) is None:
        ok = False
        print(f"FAIL 同梱の {REQUIRED_SETTINGS.name} を読めない")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
