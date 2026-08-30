#!/usr/bin/env python3
"""Stop hook: 停止を既定で禁じ、末尾行が停止宣言のものだけを許可する。

宣言は3種。`待機` は `background_tasks` か `session_crons` が在るときだけ通す——手番が戻る経路の
無いまま止まるのを防ぐ。`完了` と `要判断` は音を鳴らす。

判定は末尾行の等値比較。応答本文を渡さないハーネスでは判定せず通す——判定できないことを不許可の
理由にすると、何を書いても抜けられない恒久ブロックになる。

Usage: configured as a Stop hook. Run with --selftest.
"""
import json
import platform
import subprocess
import sys
from pathlib import Path

DONE = "[停止: 完了]"
DECISION = "[停止: 要判断]"
WAIT = "[停止: 待機]"
MARKERS = (DONE, DECISION, WAIT)
HANDOVER = (DONE, DECISION)

# 等値比較の前に畳む表記の揺れ。前後に添えた文や囲み記号は畳まない。
FOLD = {"：": ":", "［": "[", "］": "]", " ": "", "　": "", "\t": ""}

TAG = "[guard-idle-stop]"

# 音の再生には OS の音声機能を使う。端末のベル(BEL)は鳴ったかどうかをフックの側から確かめられない。
SOUND_COMMANDS = {
    "Windows": [
        sys.executable, "-c",
        "import winsound; [winsound.Beep(f, 130) for f in (880, 1175, 1568)]",
    ],
    "Darwin": ["afplay", "/System/Library/Sounds/Glass.aiff"],
    "Linux": ["canberra-gtk-play", "--id=dialog-question"],
}

_HOW = (
    "停止するには、応答の**末尾行を停止宣言だけ**にすること。前後に文や囲み記号を付けない。"
    f"{DONE} — 依頼された作業が終わり、手番を返す。"
    f"{DECISION} — ユーザーの判断が要り、それ無しでは進めない。"
    "何を選ぶのかを確定的に書いたうえで付ける。"
    f"{WAIT} — 何かの完了を待つ。手番が戻る経路として、"
    "登録された背景処理か予約済みの起床のどちらかが在るときだけ使える"
    "(完了通知は取りこぼされうるので、確実にするなら起床を併せて張る)。"
)

REASON_NO_MARKER = (
    "末尾行が停止宣言になっていない。このハーネスは停止を既定で禁じており、"
    "宣言の無い停止は、作業の途中で手番を返したものとして扱う。"
    "止まらずに作業を続けるか、止まる理由を宣言すること。"
    "宣言を文中で言及しただけ・囲み記号で包んだだけでは許可されない。" + _HOW
)
REASON_WAIT_UNSUBSTANTIATED = (
    f"{WAIT} と宣言しているが、登録された背景処理も予約済みの起床も無い。"
    "このまま止まると再開する手立てが無く、ユーザーが促すまで止まり続けることになる。"
    "取るべき行動は、待つ対象を実際に起動するか、自分の側から発火できる起床を張るか、"
    "待たずにその作業を自分で済ませること。"
    f"作業が終わっているなら {DONE}、ユーザーの判断が要るなら {DECISION} を使う。"
)
REASON_MULTIPLE = (
    "末尾行に停止宣言が複数ある。どの理由で止まるのかが決まらない。1つだけにすること。" + _HOW
)


def fold(text):
    for src, dst in FOLD.items():
        text = text.replace(src, dst)
    return text


