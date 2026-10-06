#!/usr/bin/env python3
"""導入契約の必須条項を機械確認する。

フックまたはスキルの入口から起動し、欠けた条項を報告する。
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
import time
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


def process_module():
    spec = importlib.util.spec_from_file_location("_review_process", HERE.parent / "scripts" / "review_process.py")
    process = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(process)
    return process


def required_hook_keys(plugin):
    directory = HERE.parent
    if plugin["pluginId"] != "flow@harness":
        source = plugin.get("source")
        path = source.get("path") if isinstance(source, dict) else None
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError(f"{plugin['pluginId']}: 導入先の絶対パスを取得できない")
        directory = Path(path)
    definition = load_json(directory / "hooks" / "codex-hooks.json")
    if not isinstance(definition, dict) or not isinstance(definition.get("hooks"), dict):
        raise ValueError(f"{plugin['pluginId']}: フック定義を読めない")
    keys = set()
    names = {"SessionStart": "session_start", "PreToolUse": "pre_tool_use", "Stop": "stop"}
    for event, groups in definition["hooks"].items():
        if event not in names or not isinstance(groups, list):
            raise ValueError(f"{plugin['pluginId']}: フック登録が不正")
        for group_index, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list) or not handlers:
                raise ValueError(f"{plugin['pluginId']}: フック登録が空または不正")
            for handler_index, handler in enumerate(handlers):
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    raise ValueError(f"{plugin['pluginId']}: フックの種別が不正")
                keys.add(f"{plugin['pluginId']}:hooks/codex-hooks.json:{names[event]}:{group_index}:{handler_index}")
    if not keys:
        raise ValueError(f"{plugin['pluginId']}: フック定義が空")
    return keys


def codex_hook_problems(payload, required, root):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return ["条項3(Codex): フックの一覧が不正"]
    entries = payload["data"]
    if len(entries) != 1 or not isinstance(entries[0], dict):
        return ["条項3(Codex): 対象リポジトリのフック一覧が無い"]
    entry = entries[0]
    cwd = entry.get("cwd")
    if not isinstance(cwd, str) or os.path.normcase(os.path.realpath(cwd)) != os.path.normcase(os.path.realpath(root)):
        return ["条項3(Codex): フック一覧の対象リポジトリが異なる"]
    if entry.get("errors") or not isinstance(entry.get("hooks"), list):
        return ["条項3(Codex): フックの読み込みが失敗した"]
    hooks = {}
    for hook in entry["hooks"]:
        if not isinstance(hook, dict) or not isinstance(hook.get("key"), str) or hook["key"] in hooks:
            return ["条項3(Codex): フック一覧の項目が不正または重複"]
        hooks[hook["key"]] = hook
    problems = []
    for key in sorted(required):
        hook = hooks.get(key)
        if hook is None:
            problems.append(f"条項3(Codex): 必須フックが無い: {key}")
        elif hook.get("enabled") is not True or hook.get("trustStatus") != "trusted":
            problems.append(f"条項3(Codex): 必須フックが無効または未信頼: {key}。/hooks で定義を確認し信頼・有効化する")
    return problems


def codex_settings(root):
    codex = shutil.which("codex")
    if codex is None:
        return ["条項3(Codex): codex コマンドが見つからない"]
    deadline = time.monotonic() + CODEX_SETTINGS_TIMEOUT
    try:
        process = process_module()
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
    active = [p for p in installed if isinstance(p, dict) and p.get("installed") is True and p.get("enabled") is True]
    if not any(p.get("pluginId") == "flow@harness" for p in active):
        return ["条項3(Codex): 対象リポジトリで flow@harness が導入・有効化されていない"]
    try:
        required = set()
        for plugin in active:
            if plugin.get("pluginId") in ("flow@harness", "guard@harness"):
                required.update(required_hook_keys(plugin))
        result = process.run_rpc([codex, "app-server", "--stdio"], cwd=root,
                                 requests=[
                                     {"id": 1, "method": "initialize", "params": {
                                         "clientInfo": {"name": "harness-adoption", "version": "1"},
                                         "capabilities": {"experimentalApi": True}}},
                                     {"method": "initialized"},
                                     {"id": 2, "method": "hooks/list", "params": {"cwds": [str(root)]}},
                                     {"id": 3, "method": "config/read", "params": {"cwd": str(root), "includeLayers": False}},
                                 ], timeout=max(0.01, deadline - time.monotonic()))
        replies = json.loads(result.stdout)
        if result.returncode != 0 or not isinstance(replies, list) or len(replies) != 3:
            return ["条項3(Codex): フックの実行状態を取得できない"]
        config = replies[2].get("config") if isinstance(replies[2], dict) else None
        if not isinstance(config, dict) or not isinstance(config.get("features", {}), dict):
            return ["条項3(Codex): フック機能の設定を取得できない"]
        features = config.get("features", {})
        if features.get("hooks", features.get("codex_hooks", True)) is not True:
            return ["条項3(Codex): フック機能が無効"]
        return codex_hook_problems(replies[1], required, root)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        return [f"条項3(Codex): フックの実行状態を確認できない: {error}"]


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
    expected = required_hook_keys(plugin)
    hook_payload = {"data": [{"cwd": str(HERE), "errors": [], "hooks": [
        {"key": key, "enabled": True, "trustStatus": "trusted"} for key in sorted(expected)
    ]}]}

    def cli_module(stdout, returncode=0, hooks=None, config=None):
        module = Mock()
        module.run_review.return_value = subprocess.CompletedProcess([], returncode, stdout, "failure")
        module.run_rpc.return_value = subprocess.CompletedProcess([], 0, json.dumps([
            {}, hook_payload if hooks is None else hooks, {"config": {} if config is None else config},
        ]), "")
        return module

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
        process = cli_module(json.dumps(payload), returncode)
        with patch("shutil.which", return_value="codex"), patch.dict(globals(), {"process_module": lambda: process}):
            problems = codex_settings(HERE)
        if bool(problems) == accepted:
            ok = False
            print(f"FAIL Codex の導入・有効化: payload={payload} returncode={returncode}")
    process = cli_module("{")
    with patch("shutil.which", return_value="codex"), patch.dict(globals(), {"process_module": lambda: process}):
        if not codex_settings(HERE):
            ok = False
            print("FAIL Codex の一覧が不正 JSON でも通す")
    with patch("shutil.which", return_value=None):
        if not codex_settings(HERE):
            ok = False
            print("FAIL Codex CLI が無くても通す")
    process = cli_module(json.dumps({"installed": [plugin]}))
    process.run_review.side_effect = OSError("fixture")
    with patch("shutil.which", return_value="codex"), patch.dict(globals(), {"process_module": lambda: process}):
        if not codex_settings(HERE):
            ok = False
            print("FAIL Codex CLI の起動失敗でも通す")

    for key in sorted(expected):
        for state in (None, {"enabled": False}, {"trustStatus": "untrusted"}, {"trustStatus": "changed"},
                      {"trustStatus": None}, {"enabled": 1}):
            changed = json.loads(json.dumps(hook_payload))
            hooks = changed["data"][0]["hooks"]
            selected = next(hook for hook in hooks if hook["key"] == key)
            if state is None:
                hooks.remove(selected)
            else:
                selected.update(state)
            process = cli_module(json.dumps({"installed": [plugin]}), hooks=changed)
            with patch("shutil.which", return_value="codex"), patch.dict(globals(), {"process_module": lambda: process}):
                if not codex_settings(HERE):
                    ok = False
                    print(f"FAIL 必須フックの不備を通す: {key} {state}")

    invalid_hooks = [[], {}, {"data": []}, {"data": [{"cwd": str(HERE), "hooks": None}]},
                     {"data": [{"cwd": str(HERE), "errors": ["fixture"], "hooks": []}]},
                     {"data": [{"cwd": str(HERE / 'different'), "hooks": []}]}]
    duplicate = json.loads(json.dumps(hook_payload))
    duplicate["data"][0]["hooks"].append(duplicate["data"][0]["hooks"][0])
    invalid_hooks.append(duplicate)
    for payload in invalid_hooks:
        if not codex_hook_problems(payload, expected, HERE):
            ok = False
            print(f"FAIL 不正なフック一覧を通す: {payload}")

    for config in ({"features": {"hooks": False}}, {"features": {"codex_hooks": False}},
                   {"features": []}):
        process = cli_module(json.dumps({"installed": [plugin]}), config=config)
        with patch("shutil.which", return_value="codex"), patch.dict(globals(), {"process_module": lambda: process}):
            if not codex_settings(HERE):
                ok = False
                print(f"FAIL フック機能の無効・不正設定を通す: {config}")

    for failure in (OSError("fixture"), ValueError("fixture"), subprocess.TimeoutExpired("fixture", 1)):
        process = cli_module(json.dumps({"installed": [plugin]}))
        process.run_rpc.side_effect = failure
        with patch("shutil.which", return_value="codex"), patch.dict(globals(), {"process_module": lambda: process}):
            if not codex_settings(HERE):
                ok = False
                print(f"FAIL フック取得失敗を通す: {failure}")

    with tempfile.TemporaryDirectory() as guard_root:
        directory = Path(guard_root)
        (directory / "hooks").mkdir()
        (directory / "hooks/codex-hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "fixture"} for _ in range(4)]},
            {"hooks": [{"type": "command", "command": "fixture"}]},
        ]}}), encoding="utf-8")
        guard = {"pluginId": "guard@harness", "installed": True, "enabled": True,
                 "source": {"path": str(directory)}}
        for source in (None, [], {}, {"path": ""}, {"path": "./plugins/guard"}, {"path": 1}):
            process = cli_module(json.dumps({"installed": [plugin, {**guard, "source": source}]}))
            with patch("shutil.which", return_value="codex"), patch.dict(globals(), {"process_module": lambda: process}):
                if not codex_settings(HERE):
                    ok = False
                    print(f"FAIL guard の導入先が不明でも通す: {source}")
        all_hooks = json.loads(json.dumps(hook_payload))
        guard_keys = required_hook_keys(guard)
        all_hooks["data"][0]["hooks"].extend(
            {"key": key, "enabled": True, "trustStatus": "trusted"} for key in sorted(guard_keys))
        for key in sorted(guard_keys):
            changed = json.loads(json.dumps(all_hooks))
            next(h for h in changed["data"][0]["hooks"] if h["key"] == key)["trustStatus"] = "untrusted"
            process = cli_module(json.dumps({"installed": [plugin, guard]}), hooks=changed)
            with patch("shutil.which", return_value="codex"), patch.dict(globals(), {"process_module": lambda: process}):
                if not codex_settings(HERE):
                    ok = False
                    print(f"FAIL 有効な guard の未信頼フックを通す: {key}")

    if load_json(REQUIRED_SETTINGS) is None:
        ok = False
        print(f"FAIL 同梱の {REQUIRED_SETTINGS.name} を読めない")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
