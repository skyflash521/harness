#!/usr/bin/env python3
"""PreToolUse フック: 結末の出ていない codex のラウンドを `TaskStop` で止めることを deny する。

codex は起動した時点で従量の費用が発生し、途中で止めた回は成果ゼロで費用だけが残る。止めた分は
同じ差分へもう一度払う取り直しになる。それでも停止は、待つより早く手が空くように見えるため、結末を
受け取る前に呼ばれる。

見るのは `codex:codex-rescue` を指す `TaskStop` だけで、watchdog・待機・他のエージェントへの停止は
何も出さずに通す——watchdog を止めるのは規約が要求する後始末で、codex の費用には触れない。

通すのは、そのラウンドの結末を受け取った回(エージェントの完了通知が届いた・watchdog の `OUTCOME`
行を読んだ)と、事由を宣言した回。転写を読めない回・停止先を特定できない回も通す——判定できない
ことを不許可の理由にすると、正当な停止まで塞ぐ。

使い方: プラグインルートを第1引数に渡す `TaskStop` の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

TOOL = "TaskStop"
TARGET = "codex:codex-rescue"
TAG = "[guard-codex-stop]"
TRANSCRIPT = ("scripts", "transcript.py")
WATCHDOG_DOC = ("skills", "codex-watchdog", "SKILL.md")

FIELD = "Codex停止事由"
GROUNDS = ("ハング", "起動失敗", "ユーザー指示")
GROUND_LINE = re.compile(
    r"^\s*[>*_\-\s]*{field}[*_\s]*(?:は)?[*_\s]*[::]\s*[*_`]*\s*({grounds})".format(
        field=FIELD, grounds="|".join(GROUNDS),
    ),
    re.MULTILINE,
)
OUTCOME_LINE = re.compile(r"^OUTCOME=\d", re.MULTILINE)
NOTIFIED_ID = re.compile(r"<task-id>([^<]+)</task-id>")

HOW = (
    "取るべき行動は、(1) 結末を待つ——手番を返すなら wait.py の背景実行で待って `[停止: 待機]`、"
    "(2) watchdog のバックグラウンド出力を読んで `OUTCOME` を確かめる"
    "(出ていればその回は止めてよい)、"
    "(3) それでも止めるなら「{field}: <区分>」の1行を書いてから呼び直す"
    "(区分は {grounds} のいずれか)、のいずれか。"
).format(field=FIELD, grounds="・".join(GROUNDS))

NOT_GROUNDS = (
    "**依頼中に対象を直したのでレビューが陳腐化した・方針を変えたのでやり直したい・もう要らなく"
    "なった、は事由にならない**——受け取った指摘は差分が動いた後でも照合・仕分けに使えるので、"
    "走らせたまま結末を受け取る方が安い。"
)


def plugin_root():
    roots = [arg for arg in sys.argv[1:] if arg != "--selftest"]
    return roots[0] if roots else ""


def watchdog_doc():
    """停止の時機を定める規約の絶対パス。ルートを渡されない起動では名前だけを返す。"""
    root = plugin_root()
    if not root:
        return "<flow プラグイン同梱の " + "/".join(WATCHDOG_DOC) + ">"
    return Path(root, *WATCHDOG_DOC).as_posix()


def reason_text(task_id):
    return (
        "{tag} 結末の出ていない codex のラウンド({task_id})を止めようとしている。"
        "そのラウンドのエージェント完了通知も watchdog の `OUTCOME` 行も、転写にまだ無い。\n"
        "codex は起動した時点で費用が発生しており、結果を受け取る前に止めた回は成果ゼロで費用だけが"
        "残る。止めた分は同じ差分へもう一度払う取り直しになる。\n"
        "{how}\n{not_grounds}\n"
        "停止してよい時機の正本: {doc}"
    ).format(tag=TAG, task_id=task_id, how=HOW, not_grounds=NOT_GROUNDS, doc=watchdog_doc())


def transcript_module():
    """転写の読み取りを持つ側を取り込む。読めなければ None。"""
    try:
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("_transcript", Path(root, *TRANSCRIPT))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def blocks_of(row):
    """その行の内容ブロック。文字列で来る content も1つのブロックとして扱う。"""
    if row.get("isSidechain"):
        return []
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def launches(rows):
    """バックグラウンドで起動したエージェントを `[位置, 種別, 識別子の集合]` で返す。

    識別子は起動時に付けた名前と、起動の結果が返す `agentId` の両方。`TaskStop` はどちらでも
    止められるので、どちらで呼ばれても同じ起動に解決できるようにする。
    """
    pending, found = {}, []
    for index, row in enumerate(rows):
        for block in blocks_of(row):
            if block.get("type") == "tool_use" and block.get("name") == "Agent":
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                name = args.get("name")
                entry = [index, args.get("subagent_type"),
                         {name} if isinstance(name, str) and name else set()]
                pending[block.get("id")] = entry
                found.append(entry)
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                result = row.get("toolUseResult")
                agent_id = result.get("agentId") if isinstance(result, dict) else None
                if isinstance(agent_id, str) and agent_id:
                    pending[block["tool_use_id"]][2].add(agent_id)
    return found


def target_launch(rows, task_id):
    """その `task_id` が指す codex の起動 `(位置, 識別子の集合)`。codex 以外・不明なら None。"""
    found = None
    for index, kind, names in launches(rows):
        if task_id in names:
            found = (index, kind, names)
    if found is None or found[1] != TARGET:
        return None
    return found[0], found[2]


def reported(block, text_blocks):
    """watchdog が終局を告げた出力を読んだ結果か。"""
    return (block.get("type") == "tool_result"
            and any(OUTCOME_LINE.search(str(t)) for t in text_blocks(block.get("content"))))


def notified(row, block, names):
    """そのラウンドのエージェントの完了・失敗通知か。"""
    return (row.get("type") == "user" and block.get("type") == "text"
            and bool(set(NOTIFIED_ID.findall(str(block.get("text", "")))) & names))


def declared(row, block):
    """停止事由の宣言か。"""
    return (row.get("type") == "assistant" and block.get("type") == "text"
            and bool(GROUND_LINE.search(str(block.get("text", "")))))


def settled(rows, start, names, text_blocks):
    """起動より後に、そのラウンドの結末を受け取った記録か、停止事由の宣言があるか。"""
    for row in rows[start + 1:]:
        for block in blocks_of(row):
            if (reported(block, text_blocks) or notified(row, block, names)
                    or declared(row, block)):
                return True
    return False


def decide(data, transcript=None):
    """deny する理由を返す。対象でない・判定できないなら None(pass-through)。"""
    if not isinstance(data, dict) or data.get("tool_name") != TOOL:
        return None
    task_id = (data.get("tool_input") or {}).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    transcript = transcript or transcript_module()
    if transcript is None:
        return None
    rows = transcript.rows_of(data.get("transcript_path"))
    if not rows:
        return None
    target = target_launch(rows, task_id)
    if target is None:
        return None
    start, names = target
    if settled(rows, start, names, transcript.text_blocks):
        return None
    return reason_text(task_id)


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
    import tempfile
    import types

    ok, cases = True, 0
    real = transcript_module()
    if real is None:
        print("FAIL 転写の読み取りの正本を取り込めない")
        sys.exit(1)

    def loaded(rows):
        """転写を読む側を、与えた行を返すものへ差し替える。"""
        return types.SimpleNamespace(rows_of=lambda _path: rows, text_blocks=real.text_blocks)

    def agent_use(ident, kind=TARGET, name=None):
        args = {"subagent_type": kind, "run_in_background": True}
        if name:
            args["name"] = name
        return {"type": "assistant", "isSidechain": False, "message": {"content": [
            {"type": "tool_use", "id": ident, "name": "Agent", "input": args}]}}

    def agent_result(ident, agent_id):
        return {"type": "user", "isSidechain": False,
                "toolUseResult": {"isAsync": True, "agentId": agent_id},
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": ident, "content": "Async agent launched"}]}}

    def notification(task_id, status="completed"):
        return {"type": "user", "isSidechain": False, "message": {"content": [{"type": "text", "text":
                "<task-notification>\n<task-id>{}</task-id>\n<status>{}</status>\n"
                "</task-notification>".format(task_id, status)}]}}

    def read_result(text):
        return {"type": "user", "isSidechain": False, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t9", "content": text}]}}

    def said(text):
        return {"type": "assistant", "isSidechain": False,
                "message": {"content": [{"type": "text", "text": text}]}}

    launch = [agent_use("u1"), agent_result("u1", "a1")]
    named = [agent_use("u2", name="codex-round1"), agent_result("u2", "a2")]
    other = [agent_use("u3", kind="flow:opus-reviewer"), agent_result("u3", "a3")]
    outcome = read_result("LOG=/x/task-a.log\nOUTCOME=0 completed\n")

    def check(label, rows, task_id, want_deny):
        nonlocal ok, cases
        cases += 1
        data = {"tool_name": TOOL, "tool_input": {"task_id": task_id},
                "transcript_path": "<転写>"}
        reason = decide(data, transcript=loaded(rows))
        if want_deny and reason is None:
            ok = False
            print("FAIL deny されない:", label)
        elif not want_deny and reason is not None:
            ok = False
            print("FAIL pass-through にならない:", label, "::", reason[:60])

    check("結末の前に agentId で止める", launch, "a1", True)
    check("結末の前に起動時の名前で止める", named, "codex-round1", True)
    check("完了通知が届いている", launch + [notification("a1")], "a1", False)
    check("失敗通知が届いている", launch + [notification("a1", "failed")], "a1", False)
    check("別のエージェントの通知しか無い", launch + [notification("a9")], "a1", True)
    check("watchdog の OUTCOME を読んでいる", launch + [outcome], "a1", False)
    check("OUTCOME が起動より前にしか無い", [outcome] + launch, "a1", True)
    check("規約文の中の OUTCOME に当たらない",
          launch + [read_result("`OUTCOME=4 bad-arg` で戻る")], "a1", True)
    for ground in GROUNDS:
        check("事由を宣言している: " + ground,
              launch + [said("{}: {}".format(FIELD, ground))], "a1", False)
    check("装飾付きの宣言も読む",
          launch + [said("- **{}**: {} と判断した".format(FIELD, GROUNDS[0]))], "a1", False)
    check("列挙に無い事由は宣言に数えない",
          launch + [said("{}: 陳腐化".format(FIELD))], "a1", True)
    check("宣言が起動より前にしか無い",
          [said("{}: {}".format(FIELD, GROUNDS[0]))] + launch, "a1", True)
    check("codex 以外のエージェントは見ない", other, "a3", False)
    check("watchdog の背景 Bash は見ない", launch, "b0fr1hpkc", False)
    check("同じ名前を継いだ最後の起動で判定する",
          named + [notification("a2")] + [agent_use("u4", name="codex-round1"),
                                          agent_result("u4", "a4")],
          "codex-round1", True)
    check("サブエージェントの手番は起動に数えない",
          [dict(agent_use("u5"), isSidechain=True)], "a5", False)

    for label, data in (
        ("対象外のツール", {"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        ("task_id が無い", {"tool_name": TOOL, "tool_input": {}}),
        ("task_id が文字列でない", {"tool_name": TOOL, "tool_input": {"task_id": 1}}),
        ("tool_input が無い", {"tool_name": TOOL}),
        ("辞書でない入力", []),
    ):
        cases += 1
        if decide(data, transcript=loaded(launch)) is not None:
            ok = False
            print("FAIL pass-through にならない:", label)

    cases += 1
    if decide({"tool_name": TOOL, "tool_input": {"task_id": "a1"},
               "transcript_path": None}) is not None:
        ok = False
        print("FAIL 転写を読めない回を deny した")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "transcript.jsonl")
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in launch)
                        + "\n壊れた行\n", encoding="utf-8")
        cases += 1
        if not _roundtrip_ok(path):
            ok = False

    for phrase in (FIELD, "wait.py", "OUTCOME"):
        cases += 1
        if phrase not in reason_text("a1"):
            ok = False
            print("FAIL deny メッセージに代替が無い:", phrase)

    print("ALL PASS" if ok else "SOME FAILED", "({} cases)".format(cases))
    sys.exit(0 if ok else 1)


def _roundtrip_ok(transcript):
    """ハーネスと同じ形(UTF-8 の JSON を標準入力へ)で起動して deny を確かめる。"""
    payload = json.dumps({"tool_name": TOOL, "tool_input": {"task_id": "a1"},
                          "transcript_path": transcript.as_posix()},
                         ensure_ascii=False).encode("utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=payload, capture_output=True, check=False,
    )
    try:
        out = json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("FAIL stdin roundtrip: 出力が JSON でない:", result.stdout[:200])
        return False
    if (out.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny":
        print("FAIL stdin roundtrip: deny が出ない:", out)
        return False
    return True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
