#!/usr/bin/env python3
"""セッションの転写から、完了の判定に要る材料だけを取り出して印字する。

完了を判定する側が、判定される側の要約を材料にしてはならない。ゴールを取り違えた本人にゴールを
書かせれば、取り違えたまま辻褄が合う。よってここは**転写に残る原文**だけを材料にする——ユーザーの
発言(手番の途中に割り込んだものを含む)、道具の呼び出し、エージェントの発言。

ハーネスが差し込んだ囲み(`<system-reminder>` 等)・文脈が尽きたときの圧縮要約・サブエージェントの
発言は落とす。前2つはユーザーの発言ではなく——とくに圧縮要約は作業した本人が書いたものである——
後者はこのセッションの手番ではない。

使い方: python3 goal_material.py <転写ファイルのパス> [指示一覧の開始番号]
出力: 標準出力に材料。指示が上限に収まらないときは省略した範囲とその取り出し方を出力へ書く。
転写を読めなければ終了コード1。--selftest で自己テスト。
"""
import json
import sys
from pathlib import Path

BUDGET_INSTRUCTIONS = 12000
BUDGET_ACTIONS = 5000
BUDGET_SAYS = 3000
MAX_INSTRUCTION = 1200
MAX_ACTION = 120
MAX_SAY = 400

SKIP_PREFIXES = ("<system-reminder>", "<ide_opened_file>", "<ide_selection>", "<command-",
                 "<local-command-", "<task-notification>", "<cross-session-message",
                 "[Cross-session", "[Request interrupted")
LABEL_KEYS = ("description", "command", "file_path", "pattern", "prompt", "query", "url")


def clip(text, limit, oneline=False):
    """上限で切り詰める。行動ログの1行だけは改行を潰して1行に収める。"""
    text = " ".join(str(text).split()) if oneline else str(text).strip()
    return text if len(text) <= limit else text[:limit] + "…(以下略)"


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


def tool_label(block):
    """行動ログの1行。道具名と、その呼び出しを見分けられる最初の文字列引数を並べる。"""
    args = block.get("input")
    detail = ""
    if isinstance(args, dict):
        for key in LABEL_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                detail = value
                break
    name = str(block.get("name", "?"))
    return clip(f"{name}: {detail}" if detail else name, MAX_ACTION, oneline=True)


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


def read_transcript(path):
    """転写から `(指示, 行動ログ, 発言)` を取り出す。読めなければ空を返す。"""
    instructions, actions, says = [], [], []
    for row in rows_of(path) or []:
        if row.get("isSidechain"):
            continue
        kind = row.get("type")
        if kind == "attachment":
            # 手番の途中で割り込んだユーザー発言は、この型の行に入る。
            attachment = row.get("attachment")
            if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
                continue
            origin = attachment.get("origin")
            if not isinstance(origin, dict) or origin.get("kind") != "human":
                continue
            said = spoken(text_blocks(attachment.get("prompt")))
            if said:
                instructions.append(said)
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if kind == "user":
            # 文脈が尽きたときの継続行は、作業した本人が書いた要約であってユーザーの発言ではない。
            if row.get("isMeta") or row.get("isCompactSummary"):
                continue
            said = spoken(text_blocks(content))
            if said:
                instructions.append(said)
        elif kind == "assistant":
            said = spoken(text_blocks(content))
            if said:
                says.append(said)
            if isinstance(content, list):
                actions.extend(
                    tool_label(b) for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                )
    return instructions, actions, says


def resumed_after(rows, start):
    """その位置より後に新しい指示が来て、さらに道具を使ったか。"""
    asked = False
    for row in rows[start + 1:]:
        if row.get("isSidechain"):
            continue
        kind = row.get("type")
        if kind == "attachment":
            attachment = row.get("attachment")
            origin = (attachment or {}).get("origin")
            if (isinstance(attachment, dict) and attachment.get("type") == "queued_command"
                    and isinstance(origin, dict) and origin.get("kind") == "human"
                    and spoken(text_blocks(attachment.get("prompt")))):
                asked = True
            continue
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        if kind == "user" and not row.get("isMeta"):
            if spoken(text_blocks(content)):
                asked = True
        elif kind == "assistant" and asked:
            if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
                return True
    return False


def newest_within(items, budget):
    """新しい側から予算に収まるだけ返す。落ちた件数を先頭に添える。"""
    kept, used = [], 0
    for item in reversed(items):
        used += len(item.encode("utf-8")) + 1
        if used > budget:
            break
        kept.append(item)
    dropped = len(items) - len(kept)
    return ([f"(古い{dropped}件は長さの上限で省略)"] if dropped else []) + list(reversed(kept))


def both_ends_within(items, budget):
    """古い側と新しい側から交互に詰め、`(前半, 入りきらなかった番号の範囲, 後半)` を返す。"""
    head, tail, used, left, right = [], [], 0, 0, len(items) - 1
    from_head = True
    while left <= right:
        index = left if from_head else right
        used += len(items[index][1].encode("utf-8")) + 1
        if used > budget:
            break
        if from_head:
            head.append(items[index])
            left += 1
        else:
            tail.append(items[index])
            right -= 1
        from_head = not from_head
    gap = (items[left][0], items[right][0]) if left <= right else None
    return [text for _, text in head], gap, [text for _, text in reversed(tail)]


