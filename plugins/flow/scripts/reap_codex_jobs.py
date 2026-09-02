#!/usr/bin/env python3
"""codex companion のジョブ記録のうち、進行の実体を失ったまま実行中として残ったものを終局させる。

companion は起動したジョブを状態ディレクトリへ記録し、終局のたびに書き換える。プロセスが記録の
更新を経ずに死ぬと、記録だけが `running` のまま残る。companion はそれを実行中として扱い続けるので、
以後そのスレッドを継ぐ起動が拒否され、新規スレッドへの縮退——前ラウンドの文脈を失った重い
レビュー——が続く。

同梱の `/codex:cancel` はこの残骸を片付けられない。プロセスの不在を taskkill の英語メッセージで
判定するため、日本語表示の Windows では例外に落ちて記録の書き換えまで届かない。

残骸かどうかは pid の生存だけでは決まらない。OS は pid を再利用するので、無関係なプロセスが残骸の
pid を引き継ぐと生存と見える。そこでジョブログが `codex-watchdog` の停滞閾値(watchdog.sh の
`STALL_SECS` 既定と同じ420秒)を超えて更新されていないジョブも終局させる。**プロセスは殺さない**
ので、生きた codex から奪うのは記録の実行中表示だけで、成果でも費用でもない。

**プロセス不在で終局させた回と、停滞で終局させた回は終了コードで区別する。** 後者はプロセスが
生きている可能性があり、companion は終局した記録のスレッドを継続候補にするので、継ぐとそのスレッドで
ターンが二重になりうる。継ぐかどうかを決めるのは呼び出し側で、区別できなければ決められない。

ログが新しいジョブは進行中として触れず、終了コードで知らせる。止めた codex の実行は成果ゼロで費用
だけが残るので、殺す判断はこのスクリプトの外にある。

書き換えは索引(`state.json`)と個別のジョブファイルの両方へ同時に当てる。片方だけを直すと
companion が読む2つの記録が食い違う。**書き換えるのは companion 自身が終局時に書き直す項目だけ**に
限る。それ以外を足すと、生きていたプロセスが後から完了したときに索引へ残り、ジョブファイルとの間に
食い違いを作る(索引は差分の重ね書き、ジョブファイルは全体の書き直しで更新されるため)。終局の理由は
companion が `/codex:cancel` で書くのと同じ形——ジョブログへの追記と、ジョブファイル側だけの
`errorMessage`・`cancelledAt`——で残す。結果の表示はジョブファイルの `errorMessage` へ落ちるので、
これを省くと終局した記録が「結果を保存できなかったジョブ」と読める。

対象はセッションIDが一致するジョブだけで、他のセッションが起こしたジョブには読み取り以外の操作を
一切しない。

使い方: python3 reap_codex_jobs.py [<セッションID>] [--list]
       セッションIDを省くと環境変数 CODEX_COMPANION_SESSION_ID を使う(companion が SessionStart で
       設定する。記録の sessionId と同じ値)。--list は片付けずに一覧だけを出す。
終了コード: 0=このスクリプトが継続を塞ぐものを残していない。プロセス不在の記録だけを終局させた、
             終局させるものが無かった、判定不能なジョブだけが残った、または --list で一覧した回。
             **終局させた記録が在ったかどうかは出力が示す**——0 は継いでよいことまでは意味しない。
           2=セッションIDが決まらない。
           3=進行中のジョブが残っている(触れていない)。4 の意味も兼ねる。
           4=停滞で終局させた記録がある。プロセスが生きている可能性があるのでスレッドを継がない。
"""
import contextlib
import datetime
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

ACTIVE = ("queued", "running")
SESSION_ENV = "CODEX_COMPANION_SESSION_ID"
STALL_SECONDS = 420
REAP_NOTE = "Reaped by flow: the job record stayed active without a live run behind it."


def state_files(roots=None):
    """companion の状態ディレクトリにある索引ファイル。"""
    for root in state_roots() if roots is None else roots:
        yield from Path(root).glob("*/state.json")


def state_roots():
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path(config) if config else Path.home() / ".claude"
    yield from (home / "plugins" / "data").glob("*/state")
    # CLAUDE_PLUGIN_DATA が無いまま起動された companion は状態を一時ディレクトリへ置く。
    yield Path(tempfile.gettempdir(), "codex-companion")


