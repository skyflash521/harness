#!/usr/bin/env python3
"""Stop hook: 待ちの実体が無いのにターンを終えようとする停止を block する。

ターン終了はツール呼び出しを伴わないので、PreToolUse 系のガードでは捕捉できない。何もせずに
止まった・着手を宣言して止まった・走っていないものを「実行中」と書いた、という失敗はいずれも
この瞬間に起きるため、Stop フックだけがその発火点になる。

判定は Stop フックの入力が持つ3つの実体で行う。`background_tasks`(実行中の背景処理)と
`session_crons`(予約済みの起床)は待ちの実体が在るかを機械的に示し、`last_assistant_message` は
その停止で何を主張したかを示す。**継続を主張していない停止は対象外**——作業を終えて報告した停止まで
止めると、ガードが常時発火して意味を失うため、主張が在るときだけ実体と突き合わせる。

ユーザーの応答を待つ停止も、待っていると書けば止める——待つ相手が人であることは機械で確かめ
られないので、実体の無い待ちと区別が付かない。抜けるには、待っていると書くのをやめ、何を選ぶ
のかを確定的に問う形へ直す。その出口を deny メッセージが示す。
判定は決定的な語検出で緩く広く拾い、**精度は語で作らない**。取るべき行動は deny メッセージが
自己完結して伝える——読み手が別の文書を開かなくても次の一手が決まるように書く。

**`stop_hook_active` で素通ししない。** 一度ブロックしたら通す作りにすると、指示を無視して同じ文面で
止まり直すだけで抜けられ、ガードとして成立しない。毎回の停止に掛け、条件が消えたときだけ通す
——応答を直せば(作業を始める・確定的に問う)条件は消える。ブロックの連鎖は本フックの側では止めず、ハーネス側の保護に委ねる。

Usage: configured as a Stop hook. Run with --selftest.
"""
import json
import subprocess
import sys
from pathlib import Path

# 着手の宣言。応答の末尾付近にあるものだけを見る(本文中の経過説明と区別するため)。
# 助詞を含めない——「を実装します」だけを持つと「このまま実装します」を取り逃がす。狭めた分だけ
# 抜け道が増えるので、精度は語で作らず deny メッセージに委ねる。
TAIL_WINDOW = 240
ANNOUNCE = (
    "入ります", "始めます", "開始します", "進みます", "取り掛かります", "着手します",
    "作ります", "作成します", "書きます", "実行します", "調べます", "検証します",
    "確認します", "していきます", "続けます", "進めます", "修正します", "直します",
    "適用します", "反映します", "実装します", "回します", "足します", "外します",
)

# 継続の主張。引用と否定を除いた本文のどこにあっても見る。
ONGOING = (
    "実行中", "継続中", "検査中", "処理中", "作業中", "計測中",
    "実行しています", "走っています", "動いています",
    "待機中", "待っています", "完了を待ち", "完了待ち", "応答待ち", "結果待ち", "結果を待ち",
)

# 継続の主張を打ち消す語。一致箇所の直後に現れたら主張とみなさない。
NEGATION = (
    "ではない", "ではなく", "はない", "はなく", "もない", "もなく", "ありませ",
    "が無", "は無", "も無", "がない", "がなく",
)
NEGATION_WINDOW = 12
# 「〜中」で終わる語は、直後がこれらなら状態の主張でなく時間・連体の修飾(「作業中に」「実行中の」)。
MODIFIER_AFTER = ("に", "の")

# 引用の囲み。開き文字から対応する閉じ文字までを判定対象から外す。
QUOTES = {"「": "」", "『": "』", "`": "`", '"': '"'}

TAG = "[guard-idle-stop]"

