#!/usr/bin/env python3
"""導入検査が CLI の応答を時間上限付きで取得するプロセス管理モジュール。"""

import os
import json
import queue
import signal
import subprocess
import sys
import threading
import time


def stop_process(process):
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

def run_review(command, *, cwd, input_text, timeout, env=None):
    options = {"start_new_session": True} if sys.platform != "win32" else {}
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, **options)
    try:
        stdout, stderr = process.communicate(input_text.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_process(process)
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


def run_rpc(command, *, cwd, requests, timeout):
    """id の無い要求は通知。stdout は結果の JSON 配列文字列。

    切断・不正応答は ValueError、時間上限は TimeoutExpired を送出する。
    """
    options = {"start_new_session": True} if sys.platform != "win32" else {}
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    incoming = queue.Queue()
    errors = []

    def read_stdout():
        for line in process.stdout:
            incoming.put(line)
        incoming.put(None)

    def read_stderr():
        errors.append(process.stderr.read())

    stdout_reader = threading.Thread(target=read_stdout, daemon=True)
    stderr_reader = threading.Thread(target=read_stderr, daemon=True)
    stdout_reader.start()
    stderr_reader.start()
    deadline = time.monotonic() + timeout
    replies = []
    try:
        for request in requests:
            process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            process.stdin.flush()
            if "id" not in request:
                continue
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    line = incoming.get(timeout=remaining)
                except queue.Empty as error:
                    raise subprocess.TimeoutExpired(command, timeout) from error
                if line is None:
                    raise ValueError("応答を返す前に RPC 接続が終了した")
                response = json.loads(line)
                if not isinstance(response, dict):
                    raise ValueError("RPC 応答がオブジェクトでない")
                if response.get("id") == request["id"]:
                    if "error" in response:
                        raise ValueError(f"RPC 要求が失敗した: {response['error']}")
                    if "result" not in response:
                        raise ValueError("RPC 応答に結果が無い")
                    replies.append(response["result"])
                    break
        process.stdin.close()
        process.wait(timeout=max(0.01, min(5, deadline - time.monotonic())))
        stderr_reader.join(timeout=1)
        return subprocess.CompletedProcess(command, process.returncode, json.dumps(replies),
                                           b"".join(errors).decode("utf-8", errors="replace"))
    finally:
        if process.poll() is None:
            stop_process(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if not process.stdin.closed:
            process.stdin.close()


def selftest():
    import tempfile
    import time
    from pathlib import Path

    failures = []
    requests = [{"id": 1, "method": "initialize"}, {"method": "initialized"},
                {"id": 2, "method": "hooks/list"}]
    server = (
        "import json, sys\n"
        "initialized = False\n"
        "for line in sys.stdin:\n"
        " request = json.loads(line)\n"
        " if request['method'] == 'initialized':\n"
        "  initialized = True\n"
        " elif 'id' in request:\n"
        "  sys.stderr.write('x' * 100000); sys.stderr.flush()\n"
        "  print(json.dumps({'method': 'notice'}), flush=True)\n"
        "  print(json.dumps({'id': request['id'], 'result': {'initialized': initialized}}), flush=True)\n"
    )
    result = run_rpc([sys.executable, "-u", "-c", server], cwd=os.getcwd(), requests=requests, timeout=20)
    if result.returncode != 0 or json.loads(result.stdout) != [{"initialized": False}, {"initialized": True}]:
        failures.append("RPC の初期化・通知・応答を順に扱えない")
    if len(result.stderr) != 200000:
        failures.append("RPC の標準エラーを回収できない")
    for server in ("", "print('not json', flush=True)", "print('[]', flush=True)",
                   "print('{\"id\":1,\"error\":{\"message\":\"fixture\"}}', flush=True)",
                   "print('{\"id\":1}', flush=True)"):
        try:
            run_rpc([sys.executable, "-u", "-c", server], cwd=os.getcwd(), requests=requests, timeout=5)
            failures.append("RPC の切断・不正応答を成功扱いする")
        except ValueError:
            pass
    try:
        run_rpc([sys.executable, "-c", "import time; time.sleep(30)"], cwd=os.getcwd(),
                requests=requests, timeout=1)
        failures.append("RPC の時間上限を検出しない")
    except subprocess.TimeoutExpired:
        pass
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
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(selftest() if sys.argv[1:] == ["--selftest"] else 2)
