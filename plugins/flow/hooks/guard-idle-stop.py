#!/usr/bin/env python3
"""Stop hook: 停止を原則すべて禁じ、末尾行が停止宣言のものだけを許可する。

ターン終了はツール呼び出しを伴わないので、PreToolUse 系のガードでは捕捉できない。何もせずに
止まった・着手を宣言して止まった・走っていないものを「実行中」と書いた、という失敗はいずれも
この瞬間に起きるため、Stop フックだけがその発火点になる。

**禁止する言い回しを数え上げる形(拒否リスト)は採らない。** 言い回しは無限にあり、数え上げは
必ず抜ける——拒否リストで組んだ旧実装では、助詞の有無だけで「このまま実装します」が素通りし、
助詞を落として広げると今度は疑問文「どちらで進めますか」を誤って止めた。誤検出を潰すたびに例外が
積み増され、そのたびに新しい抜けが空いた。よってここでは逆に、**停止を既定で禁じ、末尾行に停止
宣言を書いたものだけを通す**(許可リスト)。抜け道は「宣言を書く」ことしかなく、書けば何を根拠に
止まったかが応答に残る。誤検出の形も一種類(宣言の書き忘れ)で、出口は常に同じ一つになる。

宣言は3種。`待機` だけが機械で条件を課せる——`background_tasks`(登録された背景処理)か
`session_crons`(`ScheduleWakeup` 等が張った起床)のどちらかを要求する。**確かめているのは「待つ対象が
実在するか」ではなく「手番が戻ってくる経路が在るか」である。** 防ぐ失敗は、手番を返したまま再開せず、
ユーザーが促すまで止まり続けることだから、戻る経路の有無だけを見れば足りる。

**二つの経路は強さが違う。** 起床は自分の側から発火するので、張った時点で再開が確実になる——宣言する
側が自分で張れることは抜け道ではない。背景処理は完了通知に依存し、**通知は取りこぼされうる**ので、
それだけでは再開が確実にならない。確実にしたいなら起床を併せて張る規律が要るが、**その規律の正本は
各スキルであって、ここではない**(本フックは戻る経路の有無しか見ない)。

`完了` と `要判断` には課せる条件が無い。それでも宣言を要求するのは、**無意識に手番を返す経路を
消すこと自体が目的**だからで、どの理由で止まったかが応答に残る。

判定は**末尾行の等値比較**で行い、本文の走査をしない。等値なら、宣言する意図があるときだけ一致する。

`stop_hook_active` で素通ししない。一度ブロックしたら通す作りでは、宣言を書かないまま止まり直す
だけで抜けられる。ブロックの連鎖は本フックの側では止めず、ハーネス側の保護に委ねる。

**応答本文を渡さないハーネスでは判定せず通す**(キーが無い場合も `null` の場合も同じ)。
本文が無ければ宣言の有無を確かめようがなく、
止め続ければ何を書いても抜けられない恒久ブロックになる。判定できないことを不許可の理由にしない。
同じ理由で、`background_tasks` と `session_crons` を渡さないハーネスでは `待機` が使えない——この
場合は残る2種で止まることになる(行き止まりにはならないので、この限界は受け入れる)。

**この条件を「実際に走っているものが在ること」へ狭めない。** 外部のジョブを起床だけで待つ形は、
背景処理が無くても自分の側から再開できるので、防ぐべき失敗に当たらない。狭めると、目的に照らして
正しい待ちを塞ぐことになる。

Usage: configured as a Stop hook. Run with --selftest.
"""
import json
import subprocess
import sys
from pathlib import Path

# 停止宣言。応答の末尾行がこのいずれかと(表記の揺れを吸収したうえで)等しいときだけ許可する。
DONE = "[停止: 完了]"
DECISION = "[停止: 要判断]"
WAIT = "[停止: 待機]"
MARKERS = (DONE, DECISION, WAIT)

# 表記の揺れとして吸収するもの。全角のコロン・角括弧と空白の有無は、日本語入力で書き手が繰り返し
# 踏むので等値比較の前に畳む。畳んでも行の構造は変わらない。**前後に添えた文や囲み記号は畳まない**
# ——畳むと、宣言する意図のある行と、宣言に言及しただけの行の区別が付かなくなる。
FOLD = {"：": ":", "［": "[", "］": "]", " ": "", "　": "", "\t": ""}

TAG = "[guard-idle-stop]"

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
    """表記の揺れを畳む。何を畳むかは `FOLD` が持つ(ここへ列挙を写すと片方が古くなる)。"""
    for src, dst in FOLD.items():
        text = text.replace(src, dst)
    return text


