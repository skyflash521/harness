#!/usr/bin/env python3
"""自律進行の完了を、宣言でも他モデルの判定でもなく、自分が登録した作業一覧の状態で裏づけさせる。

`guard-idle-stop.py` は宣言の形しか見ないので、**完了が本当かは検査できない**。ここはその穴を埋める。
埋め方は「読んで判定する」ではなく「**自律進行が手順として登録させる `TodoWrite` の中身を見る**」。

見るのは3点。**走行の中で作業一覧が登録されていること**(自律進行は一覧を作る手順を持つので、無いなら
その手順を踏んでいない)、**最新の一覧に終わっていない項目が無いこと**、**その区間に現れた項目が最新の
一覧から消えていないこと**(消して通す経路を閉じる)。区間は、**それまでに現れた項目を残らず抱えた
まま片付いた地点**で切る——そこで走り切っているので、それより前の一覧は別の作業のものである。項目を
終わったことにするのは自己申告のままだが、それは**明示の行為として転写に残る**。

完了で手番が戻るときの音もここが鳴らす。宣言の形を見る側は自分が通したことしか分からず、
こちらが block する場面でも鳴らしてしまうため。

転写を読めない・走行が無い場合は何もせず通す——判定できないことを不許可の理由にすると、何を書いても
抜けられない恒久ブロックになる。

使い方: プラグインルートを第1引数に渡す Stop フックとして登録する。--selftest で自己テスト。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
GUARD = HOOKS / "guard-idle-stop.py"
TRANSCRIPT = ("scripts", "transcript.py")
STOP_DOC = "defect-followthrough.md"
TAG = "[guard-goal-completion]"

SKILL_TOOL = "Skill"
TODO_TOOL = "TodoWrite"
AUTONOMOUS = "autonomous-dev"
STOP_EVENT = "Stop"
DONE_STATUS = "completed"

REASON_NO_TODO = (
    f"作業一覧が登録されていない走行で `{{done}}` を宣言している。自律進行は計画をステップへ割って"
    f"`{TODO_TOOL}` に登録する手順を持つので、一覧が無いということは、計画を割らずに走ったか、"
    "割った結果を残さずに走ったかである。どちらでも、どこまでやれば終わりだったのかが誰にも"
    "確かめられない。"
    f"取るべき行動は、この走行が引き受けた作業を `{TODO_TOOL}` へ登録し、"
    "残っているものを片付けてから宣言し直すこと。"
)
REASON_OPEN = (
    "作業一覧に終わっていない項目が残っている。\n{items}\n"
    "取るべき行動は、名指しされた項目を実際に片付けること。"
    "**割り込みで入った指示が済んだだけなら、その前に受けていた指示へ戻る。**\n"
    "既に済んでいるのに残っているなら、片付いた事実を残したうえで一覧の状態を更新する。\n"
    "進められない事情があるなら、止まってよい場面かどうかを {doc} で確かめる。"
)
REASON_DROPPED = (
    "作業一覧に登録した項目が、最新の一覧から消えている。\n{items}\n"
    "**引き受けた作業は、片付ければ終わった項目として残るのであって、一覧から消えることはない。**"
    "消えたものが在るなら、それは終わっていないのに一覧から外れた作業である。\n"
    f"取るべき行動は、消えた項目を `{TODO_TOOL}` へ戻し、残っているものを片付けてから宣言し直すこと。"
    "その作業がもう要らないと判断したのなら、要らないと判断した理由を報告に残したうえで、"
    "一覧に戻して終わった扱いにする。"
    "名前を書き換えたのなら、書き換えを戻すか、書き換え前の名前も最新の一覧に残す。"
)


def load(name, path):
    """パス指定でモジュールを取り込む。`__main__` ガードが効くので `main()` は走らない。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plugin_root():
    roots = [arg for arg in sys.argv[1:] if arg != "--selftest"]
    return roots[0] if roots else ""


def stop_doc():
    """諮ってよい場面を定める規約の絶対パス。ルートを渡されない起動では名前だけを返す。"""
    root = plugin_root()
    if not root:
        return f"<flow プラグイン同梱の docs/guidance/{STOP_DOC}>"
    return Path(root, "docs", "guidance", STOP_DOC).as_posix()


def tool_calls(rows, name, start=-1):
    """その位置より後の、指定した道具の呼び出しの引数。サブエージェントの手番は数えない。"""
    found = []
    for index, row in enumerate(rows):
        if index <= start or row.get("isSidechain") or row.get("type") != "assistant":
            continue
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("name") == name:
                found.append(block.get("input") if isinstance(block.get("input"), dict) else {})
    return found