REASON_ANNOUNCE = (
    "着手を宣言して止まろうとしている。宣言は着手ではない。"
    "予約済みの起床を張っていても、それが裏付けるのは待ちが自力で終わることだけで、"
    "宣言した作業を後回しにしてよいことではない。"
    "取るべき行動は次の三つのいずれかで、宣言して手番を返すことはどれでもない。"
    "(1) 宣言した作業を、この同じターンで始める。"
    "(2) ユーザーの判断が要るなら、条件節でぼかさずに問う——"
    "「〜いただければ」「〜でよろしければ」「問題なければ」のような、"
    "承諾を先取りして着手を予告する言い方をやめ、何を選ぶのかだけを確定的に書く。"
    "選択肢は「〜する」の形か体言で書く——ます形で書くと着手の宣言と同じ形になり、また止められる。"
    "(3) 何かの完了を待っているなら、何の完了を待つのかを書き、"
    "自分の側から発火できる起床を張ってから止まる。"
    "同じ言い方で止まり直しても通らない——この検査は毎回の停止に掛かる。"
)
REASON_PHANTOM = (
    "実行中・待機中だと書いているが、実行中の背景処理も予約済みの起床も無い。"
    "出力が無いことは実行中であることを示さない。取るべき行動は、その対象を観測して事実を確かめるか、"
    "観測できないなら未検証と明記したうえで自分で取り直すこと。"
    "推測を状態の報告として書かない。"
    "待っている相手がユーザーなら、待っていると書くのをやめ、"
    "何を選ぶのかだけを確定的に問う形へ直すこと。"
    "過去の経過や、このセッションの外で動いているものの状態を述べているだけなら、"
    "その語を使わずに結果だけを書き直すこと。"
    "外部の完了を本当に待つなら、自分の側から発火できる起床を張ってから止まること。"
)
REASON_NO_WAKEUP = (
    "背景処理の完了を待つと書いているが、予約済みの起床が無い。完了通知は届かないことがあり、"
    "届かなければこの待ちは自分では終わらない。取るべき行動は、自分の側から発火できる起床を"
    "張ってから止まるか、張らないなら待たずに自分で取り直すこと。"
)


def _hit(text, needles):
    """text に needles のいずれかが含まれるか。"""
    return any(needle in text for needle in needles)


def strip_quoted(text):
    """引用の囲みの中身を落とす。

    規約そのものを編集するセッションの報告は、判定語を引用として含む(「実行中」という語そのものを論じる文など)。
    引用を残すと、語について述べただけの報告が主張として扱われる。
    """
    out = []
    closing = None
    for char in text:
        if closing is None:
            if char in QUOTES:
                closing = QUOTES[char]
            else:
                out.append(char)
        elif char == closing:
            closing = None
    return "".join(out)


def asserts_ongoing(message):
    """継続の主張が在るか。引用の中身・修飾用法・打ち消された一致は数えない。"""
    text = strip_quoted(message)
    for needle in ONGOING:
        start = text.find(needle)
        while start != -1:
            end = start + len(needle)
            after = text[end:end + NEGATION_WINDOW]
            # 打ち消しは同じ文の中だけを見る。別の文の否定を一致へ結び付けない。
            if not _hit(after.split("。")[0], NEGATION) and not _is_modifier(needle, after):
                return True
            start = text.find(needle, end)
    return False


def _is_modifier(needle, after):
    """「〜中」が状態の主張でなく修飾として使われているか(「作業中に」「実行中の」)。"""
    return needle.endswith("中") and after[:1] in MODIFIER_AFTER


def announces(message):
    """着手の宣言が末尾付近に在るか。直後が「か」の一致は疑問であって宣言ではない。"""
    tail = strip_quoted(message)[-TAIL_WINDOW:]
    for needle in ANNOUNCE:
        start = tail.find(needle)
        while start != -1:
            end = start + len(needle)
            if tail[end:end + 1] != "か":
                return True
            start = tail.find(needle, end)
    return False