def process_state(pid):
    """`alive` / `gone` / `unknown`。問い合わせる手段が無い環境では `unknown`。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "gone"
    if platform.system() == "Windows":
        # Windows の os.kill はシグナル 0 でも対象を終了させるので問い合わせに使えない。
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, errors="replace", check=False,
            )
        except OSError:
            return "unknown"
        if result.returncode != 0:
            return "unknown"
        return "alive" if f'"{pid}"' in result.stdout else "gone"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"
    return "alive"


def parse_stamp(text):
    if not isinstance(text, str):
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def idle_seconds(entry, now=None):
    """ジョブが進行の跡を残してから経った秒数。読み取れなければ 0。"""
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    log = entry.get("logFile")
    if isinstance(log, str) and log:
        try:
            return max(0.0, moment.timestamp() - Path(log).stat().st_mtime)
        except OSError:
            pass
    for key in ("updatedAt", "startedAt", "createdAt"):
        stamp = parse_stamp(entry.get(key))
        if stamp is not None:
            return max(0.0, (moment - stamp).total_seconds())
    return 0.0


def job_state(entry, now=None):
    """`gone`(プロセス不在) / `stalled`(プロセスは在るがログが停滞) / `live`(進行中) /
    `unknown`(どちらとも決められない)。"""
    state = process_state(entry.get("pid"))
    if state == "gone":
        return "gone"
    if idle_seconds(entry, now) >= STALL_SECONDS:
        return "stalled"
    return "live" if state == "alive" else "unknown"


def active_jobs(session_id, files=None, now=None):
    """指定セッションが起こしたジョブのうち、記録上まだ終局していないもの。"""
    jobs = []
    for state_file in state_files(files):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        for entry in state.get("jobs") or []:
            if not isinstance(entry, dict) or entry.get("sessionId") != session_id:
                continue
            if entry.get("status") not in ACTIVE:
                continue
            jobs.append({
                "id": entry.get("id"),
                "status": entry.get("status"),
                "pid": entry.get("pid"),
                "state": job_state(entry, now),
                "state_file": str(state_file),
                "log_file": entry.get("logFile"),
            })
    return jobs


def stamp(now=None):
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_log(log, note):
    if not isinstance(log, str) or not log:
        return
    try:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(f"[{stamp()}] {note}\n")
    except OSError:
        pass


def reap(job, now=None):
    """ジョブ記録を cancelled として終局させ、書き換えた記録のパスを返す。"""
    patch = {
        "status": "cancelled",
        "phase": "cancelled",
        "pid": None,
        "updatedAt": stamp(now),
        "completedAt": stamp(now),
    }
    state_file = Path(job["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    for entry in state.get("jobs") or []:
        if isinstance(entry, dict) and entry.get("id") == job["id"]:
            entry.update(patch)
    write_json(state_file, state)

    written = [state_file]
    job_file = state_file.parent / "jobs" / f"{job['id']}.json"
    if job_file.is_file():
        stored = json.loads(job_file.read_text(encoding="utf-8"))
        stored.update(patch)
        stored.update({"errorMessage": REAP_NOTE, "cancelledAt": patch["completedAt"]})
        write_json(job_file, stored)
        written.append(job_file)
    append_log(job.get("log_file"), REAP_NOTE)
    return written


def main(argv):
    positional = [arg for arg in argv if not arg.startswith("--")]
    session = positional[0] if positional else os.environ.get(SESSION_ENV, "")
    if len(positional) > 1 or not session:
        print(__doc__.split("使い方:")[1].strip(), file=sys.stderr)
        return 2

    jobs = active_jobs(session)
    grouped = {name: [job for job in jobs if job["state"] == name]
               for name in ("gone", "stalled", "live", "unknown")}
    labels = {"gone": "プロセス不在", "stalled": "停滞"}

    for name in ("gone", "stalled"):
        for job in grouped[name]:
            if "--list" in argv:
                print(f"{labels[name]}: {job['id']} (status={job['status']} pid={job['pid']})")
                continue
            for path in reap(job):
                print(f"{labels[name]}で終局させた: {job['id']} -> {path}")
    for job in grouped["live"]:
        print(f"進行中: {job['id']} (pid={job['pid']}) は触れない", file=sys.stderr)
    for job in grouped["unknown"]:
        print(f"判定不能: {job['id']} (pid={job['pid']}) は触れない", file=sys.stderr)

    if not jobs:
        print(f"セッション {session} が起こした未終局の codex ジョブは無い")
    if "--list" in argv:
        return 0
    if grouped["live"]:
        return 3
    return 4 if grouped["stalled"] else 0


def selftest():
    ok, cases = True, 0

    def check(name, condition):
        nonlocal ok, cases
        cases += 1
        if not condition:
            ok = False
            print(f"FAIL {name}")

    check("自分の pid は alive と判定される", process_state(os.getpid()) == "alive")
    for absent in (None, 0, -1, "1234", True, 1.5):
        check(f"pid として読めない値は gone: {absent!r}", process_state(absent) == "gone")
    ended = subprocess.Popen([sys.executable, "-c", ""])
    ended.wait()
    check("終了したプロセスの pid は gone", process_state(ended.pid) == "gone")

    now = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=datetime.timezone.utc)
    fresh = stamp(now - datetime.timedelta(seconds=STALL_SECONDS - 60))
    idle = stamp(now - datetime.timedelta(seconds=STALL_SECONDS + 60))
    check("進行中のプロセスとログが新しい記録は live",
          job_state({"pid": os.getpid(), "updatedAt": fresh}, now) == "live")
    check("進行中のプロセスでもログが停滞閾値を越えたら stalled",
          job_state({"pid": os.getpid(), "updatedAt": idle}, now) == "stalled")
    check("プロセスが消えていればログが新しくても gone",
          job_state({"pid": ended.pid, "updatedAt": fresh}, now) == "gone")
    check("時刻を読めない記録は進行中のプロセスに従う",
          job_state({"pid": os.getpid(), "updatedAt": "いつか"}, now) == "live")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "root")
        workspace = root / "repo-0123456789abcdef"
        (workspace / "jobs").mkdir(parents=True)
        target = {"id": "task-a", "status": "running", "phase": "running",
                  "pid": 0, "sessionId": "S1", "updatedAt": fresh}
        others = [
            {"id": "task-b", "status": "completed", "phase": "done", "pid": None,
             "sessionId": "S1"},
            {"id": "task-c", "status": "running", "phase": "running", "pid": 0,
             "sessionId": "S2"},
        ]
        write_json(workspace / "state.json", {"version": 1, "jobs": [target] + others})
        write_json(workspace / "jobs" / "task-a.json", dict(target, threadId="T1"))

        jobs = active_jobs("S1", files=[root], now=now)
        check("自セッションの未終局ジョブだけを拾う", [j["id"] for j in jobs] == ["task-a"])
        check("プロセスの無いジョブは gone と判定される", jobs and jobs[0]["state"] == "gone")
        check("他セッションのジョブを拾わない",
              active_jobs("S2", files=[root], now=now)[0]["id"] == "task-c")
        check("記録の無いセッションでは空", active_jobs("S9", files=[root], now=now) == [])

        written = reap(jobs[0])
        check("索引とジョブファイルの両方を書き換える", len(written) == 2)
        index = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
        entries = {entry["id"]: entry for entry in index["jobs"]}
        check("索引の対象が cancelled になる", entries["task-a"]["status"] == "cancelled")
        check("索引の対象の pid が落ちる", entries["task-a"]["pid"] is None)
        check("同セッションの終局済みジョブに触れない", entries["task-b"]["status"] == "completed")
        check("別セッションのジョブに触れない", entries["task-c"]["status"] == "running")
        stored = json.loads((workspace / "jobs" / "task-a.json").read_text(encoding="utf-8"))
        check("ジョブファイルが cancelled になる", stored["status"] == "cancelled")
        check("ジョブファイルの既存項目を保つ", stored.get("threadId") == "T1")
        companion_keys = {"status", "phase", "pid", "completedAt", "updatedAt"}
        check("索引には companion が終局時に書き直す項目しか足さない",
              set(entries["task-a"]) - set(others[0]) - {"sessionId"} <= companion_keys)
        check("ジョブファイルには終局の理由を残す", stored.get("errorMessage") == REAP_NOTE)
        check("ジョブファイルの終局時刻を揃える",
              stored.get("cancelledAt") == stored.get("completedAt"))
        check("索引に終局の理由を残さない", "errorMessage" not in entries["task-a"])
        check("片付けた後は未終局ジョブが残らない", active_jobs("S1", files=[root], now=now) == [])

        broken = root / "broken-0123456789abcdef"
        broken.mkdir()
        (broken / "state.json").write_text("{ not json", encoding="utf-8")
        check("読めない索引は素通りする", active_jobs("S1", files=[root], now=now) == [])

    with contextlib.redirect_stderr(io.StringIO()):
        env = os.environ.pop(SESSION_ENV, None)
        check("セッションIDが決まらなければ使い方を返す", main([]) == 2)
        check("位置引数が2つ以上なら使い方を返す", main(["S1", "S2"]) == 2)
        if env is not None:
            os.environ[SESSION_ENV] = env
    print("ALL PASS" if ok else "SOME FAILED", f"({cases} cases)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(selftest() if "--selftest" in sys.argv else main(sys.argv[1:]))
