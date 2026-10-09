#!/usr/bin/env python3
"""フックが import する、セッションの転写の読み取りの共有モジュール。

ハーネスが差し込んだ囲み(`<system-reminder>` 等)・文脈が尽きたときの圧縮要約・サブエージェントの
発言は、ユーザーの発言に数えない。前2つはユーザーが書いたものではなく——とくに圧縮要約は作業した
本人が書いたものである——後者はこのセッションの手番ではない。

import して使う。--selftest で自己テスト。
"""
import json
import sys
from pathlib import Path

HANDBACK = "<agent-message"
SKIP_PREFIXES = ("<system-reminder>", "<ide_opened_file>", "<ide_selection>", "<command-",
                 "<local-command-", "<task-notification>", "<cross-session-message",
                 "[Cross-session", "[Request interrupted", "<hook_prompt",
                 "<external_codex_apps_open_page>",
                 "# AGENTS.md instructions", "<environment_context>", "<user_instructions>")


def text_blocks(content):
    """文字列の content とブロック配列の content を同じ形にならす。"""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]


def spoken(blocks):
    """ハーネスが差し込んだ囲みを落として、人が書いた・エージェントが書いた本文だけを返す。"""
    kept = []
    for text in blocks:
        if not text or text.lstrip().startswith(SKIP_PREFIXES):
            continue
        text = text.strip()
        opening, closing = "<send_user_message_question_reply>", "</send_user_message_question_reply>"
        if text.startswith(opening) and text.endswith(closing):
            try:
                replies = json.loads(text[len(opening):-len(closing)].strip())
            except json.JSONDecodeError:
                replies = None
            if (isinstance(replies, list) and replies
                    and all(isinstance(reply, dict) and isinstance(reply.get("answer"), str) for reply in replies)):
                kept.extend(reply["answer"].strip() for reply in replies)
                continue
        kept.append(text)
    return "\n".join(t for t in kept if t)


def rows_of(path):
    """転写の行を辞書にして返す。読めなければ None。"""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError, ValueError):
        return None
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            normalized = normalize_row(row)
            if normalized is not None:
                rows.append(normalized)
    return rows


def normalize_row(row):
    if row.get("type") == "response_item":
        item = row.get("payload") or {}
        if not isinstance(item, dict):
            return None
        if item.get("type") == "message" and item.get("role") in ("user", "assistant"):
            role = item["role"]
            blocks = item.get("content")
            if not isinstance(blocks, list):
                return None
            content = [{"type": "text", "text": block.get("text", "")}
                       for block in blocks if isinstance(block, dict) and isinstance(block.get("text"), str)
                       and block.get("type") in ("text", "input_text", "output_text")]
            text = "\n".join(block["text"] for block in content)
            hook_feedback = role == "user" and text.lstrip().startswith((
                "[codex-flow]", "[guard-idle-stop]", "[guard-goal-completion]", "[guard-reply-language]",
                "Stop hook feedback",
                '<hook_prompt hook_run_id="stop:',
            ))
            if hook_feedback:
                content = "Stop hook feedback\n" + text
            meta = role == "user" and text.lstrip().startswith((
                "# AGENTS.md instructions", "<environment_context>", "<user_instructions>",
                "<skills_instructions>", "<turn_aborted>", "<summary>", "<hook_prompt",
            ))
            return {"type": role, "isMeta": meta or hook_feedback, "codex": True,
                    "message": {"role": role, "content": content}}
        if item.get("type") in ("function_call", "custom_tool_call"):
            name = item.get("name", "").rsplit(".", 1)[-1]
            try:
                arguments = json.loads(item.get("arguments", "{}"))
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            if "cmd" in arguments:
                arguments["command"] = arguments["cmd"]
            names = {"apply_patch": "Write", "spawn_agent": "Agent", "send_message": "SendMessage",
                     "followup_task": "SendMessage", "request_user_input": "AskUserQuestion",
                     "request_user_input_async": "AskUserQuestion"}
            return {"type": "assistant", "codex": True, "message": {"content": [
                {"type": "tool_use", "name": names.get(name, name), "input": arguments}
            ]}}
        if item.get("type") in ("function_call_output", "custom_tool_call_output"):
            output = item.get("output")
            if isinstance(output, list):
                output = "\n".join(block["text"] for block in output if isinstance(block, dict)
                                   and block.get("type") in ("text", "input_text", "output_text")
                                   and isinstance(block.get("text"), str))
            if isinstance(output, str) and "[guard-reply-language]" in output:
                return {"type": "user", "isMeta": True, "codex": True,
                        "message": {"content": output}}
        return None
    if row.get("type") == "event_msg":
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        item = payload.get("item") or {}
        if not isinstance(item, dict):
            return None
        if payload.get("type") == "item_completed" and item.get("type") == "CommandExecution":
            command = item.get("command") or []
            if not isinstance(command, list) or not command:
                return None
            return {"type": "assistant", "codex": True, "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command[-1]}}
            ]}}
        if payload.get("type") == "item_completed" and item.get("type") in (
            "FileChange", "CollabAgentSpawn", "CollabAgentSendMessage",
        ):
            return {"type": "assistant", "codex": True, "message": {"content": [
                {"type": "tool_use", "name": "Write" if item["type"] == "FileChange" else "Agent", "input": {}}
            ]}}
        return None
    return row


