"""flow のスクリプトが import する、Claude Code の設定ディレクトリの解決の共有モジュール。"""
import os
from pathlib import Path


def claude_config_dir():
    """`CLAUDE_CONFIG_DIR` が空でなければそのパス、そうでなければ `~/.claude`。"""
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(config) if config else Path.home() / ".claude"