def render(instructions, actions, says, start=1):
    """材料の本文。読む側が受け取れる量に収まるよう節ごとに予算を決め、指示は入りきらないときも
    古い側と新しい側の両端を残す。`start` は指示一覧の開始番号で、省略された範囲の取り直しに使う。"""
    listed = [(n, f"{n}. {clip(text, MAX_INSTRUCTION)}")
              for n, text in enumerate(instructions, 1) if n >= start]
    head, gap, tail = both_ends_within(listed, BUDGET_INSTRUCTIONS)
    middle = [
        f"(指示 {gap[0]}〜{gap[1]} 番は長さの上限で省略。同じスクリプトの第2引数へ {gap[0]} を渡すと"
        "そこから取り出せる。省略が残る間は判定を確定させず、取り出しきってから判定すること)"
    ] if gap else []
    return "\n\n".join([
        "# ユーザーの指示(古い順・原文)\n\n" + ("\n".join(head + middle + tail) or "(無し)"),
        "# 行動ログ(古い順・道具の呼び出し)\n\n"
        + ("\n".join(newest_within(actions, BUDGET_ACTIONS)) or "(無し)"),
        "# エージェントの発言(古い順・直近のみ)\n\n"
        + ("\n---\n".join(newest_within([clip(s, MAX_SAY) for s in says], BUDGET_SAYS)) or "(無し)"),
    ])


def main():
    # UTF-8 を明示する。既定の符号化で印字すると日本語が化けて、読む側が材料を誤る。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    targets = [arg for arg in sys.argv[1:] if arg != "--selftest"]
    if not targets:
        print("転写ファイルのパスを引数に渡すこと。", file=sys.stderr)
        sys.exit(2)
    if rows_of(targets[0]) is None:
        print(f"転写を読めない: {targets[0]}", file=sys.stderr)
        sys.exit(1)
    start = int(targets[1]) if len(targets) > 1 and targets[1].isdigit() else 1
    print(render(*read_transcript(targets[0]), start=max(1, start)))


def selftest():
    import subprocess
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

    def assistant(text=None, tool=None, args=None, sidechain=False):
        content = []
        if text:
            content.append({"type": "text", "text": text})
        if tool:
            content.append({"type": "tool_use", "name": tool, "input": args or {}})
        return {"type": "assistant", "isSidechain": sidechain,
                "message": {"role": "assistant", "content": content}}

    rows = [
        user("<ide_opened_file>/repo/a.md を開いた</ide_opened_file>"),
        user("レビューしてコミットしてプッシュしろ"),
        user("<system-reminder>これは注入</system-reminder>", meta=True),
        user("フックが差し込んだ注記。接頭辞には当たらない。", meta=True),
        summary("This session is being continued. Summary: 1. Primary Request: レビューを回す"),
        assistant("レビューを回します。", tool="Bash",
                  args={"description": "差分を見る", "command": "git diff"}),
        assistant(tool="Read", args={"file_path": "/repo/a.md"}),
        queued("ついでに README も直して"),
        queued("これは機械の割り込み", kind="hook"),
        assistant("サブエージェントの発言", sidechain=True),
        assistant("README を直しました。"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "transcript.jsonl")
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                        encoding="utf-8")
        instructions, actions, says = read_transcript(path.as_posix())
        check("指示の原文だけを拾う", instructions,
              ["レビューしてコミットしてプッシュしろ", "ついでに README も直して"])
        check("行動ログを拾う", actions, ["Bash: 差分を見る", "Read: /repo/a.md"])
        check("サブエージェントの発言を除く", says,
              ["レビューを回します。", "README を直しました。"])
        check("読めない転写は None", rows_of(Path(tmp, "no.jsonl").as_posix()), None)
        check("指示が無ければ空", read_transcript(Path(tmp, "no.jsonl").as_posix()),
              ([], [], []))

        body = render(instructions, actions, says)
        for needle in ("レビューしてコミットしてプッシュしろ", "ついでに README も直して",
                       "Bash: 差分を見る", "README を直しました。"):
            check(f"材料に載る: {needle}", needle in body, True)
        check("機械の割り込みは載せない", "これは機械の割り込み" in body, False)

        many = [user(f"指示{n}: " + "あ" * 3000) for n in range(200)]
        many += [assistant("発言" + "い" * 3000, tool="Bash",
                           args={"command": "cmd" + "u" * 300}) for _ in range(600)]
        big = Path(tmp, "big.jsonl")
        big.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in many) + "\n",
                       encoding="utf-8")
        huge = render(*read_transcript(big.as_posix()))
        check("材料が読む側の受け取れる量に収まる",
              len(huge.encode("utf-8")) <= BUDGET_INSTRUCTIONS + BUDGET_ACTIONS + BUDGET_SAYS + 500,
              True)
        check("最初の指示は落とさない", "1. 指示0:" in huge, True)
        check("最後の指示は落とさない", "200. 指示199:" in huge, True)
        check("入りきらない指示は範囲と取り出し方を示す", "番は長さの上限で省略" in huge, True)
        check("省略の続きを取り出せる", "1. 指示0:" in render(*read_transcript(big.as_posix()),
                                                          start=100), False)
        check("落とした行動ログは件数で示す", "古い" in huge, True)

        cases += 1
        done = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), path.as_posix()],
            capture_output=True, check=False,
        )
        printed = done.stdout.decode("utf-8", errors="replace")
        if done.returncode != 0 or "レビューしてコミットしてプッシュしろ" not in printed:
            ok = False
            print(f"FAIL 起動: {done.returncode} {printed[:200]!r}")

        cases += 1
        missing = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), Path(tmp, "no.jsonl").as_posix()],
            capture_output=True, check=False,
        )
        if missing.returncode != 1:
            ok = False
            print(f"FAIL 読めない転写の終了コード: {missing.returncode}")

    print("ALL PASS" if ok else "SOME FAILED", f"({cases} cases)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
