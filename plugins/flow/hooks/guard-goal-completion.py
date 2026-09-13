#!/usr/bin/env python3
"""自律進行の完了を、宣言でも他モデルの判定でもなく、走行が受け持ったステップの消化で裏づけさせる。

`guard-idle-stop.py` は宣言の形しか見ないので、**完了が本当かは検査できない**。ここはその穴を埋める。
埋め方は「読んで判定する」ではなく「**受け持った範囲と、済ませたステップの番号を突き合わせる**」。

残すのは番号だけで、走行の初めに `着手範囲: <n-m>`(計画書のぶん全部なら `全ステップ`)を1行、以後は
ステップが済むたびに `ステップ完了: <n>` を1行。**一覧も進捗表も書き写させない**——計画書が持って
いるものを二重に持たないためで、1000ステップの計画でも1ステップにつき1行で足りる。

`全ステップ` のときの総数は、起動の引数が指す計画書の実装ステップの表から数える。引数に計画書の
パスが在ることは走行の前提で、無ければ範囲を数えられないので範囲の側で終わりを書かせる。

完了で手番が戻るときの音もここが鳴らす。宣言の形を見る側は自分が通したことしか分からず、
こちらが block する場面でも鳴らしてしまうため。

転写を読めない・走行が無い場合は何もせず通す——判定できないことを不許可の理由にすると、何を書いても
抜けられない恒久ブロックになる。同じ理由で、**弾かれたスキルの起動は在ったことにしない**。

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
TRANSCRIPT = ("scripts", "transcript.py")
STOP_DOC = "defect-followthrough.md"
TAG = "[guard-goal-completion]"

SKILL_TOOL = "Skill"
AUTONOMOUS = "autonomous-dev"
STOP_EVENT = "Stop"

SCOPE_FIELD = "着手範囲"
STEP_FIELD = "ステップ完了"
WHOLE = "全ステップ"
SCOPE_LINE = re.compile(
    rf"^\s*[>*_\-\s]*{SCOPE_FIELD}[*_\s]*(?:は)?[*_\s]*[::]\s*[*_`]*\s*"
    rf"(?:(\d+)\s*[-–~〜]\s*(\d+)|(\d+)|({WHOLE}))"
)
STEP_LINE = re.compile(rf"^\s*[>*_\-\s]*{STEP_FIELD}[*_\s]*(?:は)?[*_\s]*[::]\s*[*_`]*\s*(\d+)")
PLAN_STEPS = re.compile(r"^##+\s*実装ステップ\s*$")
PLAN_ROW = re.compile(r"^\s*\|\s*(\d+)\s*\|")
PATH_SPLIT = re.compile(r"[\s、,。「」『』()（）\[\]`*]+")

HOW = (
    f"走行の初めに「{SCOPE_FIELD}: <n-m>」の1行で受け持つステップを示す"
    f"(計画書のぶんを全部やるなら「{SCOPE_FIELD}: {WHOLE}」)。"
    f"以後はステップが済むたびに「{STEP_FIELD}: <n>」の1行を足すだけでよい"
    "——一覧も進捗表も書き写す必要はない。"
)

REASON_NO_SCOPE = (
    f"受け持つステップが示されていない走行で `{{done}}` を宣言している。"
    "どこからどこまでをやる走行だったのかが決まっていなければ、終わったかどうかも決まらない。\n"
    f"取るべき行動は、この走行が受け持った範囲を示し、済ませたステップを記してから宣言し直すこと。"
    f"{HOW}"
)
REASON_NO_TOTAL = (
    f"`{SCOPE_FIELD}: {WHOLE}` と示されているが、**総数を数える計画書が見つからない**。"
    "起動の引数が指す計画書が実在しないか、その計画書に実装ステップの表が無い。\n"
    f"取るべき行動は、終わりの番号を書いた範囲へ示し直すこと——「{SCOPE_FIELD}: 1-12」の形なら"
    "計画書を読まずに判定できる。"
)
REASON_OPEN = (
    "受け持った範囲のうち、済んだ記録の無いステップが残っている: {items}\n"
    "取るべき行動は、名指しされたステップを実際に片付けること。"
    "**割り込みで入った指示が済んだだけなら、その前に受けていた指示へ戻る。**\n"
    f"既に片付いているのに残っているなら、「{STEP_FIELD}: <n>」の行が抜けている。\n"
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


def stop_doc():
    """諮ってよい場面を定める規約の絶対パス。ルートを渡されない起動では名前だけを返す。"""
    root = plugin_root()
    if not root:
        return f"<flow プラグイン同梱の docs/guidance/{STOP_DOC}>"
    return Path(root, "docs", "guidance", STOP_DOC).as_posix()


def rejected_ids(rows):
    """結果がエラーで返った呼び出しの id。"""
    found = set()
    for row in rows:
        if row.get("isSidechain"):
            continue
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("is_error") and block.get("tool_use_id"):
                found.add(block["tool_use_id"])
    return found


def launch_of(rows):
    """自律進行の最後の起動の `(位置, 引数)`。起動が無い・エラーで返った起動しか無ければ None
    ——弾かれた起動は走行を始めていないので、走行の中でしか掛からない判定をそこから始めない。"""
    failed = rejected_ids(rows)
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
            if block.get("id") in failed:
                continue
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            skill = args.get("skill")
            if isinstance(skill, str) and skill.split(":")[-1] == AUTONOMOUS:
                found = (index, str(args.get("args") or ""))
    return found


def spoken_after(rows, start):
    """走行の中でエージェントが書いた本文。サブエージェントの手番は数えない。"""
    for index, row in enumerate(rows):
        if index <= start or row.get("isSidechain") or row.get("type") != "assistant":
            continue
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                yield str(block.get("text", ""))


def scope_of(text):
    """その発言が示した着手範囲。`(始まり, 終わり)`、計画書のぶん全部なら `(始まり, None)`。"""
    for line in str(text).splitlines():
        matched = SCOPE_LINE.match(line)
        if not matched:
            continue
        first, last, single, whole = matched.groups()
        if whole:
            return (1, None)
        if single:
            return (int(single), int(single))
        return (int(first), int(last))
    return None


def steps_of(text):
    """その発言が済んだと記したステップの番号。"""
    return {int(m.group(1)) for m in
            (STEP_LINE.match(line) for line in str(text).splitlines()) if m}


def counted(body):
    """その計画書の実装ステップの総数。**1からの連番でなければ None**——部分的に数えた値を
    信用すると、数え落とした残りが未了のまま通る。"""
    numbers, depth = [], None
    for line in body.splitlines():
        head = len(line) - len(line.lstrip("#"))
        if PLAN_STEPS.match(line):
            depth = head
            continue
        if depth is None:
            continue
        if head and head <= depth:
            break
        matched = PLAN_ROW.match(line)
        if matched:
            numbers.append(int(matched.group(1)))
    return len(numbers) if numbers and sorted(numbers) == list(range(1, len(numbers) + 1)) else None


def plan_total(args, cwd):
    """起動の引数が指す計画書の実装ステップの総数。数えられなければ None。"""
    base = Path(cwd) if cwd else Path.cwd()
    for token in PATH_SPLIT.split(str(args)):
        if not token or token in (".", ".."):
            continue
        for candidate in (Path(token), base / token):
            try:
                if not candidate.is_file():
                    continue
                body = candidate.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue
            total = counted(body)
            if total:
                return total
    return None


def verdict(rows, cwd=None):
    """走行のステップの消化から `(block する理由の型, 埋める値)` を返す。通せるなら None。"""
    launch = launch_of(rows)
    if launch is None:
        return None
    start, args = launch
    scopes, done = [], set()
    for text in spoken_after(rows, start):
        found = scope_of(text)
        if found:
            scopes.append(found)
        done |= steps_of(text)
    if not scopes:
        return (REASON_NO_SCOPE, {})
    first = min(one[0] for one in scopes)
    ends = [one[1] for one in scopes if one[1] is not None]
    last = max(ends) if ends else None
    if any(one[1] is None for one in scopes):
        total = plan_total(args, cwd)
        if total is None and last is None:
            return (REASON_NO_TOTAL, {})
        if total is not None:
            last = max(last or 0, total)
    left = [n for n in range(first, last + 1) if n not in done]
    if left:
        return (REASON_OPEN, {"items": "・".join(str(n) for n in left)})
    return None


def rows_of(data):
    """転写の行。読めなければ None——判定できないことを不許可の理由にすると恒久ブロックになる。"""
    reader = load("_transcript", HOOKS.parent / Path(*TRANSCRIPT))
    return reader.rows_of(data.get("transcript_path"))


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
    found = verdict(rows, data.get("cwd"))
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

    def said(text):
        return {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}]}}

    def skill(args="plan.md に基づいて自律進行", ident="s1"):
        return {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "content": [{
                "type": "tool_use", "id": ident, "name": SKILL_TOOL,
                "input": {"skill": f"flow:{AUTONOMOUS}", "args": args}}]}}

    def failed(ident):
        return {"type": "user", "isSidechain": False, "message": {
            "role": "user", "content": [{
                "type": "tool_result", "tool_use_id": ident, "is_error": True,
                "content": "deny の理由"}]}}

    check("範囲の幅を読む", scope_of(f"{SCOPE_FIELD}: 3-7"), (3, 7))
    check("単独のステップを読む", scope_of(f"{SCOPE_FIELD}: 4"), (4, 4))
    check("全体の指定を読む", scope_of(f"{SCOPE_FIELD}: {WHOLE}"), (1, None))
    check("装飾を付けた範囲も読む", scope_of(f"**{SCOPE_FIELD}**: `1-3`"), (1, 3))
    check("波ダッシュの幅も読む", scope_of(f"{SCOPE_FIELD}: 1〜3"), (1, 3))
    check("範囲の行が無ければ None", scope_of("着手します"), None)
    check("済んだ番号を読む", steps_of(f"{STEP_FIELD}: 3"), {3})
    check("装飾を付けた完了も読む", steps_of(f"- **{STEP_FIELD}**: 12"), {12})
    check("番号でない完了は拾わない", steps_of(f"{STEP_FIELD}: 全部"), set())

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "transcript.jsonl").as_posix()
        Path(tmp, "plan.md").write_text(
            "# 計画\n\n## 実装ステップ\n\n| # | 内容 |\n|---|---|\n| 1 | あ |\n| 2 | い |\n"
            "\n## 完了条件\n\n| 1 | これは数えない |\n", encoding="utf-8")
        check("計画書の表を数える", plan_total("plan.md に基づいて自律進行", tmp), 2)
        check("実在しない計画書は None", plan_total("no-such.md", tmp), None)
        check("小見出しを挟んでも数え続ける",
              counted("## 実装ステップ\n| 1 | あ |\n### 補足\n| 2 | い |\n"), 2)
        check("同位の見出しで打ち切る",
              counted("## 実装ステップ\n| 1 | あ |\n## 別の節\n| 2 | い |\n"), 1)
        check("飛び番は数えられない扱い",
              counted("## 実装ステップ\n| 1 | あ |\n| 3 | う |\n"), None)
        check("1から始まらない表も数えない",
              counted("## 実装ステップ\n| 2 | い |\n| 3 | う |\n"), None)

        def write(rows):
            Path(path).write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )
            return path

        def stop(message, transcript=path):
            return {"hook_event_name": STOP_EVENT, "last_assistant_message": message,
                    "transcript_path": transcript, "cwd": tmp, "session_id": "S1"}

        done = f"済みました。\n\n{guard.DONE}"
        scope13 = said(f"着手します。\n\n{SCOPE_FIELD}: 1-3")

        write([skill(), scope13, said(f"{STEP_FIELD}: 1"), said(f"{STEP_FIELD}: 2")])
        blocked = decide(stop(done))
        check("済んでいないステップが残れば block", blocked is not None, True)
        check("残っている番号を名指しする", "3" in (blocked or ""), True)
        check("宣言が完了でなければ通す", decide(stop(f"待ちます。\n\n{guard.WAIT}")), None)
        check("宣言が無ければ通す", decide(stop("コミットしました。")), None)

        write([skill(), scope13, said(f"{STEP_FIELD}: 1"), said(f"{STEP_FIELD}: 2"),
               said(f"{STEP_FIELD}: 3")])
        check("範囲が埋まれば通す", decide(stop(done)), None)

        write([skill(), said(f"{SCOPE_FIELD}: 2"), said(f"{STEP_FIELD}: 2")])
        check("単独のステップの走行も通る", decide(stop(done)), None)
        write([skill(), said(f"{SCOPE_FIELD}: 2"), said(f"{STEP_FIELD}: 1")])
        check("範囲の外の番号では埋まらない", decide(stop(done)) is not None, True)

        write([skill(), said(f"{SCOPE_FIELD}: {WHOLE}"), said(f"{STEP_FIELD}: 1")])
        check("全体の指定は計画書の総数で判定する", decide(stop(done)) is not None, True)
        write([skill(), said(f"{SCOPE_FIELD}: {WHOLE}"), said(f"{STEP_FIELD}: 1"),
               said(f"{STEP_FIELD}: 2")])
        check("総数まで埋まれば通す", decide(stop(done)), None)
        write([skill("no-such.md に基づいて自律進行"), said(f"{SCOPE_FIELD}: {WHOLE}")])
        total = decide(stop(done))
        check("総数を数えられなければ範囲の書き直しを求める",
              total is not None and "1-12" in total, True)

        write([skill(), scope13, said(f"{SCOPE_FIELD}: 1-2"), said(f"{STEP_FIELD}: 1"),
               said(f"{STEP_FIELD}: 2")])
        check("範囲を縮めても通らない", decide(stop(done)) is not None, True)
        write([skill(), scope13, said(f"{SCOPE_FIELD}: 1-5")]
              + [said(f"{STEP_FIELD}: {n}") for n in range(1, 6)])
        check("範囲を広げる向きは採る", decide(stop(done)), None)
        write([skill("no-such.md に基づいて自律進行"), said(f"{SCOPE_FIELD}: {WHOLE}"),
               said(f"{SCOPE_FIELD}: 1-2"), said(f"{STEP_FIELD}: 1"), said(f"{STEP_FIELD}: 2")])
        check("総数を数えられないときは書き直した範囲で判定する", decide(stop(done)), None)

        write([skill()])
        check("範囲が示されていなければ block", decide(stop(done)) is not None, True)
        check("書き方を渡す", SCOPE_FIELD in (decide(stop(done)) or ""), True)
        write([scope13, said(f"{STEP_FIELD}: 1")])
        check("自律進行の起動が無ければ通す", decide(stop(done)), None)
        write([scope13, said(f"{STEP_FIELD}: 1"), skill()])
        check("起動より前の記録は数えない", decide(stop(done)) is not None, True)

        write([skill("plan.md", "s9"), failed("s9")])
        check("弾かれた起動は走行を始めていない", decide(stop(done)), None)

        side = said(f"{STEP_FIELD}: 3")
        side["isSidechain"] = True
        write([skill(), scope13, said(f"{STEP_FIELD}: 1"), said(f"{STEP_FIELD}: 2"), side])
        check("サブエージェントの記録は数えない", decide(stop(done)) is not None, True)

        check("転写が読めなければ通す",
              decide(stop(done, Path(tmp, "no.jsonl").as_posix())), None)

        write([skill(), scope13, said(f"{STEP_FIELD}: 1")])
        cases += 1
        if not _roundtrip_ok(stop(done), "2・3"):
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