def last_line(message):
    for line in reversed(message.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def play_sound():
    """起動しっぱなしにして待たない。鳴らせない環境では黙って諦める。"""
    command = SOUND_COMMANDS.get(platform.system())
    if not command:
        return
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def decide(data):
    """`(通した宣言, block する理由)` を返す。両方 None なら判定しない入力。"""
    message = data.get("last_assistant_message")
    if message is None:
        return None, None
    line = fold(last_line(message))
    found = [m for m in MARKERS if fold(m) in line]
    if len(found) > 1:
        return None, REASON_MULTIPLE
    if len(found) != 1 or line != fold(found[0]):
        return None, REASON_NO_MARKER
    if found[0] == WAIT:
        if not (data.get("background_tasks") or data.get("session_crons")):
            return None, REASON_WAIT_UNSUBSTANTIATED
    return found[0], None


def main():
    # UTF-8 を明示する。既定の符号化で読むと宣言が化けて一致せず、全ての停止をブロックし続ける。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    marker, reason = decide(data)
    if reason:
        print(json.dumps({"decision": "block", "reason": f"{TAG} {reason}"}))
        return
    if marker in HANDOVER:
        play_sound()


def selftest():
    def stop(message, tasks=(), crons=(), active=False):
        return {
            "hook_event_name": "Stop",
            "stop_hook_active": active,
            "last_assistant_message": message,
            "background_tasks": list(tasks),
            "session_crons": list(crons),
        }

    task = {"id": "b1", "type": "shell", "status": "running", "description": "検査"}
    cron = {"id": "c1"}
    block_cases = [
        (stop("コミットしました。ハッシュは 90d8326 です。"), REASON_NO_MARKER),
        (stop("次はレビューを回します。"), REASON_NO_MARKER),
        (stop("お任せいただけるなら、このまま実装します。"), REASON_NO_MARKER),
        (stop("どちらで進めますか。1. フックを作る 2. 文書だけにする"), REASON_NO_MARKER),
        (stop(""), REASON_NO_MARKER),
        # 宣言が末尾行でない
        (stop("[停止: 完了]\n\n続けて別の作業もあります。"), REASON_NO_MARKER),
        # 宣言に文を添えた・囲み記号で包んだ末尾行(等値でない)
        (stop("末尾に [停止: 完了] と書く決まりにしました。"), REASON_NO_MARKER),
        (stop("作業は終わりました。\n\n`[停止: 完了]`"), REASON_NO_MARKER),
        (stop("作業は終わりました。\n\n[停止: 完了] 以上です。"), REASON_NO_MARKER),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]"), REASON_WAIT_UNSUBSTANTIATED),
        (stop("作業は終わりました。\n\n[停止: 完了] [停止: 要判断]"), REASON_MULTIPLE),
        # 一度ブロックした後も素通ししない
        (stop("これからフックを書きます。", active=True), REASON_NO_MARKER),
    ]
    pass_cases = [
        (stop("コミットしました。ハッシュは 90d8326 です。\n\n[停止: 完了]"), "[停止: 完了]"),
        (stop("どちらで進めますか。1. フックを作る 2. 文書だけにする\n\n[停止: 要判断]"),
         "[停止: 要判断]"),
        # 表記の揺れ
        (stop("作業は終わりました。\n\n[停止：完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止:完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n［停止：完了］"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止：　完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止:\t完了]"), "[停止: 完了]"),
        # 本文を渡さないハーネス
        ({"hook_event_name": "Stop", "last_assistant_message": None}, None),
        ({"hook_event_name": "Stop", "stop_hook_active": False}, None),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[task]), "[停止: 待機]"),
        (stop("外部の CI の完了を待ちます。\n\n[停止: 待機]", crons=[cron]), "[停止: 待機]"),
        (stop("作業は終わりました。\n\n[停止: 完了]", active=True), "[停止: 完了]"),
    ]
    ok = True
    for data, expected in block_cases:
        actual = decide(data)
        if actual != (None, expected):
            ok = False
            print(f"FAIL expected block: {data.get('last_assistant_message')!r} -> {actual!r}")
    for data, expected in pass_cases:
        actual = decide(data)
        if actual != (expected, None):
            ok = False
            print(f"FAIL expected pass: {data.get('last_assistant_message')!r} -> {actual!r}")
    if not _roundtrip_ok(block_cases[0][0], block_cases[0][1]):
        ok = False
    total = len(block_cases) + len(pass_cases) + 1
    print("ALL PASS" if ok else "SOME FAILED", f"({total} cases)")
    sys.exit(0 if ok else 1)


def _roundtrip_ok(data, expected):
    """ハーネスと同じ形(UTF-8 の JSON を標準入力へ)で起動する。判定関数を直接叩く検査は標準入力の
    復号を通らないので、そこが壊れていても合格してしまう。"""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=payload, capture_output=True, check=False,
    )
    try:
        out = json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"FAIL stdin roundtrip: 出力が JSON でない: {result.stdout[:200]!r}")
        return False
    if out.get("decision") != "block" or expected not in out.get("reason", ""):
        print(f"FAIL stdin roundtrip: block が出ない: {out!r}")
        return False
    return True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
