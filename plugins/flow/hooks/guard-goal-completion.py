#!/usr/bin/env python3
"""Stop フック: 完了判定エージェントの達成判定を伴わない `[停止: 完了]` を block する。

`guard-idle-stop.py` は宣言の形しか見ないので、**完了が本当かは検査できない**。ここはその穴だけを
埋める。埋め方は「フックが判定する」ではなく「**作業した本人とは別のモデルに判定させ、その判定が
在ることをフックが確かめる**」——判定の材料(ユーザーの指示の原文とこのセッションの行動)は転写に
在り、それを読むのは同梱の完了判定エージェントである。

確かめるのは3点。**このセッションの転写パスを渡して完了判定エージェントを起動したこと**(渡して
いなければ、判定は材料でなく呼び出し元の説明を読んだことになる)、**その判定が達成であること**、
**その判定より後に新しい指示を受けて作業していないこと**(していれば判定はその作業を見ていない)。

判定を求めるのは**自律進行のスキルが起動され、その走行がまだ達成と判定されていない間**だけ。
1回の走行につき判定は1回で足りる。単発の指示や会話にまで掛ければ、判定は自明に達成を返し、
サブエージェントの費用だけが残る。走行の途中で受けた質問に答えて手番を返すのは完了の主張ではない
ので、その停止はここを通らない。

判定の実体を持たないので、判定を偽ることはできても**偽った判定は転写に残る**。転写を読めない・
宣言が完了でない場合は何もせず通す——判定できないことを不許可の理由にすると、何を書いても
抜けられない恒久ブロックになる。

完了で手番が戻るときの音もここが鳴らす。宣言の形を見る側は自分が通したことしか分からず、
こちらが block する場面でも鳴らしてしまうため。

使い方: プラグインルートを第1引数に渡す Stop フックとして登録する。--selftest で自己テスト。
"""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
GUARD = HOOKS / "guard-idle-stop.py"
MATERIAL = ("scripts", "goal_material.py")
STOP_DOC = "defect-followthrough.md"
TAG = "[guard-goal-completion]"

AUDITOR = "goal-auditor"
AGENT_TOOL = "Agent"
SKILL_TOOL = "Skill"
AUTONOMOUS = "autonomous-dev"
VERDICT = re.compile(
    r"^\s*[>*_\-\s]*ゴール到達[*_\s]*(?:は)?[*_\s]*[::]?[*_\s]*(達成|未達)[*_\s]*[—\-–:：]?\s*(.*)$"
)

HOW = (
    "取るべき行動は、完了判定エージェント `{auditor}` を Agent ツールで起動し、"
    "その依頼文に**このセッションの転写の絶対パス** `{transcript}` と"
    "**材料取り出しスクリプトの絶対パス** `{material}` を書いて渡すこと。"
    "判定はそのエージェントが材料を読んで出すもので、依頼文に済んだ/済んでいないを書いて"
    "誘導しない。達成が返ってから宣言し直す。"
    "**このセッションに Agent ツールが無く起動できないなら、それは停止規定の発火にあたる**"
    "——判定を省いて完了を宣言せず、要判断で諮る。"
)

