#!/usr/bin/env python3
"""Codex 上でコミットを成立させる唯一の経路。レビューの結末と対象範囲を検査してからコミットする。

Codex のフックは呼び出し元のエージェントを識別できないので、Claude 側のように「ワーカーだけが
コミットできる」とは書けない。代わりに、コミットの許可を本スクリプトへ集める。Codex 用の書込ガード
(hooks/codex-guard-git-write.py)が直接のコミット操作を拒否するので、コミットはここを通る。

呼び出し形:

    codex_commit.py --repo REPO --files PATH [PATH ...]
    codex_commit.py --selftest

標準入力: UTF-8 の JSON オブジェクト {"review": "レビュー原文", "message": "起草済みメッセージ"}。
- review: レビュアーの最終応答の原文。末尾の `未解決の指摘: 0件` の申告が無ければ拒否する。
  千日手を押して通す経路は Codex 上には無い
- message: 起草済みのコミットメッセージ。件名に日本語が無い・末尾に Codex 名義の
  Co-Authored-By トレーラが無い・作業過程参照を含むときは拒否する
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
CODEX_TRAILER = "Co-Authored-By: Codex <noreply@openai.com>"
TRAILER = re.compile(r"^\s*co-authored-by:", re.IGNORECASE)
NUMBERED_STEP = re.compile(r"Step\s*[0-9]|ステップ\s*[0-9]|(?<!グ)ラウンド")


def load_hook(name):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "hooks" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args, input_text=None):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
        input=input_text,
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
    lines = message.rstrip().splitlines()
    trailers = [line for line in lines if TRAILER.match(line)]
    if trailers != [CODEX_TRAILER] or not lines or lines[-1] != CODEX_TRAILER:
        return "末尾の独立行に Co-Authored-By: Codex <noreply@openai.com> を1行だけ付ける"
    if len(lines) < 2 or lines[-2].strip():
        return "Co-Authored-By の前に空行が必要"
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
    result = git(repo, "diff", "--cached", "--no-renames", "--name-only", "-z")
    return {name for name in result.stdout.split("\0") if name}


def main(argv):
    if argv == ["--selftest"]:
        return selftest()
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--files", nargs="+", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        return refusal(f"標準入力の JSON を読めない: {error}")
    if (not isinstance(payload, dict) or set(payload) != {"review", "message"}
            or not all(isinstance(payload[key], str) for key in ("review", "message"))):
        return refusal("標準入力は review・message の文字列を持つ JSON オブジェクトを渡す")
    repo = Path(args.repo).resolve()
    if not (repo / "docs" / "conventions" / "verification.md").is_file():
        return refusal("導入契約の検証手順書 docs/conventions/verification.md が無い")
    reason = check_review(payload["review"])
    if reason:
        return refusal(reason)
    message = payload["message"]
    reason = check_message(message) or check_paths(repo, args.files)
    if reason:
        return refusal(reason)
    staged = staged_names(repo)
    outside = staged - set(args.files)
    if outside:
        return refusal("指定外のファイルが既にステージされている: " + ", ".join(sorted(outside)))
    to_add = [name for name in args.files if (repo / name).exists() or name not in staged]
    if to_add:
        added = git(repo, "add", "--", *to_add)
        if added.returncode != 0:
            print(json.dumps({"status": "failed", "reason": added.stderr.strip()[-300:]}))
            return 1
    if staged_names(repo) != set(args.files):
        return refusal("ステージ済みの集合が指定ファイルと一致しない(変更の無いファイルを指定していないか)")
    if git(repo, "diff", "--name-only", "--", *args.files).stdout.strip():
        return refusal("指定ファイルに未ステージの変更が残っている")
    made = git(repo, "commit", "-q", "-F", "-", input_text=message)
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
    import tempfile

    failures = []
    hygiene = load_hook("guard-artifact-hygiene")
    relative_word = next(atom.label for atom in hygiene.ATOMS if atom.exempt_group == "P2")
    good_review = "指摘なし。\n\n未解決の指摘: 0件\n"
    good_message = "a.txt を追加する\n\n- 初期内容を置く\n\n" + CODEX_TRAILER + "\n"

    def must_git(repo, *args):
        result = git(repo, *args)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {result.stderr}")
        return result.stdout

    def run(repo, review, message, files):
        return run_input(repo, json.dumps({"review": review, "message": message}, ensure_ascii=False), files)

    def run_input(repo, text, files):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--repo", str(repo),
                                 "--files", *files], input=text, capture_output=True,
                                text=True, encoding="utf-8")
        return result.returncode, json.loads(result.stdout)

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
        ("トレーラ無し", good_review, "a.txt を追加する\n", ["a.txt"]),
        ("Claude 名義のトレーラ", good_review, good_message.replace(CODEX_TRAILER, "Co-Authored-By: Claude Opus <someone@example.com>"), ["a.txt"]),
        ("製品名を含まない名義でも Anthropic のアドレス", good_review, good_message.replace(CODEX_TRAILER, "Co-Authored-By: X <noreply@anthropic.com>"), ["a.txt"]),
        ("Codex トレーラの重複", good_review, good_message + "\n" + CODEX_TRAILER + "\n", ["a.txt"]),
        ("字下げされたトレーラの重複", good_review, good_message + "\n " + CODEX_TRAILER + "\n", ["a.txt"]),
        ("Codex トレーラのアドレス違い", good_review, good_message.replace("noreply@openai.com", "other@example.com"), ["a.txt"]),
        ("Codex トレーラが末尾でない", good_review, good_message + "本文\n", ["a.txt"]),
        ("Codex トレーラの前に空行無し", good_review, good_message.replace("\n\n" + CODEX_TRAILER, "\n" + CODEX_TRAILER), ["a.txt"]),
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

    for invalid in ("", "{", "[]", "null", '{"review":0,"message":"本文"}',
                    '{"review":"原文"}', '{"review":"原文","message":"本文","extra":1}'):
        with tempfile.TemporaryDirectory() as scratch:
            repo = fresh(Path(scratch))
            expect("不正な標準入力", run_input(repo, invalid, ["a.txt"]), 3, "refused", repo)
            if staged_names(repo):
                failures.append("不正な標準入力でステージが変わった")

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
        if recorded.rstrip().splitlines()[-1] != CODEX_TRAILER:
            failures.append("Codex のトレーラが末尾に無い")
        if history_length(repo) != 1 or staged_names(repo):
            failures.append("コミットの後にステージが残った")
        if {p.name for p in repo.parent.iterdir()} != {"repo"}:
            failures.append("コミットの入力ファイルが作られた")
        if "b.txt" in must_git(repo, "show", "--name-only", "--format=", "HEAD"):
            failures.append("指定外のファイルがコミットに入った")
        expect("変更の無いファイルの指定", run(repo, good_review, good_message, ["a.txt"]), 3, "refused")
        if history_length(repo) != 1:
            failures.append("変更の無い指定でコミットが増えた")

    for mixed in (False, True):
        with tempfile.TemporaryDirectory() as scratch:
            repo = fresh(Path(scratch))
            expect("削除ケースの初期コミット", run(repo, good_review, good_message, ["a.txt", "b.txt"]), 0, "committed")
            must_git(repo, "rm", "--", "a.txt")
            files = ["a.txt"]
            if mixed:
                (repo / "b.txt").write_text("変更\n", encoding="utf-8")
                files.append("b.txt")
            message = "a.txt を削除する\n\n" + CODEX_TRAILER + "\n"
            expect("ステージ済み削除を含む対象", run(repo, good_review, message, files), 0, "committed")
            committed = set(must_git(repo, "show", "--name-only", "--format=", "HEAD").splitlines())
            if history_length(repo) != 2 or staged_names(repo) or committed != set(files):
                failures.append("ステージ済み削除を含む対象が過不足なくコミットされない")
            if git(repo, "cat-file", "-e", "HEAD:a.txt").returncode == 0:
                failures.append("削除したファイルがコミットに残る")

    for complete in (False, True):
        with tempfile.TemporaryDirectory() as scratch:
            repo = fresh(Path(scratch))
            expect("名前変更ケースの初期コミット", run(repo, good_review, good_message, ["a.txt", "b.txt"]), 0, "committed")
            must_git(repo, "mv", "--", "a.txt", "c.txt")
            files = ["a.txt", "c.txt"] if complete else ["c.txt"]
            message = "a.txt を c.txt へ改名する\n\n" + CODEX_TRAILER + "\n"
            outcome = run(repo, good_review, message, files)
            if complete:
                expect("名前変更の両パスを指定", outcome, 0, "committed")
                if (history_length(repo) != 2 or staged_names(repo)
                        or git(repo, "cat-file", "-e", "HEAD:a.txt").returncode == 0
                        or must_git(repo, "show", "HEAD:c.txt") != "a\n"):
                    failures.append("名前変更の両パスが正しくコミットされない")
            else:
                expect("名前変更の元パスを省略", outcome, 3, "refused")
                if history_length(repo) != 1 or staged_names(repo) != {"a.txt", "c.txt"}:
                    failures.append("名前変更の元パスを省略した拒否で履歴・ステージが変わる")

    for failure in failures:
        print(f"FAIL {failure}")
    print("ALL PASS" if not failures else "SOME FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
