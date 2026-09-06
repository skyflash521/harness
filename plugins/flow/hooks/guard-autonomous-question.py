#!/usr/bin/env python3
"""PreToolUse フック: 自律進行の走行中の `AskUserQuestion` を deny する。

`guard-idle-stop.py` は停止宣言に区分の申告と区分外に当たらないことの確認を課すが、それが掛かるのは
**宣言を書いて手番を返す停止**だけである。`AskUserQuestion` はツールとして結果が返り手番が続くので
Stop フックが発火せず、**同じ「ユーザーが答えるまで進まない」状態をゲート抜きで作れる**。自律進行は
止まらず最後まで進めるという指示で始まった走行なので、そこに開いたこの抜け道を塞ぐ。

塞ぐのは走行中だけ。単発の指示や会話では、聞いたほうが早い場面で聞けることに価値がある。走行中かどうかは
`guard-goal-completion.py` が完了の宣言に当てる判定をそのまま使う——**その判定が通す状態は、完了を
宣言できる状態である**。宣言できる状態に達した後の停止は完了の宣言が受け持つので、ここは通す。

質問する手立てそのものを奪うわけではない。区分に当たる判断は `[停止: 要判断]` で諮れて、そちらは
区分の申告と区分外の確認を受ける。ここが消すのは**その確認を受けない経路**だけである。

転写を読めない場合と、対象でないツールの場合は何も出力せず通す——判定できないことを不許可の理由に
すると、何を書いても抜けられない恒久ブロックになる。

使い方: プラグインルートを第1引数に渡す `AskUserQuestion` の PreToolUse フックとして登録する。
--selftest で自己テスト。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
COMPLETION = HOOKS / "guard-goal-completion.py"
IDLE = HOOKS / "guard-idle-stop.py"

ASK_TOOL = "AskUserQuestion"
TAG = "[guard-autonomous-question]"

REASON = (
    "自律進行の走行中に {tool} でユーザーへ問い返そうとしている。"
    "自律進行は止まらず最後まで進めるという指示で始まった走行であり、その最中の問い返しは、"
    "停止宣言に課された区分の申告と区分外に当たらないことの確認を受けないまま、"
    "ユーザーが答えるまで作業が進まない状態を作る。"
    "止まってよいのは次の4区分だけである。{kinds}{excluded}"
    "取るべき行動は、区分に当たるなら {tool} ではなく {decision} を宣言して止まること"
    "——そちらは区分の申告と区分外の確認を受ける。"
    "当たらないなら止まらずに自分で決めて進み、決めた理由を報告に残すこと。"
)


def load(name, path):
    """パス指定でモジュールを取り込む。`__main__` ガードが効くので `main()` は走らない。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decide(data):
    """deny する理由を返す。対象の起動でなければ None(pass-through)。"""
    if not isinstance(data, dict) or data.get("tool_name") != ASK_TOOL:
        return None
    completion = load("_guard_goal_completion", COMPLETION)
    rows = completion.rows_of(data)
    if rows is None or completion.verdict(rows) is None:
        return None
    idle = load("_guard_idle_stop", IDLE)
    return REASON.format(
        tool=ASK_TOOL,
        kinds=idle.DECISION_KINDS_TEXT,
        excluded=idle.DECISION_EXCLUDED_TEXT.format(doc=idle.stop_doc()),
        decision=idle.DECISION,
    )


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
        "permissionDecisionReason": f"{TAG} {reason}",
    }}))


def selftest():
    import tempfile

    completion = load("_guard_goal_completion", COMPLETION)
    ok, cases = True, 0

    def check(label, actual, expected):
        nonlocal ok, cases
        cases += 1
        if actual != expected:
            ok = False
            print(f"FAIL {label}: {actual!r} != {expected!r}")

    def user(text):
        return {"type": "user", "isSidechain": False,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    def skill(name=f"flow:{completion.AUTONOMOUS}"):
        return {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "content": [{
                "type": "tool_use", "id": "s1", "name": completion.SKILL_TOOL,
                "input": {"skill": name}}]}}

    def todo(*items):
        return {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "content": [{
                "type": "tool_use", "id": "t1", "name": completion.TODO_TOOL,
                "input": {"todos": [
                    {"content": content, "status": status, "activeForm": content}
                    for content, status in items
                ]}}]}}

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "transcript.jsonl").as_posix()
        left = ("ツール群の実装", "pending")
        finished = ("ツール群の実装", completion.DONE_STATUS)

        def write(rows):
            Path(path).write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )
            return path

        def ask(transcript=path, tool=ASK_TOOL):
            return {"hook_event_name": "PreToolUse", "tool_name": tool,
                    "tool_input": {"questions": [{"question": "どちらにする"}]},
                    "transcript_path": transcript, "cwd": tmp, "session_id": "S1"}

        ask_user = user("計画に沿って自律で最後まで進めろ")

        write([ask_user])
        check("自律進行の起動が無ければ通す", decide(ask()), None)

        write([ask_user, skill(), todo(left)])
        blocked = decide(ask())
        check("終わっていない項目が在れば deny", blocked is not None, True)
        check("区分を渡す", "要求仕様" in (blocked or ""), True)
        check("区分外を渡す", "段取り" in (blocked or ""), True)
        check("宣言の方へ誘導する", "[停止: 要判断]" in (blocked or ""), True)

        check("別のツールは通す", decide(ask(tool="Bash")), None)
        check("ツール名が無ければ通す", decide({"transcript_path": path}), None)
        check("辞書でない入力は通す", decide([]), None)
        check("転写が読めなければ通す",
              decide(ask(transcript=Path(tmp, "no.jsonl").as_posix())), None)

        write([ask_user, skill(), todo(finished)])
        check("残らず片付いていれば通す", decide(ask()), None)

        write([ask_user, skill()])
        check("作業一覧が無ければ走行中として deny", decide(ask()) is not None, True)

        cases += 1
        if not _roundtrip_ok(ask()):
            ok = False

    print("ALL PASS" if ok else "SOME FAILED", f"({cases} cases)")
    sys.exit(0 if ok else 1)


def _roundtrip_ok(data):
    """ハーネスと同じ形(UTF-8 の JSON を標準入力へ)で起動して deny を確かめる。判定関数を直接叩く
    検査は標準入力の復号を通らないので、そこが壊れていても合格してしまう。"""
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        capture_output=True, check=False, timeout=60,
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
