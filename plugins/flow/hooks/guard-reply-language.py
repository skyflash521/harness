#!/usr/bin/env python3
"""Stop と PreToolUse のフック。--selftest で自己テスト。"""
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
TRANSCRIPT = HOOKS.parent / "scripts" / "transcript.py"
TAG = "[guard-reply-language]"
SYNTHETIC = "<synthetic>"
CHUNK = 1 << 20

FENCE = re.compile(r"^\s*(```|~~~)")
QUOTE_BLOCK = re.compile(r"^\s*>")
INLINE_CODE = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"\[[^\]\n]*\]\([^)\n]*\)")
QUOTED = re.compile(r"「[^」\n]*」|『[^』\n]*』|“[^”\n]*”|\"[^\"\n]*\"")
WORD = re.compile(r"^[(\[\"'“]*[A-Za-z]+(?:'[A-Za-z]+)?[)\]\"'”,.:;!?、。]*$")
KANA = re.compile(r"[ぁ-ゟ゠-ヿｦ-ﾟ]")
JAPANESE = re.compile(r"[ぁ-ヿ㐀-鿿ｦ-ﾟ]")
WORDS_PER_LINE = 4
SHOWN = 80

HOW = (
    "取るべき行動は、その文を日本語で書き直すこと。日本語を「」で引用しても地の文には数えない。"
    "識別子・コマンド・パスはインラインコードに、外部ツールの出力はコードブロックに入れれば原文の"
    "ままでよい。停止宣言の末尾行はそのまま残す。"
)
REASON_LINE = "ユーザーへ出した文に英文の行がある: {line}\nユーザーへの文は日本語で書く。\n" + HOW
REASON_ALL = (
    "ユーザーへ出した文が英語だけで書かれている(地の文に仮名が1文字も無い)。"
    "ユーザーへの文は日本語で書く。\n" + HOW
)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def protocol_lines():
    guard = load("_guard_idle_stop", HOOKS / "guard-idle-stop.py")
    completion = load("_guard_goal_completion", HOOKS / "guard-goal-completion.py")
    return (
        guard.DECISION_KIND_LINE, guard.DECISION_CONFIRM_LINE, guard.DECISION_SPEC_LINE,
        guard.RESPOND_LINE, completion.SCOPE_LINE, completion.STEP_LINE, completion.SETTLED_LINE,
    ), guard.MARKERS


def prose(message):
    patterns, markers = protocol_lines()
    kept, fenced = [], False
    for line in message.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if (fenced or QUOTE_BLOCK.match(line) or line.strip() in markers
                or any(p.match(line) for p in patterns)):
            continue
        kept.append(LINK.sub(" ", INLINE_CODE.sub(" ", line)))
    return kept


def words(line):
    return sum(1 for token in line.split() if WORD.match(token))


def judge(message):
    """英語の文なら block する理由を、日本語なら None を返す。"""
    lines = prose(message)
    outside = [QUOTED.sub(" ", line) for line in lines]
    for line in outside:
        if not KANA.search(line) and words(line) >= WORDS_PER_LINE:
            return REASON_LINE.format(line=line.strip()[:SHOWN])
    if any(JAPANESE.search(line) for line in outside):
        return None
    if sum(words(line) for line in lines):
        return REASON_ALL
    return None


def reported(row):
    if row.get("type") == "assistant":
        return False
    if row.get("type") == "attachment":
        return TAG in json.dumps(row.get("attachment"), ensure_ascii=False)
    content = (row.get("message") or {}).get("content")
    if row.get("isMeta") and isinstance(content, str):
        return TAG in content
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")
        and TAG in json.dumps(b.get("content"), ensure_ascii=False)
        for b in content
    )


