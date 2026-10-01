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

from review_process import run_review

DEFAULT_TIMEOUT = 900
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
