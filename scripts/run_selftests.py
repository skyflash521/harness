#!/usr/bin/env python3
"""自己テストを持つスクリプトを追跡ファイルから発見して実行する。

検査対象を文書やワークフローへ列挙すると、スクリプトが増えるたびに追記漏れが起きる。ここでは
母集団を `git ls-files` に取り、`--selftest` を受け付けると宣言している追跡スクリプトを見つけて
すべて実行する。新しいフックを足せば、その時点で自動的に検査対象へ入る。

判定は「ファイル本文に `--selftest` の文字列があること」で行う。自己テストを持たないスクリプトは
その旨を出力に並べるので、持つべきものが漏れていれば一覧を見て気付ける。

Usage: python3 scripts/run_selftests.py
Exit code: 実行した自己テストがすべて成功なら 0、1つでも失敗すれば 1。
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
MARKER = "--selftest"


def tracked_scripts():
    """追跡下の実行可能スクリプト候補を返す。"""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    for rel in (line for line in out.split("\0") if line):
        if rel.endswith((".py", ".sh")) or rel.startswith(".githooks/"):
            yield rel


def declares_selftest(path):
    try:
        return MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def launcher(path):
    """起動に使うインタプリタ。shebang で判定し、読めなければ Python とみなす。"""
    try:
        shebang = path.read_text(encoding="utf-8").split("\n", 1)[0]
    except (OSError, UnicodeDecodeError):
        shebang = ""
    return "bash" if "sh" in shebang.rsplit("/", 1)[-1] and "python" not in shebang else sys.executable


def main():
    targets, skipped = [], []
    for rel in sorted(tracked_scripts()):
        path = REPO_ROOT / rel
        if not path.is_file() or path.resolve() == SELF:
            continue
        (targets if declares_selftest(path) else skipped).append(rel)

    failures = []
    for rel in targets:
        path = REPO_ROOT / rel
        result = subprocess.run(
            [launcher(path), str(path), MARKER],
            cwd=REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        )
        status = "OK" if result.returncode == 0 else f"FAIL (exit {result.returncode})"
        print(f"{status}: {rel}")
        if result.returncode != 0:
            failures.append(rel)
            for stream in (result.stdout, result.stderr):
                if stream.strip():
                    print(stream.rstrip())

    if skipped:
        print("\n自己テストを持たないスクリプト(検査対象外):")
        for rel in skipped:
            print(f"  {rel}")

    if failures:
        print(f"\n{len(failures)}件の自己テストが失敗した。")
        return 1
    print(f"\n自己テスト OK({len(targets)}件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