def said_by_user(row):
    """その行がユーザーの発言か。手番の途中で割り込んだものは `attachment` の型で来る。"""
    if row.get("isSidechain"):
        return False
    if row.get("type") == "attachment":
        attachment = row.get("attachment")
        origin = (attachment or {}).get("origin")
        return bool(
            isinstance(attachment, dict) and attachment.get("type") == "queued_command"
            and isinstance(origin, dict) and origin.get("kind") == "human"
            and spoken(text_blocks(attachment.get("prompt")))
        )
    if row.get("type") != "user" or row.get("isMeta") or row.get("isCompactSummary"):
        return False
    return bool(spoken(text_blocks((row.get("message") or {}).get("content"))))


def spoken_of(row):
    """その行でユーザーが書いた本文。割り込みとそれ以外で content の置き場が違う。"""
    content = ((row.get("attachment") or {}).get("prompt")
               if row.get("type") == "attachment"
               else (row.get("message") or {}).get("content"))
    return spoken(text_blocks(content))


def latest_instruction(rows):
    """直近のユーザー発言の本文。発言が1件も無ければ None。"""
    found = None
    for row in rows:
        if said_by_user(row):
            found = spoken_of(row)
    return found


def instructions_after(rows, pattern, wanted):
    """サブエージェントの応答の末尾の宣言が wanted だった後の、ユーザー発言の本文。無ければ空。

    応答はハーネスが `<agent-message …>` を含む囲みに入れて親の転写へ挿す。道具の結果は見ない。
    宣言が複数あるときは末尾のものを採る。"""
    found = None
    for index, row in enumerate(rows):
        if row.get("toolUseResult") is not None or not row.get("isMeta"):
            continue
        text = "\n".join(text_blocks((row.get("message") or {}).get("content")))
        declared = [m.group(1) for m in pattern.finditer(text)]
        if HANDBACK in text and declared and declared[-1].startswith(wanted):
            found = index
    if found is None:
        return []
    return [spoken_of(row) for row in rows[found + 1:] if said_by_user(row)]


