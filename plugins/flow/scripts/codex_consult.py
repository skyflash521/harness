#!/usr/bin/env python3
"""Codex 上の単発相談のエントリポイント。

呼び出し形:
    codex_consult.py --cwd REPO --prompt-file FILE [--write] [--timeout SECS]

status・session_id・result を JSON で返す。
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from codex_review import DEFAULT_TIMEOUT, EXIT_CODES, classify_error, parse_events
from review_process import run_review


def build_command(codex, output_file, write=False):
    sandbox = "workspace-write" if write else "read-only"
    return [codex, "exec", "--json", "-c", f"sandbox_mode={sandbox}",
            "-c", "approval_policy=never", "-o", str(output_file), "-"]


def emit(status, session_id, result):
    print(json.dumps({"status": status, "session_id": session_id, "result": result}))
    return EXIT_CODES[status]


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    codex = shutil.which("codex")
    if codex is None:
        return emit("failed", None, "codex コマンドが見つからない")
    repo = Path(args.cwd).resolve()
    prompt_file = Path(args.prompt_file).resolve()
    if not repo.is_dir() or not prompt_file.is_file() or args.timeout <= 0:
        return emit("failed", None, "作業ディレクトリ・指示文・時間上限を確認できない")
    prompt = prompt_file.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="flow-codex-consult-") as scratch:
        output_file = Path(scratch) / "result.txt"
        command = build_command(codex, output_file, args.write)
        try:
            completed = run_review(command, cwd=repo, input_text=prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return emit("timeout", None, f"{args.timeout}秒以内に終わらなかった")
        except OSError as error:
            return emit("failed", None, str(error))
        session_id, message, finished, errors = parse_events(completed.stdout)
        if completed.returncode != 0 or not finished:
            detail = "\n".join(x for x in (errors, completed.stderr, completed.stdout[-500:]) if x)
            return emit(classify_error(detail), session_id, detail[-1000:])
        if output_file.is_file():
            message = output_file.read_text(encoding="utf-8").strip() or message
        if not message or not session_id:
            return emit("failed", session_id, "完了した相談の本文またはセッション ID が無い")
        return emit("ok", session_id, message)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
