#!/usr/bin/env python3
"""Codex CLI 委譲のコマンドラインエントリポイント。

呼び出し形:
    codex_review.py --cwd REPO --prompt-file FILE [--resume SESSION_ID] [--timeout SECS]

結果は claude_review.py と同じ status・session_id・result を JSON で返す。
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from review_process import DEFAULT_TIMEOUT, run_review

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
EXIT_CODES = {"ok": 0, "failed": 1, "usage_limit": 3, "timeout": 4, "unavailable": 5, "resume_unavailable": 6}


def classify_error(output):
    lower = output.lower()
    if "no conversation found" in lower or "session not found" in lower:
        return "resume_unavailable"
    if any(word in lower for word in ("usage limit", "rate limit", "quota", "limit reached")):
        return "usage_limit"
    if any(word in lower for word in ("model not found", "model is not available", "model unavailable")):
        return "unavailable"
    return "failed"


def parse_events(output):
    session_id = None
    last_message = None
    errors = []
    completed = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            session_id = event.get("thread_id")
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                last_message = item.get("text")
        elif kind == "turn.completed":
            completed = True
        elif kind in ("error", "turn.failed"):
            errors.append(json.dumps(event, ensure_ascii=False))
    return session_id, last_message, completed, "\n".join(errors)


def build_command(codex, resume, output_file):
    if resume:
        return [codex, "exec", "resume", "--json", "-c", "sandbox_mode=read-only",
                "-o", str(output_file), resume, "-"]
    return [codex, "exec", "review", "--json",
            "-c", "sandbox_mode=read-only", "-o", str(output_file), "-"]


def emit(status, session_id, result):
    print(json.dumps({"status": status, "session_id": session_id, "result": result}))
    return EXIT_CODES[status]


def main(argv):
    if argv == ["--selftest"]:
        return selftest()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--resume")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    codex = shutil.which("codex")
    if codex is None:
        return emit("failed", None, "codex コマンドが見つからない")
    repo = Path(args.cwd).resolve()
    prompt_file = Path(args.prompt_file).resolve()
    if not repo.is_dir() or not prompt_file.is_file() or args.timeout <= 0:
        return emit("failed", None, "作業ディレクトリ・指示文・時間上限を確認できない")
    criteria = PLUGIN_ROOT / "docs" / "criteria"
    guidance = "\n\n".join((criteria / name).read_text(encoding="utf-8") for name in
                           ("review-viewpoints.md", "review-response.md", "review-request.md"))
    prompt = prompt_file.read_text(encoding="utf-8") + "\n\n[レビュー規約の正本]\n" + guidance
    with tempfile.TemporaryDirectory(prefix="flow-codex-review-") as scratch:
        output_file = Path(scratch) / "result.txt"
        command = build_command(codex, args.resume, output_file)
        try:
            completed = run_review(command, cwd=repo, input_text=prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return emit("timeout", args.resume, f"{args.timeout}秒以内に終わらなかった")
        session_id, message, finished, errors = parse_events(completed.stdout)
        session_id = session_id or args.resume
        if completed.returncode != 0 or not finished:
            detail = "\n".join(x for x in (errors, completed.stderr, completed.stdout[-500:]) if x)
            return emit(classify_error(detail), session_id, detail[-1000:])
        if output_file.is_file():
            message = output_file.read_text(encoding="utf-8").strip() or message
        if not message or not session_id:
            return emit("failed", session_id, "完了したレビューの本文またはセッション ID が無い")
        return emit("ok", session_id, message)


def selftest():
    import contextlib
    import io
    import types
    from unittest.mock import patch

    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: want={want!r} got={got!r}")

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        prompt_file = root / "prompt.md"
        prompt_file.write_text("未コミット差分を調べる", encoding="utf-8")
        calls = []

        def execute(events, *, resume=None, code=0, stderr=""):
            def fake_runner(command, **kwargs):
                calls.append((command, kwargs))
                Path(command[command.index("-o") + 1]).write_text("レビュー本文", encoding="utf-8")
                return types.SimpleNamespace(stdout=events, stderr=stderr, returncode=code)

            arguments = ["--cwd", str(root), "--prompt-file", str(prompt_file)]
            if resume:
                arguments += ["--resume", resume]
            output = io.StringIO()
            with patch(__name__ + ".run_review", fake_runner), patch(__name__ + ".shutil.which", return_value="codex"):
                with contextlib.redirect_stdout(output):
                    exit_code = main(arguments)
            return exit_code, json.loads(output.getvalue())

        success = "\n".join((json.dumps({"type": "thread.started", "thread_id": "s1"}),
                             json.dumps({"type": "turn.completed"})))
        code, result = execute(success)
        check("初回の結果", (code, result["status"], result["session_id"], result["result"]),
              (0, "ok", "s1", "レビュー本文"))
        check("初回のレビュアー", calls[-1][0][1:3], ["exec", "review"])
        check("読み取り専用", "sandbox_mode=read-only" in calls[-1][0], True)
        check("規約本文の受け渡し", "[レビュー規約の正本]" in calls[-1][1]["input_text"], True)
        code, result = execute(success, resume="s1")
        check("継続の結果", (code, result["status"], result["session_id"]), (0, "ok", "s1"))
        check("継続先", calls[-1][0][1:3] == ["exec", "resume"] and "s1" in calls[-1][0], True)
        check("継続も読み取り専用", "sandbox_mode=read-only" in calls[-1][0], True)
        error = json.dumps({"type": "turn.failed", "error": {"message": "model not found"}})
        code, result = execute(error, code=1)
        check("モデル利用不可で代替しない", (code, result["status"], len(calls)), (5, "unavailable", 3))
        code, result = execute("", code=1, stderr="usage limit reached")
        check("利用上限", (code, result["status"]), (3, "usage_limit"))
        code, result = execute("", resume="missing", code=1, stderr="No conversation found")
        check("再開先なし", (code, result["status"]), (6, "resume_unavailable"))
        code, result = execute(json.dumps({"type": "thread.started", "thread_id": "s2"}))
        check("完了イベントなし", (code, result["status"]), (1, "failed"))

        def fake_timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        output = io.StringIO()
        with patch(__name__ + ".run_review", fake_timeout), patch(__name__ + ".shutil.which", return_value="codex"):
            with contextlib.redirect_stdout(output):
                code = main(["--cwd", str(root), "--prompt-file", str(prompt_file), "--timeout", "1"])
        check("時間上限", (code, json.loads(output.getvalue())["status"]), (4, "timeout"))

    for failure in failures:
        print("FAIL " + failure)
    print("ALL PASS" if not failures else "SOME FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