def last_line(message):
    """末尾の空でない行。

    **引用の除去をここへ足さない。** 行をまたいで消す実装は、閉じられない囲みが本文に1つあるだけで
    宣言行ごと落とし、逆に末尾が引用なら本文中間の行を末尾へ昇格させる。等値比較にした以上、
    説明のための言及を落とす仕組みは要らない。
    """
    for line in reversed(message.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def verdict(data):
    """block する理由を返す。block しないなら None。"""
    if data.get("hook_event_name") != "Stop":
        return None
    # 本文を渡さないハーネスでは判定材料が無い。キーの不在も null も情報量は同じなので、どちらも
    # 不許可の理由にしない(止め続ければ何を書いても抜けられない恒久ブロックになる)。空文字は
    # 宣言を書けば抜けられるので、これだけは block 側に残す。
    message = data.get("last_assistant_message")
    if message is None:
        return None
    line = fold(last_line(message))
    found = [m for m in MARKERS if fold(m) in line]
    if len(found) > 1:
        return REASON_MULTIPLE
    if len(found) != 1 or line != fold(found[0]):
        return REASON_NO_MARKER
    if found[0] == WAIT:
        if not (data.get("background_tasks") or data.get("session_crons")):
            return REASON_WAIT_UNSUBSTANTIATED
    return None


def main():
    # 出力・入力の符号化を固定する。ハーネスが渡す JSON は UTF-8 で、既定の符号化で読むと日本語の
    # 宣言が化けて一致せず、すべての停止をブロックし続ける。理由の日本語も出力時に落ちる。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    reason = verdict(data)
    if reason:
        print(json.dumps({"decision": "block", "reason": f"{TAG} {reason}"}))


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
        # 宣言が無い停止は、内容によらずすべて止める
        (stop("コミットしました。ハッシュは 90d8326 です。"), REASON_NO_MARKER),
        (stop("次はレビューを回します。"), REASON_NO_MARKER),
        (stop("お任せいただけるなら、このまま実装します。"), REASON_NO_MARKER),
        (stop("どちらで進めますか。1. フックを作る 2. 文書だけにする"), REASON_NO_MARKER),
        (stop(""), REASON_NO_MARKER),
        # 宣言が末尾行でない
        (stop(f"{DONE}\n\n続けて別の作業もあります。"), REASON_NO_MARKER),
        # 宣言に文を添えた・囲み記号で包んだ末尾行は許可しない(等値でないため)
        (stop(f"末尾に {DONE} と書く決まりにしました。"), REASON_NO_MARKER),
        (stop(f"作業は終わりました。\n\n`{DONE}`"), REASON_NO_MARKER),
        (stop(f"作業は終わりました。\n\n{DONE} 以上です。"), REASON_NO_MARKER),
        # 待機の宣言に実体が無い
        (stop(f"レビューの完了を待ちます。\n\n{WAIT}"), REASON_WAIT_UNSUBSTANTIATED),
        # 宣言が複数
        (stop(f"作業は終わりました。\n\n{DONE} {DECISION}"), REASON_MULTIPLE),
        # 一度ブロックした後でも、宣言が無ければ通さない
        (stop("これからフックを書きます。", active=True), REASON_NO_MARKER),
    ]
    pass_cases = [
        # 完了の宣言
        stop(f"コミットしました。ハッシュは 90d8326 です。\n\n{DONE}"),
        # 要判断の宣言
        stop(f"どちらで進めますか。1. フックを作る 2. 文書だけにする\n\n{DECISION}"),
        # 表記の揺れ(全角コロン・全角角括弧・空白の有無)は吸収する
        stop("作業は終わりました。\n\n[停止：完了]"),
        stop("作業は終わりました。\n\n[停止:完了]"),
        stop("作業は終わりました。\n\n［停止：完了］"),
        # 本文が null のハーネスでも判定しない(恒久ブロックを避ける)
        {"hook_event_name": "Stop", "last_assistant_message": None},
        # 待機の宣言と実体
        stop(f"レビューの完了を待ちます。\n\n{WAIT}", tasks=[task]),
        stop(f"外部の CI の完了を待ちます。\n\n{WAIT}", crons=[cron]),
        # 一度ブロックした後でも、宣言があれば通す
        stop(f"作業は終わりました。\n\n{DONE}", active=True),
        # 応答本文を渡さないハーネスでは判定しない(恒久ブロックを避ける)
        {"hook_event_name": "Stop", "stop_hook_active": False},
        # 入力が Stop でない
        {"hook_event_name": "SubagentStop", "last_assistant_message": "これから書きます。"},
    ]
    ok = True
    for data, expected in block_cases:
        actual = verdict(data)
        if actual != expected:
            ok = False
            print(f"FAIL expected block: {data.get('last_assistant_message')!r} -> {actual!r}")
    for data in pass_cases:
        actual = verdict(data)
        if actual is not None:
            ok = False
            print(f"FAIL expected pass: {data.get('last_assistant_message')!r} -> {actual!r}")
    if not _roundtrip_ok(block_cases[0][0], block_cases[0][1]):
        ok = False
    total = len(block_cases) + len(pass_cases) + 1
    print("ALL PASS" if ok else "SOME FAILED", f"({total} cases)")
    sys.exit(0 if ok else 1)


def _roundtrip_ok(data, expected):
    """ハーネスと同じ形(UTF-8 の JSON を標準入力へ)で自分を起動し、block が出るか確かめる。

    判定関数を直接叩くだけの自己テストは、標準入力の復号を通らない。既定の符号化で読むと日本語の
    宣言が化けて一致せず、判定が変わる——この経路を通さない検査は、壊れていても合格する。
    """
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
    # 自己テストの出力も日本語を含むので、分岐より前に出力の符号化を固定する。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