def calls_since_last_instruction(rows):
    """直近のユーザー発言より後の道具の呼び出し。発言が1件も無ければ None。"""
    latest = None
    for index, row in enumerate(rows):
        if said_by_user(row):
            latest = index
    if latest is None:
        return None
    found = []
    for row in rows[latest + 1:]:
        if row.get("isSidechain") or row.get("type") != "assistant":
            continue
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        found.extend(b for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
    return found


def blocks_of(row):
    """その行の内容ブロック。文字列で来る content も1つのブロックとして扱う。"""
    if row.get("isSidechain"):
        return []
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def launches(rows):
    """起動したエージェントを `[位置, 種別, 識別子の集合]` で返す。

    識別子は起動時に付けた名前と、起動の結果が返す `agentId` の両方。`TaskStop`・`SendMessage` は
    どちらでも相手を指せるので、どちらで呼ばれても同じ起動に解決できるようにする。
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


def selftest():
    import tempfile

    ok, cases = True, 0

    def check(label, actual, expected):
        nonlocal ok, cases
        cases += 1
        if actual != expected:
            ok = False
            print(f"FAIL {label}: {actual!r} != {expected!r}")

    def user(text, meta=False):
        return {"type": "user", "isSidechain": False, "isMeta": meta,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    def summary(text):
        return {"type": "user", "isSidechain": False, "isCompactSummary": True,
                "message": {"role": "user", "content": text}}

    def queued(text, kind="human"):
        return {"type": "attachment", "isSidechain": False,
                "attachment": {"type": "queued_command", "origin": {"kind": kind},
                               "prompt": [{"type": "text", "text": text}]}}

    def assistant(tool=None, args=None, sidechain=False):
        content = [{"type": "tool_use", "name": tool, "input": args or {}}] if tool else []
        return {"type": "assistant", "isSidechain": sidechain,
                "message": {"role": "assistant", "content": content}}

    check("ハーネスの囲みは発言に数えない",
          said_by_user(user("<system-reminder>これは注入</system-reminder>")), False)
    check("圧縮要約は発言に数えない", said_by_user(summary("Summary: レビューを回す")), False)
    check("割り込みの発言を数える", said_by_user(queued("ついでに README も直して")), True)
    check("機械の割り込みは数えない", said_by_user(queued("片付いた", kind="hook")), False)
    check("素のユーザー発言を数える", said_by_user(user("レビューしろ")), True)
    page = '<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'
    check("画面状態は発言に数えない", said_by_user(user(page)), False)
    check("画面状態と同じ行の別ブロックの指示を残す", spoken([page, "レビューしろ"]), "レビューしろ")
    reply = '<send_user_message_question_reply>' + json.dumps([
        {"question": "質問の文面", "answer": "回答の本文"},
        {"question": "次の質問", "answer": "次の回答"},
    ]) + '</send_user_message_question_reply>'
    check("確認フォームは回答だけを発言に数える", spoken_of(user(reply)), "回答の本文\n次の回答")
    for malformed in ("not json", "[]", '[{"question":"質問だけ"}]', '[{"answer":null}]'):
        text = '<send_user_message_question_reply>' + malformed + '</send_user_message_question_reply>'
        check("読めないフォームで発言を捨てない", spoken_of(user(text)), text)
    for text, expected in ((page, ""), (reply, "回答の本文\n次の回答")):
        row = normalize_row({"type": "response_item", "payload": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": text}]}})
        check("Codex 転写でもユーザー本文を選ぶ", spoken_of(row), expected)

    check("直近の発言の本文を返す",
          latest_instruction([user("古い指示"), assistant(tool="Bash"), user("新しい指示")]),
          "新しい指示")
    check("割り込みの本文も返す",
          latest_instruction([user("古い指示"), queued("あとで直して")]), "あとで直して")
    check("ハーネスの囲みは本文に数えない",
          latest_instruction([user("指示"), user("<system-reminder>注入</system-reminder>")]),
          "指示")
    check("発言が無ければ None", latest_instruction([assistant(tool="Bash")]), None)

    import re

    mark = re.compile(r"^結末[::](\S+)", re.MULTILINE)

    def handback(text):
        row = user('<agent-message from="reviewer">\n' + text)
        row["isMeta"] = True
        return row

    def result(text):
        row = user("道具の結果")
        row["toolUseResult"] = {"prompt": text, "content": text}
        return row

    check("応答より後の発言だけを返す",
          instructions_after([user("先の指示"), handback("結末:千日手"), user("後の指示")],
                             mark, "千日手"),
          ["後の指示"])
    check("応答が無ければ空",
          instructions_after([user("先の指示"), user("後の指示")], mark, "千日手"), [])
    check("発言の本文にある印は起点にならない",
          instructions_after([user("結末:千日手"), user("後の指示")], mark, "千日手"), [])
    check("道具の結果にある印は起点にならない",
          instructions_after([result("結末:千日手"), user("後の指示")], mark, "千日手"), [])
    check("宣言が継続なら起点にならない",
          instructions_after([handback("結末:継続"), user("後の指示")], mark, "千日手"), [])
    check("末尾の宣言を採る",
          instructions_after([handback("結末:千日手\n結末:継続"), user("後の指示")],
                             mark, "千日手"), [])

    rows = [
        user("レビューしろ"),
        assistant(tool="Bash", args={"command": "git diff"}),
        queued("ついでに README も直して"),
        assistant(sidechain=True),
        assistant(tool="Edit", args={"file_path": "/repo/README.md"}),
    ]
    calls = calls_since_last_instruction(rows)
    check("直近の発言より後の呼び出しだけを返す", [c.get("name") for c in calls], ["Edit"])
    check("発言が無ければ None", calls_since_last_instruction([assistant(tool="Bash")]), None)
    check("呼び出しが無ければ空", calls_since_last_instruction([user("読め")]), [])

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "transcript.jsonl")
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n" + "壊れた行\n",
            encoding="utf-8",
        )
        check("転写を読む", len(rows_of(path.as_posix())), len(rows))
        check("壊れた行は落とす", all(isinstance(r, dict) for r in rows_of(path.as_posix())), True)
        check("読めない転写は None", rows_of(Path(tmp, "no.jsonl").as_posix()), None)
        check("パスでないものも None", rows_of(None), None)

    print("ALL PASS" if ok else "SOME FAILED", f"({cases} cases)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