def todo_lists(rows, start):
    """走行の中で登録された作業一覧の並び。項目の形をしていないものは落とす。"""
    found = []
    for args in tool_calls(rows, TODO_TOOL, start):
        todos = args.get("todos")
        if isinstance(todos, list):
            found.append([t for t in todos if isinstance(t, dict)])
    return found


def display(todo):
    """項目の表示名。同一性の判定にも使うので、名前が無いものは同じ1件に畳む。"""
    return str(todo.get("content") or todo.get("activeForm") or "(名前の無い項目)")


def unfinished(todos):
    """終わっていない項目の表示名。"""
    return [display(t) for t in todos if t.get("status") != DONE_STATUS]


def segment(lists):
    """最新の一覧が属する区間。**それまでに現れた項目を残らず抱えたまま片付いた地点**より後を返す。
    片付いたことだけを切り口にすると、消した当の一覧が切り口になって、消えた項目が区間の外へ出る。"""
    start, seen = 0, set()
    for index, one in enumerate(lists[:-1]):
        names = {display(t) for t in one}
        seen |= names
        if one and not unfinished(one) and seen <= names:
            start, seen = index + 1, set()
    return lists[start:]


def autonomous_launch(rows):
    """自律進行のスキルを起動した最後の位置。起動が無ければ None。"""
    found = None
    for index, row in enumerate(rows):
        if row.get("isSidechain") or row.get("type") != "assistant":
            continue
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("name") != SKILL_TOOL:
                continue
            skill = (block.get("input") or {}).get("skill")
            if isinstance(skill, str) and skill.split(":")[-1] == AUTONOMOUS:
                found = index
    return found


def rows_of(data):
    """転写の行。読めなければ None——判定できないことを不許可の理由にすると恒久ブロックになる。"""
    reader = load("_transcript", HOOKS.parent / Path(*TRANSCRIPT))
    return reader.rows_of(data.get("transcript_path"))


def verdict(rows):
    """走行の作業一覧から `(block する理由の型, 埋める値)` を返す。通せるなら None。"""
    launch = autonomous_launch(rows)
    if launch is None:
        return None
    lists = todo_lists(rows, launch)
    if not lists:
        return (REASON_NO_TODO, {})
    current = segment(lists)
    latest = current[-1]
    left = unfinished(latest)
    if left:
        return (REASON_OPEN, {"items": listed(left)})
    kept = {display(t) for t in latest}
    dropped = sorted({display(t) for one in current for t in one} - kept)
    if dropped:
        return (REASON_DROPPED, {"items": listed(dropped)})
    return None


def listed(items):
    return "\n".join(f"- {item}" for item in items)


def decide(data):
    """block する理由を返す。通すときは `None`。"""
    guard = load("_guard_idle_stop", GUARD)
    message = data.get("last_assistant_message")
    if not isinstance(message, str):
        return None
    if guard.fold(guard.last_line(message)) != guard.fold(guard.DONE):
        return None
    rows = rows_of(data)
    if rows is None:
        return None
    found = verdict(rows)
    if found is None:
        return None
    return found[0].format(done=guard.DONE, doc=stop_doc(), **found[1])


def main():
    # ハーネスが渡す JSON は UTF-8 で、既定の符号化では復号できずに落ちる。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    reason = decide(data)
    if reason:
        print(json.dumps({"decision": "block", "reason": f"{TAG} {reason}"}))
        return
    announce(data)


def announce(data):
    """完了で手番が実際に戻るときだけ音を鳴らす。"""
    guard = load("_guard_idle_stop", GUARD)
    marker, blocked = guard.decide(data, guard.codex_jobs(data))
    if marker == guard.DONE and blocked is None:
        guard.play_sound()