def tail_rows(path, module):
    """直近のユーザー発言より後の転写の行を返す。読めなければ None。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            rows, rest = [], b""
            while end > 0:
                start = max(0, end - CHUNK)
                f.seek(start)
                lines = (f.read(end - start) + rest).split(b"\n")
                rest = lines.pop(0) if start > 0 else b""
                for line in reversed(lines):
                    row = parse(line)
                    if row is None:
                        continue
                    if module.said_by_user(row):
                        return rows[::-1]
                    rows.append(row)
                end = start
            return rows[::-1]
    except (OSError, TypeError, ValueError):
        return None


def parse(line):
    try:
        row = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def unreported(rows, module):
    texts = []
    for row in rows:
        if reported(row):
            texts = []
        elif (row.get("type") == "assistant" and not row.get("isSidechain")
              and (row.get("message") or {}).get("model") != SYNTHETIC):
            texts.extend(module.text_blocks((row.get("message") or {}).get("content")))
    return texts


def decide(data):
    """block する理由を返す。通すなら None。"""
    if data.get("agent_id"):
        return None
    module = load("_transcript", TRANSCRIPT)
    rows = tail_rows(data.get("transcript_path"), module)
    texts = unreported(rows, module) if rows else []
    message = data.get("last_assistant_message")
    if data.get("hook_event_name") == "Stop" and isinstance(message, str):
        texts.append(message)
    for text in texts:
        reason = judge(text)
        if reason:
            return reason
    return None


def main():
    # UTF-8 を明示する。既定の符号化で読むと仮名が化け、日本語の文を英語とみなしてしまう。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    reason = decide(data)
    if not reason:
        return
    reason = f"{TAG} {reason}"
    if data.get("hook_event_name") == "PreToolUse":
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))
    else:
        print(json.dumps({"decision": "block", "reason": reason}))


QUOTING_REPLY = """I've read "垂直方向からサンダルが傾くのはだめだ" (no tilting the sandal away from vertical).
This condition and "合わせるのは踵から指の付け根まで" can't both hold as they stand.

