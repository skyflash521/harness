#!/usr/bin/env python3
"""Codex 用の配布メタデータ(manifest・marketplace)を Claude 側の正本から生成する。

正本は `plugins/<name>/.claude-plugin/plugin.json` と `.claude-plugin/marketplace.json`。
Codex 側のファイルは手で編集せず、このスクリプトで生成する。バージョン刻印は
`stamp_plugin_version.py` が刻印のたびに本スクリプトの同期を呼ぶ。

登録するのは Codex 用 manifest を持つプラグイン(CODEX_PLUGINS)だけ。

呼び出し形:

    codex_manifest.py          CODEX_PLUGINS の manifest と marketplace を生成する(冪等)
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_PLUGINS = ["flow"]
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
    return {
        "name": claude["name"],
        "version": claude["version"],
        "description": claude["description"],
        "skills": "./skills/",
    }


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


if __name__ == "__main__":
    if sys.argv[1:]:
        print("usage: codex_manifest.py")
        sys.exit(2)
    for rel in sync_all():
        print(f"生成: {rel}")