REASON_NO_AUDIT = (
    "完了判定を経ていない `[停止: 完了]` である。宣言を書けることは完了の裏付けにならないので、"
    "このハーネスは**作業した本人とは別のモデル**に、ユーザーの指示の原文とこのセッションの行動を"
    "突き合わせさせる。" + HOW
)
REASON_RESUMED = (
    "完了判定の後に新しい指示を受けて作業している。その判定はその作業を見ていないので、"
    "いまの完了の裏付けにならない。**割り込みで入った指示が済んだだけなら、その前に受けていた"
    "指示へ戻る。**" + HOW
)
REASON_NOT_MET = (
    "完了判定エージェントが**未達**と判定した: {detail}\n"
    "取るべき行動は、名指しされた項目を実際に進めること。"
    "**割り込みで入った指示が済んだだけなら、その前に受けていた指示へ戻る。**\n"
    "既に済んでいるのに未達と読まれたのなら、済んだことを示す事実(コミットハッシュ・実行結果・"
    "ファイルの状態)を残してから判定を取り直す。判定を取り直さずに宣言し直しても同じ判定に当たる。\n"
    "進められない事情があるなら、止まってよい場面かどうかを {doc} で確かめる。"
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


def plugin_file(parts, label):
    """同梱ファイルの絶対パス。ルートを渡されない起動では名前だけを返す。"""
    root = plugin_root()
    if not root:
        return f"<flow プラグイン同梱の {label}>"
    return Path(root, *parts).as_posix()


def material_script():
    return plugin_file(MATERIAL, "/".join(MATERIAL))


def stop_doc():
    return plugin_file(("docs", "guidance", STOP_DOC), f"docs/guidance/{STOP_DOC}")


def same_path(text, path):
    """依頼文がそのファイルを指しているか。区切りと大文字小文字の表記揺れを吸収して見る。"""
    if not isinstance(text, str) or not isinstance(path, str) or not path:
        return False
    return path.replace("\\", "/").lower() in text.replace("\\", "/").lower()


def verdict_of(text):
    """応答から `(達成か, 詳細)` を取り出す。申告行が無ければ None。"""
    found = None
    for line in str(text).splitlines():
        matched = VERDICT.match(line)
        if matched:
            found = (matched.group(1) == "達成", matched.group(2).strip())
    return found


def result_text(content):
    """tool_result の中身を文字列にならす。ブロック配列でも文字列でも同じ形にする。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def audits_of(rows, transcript):
    """`(起動の位置, 判定の中身)` の並び。この転写のパスを渡した完了判定エージェントの起動で、
    結果が返っているものだけを数える——パスを渡していない起動は材料を読んでいない。"""
    found, pending = [], {}
    for index, row in enumerate(rows):
        if row.get("isSidechain"):
            continue
        kind = row.get("type")
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if kind == "user" and block.get("type") == "tool_result":
                launched = pending.pop(block.get("tool_use_id"), None)
                if launched is not None:
                    found.append((launched, verdict_of(result_text(block.get("content")))))
            elif kind == "assistant" and block.get("name") == AGENT_TOOL:
                args = block.get("input")
                if not isinstance(args, dict):
                    continue
                subagent = args.get("subagent_type")
                if not isinstance(subagent, str) or subagent.split(":")[-1] != AUDITOR:
                    continue
                if same_path(args.get("prompt"), transcript):
                    pending[block.get("id")] = index
    return sorted(found, key=lambda item: item[0])


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


def decide(data):
    """block する理由を返す。通すときは `None`。"""
    guard = load("_guard_idle_stop", GUARD)
    message = data.get("last_assistant_message")
    if not isinstance(message, str):
        return None
    if guard.fold(guard.last_line(message)) != guard.fold(guard.DONE):
        return None
    transcript = data.get("transcript_path")
    material = load("_goal_material", HOOKS.parent / Path(*MATERIAL))
    rows = material.rows_of(transcript)
    if rows is None:
        return None
    launch = autonomous_launch(rows)
    if launch is None:
        return None
    audits = [a for a in audits_of(rows, transcript) if a[0] > launch]
    how = HOW.format(auditor=AUDITOR, transcript=transcript, material=material_script())
    if not audits:
        return REASON_NO_AUDIT.format(auditor=AUDITOR, transcript=transcript,
                                      material=material_script())
    index, verdict = audits[-1]
    if verdict is None:
        return ("完了判定エージェントの応答に `ゴール到達: 達成` / `ゴール到達: 未達 — <理由>` の"
                "申告行が無い。申告の無い応答は、地の文が済んだと読めても完了の裏付けにならない。"
                + how)
    if not verdict[0]:
        return REASON_NOT_MET.format(detail=verdict[1] or "(理由の記述なし)", doc=stop_doc())
    if material.resumed_after(rows, index):
        return REASON_RESUMED.format(auditor=AUDITOR, transcript=transcript,
                                     material=material_script())
    return None


def main():
    # UTF-8 を明示する。既定の符号化で読むと日本語が化けて、判定も deny 文も壊れる。
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

    def user(text):
        return {"type": "user", "isSidechain": False,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    def queued(text):
        return {"type": "attachment", "isSidechain": False,
                "attachment": {"type": "queued_command", "origin": {"kind": "human"},
                               "prompt": [{"type": "text", "text": text}]}}

    def launch(call_id, prompt, subagent=f"flow:{AUDITOR}", tool=AGENT_TOOL):
        return {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "content": [{
                "type": "tool_use", "id": call_id, "name": tool,
                "input": {"subagent_type": subagent, "prompt": prompt}}]}}

    def skill(name=f"flow:{AUTONOMOUS}"):
        return {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "content": [{
                "type": "tool_use", "id": "s1", "name": SKILL_TOOL, "input": {"skill": name}}]}}

    def edit(path):
        return {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "content": [{
                "type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": path}}]}}

    def result(call_id, text):
        return {"type": "user", "isSidechain": False, "message": {
            "role": "user", "content": [{
                "type": "tool_result", "tool_use_id": call_id,
                "content": [{"type": "text", "text": text}]}]}}

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "transcript.jsonl").as_posix()

        def write(rows):
            Path(path).write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )
            return path

        def stop(message, transcript=path):
            return {"hook_event_name": "Stop", "last_assistant_message": message,
                    "transcript_path": transcript, "session_id": "S1"}

        done = f"済みました。\n\n{guard.DONE}"
        ask = user("レビューしてコミットしてプッシュしろ")
        request = f"転写: {path}\nスクリプト: /p/flow/scripts/goal_material.py"

        write([ask])
        check("自律進行の起動が無ければ通す", decide(stop(done)), None)
        write([ask, edit("a.py"), edit("b.py")])
        check("単発の書き換え指示でも起動が無ければ通す", decide(stop(done)), None)

        def run(*extra):
            return write([ask, skill(), *extra])

        run()
        check("走行の判定が無ければ block", decide(stop(done)) is not None, True)
        check("宣言が完了でなければ通す", decide(stop(f"待ちます。\n\n{guard.WAIT}")), None)
        check("宣言が無ければ通す", decide(stop("コミットしました。")), None)

        run(launch("a1", request), result("a1", "全部済んでいる。\nゴール到達: 達成"))
        check("達成の判定があれば通す", decide(stop(done)), None)
        check("全角の宣言も判定に載せる", decide(stop("済みました。\n\n[停止：完了]")), None)

        run(launch("a1", request), result("a1", "ゴール到達: 達成"),
            queued("これはどうなってる"), user("追加の質問"))
        check("達成の後の会話は判定を求めない", decide(stop(done)), None)

        run(launch("a1", request), result("a1", "ゴール到達: 達成"),
            queued("次の計画も同じように進めろ"), edit("next.py"))
        check("達成の後に指示を受けて作業したら判定を取り直させる",
              decide(stop(done)) is not None, True)

        run(launch("a1", request), result("a1", "ゴール到達: 達成"),
            queued("<task-notification>片付いた</task-notification>"), edit("next.py"))
        check("ハーネスの注入は新しい指示に数えない", decide(stop(done)), None)

        both = {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "content": [
                {"type": "tool_use", "id": "p1", "name": AGENT_TOOL,
                 "input": {"subagent_type": AUDITOR, "prompt": request}},
                {"type": "tool_use", "id": "p2", "name": AGENT_TOOL,
                 "input": {"subagent_type": AUDITOR, "prompt": request}}]}}
        run(both, result("p1", "ゴール到達: 達成"), result("p2", "所見のみ"))
        check("同じ手番で並べて起動しても落ちない", decide(stop(done)) is not None, True)

        run(launch("a1", request), result("a1", "残る。\nゴール到達: 未達 — コミットとプッシュが未了"),
            queued("これはどうなってる"))
        blocked = decide(stop(done))
        check("未達なら会話だけの手番でも block", blocked is not None, True)
        check("未達の理由を渡す", "コミットとプッシュが未了" in (blocked or ""), True)

        run(launch("a1", request), result("a1", "ゴール到達: 未達 — 残り"),
            launch("a2", request), result("a2", "ゴール到達: 達成"))
        check("最後の判定で決める", decide(stop(done)), None)

        write([ask, launch("a1", request), result("a1", "ゴール到達: 達成"), skill()])
        check("起動より前の判定は数えない", decide(stop(done)) is not None, True)

        run(launch("a1", "転写を渡さない依頼文"), result("a1", "ゴール到達: 達成"))
        check("転写を渡さない起動は判定に数えない", decide(stop(done)) is not None, True)

        run(launch("a1", request, subagent="flow:opus-reviewer"), result("a1", "ゴール到達: 達成"))
        check("別のエージェントの応答は判定に数えない", decide(stop(done)) is not None, True)

        run(launch("a1", request), result("a1", "全部済んでいる。"))
        check("申告行が無ければ block", decide(stop(done)) is not None, True)

        run(launch("a1", request))
        check("結果が返っていない起動は判定に数えない", decide(stop(done)) is not None, True)

        check("転写が読めなければ通す",
              decide(stop(done, Path(tmp, "no.jsonl").as_posix())), None)

        check("申告行を読む", verdict_of("ゴール到達: 達成"), (True, ""))
        check("未達の理由を読む",
              verdict_of("**ゴール到達: 未達 — コミットが未了**"), (False, "コミットが未了**"))
        check("申告の無い応答", verdict_of("済んでいると思う"), None)
        check("最後の申告で決める",
              verdict_of("ゴール到達: 未達 — 残り\nゴール到達: 達成"), (True, ""))

        run()
        cases += 1
        if not _roundtrip_ok(stop(done), "完了判定を経ていない"):
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
