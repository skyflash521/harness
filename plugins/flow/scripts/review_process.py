#!/usr/bin/env python3
"""レビュー起動スクリプトが共有するプロセス管理モジュール。"""

import os
import signal
import subprocess
import sys


def run_review(command, *, cwd, input_text, timeout, env=None):
    options = {"start_new_session": True} if sys.platform != "win32" else {}
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, **options)
    try:
        stdout, stderr = process.communicate(input_text.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            try:
                killed = subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                                        capture_output=True, timeout=10, check=False)
                if killed.returncode != 0:
                    process.kill()
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.stdout.close()
            process.stderr.close()
        raise
    return subprocess.CompletedProcess(command, process.returncode,
                                       stdout.decode("utf-8", errors="replace"),
                                       stderr.decode("utf-8", errors="replace"))