def selftest():
    import tempfile

    guard = load("_guard_idle_stop", GUARD)
    ok, cases = True, 0

    def check(label, actual, expected):
        nonlocal ok, cases
        cases += 1
        if actual != expected:
            ok = False
            print(f"FAIL {label}: {actual!r} != {expected!r}")

    def call(name, args, ident="t1"):
        return {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "content": [{
                "type": "tool_use", "id": ident, "name": name, "input": args}]}}

    def skill(name=f"flow:{AUTONOMOUS}"):
        return call(SKILL_TOOL, {"skill": name})

    def todo(*items):
        return call(TODO_TOOL, {"todos": [
            {"content": content, "status": status, "activeForm": content} for content, status in items
        ]})

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "transcript.jsonl").as_posix()

        def write(rows):
            Path(path).write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )
            return path

        def stop(message, transcript=path):
            return {"hook_event_name": STOP_EVENT, "last_assistant_message": message,
                    "transcript_path": transcript, "session_id": "S1"}

        done = f"済みました。\n\n{guard.DONE}"
        step1 = ("共通契約の実装とテスト", DONE_STATUS)
        step2 = ("ツール群の実装", "pending")
        step2done = ("ツール群の実装", DONE_STATUS)

        write([skill(), todo(step1, step2)])
        blocked = decide(stop(done))
        check("終わっていない項目が在れば block", blocked is not None, True)
        check("残っている項目を名指しする", "ツール群の実装" in (blocked or ""), True)
        check("宣言が完了でなければ通す", decide(stop(f"待ちます。\n\n{guard.WAIT}")), None)
        check("宣言が無ければ通す", decide(stop("コミットしました。")), None)

        write([skill(), todo(step1, step2), todo(step1, step2done)])
        check("最新の一覧で判定する", decide(stop(done)), None)

        write([skill(), todo(step1, step2done), todo(step1, step2)])
        check("後から差し戻された項目も見る", decide(stop(done)) is not None, True)

        write([skill(), todo(step1, step2), todo(step1)])
        dropped = decide(stop(done))
        check("項目が消えたら block", dropped is not None, True)
        check("消えた項目を名指しする", "ツール群の実装" in (dropped or ""), True)

        other = ("別件の作業", DONE_STATUS)
        write([skill(), todo(step1, step2), todo(step1, other)])
        check("消して別の項目を足しても block", decide(stop(done)) is not None, True)

        write([skill(), todo(step1, step2done), todo(other)])
        check("残らず片付いた後の一覧は別の区間として見る", decide(stop(done)), None)

        write([skill(), todo(step1, step2done), todo(step1, step2), todo(step1)])
        check("片付いた後に始めた作業も消えれば block", decide(stop(done)) is not None, True)

        write([skill(), todo(step1, step2), todo(step1), todo(step1)])
        check("消した一覧は区間の切り口にならない", decide(stop(done)) is not None, True)
        write([skill(), todo(step1, step2), todo(step1), todo(other)])
        check("消した後に別の一覧を出しても block", decide(stop(done)) is not None, True)

        write([skill()])
        check("一覧が無ければ block", decide(stop(done)) is not None, True)
        write([todo(step1, step2)])
        check("自律進行の起動が無ければ通す", decide(stop(done)), None)
        write([todo(step1, step2), skill()])
        check("起動より前の一覧は数えない", decide(stop(done)) is not None, True)

        write([skill(), call(TODO_TOOL, {"todos": "壊れた形"})])
        check("項目の形をしていない引数は一覧に数えない", decide(stop(done)) is not None, True)

        sidechain = todo(step1, step2)
        sidechain["isSidechain"] = True
        write([skill(), sidechain, todo(step1)])
        check("サブエージェントの一覧は数えない", decide(stop(done)), None)

        check("転写が読めなければ通す",
              decide(stop(done, Path(tmp, "no.jsonl").as_posix())), None)

        check("終わった項目は残らない", unfinished([{"content": "a", "status": DONE_STATUS}]), [])
        check("名前の無い項目も挙げる", unfinished([{"status": "pending"}]), ["(名前の無い項目)"])

        write([skill(), todo(step1, step2)])
        cases += 1
        if not _roundtrip_ok(stop(done), "ツール群の実装"):
            ok = False
        cases += 1
        if not _roundtrip_silent_ok(stop("コミットしました。")):
            ok = False

    print("ALL PASS" if ok else "SOME FAILED", f"({cases} cases)")
    sys.exit(0 if ok else 1)


def _run(data):
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        capture_output=True, check=False, timeout=60,
    )


def _roundtrip_ok(data, expected):
    """ハーネスと同じ形(UTF-8 の JSON を標準入力へ)で起動する。判定関数を直接叩く検査は標準入力の
    復号を通らないので、そこが壊れていても合格してしまう。"""
    result = _run(data)
    try:
        out = json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"FAIL stdin roundtrip: 出力が JSON でない: {result.stdout[:200]!r}")
        return False
    if out.get("decision") != "block" or expected not in out.get("reason", ""):
        print(f"FAIL stdin roundtrip: block が出ない: {out!r}")
        return False
    return True


def _roundtrip_silent_ok(data):
    """止める理由が無いときに何も出さないことを、同じ起動の形で確かめる。"""
    result = _run(data)
    if result.returncode != 0 or result.stdout.strip():
        print(f"FAIL stdin roundtrip: 黙って通らない: {result.returncode} {result.stdout[:200]!r}")
        return False
    return True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
