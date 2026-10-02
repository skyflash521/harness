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
    if argv == ["--selftest"]:
        return selftest()
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


def selftest():
    import contextlib
    import io
    from unittest.mock import patch

    failures = []
    calls = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: want={want!r} got={got!r}")

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        prompt_file = root / "prompt.md"
        prompt = "目的: 起動条件を調査する。\n読み取り専用で回答する。"
        prompt_file.write_text(prompt, encoding="utf-8")
        arguments = ["--cwd", str(root), "--prompt-file", str(prompt_file)]

        def execute(events="", *, file_text=None, code=0, stderr="", error=None,
                    extra=(), which="codex", argv=None):
            def runner(command, **kwargs):
                calls.append((command, kwargs))
                if error is not None:
                    raise error
                if file_text is not None:
                    Path(command[command.index("-o") + 1]).write_text(file_text, encoding="utf-8")
                return subprocess.CompletedProcess(command, code, events, stderr)

            output = io.StringIO()
            with patch(__name__ + ".run_review", runner), patch(__name__ + ".shutil.which", return_value=which):
                with contextlib.redirect_stdout(output):
                    exit_code = main((arguments if argv is None else argv) + list(extra))
            return exit_code, json.loads(output.getvalue())

        started = {"type": "thread.started", "thread_id": "consult-session"}
        message = {"type": "item.completed", "item": {"type": "agent_message", "text": "イベントの回答"}}
        finished = {"type": "turn.completed"}

        def events(*items):
            return "\n".join(json.dumps(item, ensure_ascii=False) for item in items)

        success = events(started, message, finished)
        code, result = execute(success, file_text=" ファイルの回答 \n")
        check("正常終了", (code, result), (0, {"status": "ok", "session_id": "consult-session",
                                           "result": "ファイルの回答"}))
        command, options = calls[-1]
        check("単発相談", command[1:3], ["exec", "--json"])
        check("既定の読み取り専用", "sandbox_mode=read-only" in command, True)
        check("無許可の権限拡大なし", "danger-full-access" in " ".join(command), False)
        check("承認待ちなし", "approval_policy=never" in command, True)
        check("標準入力", command[-1], "-")
        check("指示文と実行環境", options, {"cwd": root.resolve(), "input_text": prompt,
                                         "timeout": DEFAULT_TIMEOUT})
        code, result = execute(success, extra=("--write", "--timeout", "7"))
        check("書き込み指定", "sandbox_mode=workspace-write" in calls[-1][0], True)
        check("指定した時間上限", calls[-1][1]["timeout"], 7)
        check("ファイル未生成時の本文", (code, result["result"]), (0, "イベントの回答"))
        code, result = execute(success, file_text=" \n")
        check("空ファイル時の本文", (code, result["result"]), (0, "イベントの回答"))

        for name, stream, returncode, stderr, want in (
            ("完了なし", events(started, message), 0, "", "failed"),
            ("セッションなし", events(message, finished), 0, "", "failed"),
            ("本文なし", events(started, finished), 0, "", "failed"),
            ("異常終了の部分回答", success, 1, "起動失敗", "failed"),
            ("利用上限", events({"type": "turn.failed", "error": {"message": "usage limit reached"}}),
             1, "", "usage_limit"),
            ("モデル利用不可", "", 1, "model is not available", "unavailable"),
            ("認証失敗", "", 1, "authentication failed", "failed"),
        ):
            count = len(calls)
            code, result = execute(stream, code=returncode, stderr=stderr)
            check(name, (code, result["status"]), (EXIT_CODES[want], want))
            check(name + "で内部再試行なし", len(calls) - count, 1)

        for name, error, want in (
            ("時間切れ", subprocess.TimeoutExpired("codex", DEFAULT_TIMEOUT), "timeout"),
            ("起動失敗", OSError("起動を拒否された"), "failed"),
        ):
            code, result = execute(error=error)
            check(name, (code, result["status"], result["session_id"]), (EXIT_CODES[want], want, None))

        for name, argv, which in (
            ("CLI 不在", arguments, None),
            ("作業ディレクトリ不在", ["--cwd", str(root / "missing"), "--prompt-file", str(prompt_file)], "codex"),
            ("指示文不在", ["--cwd", str(root), "--prompt-file", str(root / "missing.md")], "codex"),
            ("ゼロの時間上限", arguments + ["--timeout", "0"], "codex"),
            ("負の時間上限", arguments + ["--timeout", "-1"], "codex"),
        ):
            count = len(calls)
            code, result = execute(argv=argv, which=which)
            check(name, (code, result["status"], len(calls) - count), (1, "failed", 0))

    for failure in failures:
        print("FAIL " + failure)
    print("ALL PASS" if not failures else "SOME FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
