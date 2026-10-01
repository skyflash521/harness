#!/usr/bin/env python3
"""Codex 用の配布メタデータ(manifest・marketplace)を Claude 側の正本から生成する。

正本は `plugins/<name>/.claude-plugin/plugin.json` と `.claude-plugin/marketplace.json`。
Codex 側のファイルは手で編集せず、このスクリプトで生成する。バージョン刻印は
`stamp_plugin_version.py` が刻印のたびに本スクリプトの同期を呼ぶ。

登録するのは Codex 用 manifest を持つプラグイン(CODEX_PLUGINS)だけ。

呼び出し形:

    codex_manifest.py            CODEX_PLUGINS の manifest と marketplace を生成する(冪等)
    codex_manifest.py --check    生成結果と作業ツリーのずれ・バージョン不一致・参照先の欠落を
                                 検出して非0終了(書き換えない)
    codex_manifest.py --selftest 生成・検出の判定ロジックの自己テスト
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_PLUGINS = ["flow"]
CODEX_HOOKS = {"flow": "./hooks/codex-hooks.json"}
CLAUDE_MARKETPLACE = ".claude-plugin/marketplace.json"
CODEX_MARKETPLACE = ".agents/plugins/marketplace.json"


def claude_manifest_path(name):
    return f"plugins/{name}/.claude-plugin/plugin.json"


def codex_manifest_path(name):
    return f"plugins/{name}/.codex-plugin/plugin.json"


def read_json(rel):
    return json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))


def write_json(rel, data):
    path = REPO_ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_manifest(claude):
    manifest = {
        "name": claude["name"],
        "version": claude["version"],
        "description": claude["description"],
        "skills": "./skills/",
    }
    if claude["name"] in CODEX_HOOKS:
        manifest["hooks"] = CODEX_HOOKS[claude["name"]]
    return manifest


def build_marketplace(claude_marketplace, names):
    plugins = []
    for entry in claude_marketplace["plugins"]:
        if entry["name"] not in names:
            continue
        plugins.append({
            "name": entry["name"],
            "source": {"source": "local", "path": entry["source"]},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Developer Tools",
        })
    return {"name": claude_marketplace["name"], "plugins": plugins}


def sync_manifest(name):
    """Claude 側の plugin.json から Codex 側の manifest を書き、そのパスを返す。"""
    rel = codex_manifest_path(name)
    write_json(rel, build_manifest(read_json(claude_manifest_path(name))))
    return rel


def sync_marketplace():
    write_json(CODEX_MARKETPLACE, build_marketplace(read_json(CLAUDE_MARKETPLACE), CODEX_PLUGINS))
    return CODEX_MARKETPLACE


def sync_all():
    return [sync_manifest(name) for name in CODEX_PLUGINS] + [sync_marketplace()]


def find_problems(read, exists):
    """期待する生成物と実ファイルを突き合わせ、問題の一覧を返す。

    read(rel) は JSON を返し、無ければ None。exists(rel) はパスの実在を返す。
    """
    problems = []
    claude_marketplace = read(CLAUDE_MARKETPLACE)
    if claude_marketplace is None:
        return [f"{CLAUDE_MARKETPLACE} が読めない"]
    for name in CODEX_PLUGINS:
        claude = read(claude_manifest_path(name))
        actual = read(codex_manifest_path(name))
        if claude is None:
            problems.append(f"{claude_manifest_path(name)} が読めない")
            continue
        if actual is None:
            problems.append(f"{codex_manifest_path(name)} が無い(生成すること)")
            continue
        if actual.get("version") != claude.get("version"):
            problems.append(
                f"{name}: Codex manifest のバージョン {actual.get('version')} が"
                f" Claude 側 {claude.get('version')} と一致しない"
            )
        if actual != build_manifest(claude):
            problems.append(f"{codex_manifest_path(name)} が生成結果と異なる(手で編集していないか)")
        if not exists(f"plugins/{name}/skills"):
            problems.append(f"{name}: manifest の skills の参照先 plugins/{name}/skills が無い")
    actual_marketplace = read(CODEX_MARKETPLACE)
    if actual_marketplace is None:
        problems.append(f"{CODEX_MARKETPLACE} が無い(生成すること)")
    else:
        if actual_marketplace != build_marketplace(claude_marketplace, CODEX_PLUGINS):
            problems.append(f"{CODEX_MARKETPLACE} が生成結果と異なる")
        for entry in actual_marketplace.get("plugins", []):
            path = entry.get("source", {}).get("path", "")
            if not exists(path.removeprefix("./") + "/.codex-plugin/plugin.json"):
                problems.append(f"marketplace の {entry.get('name')} の参照先 {path} に Codex manifest が無い")
    return problems


def cmd_check():
    def read(rel):
        try:
            return read_json(rel)
        except (OSError, json.JSONDecodeError):
            return None

    problems = find_problems(read, lambda rel: (REPO_ROOT / rel).exists())
    for problem in problems:
        print(f"Codex 配布メタデータ: {problem}")
    if problems:
        print("python3 scripts/codex_manifest.py で生成し直すこと")
        return 1
    print("Codex 配布メタデータ OK")
    return 0


def _selftest():
    ok = True
    claude_mp = {"name": "m", "plugins": [
        {"name": "flow", "source": "./plugins/flow", "description": "d"},
        {"name": "other", "source": "./plugins/other", "description": "d"},
    ]}
    claude = {"name": "flow", "version": "1.2.3", "description": "d"}

    def files(**overrides):
        data = {
            CLAUDE_MARKETPLACE: claude_mp,
            claude_manifest_path("flow"): claude,
            codex_manifest_path("flow"): build_manifest(claude),
            CODEX_MARKETPLACE: build_marketplace(claude_mp, CODEX_PLUGINS),
        }
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def run(data, absent=()):
        present = set(data) | {"plugins/flow/skills", "plugins/flow/.codex-plugin/plugin.json"}
        return find_problems(data.get, lambda rel: rel in present and rel not in absent)

    marketplace = build_marketplace(claude_mp, CODEX_PLUGINS)
    if [p["name"] for p in marketplace["plugins"]] != ["flow"]:
        ok = False
        print("FAIL build_marketplace: Codex manifest を持つプラグインだけを登録しない")
    cases = [
        ("正常", files(), 0),
        ("バージョン不一致", files(**{codex_manifest_path("flow"): {**build_manifest(claude), "version": "1.2.2"}}), 2),
        ("manifest 欠落", files(**{codex_manifest_path("flow"): None}), 1),
        ("marketplace 欠落", files(**{CODEX_MARKETPLACE: None}), 1),
        ("手編集", files(**{codex_manifest_path("flow"): {**build_manifest(claude), "keywords": ["x"]}}), 1),
        ("marketplace 不一致", files(**{CODEX_MARKETPLACE: {"name": "m", "plugins": []}}), 1),
    ]
    manifest_target = "plugins/flow/.codex-plugin/plugin.json"
    for name, data, absent, want in [(n, d, (), w) for n, d, w in cases] + [
        ("marketplace 参照先欠落", files(), (manifest_target,), 1),
        ("skills 参照先欠落", files(), ("plugins/flow/skills",), 1),
    ]:
        got = len(run(data, absent))
        if got != want:
            ok = False
            print(f"FAIL find_problems {name}: want={want} got={got}")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(_selftest())
    if sys.argv[1:] == ["--check"]:
        sys.exit(cmd_check())
    if sys.argv[1:]:
        print("usage: codex_manifest.py [--check | --selftest]")
        sys.exit(2)
    for rel in sync_all():
        print(f"生成: {rel}")
