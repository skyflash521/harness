#!/usr/bin/env python3
"""Stop hook: 停止を既定で禁じ、末尾行が停止宣言のものだけを許可する。

宣言は3種。`待機` は終端でない `background_tasks` が在るときだけ通す——手番が戻る経路の無いまま止まるのを
防ぐ。`完了` と `要判断` は音を鳴らす。

併せてこのセッションが起動した背景処理を数え、生存しているものが残ったままの `完了`・`要判断` と、
生存している `wait.py` が2つ以上ある `待機` をブロックする。

判定は末尾行の等値比較。応答本文を渡さないハーネスでは判定せず通す——判定できないことを不許可の
理由にすると、何を書いても抜けられない恒久ブロックになる。

Usage: configured as a Stop hook with the plugin root as first argument.
Run with --selftest.
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

WAIT_SCRIPT = "wait.py"
LIVE = ("running", "pending", "backgrounded")
ENDED = ("completed", "failed", "killed", "cancelled", "canceled", "timeout")


def wait_script():
    """誘導先 wait.py の絶対パス。第1引数(プラグインルート)から組み立てる。"""
    roots = [arg for arg in sys.argv[1:] if arg != "--selftest"]
    if not roots:
        return f"<flow プラグイン同梱の scripts/{WAIT_SCRIPT}>"
    return Path(roots[0], "scripts", WAIT_SCRIPT).as_posix()


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
    f"{WAIT} — 何かの完了を待つ。手番が戻る経路として、登録された背景処理が在るときだけ使える。"
)

REASON_NO_MARKER = (
    "末尾行が停止宣言になっていない。このハーネスは停止を既定で禁じており、"
    "宣言の無い停止は、作業の途中で手番を返したものとして扱う。"
    "止まらずに作業を続けるか、止まる理由を宣言すること。"
    "宣言を文中で言及しただけ・囲み記号で包んだだけでは許可されない。" + _HOW
)
REASON_WAIT_UNSUBSTANTIATED = (
    f"{WAIT} と宣言しているが、手番が戻る経路になる背景処理が無い(終わった処理は経路にならない)。"
    "このまま止まると再開する手立てが無く、ユーザーが促すまで止まり続けることになる。"
    f"取るべき行動は、待つ対象を実際に起動するか、時間で待つなら {wait_script()} を"
    "run_in_background の Bash で起動するか、待たずにその作業を自分で済ませること。"
    f"作業が終わっているなら {DONE}、ユーザーの判断が要るなら {DECISION} を使う。"
)
REASON_MULTIPLE = (
    "末尾行に停止宣言が複数ある。どの理由で止まるのかが決まらない。1つだけにすること。" + _HOW
)
REASON_TASK_LEFT_RUNNING = (
    "このセッションが起動した背景処理が残ったまま手番を返そうとしている: {tasks}。"
    "残したものは後で終わって手番を戻し、確認するものが無いターンを1つ作る。"
    "用が済んだものは TaskStop で止めてから宣言し直すこと(対象のIDは起動時の戻り値が示す)。"
    f"まだ待つのであれば、止めずに {WAIT} を使う。"
)
REASON_WAIT_DUPLICATED = (
    f"{WAIT_SCRIPT} が2つ以上動いている。待つ対象は1つなので、先に用が済んだ後も残りが発火し、"
    "確認するものが無いターンを作る。TaskStop で余分な方を止めてから宣言し直すこと。"
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


def tasks_of(data):
    tasks = data.get("background_tasks")
    return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []


def live_tasks(data):
    """手番が戻る経路になりうる背景処理。終端と分かるものだけを除く。"""
    return [t for t in tasks_of(data) if t.get("status") not in ENDED]


def label(tasks):
    """deny 文で残っている処理を名指しするための一覧。多いときは残りを件数で補う。"""
    shown = ", ".join(str(t.get("id", "?")) for t in tasks[:5])
    return shown if len(tasks) <= 5 else f"{shown} ほか{len(tasks) - 5}件"


def live_now(data):
    """生存と分かる背景処理。語彙に無い状態は数えない——読めない値を生存とみなすと、
    既に終わった処理を止めようがないまま停止が塞がり続ける。語彙が増えて漏れても、
    漏れは素通り側にだけ倒れる。"""
    return [t for t in tasks_of(data) if t.get("status") in LIVE]


def live_waits(data):
    """生存と分かる wait.py の件数。"""
    def is_wait(task):
        fields = " ".join(str(task.get(key, "")) for key in ("command", "description"))
        return f"/{WAIT_SCRIPT}" in fields.replace("\\", "/")
    return sum(1 for t in live_now(data) if is_wait(t))


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
        if not live_tasks(data):
            return None, REASON_WAIT_UNSUBSTANTIATED
        if live_waits(data) > 1:
            return None, REASON_WAIT_DUPLICATED
    elif live_now(data):
        return None, REASON_TASK_LEFT_RUNNING.format(tasks=label(live_now(data)))
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
    waiting = {
        "id": "b2", "type": "shell", "status": "running", "description": "上限明けまで待つ",
        "command": 'python3 "/p/flow/scripts/wait.py" "2026-08-30 21:00"',
    }
    waiting_seconds = dict(waiting, id="b3", command="python3 /p/flow/scripts/wait.py 300")
    waiting_ended = dict(waiting, id="b4", status="completed")
    other_test = dict(waiting, id="b5", description="回帰検査",
                      command="python3 -m pytest tests/test_wait.py")
    unknown = dict(task, id="b6", status="mystery")
    pending = dict(task, id="b7", status="pending")
    crowd = [dict(task, id=f"c{n}") for n in range(6)]
    block_cases = [
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[task]),
         REASON_TASK_LEFT_RUNNING.format(tasks="b1")),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[pending]),
         REASON_TASK_LEFT_RUNNING.format(tasks="b7")),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=crowd),
         REASON_TASK_LEFT_RUNNING.format(tasks="c0, c1, c2, c3, c4 ほか1件")),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[other_test]),
         REASON_TASK_LEFT_RUNNING.format(tasks="b5")),
        (stop("コミットしました。ハッシュは 90d8326 です。"), REASON_NO_MARKER),
        (stop("次はレビューを回します。"), REASON_NO_MARKER),
        (stop("お任せいただけるなら、このまま実装します。"), REASON_NO_MARKER),
        (stop("どちらで進めますか。1. フックを作る 2. 文書だけにする"), REASON_NO_MARKER),
        (stop(""), REASON_NO_MARKER),
        (stop("[停止: 完了]\n\n続けて別の作業もあります。"), REASON_NO_MARKER),
        (stop("末尾に [停止: 完了] と書く決まりにしました。"), REASON_NO_MARKER),
        (stop("作業は終わりました。\n\n`[停止: 完了]`"), REASON_NO_MARKER),
        (stop("作業は終わりました。\n\n[停止: 完了] 以上です。"), REASON_NO_MARKER),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]"), REASON_WAIT_UNSUBSTANTIATED),
        (stop("外部の CI の完了を待ちます。\n\n[停止: 待機]", crons=[cron]),
         REASON_WAIT_UNSUBSTANTIATED),
        (stop("外部の CI の完了を待ちます。\n\n[停止: 待機]", tasks=[waiting_ended]),
         REASON_WAIT_UNSUBSTANTIATED),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[waiting]), REASON_TASK_LEFT_RUNNING.format(tasks="b2")),
        (stop("どちらで進めますか。\n\n[停止: 要判断]", tasks=[waiting_seconds]),
         REASON_TASK_LEFT_RUNNING.format(tasks="b3")),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting, waiting_seconds]),
         REASON_WAIT_DUPLICATED),
        (stop("作業は終わりました。\n\n[停止: 完了] [停止: 要判断]"), REASON_MULTIPLE),
        (stop("これからフックを書きます。", active=True), REASON_NO_MARKER),
    ]
    pass_cases = [
        (stop("コミットしました。ハッシュは 90d8326 です。\n\n[停止: 完了]"), "[停止: 完了]"),
        (stop("どちらで進めますか。1. フックを作る 2. 文書だけにする\n\n[停止: 要判断]"),
         "[停止: 要判断]"),
        (stop("作業は終わりました。\n\n[停止：完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止:完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n［停止：完了］"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止：　完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止:\t完了]"), "[停止: 完了]"),
        ({"hook_event_name": "Stop", "last_assistant_message": None}, None),
        ({"hook_event_name": "Stop", "stop_hook_active": False}, None),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[task]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[unknown]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting, task]), "[停止: 待機]"),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[waiting_ended]), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[unknown]), "[停止: 完了]"),
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
