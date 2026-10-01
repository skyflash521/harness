#!/usr/bin/env python3
"""Codex 上でコミットを成立させる唯一の経路。レビューの結末と対象範囲を検査してからコミットする。

Codex のフックは呼び出し元のエージェントを識別できないので、Claude 側のように「ワーカーだけが
コミットできる」とは書けない。代わりに、コミットの許可を本スクリプトへ集める。Codex 用の書込ガード
(hooks/codex-guard-git-write.py)が直接のコミット操作を拒否するので、コミットはここを通る。

呼び出し形:

    codex_commit.py --repo REPO --review-file FILE --message-file FILE --files PATH [PATH ...]
    codex_commit.py --selftest

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
    if argv == ["--selftest"]:
        return selftest()
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


def selftest():
    import os

    isolated = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    saved = {name: os.environ.get(name) for name in isolated}
    os.environ.update(isolated)
    try:
        return run_selftest()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_selftest():
    import contextlib
    import io
    import tempfile

    failures = []
    hygiene = load_hook("guard-artifact-hygiene")
    relative_word = next(atom.label for atom in hygiene.ATOMS if atom.exempt_group == "P2")
    good_review = "指摘なし。\n\n未解決の指摘: 0件\n"
    good_message = "a.txt を追加する\n\n- 初期内容を置く\n"

    def must_git(repo, *args):
        result = git(repo, *args)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {result.stderr}")
        return result.stdout

    def run(repo, review, message, files):
        review_file = repo.parent / "review.md"
        message_file = repo.parent / "message.txt"
        review_file.write_text(review, encoding="utf-8")
        message_file.write_text(message, encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--repo", str(repo), "--review-file", str(review_file),
                         "--message-file", str(message_file), "--files", *files])
        return code, json.loads(buffer.getvalue())

    def fresh(base, with_manual=True):
        repo = base / "repo"
        (repo / "docs" / "conventions").mkdir(parents=True)
        must_git(repo, "init", "-q")
        must_git(repo, "config", "user.email", "t@example.com")
        must_git(repo, "config", "user.name", "t")
        must_git(repo, "config", "commit.gpgsign", "false")
        must_git(repo, "config", "core.hooksPath", str(base / "no-hooks"))
        if with_manual:
            (repo / "docs" / "conventions" / "verification.md").write_text("# 検証\n", encoding="utf-8")
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        (repo / "b.txt").write_text("b\n", encoding="utf-8")
        return repo

    def history_length(repo):
        return len(git(repo, "log", "--oneline").stdout.splitlines())

    def expect(label, outcome, want_code, want_status, repo=None, kept_history=True):
        code, result = outcome
        if (code, result["status"]) != (want_code, want_status):
            failures.append(f"{label}: want={(want_code, want_status)} got={(code, result['status'])}")
        if repo is not None and kept_history and history_length(repo) != 0:
            failures.append(f"{label}: 拒否したのにコミットが増えた")

    refusals = (
        ("申告が無い", "指摘なし。\n", good_message, ["a.txt"]),
        ("未解決が残る", "未解決の指摘: 2件\n", good_message, ["a.txt"]),
        ("申告が末尾で非0へ戻る", "未解決の指摘: 0件\n\n追加で見つかった。\n\n未解決の指摘: 3件\n", good_message, ["a.txt"]),
        ("件名に日本語が無い", good_review, "add a.txt\n", ["a.txt"]),
        ("Claude 名義のトレーラ", good_review, good_message + "\nCo-Authored-By: Claude Opus <someone@example.com>\n", ["a.txt"]),
        ("製品名を含まない名義でも Anthropic のアドレス", good_review, good_message + "\nCo-Authored-By: X <noreply@anthropic.com>\n", ["a.txt"]),
        ("番号付きの作業工程の参照", good_review, "Step 3 で a.txt を追加する\n", ["a.txt"]),
        ("ラウンド番号の参照", good_review, "ラウンド2の指摘に対応して a.txt を追加する\n", ["a.txt"]),
        ("相対参照の語", good_review, relative_word + "の値を変えて a.txt を追加する\n", ["a.txt"]),
        ("ディレクトリ指定", good_review, good_message, ["docs"]),
        ("親ディレクトリ", good_review, good_message, ["../a.txt"]),
        ("グロブ", good_review, good_message, ["*.txt"]),
        ("ドット", good_review, good_message, ["."]),
    )
    for label, review, message, files in refusals:
        with tempfile.TemporaryDirectory() as scratch:
            repo = fresh(Path(scratch))
            expect(label, run(repo, review, message, files), 3, "refused", repo)

    with tempfile.TemporaryDirectory() as scratch:
        repo = fresh(Path(scratch))
        absolute = str(repo / "a.txt")
        expect("絶対パス", run(repo, good_review, good_message, [absolute]), 3, "refused", repo)

    with tempfile.TemporaryDirectory() as scratch:
        repo = fresh(Path(scratch), with_manual=False)
        expect("検証手順書が無い", run(repo, good_review, good_message, ["a.txt"]), 3, "refused", repo)

    with tempfile.TemporaryDirectory() as scratch:
        repo = fresh(Path(scratch))
        must_git(repo, "add", "--", "b.txt")
        expect("指定外が既にステージ済み", run(repo, good_review, good_message, ["a.txt"]), 3, "refused", repo)
        if staged_names(repo) != {"b.txt"}:
            failures.append("既存のステージを書き換えた")

    with tempfile.TemporaryDirectory() as scratch:
        repo = fresh(Path(scratch))
        expect("ステージが空になる指定", run(repo, good_review, good_message, ["missing.txt"]), 1, "failed", repo)

    with tempfile.TemporaryDirectory() as scratch:
        repo = fresh(Path(scratch))
        outcome = run(repo, good_review, good_message, ["a.txt"])
        expect("収束した対象", outcome, 0, "committed")
        recorded = must_git(repo, "log", "-1", "--format=%B")
        if recorded.strip() != good_message.strip():
            failures.append(f"起草したメッセージと記録されたメッセージが違う: {recorded!r}")
        if must_git(repo, "show", "HEAD:a.txt") != "a\n":
            failures.append("記録された a.txt の内容が作業ツリーと違う")
        if "Co-Authored-By" in recorded:
            failures.append("トレーラが入っている")
        if history_length(repo) != 1 or staged_names(repo):
            failures.append("コミットの後にステージが残った")
        if "b.txt" in must_git(repo, "show", "--name-only", "--format=", "HEAD"):
            failures.append("指定外のファイルがコミットに入った")
        expect("変更の無いファイルの指定", run(repo, good_review, good_message, ["a.txt"]), 3, "refused")
        if history_length(repo) != 1:
            failures.append("変更の無い指定でコミットが増えた")

    for failure in failures:
        print(f"FAIL {failure}")
    print("ALL PASS" if not failures else "SOME FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
