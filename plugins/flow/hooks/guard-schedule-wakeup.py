#!/usr/bin/env python3
"""PreToolUse フック: `ScheduleWakeup` を deny し、時間待ちを wait.py の背景実行へ誘導する。

禁止の理由と代替は deny メッセージが持つ。ツールを呼んだその場で届く唯一の説明になるため。

使い方: プラグインルートを第1引数に渡す `ScheduleWakeup` の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

TOOL = "ScheduleWakeup"
GUARD = Path(__file__).resolve().parent / "guard-idle-stop.py"


def load_guard():
    """誘導先を持つ側を取り込む。`__main__` ガードが効くので `main()` は走らない。"""
    spec = importlib.util.spec_from_file_location("_guard_idle_stop", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_guard = load_guard()
WAIT_SCRIPT = _guard.WAIT_SCRIPT


REASON = (
    f"[guard-schedule-wakeup] ScheduleWakeup は使えない。待機は {_guard.wait_script()} に"
    "一本化されている。"
    "自分で起動した背景処理(run_in_background の Bash・Agent・Monitor・Workflow)は、終われば"
    "自動で手番が戻る。その完了を待つ起床はポーリングでしかなく、先に発火すれば空振りのターンが"
    "1つ増える。加えて起床の prompt はユーザーのメッセージと同じ経路で戻るので、続行の指示を"
    "書けば自分宛の命令になる。"
    "取るべき行動は、(1) 待つ対象の完了通知で戻る——その上に何も重ねない、"
    "(2) 終了条件を外から観測できるなら、成立した時点で終わる背景コマンドを張る、"
    f"(3) 時間で待つなら {_guard.wait_script()} を run_in_background の Bash で起動する、のいずれか。"
    "実イベントが先に届いたら、張った待機は TaskStop で止める。"
    "CronCreate 等の別経路で同じ起床を張って回避しないこと。"
)


def decide(data):
    """deny する理由を返す。対象のツールでなければ None(pass-through)。"""
    if not isinstance(data, dict) or data.get("tool_name") != TOOL:
        return None
    return REASON


def main():
    # ハーネスが渡す JSON は UTF-8 で、既定の符号化では復号できずに落ちる。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    reason = decide(data)
    if reason is None:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


def selftest():
    ok, cases = True, 0
    deny_inputs = [
        {"tool_name": TOOL, "tool_input": {"delaySeconds": 300, "prompt": "続きを進めろ"}},
        {"tool_name": TOOL, "tool_input": {"stop": True}},
        {"tool_name": TOOL},
    ]
    pass_inputs = [
        {"tool_name": "Bash", "tool_input": {"command": "python3 wait.py 300"}},
        {"tool_name": "TaskStop", "tool_input": {"task_id": "b1"}},
        {"tool_name": "CronCreate", "tool_input": {}},
        {},
        [],
    ]
    for data in deny_inputs:
        cases += 1
        if decide(data) != REASON:
            ok = False
            print(f"FAIL deny されない: {data!r}")
    for data in pass_inputs:
        cases += 1
        if decide(data) is not None:
            ok = False
            print(f"FAIL pass-through にならない: {data!r}")
    for phrase in (WAIT_SCRIPT, "TaskStop", "CronCreate"):
        cases += 1
        if phrase not in REASON:
            ok = False
            print(f"FAIL deny メッセージに代替が無い: {phrase}")
    cases += 1
    if not _roundtrip_ok():
        ok = False
    print("ALL PASS" if ok else "SOME FAILED", f"({cases} cases)")
    sys.exit(0 if ok else 1)


def _roundtrip_ok():
    """ハーネスと同じ形(UTF-8 の JSON を標準入力へ)で起動して deny を確かめる。"""
    payload = json.dumps(
        {"tool_name": TOOL, "tool_input": {"prompt": "日本語の続行指示"}}, ensure_ascii=False,
    ).encode("utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=payload, capture_output=True, check=False,
    )
    try:
        out = json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"FAIL stdin roundtrip: 出力が JSON でない: {result.stdout[:200]!r}")
        return False
    if (out.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny":
        print(f"FAIL stdin roundtrip: deny が出ない: {out!r}")
        return False
    return True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
