#!/usr/bin/env python3
"""セッションの転写を、停止の判定に使える形で読む。

停止を判定するフックが共有する読み取りの正本。転写の行を辞書にして返すことと、**直近のユーザー
発言より後にどの道具を呼んだか**を返すことを引き受ける。

ハーネスが差し込んだ囲み(`<system-reminder>` 等)・文脈が尽きたときの圧縮要約・サブエージェントの
発言は、ユーザーの発言に数えない。前2つはユーザーが書いたものではなく——とくに圧縮要約は作業した
本人が書いたものである——後者はこのセッションの手番ではない。

import して使う。--selftest で自己テスト。
"""
import json
import sys
from pathlib import Path

SKIP_PREFIXES = ("<system-reminder>", "<ide_opened_file>", "<ide_selection>", "<command-",
                 "<local-command-", "<task-notification>", "<cross-session-message",
                 "[Cross-session", "[Request interrupted")


def text_blocks(content):
    """文字列の content とブロック配列の content を同じ形にならす。"""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]


def spoken(blocks):
    """ハーネスが差し込んだ囲みを落として、人が書いた・エージェントが書いた本文だけを返す。"""
    kept = [t.strip() for t in blocks if t and not t.lstrip().startswith(SKIP_PREFIXES)]
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
            rows.append(row)
    return rows


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