A. Heel only: Turn only about Y and move so the heel seat meets the heel.
Bug (severity 中): model_place_elements can't target vertices by material.
要判断の区分: 指示不明
区分外に当たらないことを確かめた(「垂直方向から傾く」の指す回転が前後か左右か)
[停止: 要判断]"""


def _judge_ok():
    done = "[停止: 完了]"
    cases = [
        ("日本語の応答", f"変更しました。\n\n{done}", False),
        ("英語の応答", f"I have updated the file.\n\n{done}", True),
        ("宣言なしの英語", "Done.", True),
        ("日本語を引用した英語の応答", QUOTING_REPLY, True),
        ("合間の英語の報告", "The sandal fitting isn't finished yet, so I'll keep going.", True),
        ("日本語の中に英文が1行", f"直しました。\nThe hook now checks each line.\n\n{done}", True),
        ("識別子を含む日本語", f"`decide()` を直しました。see README\n\n{done}", False),
        ("裸のパスを並べた行", f"次を変えた。\n- plugins/flow/hooks/hooks.json AGENTS.md README.md x.py\n{done}",
         False),
        ("引数を並べた行", "順に呼んだ。\n2. (parentAll=true, all=true, limit=3) → ok", False),
        ("識別子だけの回答", "model_update_joints\n\n対応済み: 説明に update を含むもの\n\n" + done, False),
        ("リンクだけの行", f"変更点:\n- [hooks.json](plugins/flow/hooks/hooks.json)\n{done}", False),
        ("コードブロックだけが英語", f"結果です。\n```\nALL PASS and all is fine here\n```\n{done}", False),
        ("引用ブロックの英文", f"エラーはこれです。\n> Commit subject must be Japanese\n{done}", False),
        ("コードブロック外が英語", f"Result:\n```\nテスト\n```\n{done}", True),
        ("引用だけで書いた英語", f"“All done.”\n\n{done}", True),
        ("英語の引用だけの行", "引数の説明:\n- \"Present only when the hook fires from a subagent.\"\n", False),
        ("日本語のラベルで引いた英語の出力", "原因: \"No such file or directory.\"", False),
        ("日本語のラベルで示したツールの出力", "接続先: pmx-editor-mcp-15332 / pong", False),
        ("日本語の中の英語の引用", f"エラーは \"Commit subject must be Japanese\" です。\n{done}", False),
        ("インラインコードの仮名", f"Fixed `ひらがな` handling.\n\n{done}", True),
        ("リンクの仮名", f"See [かな](docs/かな.md).\n\n{done}", True),
        ("対応済みの引用", f"Done.\n対応済み: 日本語で書け\n\n{done}", True),
        ("答えた質問の引用", "Yes.\n答えた質問: どうなってる?\n\n[停止: 応答]", True),
        ("句点だけが日本語", f"OK。\n\n{done}", True),
        ("数字と記号だけ", f"123\n\n{done}", False),
        ("宣言だけ", done, False),
    ]
    ok = True
    for label, message, expected in cases:
        if (judge(message) is not None) != expected:
            ok = False
            print(f"FAIL {label}: expected block={expected}")
    return ok, len(cases)


def _transcript_ok():
    def user(text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    def said(text):
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

    def denied():
        return {"type": "user", "message": {"content": [{
            "type": "tool_result", "is_error": True, "content": f"{TAG} {REASON_ALL}"}]}}

    def fed_back():
        return {"type": "user", "isMeta": True, "message": {"content": f"Stop hook feedback:\n{TAG} x"}}

    def synthetic(text):
        return {"type": "assistant", "message": {"model": SYNTHETIC, "content": [
            {"type": "text", "text": text}]}}

    english, japanese = "First I'll put the sandal back.", "まずサンダルを元に戻します。"
    cases = [
        ("合間の英文", [user("直せ"), said(english)], "PreToolUse", None, True),
        ("合間の日本語", [user("直せ"), said(japanese)], "PreToolUse", None, False),
        ("書き出しの遅れた英文", [user("直せ"), said(english), said(japanese)], "PreToolUse", None, True),
        ("指摘済みの英文", [user("直せ"), said(english), denied(), said(japanese)], "PreToolUse", None,
         False),
        ("停止で指摘済みの英文", [user("直せ"), said(english), fed_back()], "Stop", japanese, False),
        ("ユーザー発言より前の英文", [said(english), user("直せ")], "PreToolUse", None, False),
        ("最終応答だけが英語", [user("直せ")], "Stop", english, True),
        ("サブエージェント", [user("直せ"), said(english)], "PreToolUse", None, False),
        ("ハーネスが差し込んだ文", [user("直せ"), synthetic("You've hit your session limit")],
         "PreToolUse", None, False),
    ]
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "transcript.jsonl")
        for label, rows, event, message, expected in cases:
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
            data = {"hook_event_name": event, "transcript_path": path.as_posix()}
            if message is not None:
                data["last_assistant_message"] = message
            if label == "サブエージェント":
                data["agent_id"] = "a1"
            if (decide(data) is not None) != expected:
                ok = False
                print(f"FAIL {label}: expected block={expected}")
        missing = {"hook_event_name": "PreToolUse", "transcript_path": Path(tmp, "none").as_posix()}
        if decide(missing) is not None:
            ok = False
            print("FAIL 転写が読めなければ通す")
    return ok, len(cases) + 1


def _roundtrip_ok():
    ok = True
    for event in ("Stop", "PreToolUse"):
        payload = json.dumps({
            "hook_event_name": event, "last_assistant_message": "All done.",
            "transcript_path": "",
        }, ensure_ascii=False).encode("utf-8")
        if event == "PreToolUse":
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "t.jsonl")
                path.write_text(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "All done here, moving on now."}]}}), encoding="utf-8")
                payload = json.dumps({"hook_event_name": event, "transcript_path": path.as_posix()},
                                     ensure_ascii=False).encode("utf-8")
                result = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                                        input=payload, capture_output=True, check=False)
        else:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                                    input=payload, capture_output=True, check=False)
        try:
            out = json.loads(result.stdout.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            out = {}
        blocked = out.get("decision") == "block" or (
            (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny")
        if not blocked or TAG not in json.dumps(out, ensure_ascii=False):
            ok = False
            print(f"FAIL stdin roundtrip {event}: block が出ない: {result.stdout[:200]!r}")
    return ok, 2


def selftest():
    results = [_judge_ok(), _transcript_ok(), _roundtrip_ok()]
    ok = all(r[0] for r in results)
    print("ALL PASS" if ok else "SOME FAILED", f"({sum(r[1] for r in results)} cases)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
