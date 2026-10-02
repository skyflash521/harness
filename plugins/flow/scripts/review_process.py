#!/usr/bin/env python3
"""導入検査が CLI の応答を時間上限付きで取得するプロセス管理モジュール。"""

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


def selftest():
    import tempfile
    import time
    from pathlib import Path

    failures = []
    with tempfile.TemporaryDirectory() as scratch:
        result = run_review([sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                            cwd=Path(scratch), input_text="日本語", timeout=20)
        if result.returncode != 0 or result.stdout.strip() != "日本語":
            failures.append("標準入力と出力の往復")
        marker = Path(scratch) / "grandchild-survived"
        spawned = Path(scratch) / "grandchild-spawned"
        grandchild = f"import time; from pathlib import Path; time.sleep(8); Path({str(marker)!r}).touch()"
        parent = ("import subprocess, sys, time; from pathlib import Path; "
                  f"subprocess.Popen([sys.executable, '-c', {grandchild!r}], "
                  f"stdout=sys.stdout, stderr=sys.stderr); Path({str(spawned)!r}).touch(); time.sleep(30)")
        start = time.monotonic()
        try:
            run_review([sys.executable, "-c", parent],
                       cwd=Path(scratch), input_text="", timeout=5)
            failures.append("時間上限を検出しない")
        except subprocess.TimeoutExpired:
            if time.monotonic() - start > 30:
                failures.append("時間上限後に復帰しない")
        if not spawned.exists():
            failures.append("孫プロセスを起動する前に時間上限へ達した")
        time.sleep(9)
        if marker.exists():
            failures.append("時間上限後に孫プロセスが残る")
    for failure in failures:
        print("FAIL " + failure)
    print("ALL PASS" if not failures else "SOME FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(selftest() if sys.argv[1:] == ["--selftest"] else 2)
