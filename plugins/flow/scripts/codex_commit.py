#!/usr/bin/env python3
"""Codex 上でコミットを成立させる唯一の経路。レビューの結末と対象範囲を検査してからコミットする。

Codex のフックは呼び出し元のエージェントを識別できないので、Claude 側のように「ワーカーだけが
コミットできる」とは書けない。代わりに、コミットの許可を本スクリプトへ集める。Codex 用の書込ガード
(hooks/codex-guard-git-write.py)が直接のコミット操作を拒否するので、コミットはここを通る。

呼び出し形:

    codex_commit.py --repo REPO --review-file FILE --message-file FILE --files PATH [PATH ...]

- review-file: レビュアーの最終応答の原文。末尾の `未解決の指摘: 0件` の申告が無ければ拒否する。
  千日手を押して通す経路は Codex 上には無い
- message-file: 起草済みのコミットメッセージ。件名に日本語が無い・Claude 名義のトレーラがある・
  作業過程参照を含むときは拒否する
- files: ステージする個別ファイル(リポジトリ内の相対パス)。ステージ済みがこの集合と一致しなければ拒否する

終了コード: 0 はコミットした。3 は検査で拒否した(何もコミットしていない)。1 は git の失敗、2 は引数の誤り。
標準出力は JSON 1個(status・理由またはハッシュと件名)。
"""
import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
GLOB_CHARS = set("*?[]")
JAPANESE = re.compile(r"[぀-ヿ㐀-鿿]")
TRAILER = re.compile(r"^co-authored-by:.*(?:claude|anthropic)", re.IGNORECASE | re.MULTILINE)
NUMBERED_STEP = re.compile(r"Step\s*[0-9]|ステップ\s*[0-9]|(?<!グ)ラウンド")


def load_hook(name):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "hooks" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def refusal(reason):
    print(json.dumps({"status": "refused", "reason": reason}))
    return 3


def check_review(text):
    gate = load_hook("guard-commit-gate")
    counts = [int(m.group(1)) for m in map(gate.DECLARATION.match, text.splitlines()) if m]
    if not counts:
        return "レビュー応答の原文の末尾に、未解決件数の申告(未解決の指摘: N件)が無い"
    if counts[-1] != 0:
        return f"レビュー応答の申告が未解決 {counts[-1]} 件で、収束していない"
    return None


def check_message(message):
    subject = message.strip().splitlines()[0] if message.strip() else ""
    if not JAPANESE.search(subject):
        return "件名に日本語が無い"
    if TRAILER.search(message):
        return "Claude 名義の Co-Authored-By トレーラを付けてはならない"
    hygiene = load_hook("guard-artifact-hygiene")
    relative = [atom.label for atom in hygiene.ATOMS if atom.exempt_group == "P2" and atom.pattern.search(message)]
    found = NUMBERED_STEP.search(message)
    if relative or found:
        return "作業過程参照・相対参照を含む: " + ", ".join(relative + ([found.group(0)] if found else []))
    return None


def check_paths(repo, files):
    for name in files:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or GLOB_CHARS & set(name) or name in (".", ""):
            return f"対象は個別ファイルの相対パスだけである: {name}"
        if (repo / path).is_dir():
            return f"ディレクトリは対象にできない: {name}"
    return None


def staged_names(repo):
    result = git(repo, "diff", "--cached", "--name-only", "-z")
    return {name for name in result.stdout.split("\0") if name}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--review-file", required=True)
    parser.add_argument("--message-file", required=True)
    parser.add_argument("--files", nargs="+", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    repo = Path(args.repo).resolve()
    if not (repo / "docs" / "conventions" / "verification.md").is_file():
        return refusal("導入契約の検証手順書 docs/conventions/verification.md が無い")
    reason = check_review(Path(args.review_file).read_text(encoding="utf-8"))
    if reason:
        return refusal(reason)
    message = Path(args.message_file).read_text(encoding="utf-8")
    reason = check_message(message) or check_paths(repo, args.files)
    if reason:
        return refusal(reason)
    outside = staged_names(repo) - set(args.files)
    if outside:
        return refusal("指定外のファイルが既にステージされている: " + ", ".join(sorted(outside)))
    added = git(repo, "add", "--", *args.files)
    if added.returncode != 0:
        print(json.dumps({"status": "failed", "reason": added.stderr.strip()[-300:]}))
        return 1
    if staged_names(repo) != set(args.files):
        return refusal("ステージ済みの集合が指定ファイルと一致しない(変更の無いファイルを指定していないか)")
    if git(repo, "diff", "--name-only", "--", *args.files).stdout.strip():
        return refusal("指定ファイルに未ステージの変更が残っている")
    made = git(repo, "commit", "-q", "-F", str(Path(args.message_file).resolve()))
    if made.returncode != 0:
        print(json.dumps({"status": "failed", "reason": made.stderr.strip()[-300:]}))
        return 1
    head = git(repo, "log", "-1", "--format=%h%n%s").stdout.splitlines()
    print(json.dumps({"status": "committed", "hash": head[0], "subject": head[1]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
