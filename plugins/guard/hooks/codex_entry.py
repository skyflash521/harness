"""Codex の Bash の PreToolUse フック guard-rm.py が読み込むモジュール。

Codex はフックの信頼を登録の位置と定義に結び付けて記録し、スクリプトの中身は照合しない。
"""

import io
import runpy
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
BASH_GUARDS = ("guard-time.py", "guard-root-scan.py")


def capture(function, payload):
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    try:
        function()
    except SystemExit:
        pass
    finally:
        output = sys.stdout
        sys.stdin, sys.stdout = stdin, stdout
    output.flush()
    return output.buffer.getvalue().decode("utf-8").strip()


def run_script(script, payload):
    argv = sys.argv
    sys.argv = [str(HOOKS / script), "--codex"]
    try:
        return capture(lambda: runpy.run_path(sys.argv[0], run_name="__main__"), payload)
    finally:
        sys.argv = argv


def run_bash_guards(rm_main):
    payload = sys.stdin.buffer.read()
    outputs = [lambda: capture(rm_main, payload)] + [
        lambda script=script: run_script(script, payload) for script in BASH_GUARDS]
    for output in outputs:
        text = output()
        if text:
            sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
            return
