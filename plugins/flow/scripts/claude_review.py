#!/usr/bin/env python3
"""Codex から Claude Code CLI の非対話セッションへレビューを委譲する。

Codex 上の反復レビュー・ループ(flow:opus-review-loop・flow:fable-review-loop の Codex 経路)が
1ラウンドごとに起動する。固定モデル・読み取り専用・時間上限・ラウンド継続を、この
スクリプトが守る。結果は標準出力へ JSON 1個で返す。

呼び出し形:

    claude_review.py --family opus --cwd REPO --base REV --prompt-file FILE [--resume SESSION_ID]
                     [--timeout SECS] [--out-dir DIR]
    claude_review.py --selftest

- family: opus か fable。実行モデルの系統でもあり、定義 agents/<family>-reviewer.md を選ぶ。
  実際に応答したモデルが系統と一致しなければ、結果を採らず status を unavailable にする(代替しない)
- base: 差分の比較の基点。作業ツリーとの差分を diff.patch、状態を status.txt に書いて渡す
- prompt-file: レビュー指示文。標準入力でセッションへ渡す
- resume: 前ラウンドの session_id。継続ラウンドで指定する(定義の再付与はしない)
- out-dir: diff.patch・status.txt の置き場(既定は実行ごとに作る一時ディレクトリで、終了時に消す)

隔離: 通常のセッションに入る flow・guard のフックは、委譲したセッションへ停止の申告や対応済みの
記録を求めてしまうので、--safe-mode でプラグイン・フック・カスタムエージェントを無効にする。
安全装置も外れるため、読み取り専用はツールの限定で守る。使えるツールは Read・Grep・Glob だけで、
Bash と編集系ツールは渡さず、明示の拒否も併せて渡す。差分は呼び出し側が diff.patch へ書いて渡す。

終了コード: 0 は ok。1 は failed(上記以外の失敗)、2 は引数の誤り、3 は usage_limit(使用量上限)、
4 は timeout(時間上限)、5 は unavailable(モデルが使えない、または応答したモデルが系統と不一致)。
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ("opus", "fable")
DEFAULT_TIMEOUT = 900
OVERRIDE = (
    "## この実行での読み替え\n\n"
    "この実行で使えるツールは Read・Grep・Glob だけで、Bash は使えない。上の定義にある、git で差分を"
    "自分で取得する手順と Bash の使用は、依頼文が示す差分ファイルと状態ファイルを Read で読むことに"
    "読み替える。それ以外の定義はそのまま従う。"
)
EXIT_CODES = {"ok": 0, "failed": 1, "usage_limit": 3, "timeout": 4, "unavailable": 5}
LIMIT_WORDS = ("usage limit", "limit reached", "rate limit", "quota")
UNAVAILABLE_WORDS = ("model", "not found", "not available", "unavailable", "access", "entitle")


def run_git(repo, *args):
    """git の標準出力を返す。失敗したら RuntimeError を送出する。"""
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} が失敗した: {result.stderr.strip()[-300:]}")
    return result.stdout


def write_inputs(repo, base, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    diff = out_dir / "diff.patch"
    status = out_dir / "status.txt"
    diff.write_text(run_git(repo, "diff", "--no-ext-diff", "--no-textconv", base), encoding="utf-8")
    status.write_text(run_git(repo, "status", "--short", "--untracked-files=all"), encoding="utf-8")
    return diff, status


def definition_body(family):
    text = (PLUGIN_ROOT / "agents" / f"{family}-reviewer.md").read_text(encoding="utf-8")
    parts = text.split("---", 2)
    body = parts[2].strip() if len(parts) == 3 else text
    return body + "\n\n" + OVERRIDE


def preface(base, diff, status, resume):
    lines = [
        "[実行環境] レビュー対象の差分は次のファイルにあり、毎ラウンド作り直されている。",
        f"- 差分(比較の基点 {base} から作業ツリーまで): {diff}",
        f"- 状態(新規ファイルは差分に現れないのでここで確かめる): {status}",
    ]
    if resume:
        lines.append("- これは継続ラウンドである。前ラウンドの定義・目的・枠は保持している前提で、次の依頼に答える。")
    return "\n".join(lines) + "\n\n"


def classify(payload, family, returncode, stderr):
    """戻り値は (status, model, session_id, text)。"""
    if payload is None:
        return "failed", None, None, (stderr or "").strip()[-500:] or f"終了コード {returncode}"
    text = payload.get("result") or ""
    session_id = payload.get("session_id")
    usage = payload.get("modelUsage") or {}
    model = max(usage, key=lambda name: usage[name].get("outputTokens", 0)) if usage else None
    if payload.get("is_error"):
        lowered = text.lower()
        if any(word in lowered for word in LIMIT_WORDS):
            return "usage_limit", model, session_id, text
        if any(word in lowered for word in UNAVAILABLE_WORDS):
            return "unavailable", model, session_id, text
        return "failed", model, session_id, text
    if model is None or not model.startswith(f"claude-{family}"):
        return "unavailable", model, session_id, f"応答したモデル {model} が系統 {family} と一致しない"
    return "ok", model, session_id, text


def build_command(claude, family, resume, definition_file):
    command = [
        claude, "-p", "--safe-mode", "--model", family,
        "--permission-mode", "dontAsk",
        "--tools", "Read,Grep,Glob",
        "--allowedTools", "Read", "Grep", "Glob",
        "--disallowedTools", "Edit", "Write", "NotebookEdit", "Bash",
        "--output-format", "json",
    ]
    if resume:
        command += ["--resume", resume]
    else:
        command += ["--append-system-prompt-file", str(definition_file)]
    return command


def emit(outcome, model, session_id, text):
    # Windows では標準出力の文字コードが環境依存
    print(json.dumps({"status": outcome, "model": model, "session_id": session_id, "result": text}))


def main(argv):
    if argv == ["--selftest"]:
        return selftest()
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--family", required=True, choices=FAMILIES)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--resume")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--out-dir")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    repo = Path(args.cwd).resolve()
    claude = shutil.which("claude")
    if claude is None:
        emit("failed", None, None, "claude コマンドが見つからない")
        return EXIT_CODES["failed"]
    if args.out_dir:
        return delegate(args, repo, claude, Path(args.out_dir))
    with tempfile.TemporaryDirectory(prefix="flow-claude-review-") as scratch:
        return delegate(args, repo, claude, Path(scratch))


def delegate(args, repo, claude, out_dir, runner=subprocess.run):
    try:
        diff, status_file = write_inputs(repo, args.base, out_dir)
    except RuntimeError as error:
        emit("failed", None, None, str(error))
        return EXIT_CODES["failed"]
    definition_file = out_dir / f"{args.family}-reviewer-definition.md"
    definition_file.write_text(definition_body(args.family), encoding="utf-8")
    prompt = preface(args.base, diff, status_file, args.resume) + Path(args.prompt_file).read_text(encoding="utf-8")
    try:
        completed = runner(
            build_command(claude, args.family, args.resume, definition_file),
            cwd=repo, input=prompt, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        emit("timeout", None, args.resume, f"{args.timeout}秒以内に終わらなかった")
        return EXIT_CODES["timeout"]
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    outcome, model, session_id, text = classify(payload, args.family, completed.returncode, completed.stderr)
    emit(outcome, model, session_id, text)
    return EXIT_CODES[outcome]


def selftest():
    import contextlib
    import io
    import types

    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: want={want!r} got={got!r}")

    def payload(text="x", model="claude-opus-5-5", is_error=False, session="s1"):
        return {
            "is_error": is_error, "result": text, "session_id": session,
            "modelUsage": {model: {"outputTokens": 100}, "claude-haiku-4-5": {"outputTokens": 3}},
        }

    check("ok", classify(payload(), "opus", 0, "")[0], "ok")
    check("系統の不一致は代替せず unavailable", classify(payload(model="claude-sonnet-5"), "opus", 0, "")[0], "unavailable")
    check("系統が違う Fable も unavailable", classify(payload(), "fable", 0, "")[0], "unavailable")
    check("使用量上限", classify(payload("Usage limit reached", is_error=True), "opus", 1, "")[0], "usage_limit")
    check("モデル利用不可", classify(payload("model not available", is_error=True), "opus", 1, "")[0], "unavailable")
    check("その他の失敗", classify(payload("boom", is_error=True), "opus", 1, "")[0], "failed")
    check("JSON でない出力", classify(None, "opus", 1, "err")[0], "failed")
    check("モデル情報が無い応答は採らない", classify({"result": "x", "session_id": "s"}, "opus", 0, "")[0], "unavailable")
    check("未解決が残る応答も本文を変えず返す", classify(payload("未解決の指摘: 2件"), "opus", 0, "")[3], "未解決の指摘: 2件")
    check("終了コードは区別される", len(set(EXIT_CODES.values())), len(EXIT_CODES))

    first_round = build_command("claude", "opus", None, Path("d.md"))
    next_round = build_command("claude", "opus", "sid-1", Path("d.md"))
    check("モデル固定", first_round[first_round.index("--model") + 1], "opus")
    check("隔離", "--safe-mode" in first_round, True)
    check("ツールの限定", first_round[first_round.index("--tools") + 1], "Read,Grep,Glob")
    denied = first_round[first_round.index("--disallowedTools") + 1:first_round.index("--output-format")]
    check("書き込み系の明示拒否", {"Edit", "Write", "Bash"} <= set(denied), True)
    check("ラウンド1は定義を渡す", "--append-system-prompt-file" in first_round and "--resume" not in first_round, True)
    check("継続は resume で定義を再付与しない", "--resume" in next_round and "--append-system-prompt-file" not in next_round, True)
    check("継続の session_id", next_round[next_round.index("--resume") + 1], "sid-1")
    check("定義に読み替えを付ける", OVERRIDE in definition_body("opus"), True)

    calls = []

    def fake_ok(command, **kwargs):
        calls.append(command)
        return types.SimpleNamespace(
            stdout=json.dumps(payload("未解決の指摘: 1件\n結末: 継続")), stderr="", returncode=0)

    def fake_timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    def run_delegate(runner, base=None, resume=None):
        with tempfile.TemporaryDirectory() as scratch:
            repo = Path(scratch) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            base = base or run_git(repo, "write-tree").strip()
            (repo / "a.txt").write_text("a", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
            prompt = Path(scratch) / "prompt.md"
            prompt.write_text("p", encoding="utf-8")
            args = types.SimpleNamespace(
                family="opus", base=base, resume=resume, timeout=5, prompt_file=str(prompt))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = delegate(args, repo, "claude", Path(scratch) / "out", runner)
            return code, json.loads(buffer.getvalue())

    code, result = run_delegate(fake_ok)
    check("通常の結果の終了コード", code, 0)
    check("通常の結果の status", result["status"], "ok")
    check("指摘が残る結末を変えず返す", result["result"], "未解決の指摘: 1件\n結末: 継続")
    check("通常の結果の session_id", result["session_id"], "s1")
    run_delegate(fake_ok, resume="sid-1")
    check("継続ラウンドの resume 指定", calls[-1][calls[-1].index("--resume") + 1], "sid-1")
    code, result = run_delegate(fake_timeout, resume="sid-1")
    check("時間上限の終了コード", code, EXIT_CODES["timeout"])
    check("時間上限の status", result["status"], "timeout")
    calls.clear()
    code, result = run_delegate(fake_ok, "no-such-rev")
    check("差分を作れなければ失敗", (code, result["status"]), (EXIT_CODES["failed"], "failed"))
    check("差分を作れなければレビューを起動しない", calls, [])

    for failure in failures:
        print(f"FAIL {failure}")
    print("ALL PASS" if not failures else "SOME FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