def verdict(data):
    """block する理由を返す。block しないなら None。"""
    if data.get("hook_event_name") != "Stop":
        return None
    message = data.get("last_assistant_message") or ""
    if not message:
        return None
    tasks = data.get("background_tasks") or []
    crons = data.get("session_crons") or []
    if asserts_ongoing(message):
        # 予約済みの起床が在れば、自分の側から発火できる再開経路を持っている。待ちとして成立するので
        # 主張を裏取り不足として扱わない(背景処理に載らない外部ジョブの待ちがこれに当たる)。
        # **この免除を待ちの判定の外へ出さない。** 起床が裏付けるのは待ちが自力で終わることだけで、
        # 宣言した作業を後回しにしてよいことではない。外へ出すと、ハートビートを張る手順
        # (レビューループ・自律開発)の実行中は宣言して止まる失敗が一切検出されなくなる。
        if crons:
            return None
        return REASON_NO_WAKEUP if tasks else REASON_PHANTOM
    if not tasks and announces(message):
        return REASON_ANNOUNCE
    return None


def main():
    # 出力・入力の符号化を固定する。ハーネスが渡す JSON は UTF-8 で、既定の cp932 で読むと日本語の
    # 語が化けて一致せず、フックが停止を無言で通す。理由の日本語も出力時に落ちる。
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
        print(json.dumps({
            "decision": "block",
            "reason": f"{TAG} {reason}",
        }))


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
        # 宣言して止まった(待つ対象が無い)
        (stop("記録しました。\n\nこれからフック本体を書きます。"), REASON_ANNOUNCE),
        (stop("次は Stop フックの検証に入ります。"), REASON_ANNOUNCE),
        (stop("指摘を反映しました。次はレビューを回します。"), REASON_ANNOUNCE),
        # 承諾を先取りして着手を予告する曖昧な言い方も止める
        (stop("お任せいただけるなら、このまま実装します。"), REASON_ANNOUNCE),
        (stop("問題なければ次の修正に進みます。"), REASON_ANNOUNCE),
        (stop("よろしければレビューを回します。"), REASON_ANNOUNCE),
        (stop("指示していただければ、それだけを実行します。"), REASON_ANNOUNCE),
        # 走っていないのに実行中と書いた
        (stop("検査を実行中です(自己テストのみ継続中)。"), REASON_PHANTOM),
        (stop("codex の結果待ちです。"), REASON_PHANTOM),
        # 一度ブロックした後でも、同じ文面なら通さない
        (stop("これからフックを書きます。", active=True), REASON_ANNOUNCE),
        # 背景処理は在るが再開経路が無い
        (stop("検査を実行中です。", tasks=[task]), REASON_NO_WAKEUP),
        # 起床の免除は待ちの判定にだけ効く。宣言して止まることは免除しない
        (stop("次はレビューを回します。", crons=[cron]), REASON_ANNOUNCE),
        # ます形の選択肢は着手の宣言と同じ形になる
        (stop("どちらにしますか。1. 先に修正を適用します 2. レビューを回します"), REASON_ANNOUNCE),
    ]
    pass_cases = [
        # 作業を終えて報告しただけの停止
        stop("コミットしました。ハッシュは 90d8326 です。"),
        stop("5検査すべて合格しました。"),
        # 選択肢を確定的に示して判断を求める停止(着手の予告を含まない)
        stop("どちらで進めますか。1. フックを作る 2. 文書だけにする"),
        stop("この方針でよろしいですか。"),
        # 継続の主張を打ち消している完了報告
        stop("実行中の背景処理はありません。すべて完了しました。"),
        stop("待機中のタスクは無く、作業は終わっています。"),
        # 判定語を引用しただけの報告
        stop("判定語の一覧に「実行中」「継続中」を並べました。"),
        # 「〜中に」「〜中の」は状態の主張でなく修飾
        stop("作業中に見つけた別件は打ち切りました。"),
        # 予約済みの起床が在る正しい待ち
        stop("レビューの完了を待っています。", tasks=[task], crons=[cron]),
        stop("外部の CI の完了を待っています。", crons=[cron]),
        # 背景処理が在る停止は待ちの実体があるので、宣言を咎めない
        stop("次はレビューを回します。", tasks=[task]),
        # 入力が Stop でない
        {"hook_event_name": "SubagentStop", "last_assistant_message": "これから書きます。"},
        # 応答本文が取れない
        stop(""),
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
    語が化けて一致せず、フックは例外も出さずに停止を通す——この経路を通さない検査は、壊れていても
    合格する。
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
