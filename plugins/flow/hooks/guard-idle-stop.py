#!/usr/bin/env python3
"""Stop hook: 待ちの実体が無いのにターンを終えようとする停止を block する。

ターン終了はツール呼び出しを伴わないので、PreToolUse 系のガードでは捕捉できない。何もせずに
止まった・着手を宣言して止まった・走っていないものを「実行中」と書いた、という失敗はいずれも
この瞬間に起きるため、Stop フックだけがその発火点になる。

判定は Stop フックの入力が持つ3つの実体で行う。`background_tasks`(実行中の背景処理)と
`session_crons`(予約済みの起床)は待ちの実体が在るかを機械的に示し、`last_assistant_message` は
その停止で何を主張したかを示す。**継続を主張していない停止は対象外**——作業を終えて報告した停止まで
止めると、ガードが常時発火して意味を失うため、主張が在るときだけ実体と突き合わせる。

ユーザーの判断を待つ停止は対象外(待つ相手が機構ではなく人であり、実体が在るかを機械で見られない)。
判定は決定的な語検出で、規約の正本は docs/waiting-discipline.md に置く。

Usage: configured as a Stop hook. Run with --selftest.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

# 着手の宣言。応答の末尾付近にあるものだけを見る(本文中の経過説明と区別するため)。
TAIL_WINDOW = 240
ANNOUNCE = (
    "に入ります", "へ入ります", "を始めます", "を開始します", "に進みます", "へ進みます",
    "に取り掛かります", "に着手します", "を作ります", "を作成します", "を書きます",
    "を実行します", "を調べます", "を検証します", "を確認します", "していきます",
    "を続けます", "を進めます", "を修正します", "を直します", "を適用します",
    "を反映します", "を実装します", "を回します", "を足します", "を外します",
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

# ユーザーの判断・確認を待つ停止。応答の末尾付近だけを見る(本文中の定型句で免除させないため)。
CONSULT = (
    "どうしますか", "どちらにしますか", "いかがでしょう", "よろしいですか",
    "選んでください", "決めてください", "ご指示ください", "判断を仰", "ご判断",
    "教えてください", "指定してください", "お知らせください",
    "ご確認ください", "確認してください", "お願いします",
    "ご承認", "承認ください", "承認をお",
    "指示を待", "判断を待", "返答を待", "回答を待", "ご連絡を待",
)

# 引用の囲み。開き文字から対応する閉じ文字までを判定対象から外す。
QUOTES = {"「": "」", "『": "』", "`": "`", '"': '"'}

DOC = "flow の waiting-discipline.md"
TAG = "[guard-idle-stop]"

REASON_ANNOUNCE = (
    "着手を宣言して止まろうとしている。実行中の背景処理も予約済みの起床も無く、"
    "待っている対象が存在しない。宣言は着手ではないので、"
    "同じターンで宣言した作業を始めること。"
    f"根拠: {DOC} §7(宣言は着手ではない)。"
)
REASON_PHANTOM = (
    "実行中・待機中だと書いているが、実行中の背景処理も予約済みの起床も無い。"
    "出力が無いことは実行中であることを示さない。観測できないなら未検証と書き、"
    "その対象を自分で取り直すこと。"
    f"根拠: {DOC} §4(実行中と言ってよいのは観測したときだけ)。"
)
REASON_NO_WAKEUP = (
    "背景処理の完了を待つと書いているが、予約済みの起床が無い。完了通知は届かないことがあり、"
    "届かなければこの待ちは自分では終わらない。起床を張るか、待たずに自分で取り直すこと。"
    f"根拠: {DOC} §5(完了通知を唯一の再開手段にしない)。"
)


def _hit(text, needles):
    """text に needles のいずれかが含まれるか。"""
    return any(needle in text for needle in needles)


def strip_quoted(text):
    """引用の囲みの中身を落とす。

    規約そのものを編集するセッションの報告は、判定語を引用として含む(「実行中と言ってよいのは…」)。
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


def is_consult(message):
    """ユーザーの判断・確認を待つ停止か(末尾付近の語か、末尾の疑問符で判定)。"""
    # 引用の中身は落とす。依頼語を引用しただけの報告で免除させない(3判定で扱いを揃える)。
    tail = strip_quoted(message)[-TAIL_WINDOW:]
    return _hit(tail, CONSULT) or bool(re.search(r"[?？]\s*$", message))


def verdict(data):
    """block する理由を返す。block しないなら None。"""
    if data.get("hook_event_name") != "Stop":
        return None
    # 既に一度ブロックした停止サイクルでは重ねてブロックしない(無限ループを避ける)。
    if data.get("stop_hook_active"):
        return None
    message = data.get("last_assistant_message") or ""
    if not message or is_consult(message):
        return None
    tasks = data.get("background_tasks") or []
    crons = data.get("session_crons") or []
    # 予約済みの起床が在れば、自分の側から発火できる再開経路を持っている。待ちとして成立するので
    # 継続の主張を裏取り不足として扱わない(背景処理に載らない外部ジョブの待ちがこれに当たる)。
    if crons:
        return None
    if asserts_ongoing(message):
        return REASON_NO_WAKEUP if tasks else REASON_PHANTOM
    if not tasks and _hit(strip_quoted(message)[-TAIL_WINDOW:], ANNOUNCE):
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
        (stop("修正しました。続けてレビューを実行します。"), REASON_ANNOUNCE),
        (stop("指摘を反映しました。次はレビューを回します。"), REASON_ANNOUNCE),
        # 走っていないのに実行中と書いた
        (stop("検査を実行中です(自己テストのみ継続中)。"), REASON_PHANTOM),
        (stop("レビューの応答待ちです。"), REASON_PHANTOM),
        (stop("codex の結果待ちです。"), REASON_PHANTOM),
        # 諮る語が本文中にあるだけでは免除しない(末尾は主張のまま)
        (stop("何かあれば教えてください。" + "あ" * TAIL_WINDOW + "検査を実行中です。"),
         REASON_PHANTOM),
        # 背景処理は在るが再開経路が無い
        (stop("検査を実行中です。", tasks=[task]), REASON_NO_WAKEUP),
        # 「承認」を含む語(自動承認等)では免除しない
        (stop("自動承認の設定を確認しました。検査を実行中です。"), REASON_PHANTOM),
        # 別の文の否定は一致へ結び付けない
        (stop("検査を実行中です。問題はありません。"), REASON_PHANTOM),
        # 依頼語を引用しただけでは免除しない
        (stop("CONSULT に「ご確認ください」を足しました。検査を実行中です。"), REASON_PHANTOM),
    ]
    pass_cases = [
        # 作業を終えて報告しただけの停止
        stop("コミットしました。ハッシュは 90d8326 です。"),
        stop("5検査すべて合格しました。"),
        # 継続の主張を打ち消している完了報告
        stop("実行中の背景処理はありません。すべて完了しました。"),
        stop("待機中のタスクは無く、作業は終わっています。"),
        # 判定語を引用しただけの報告(この規約群を編集するセッションが踏む)
        stop("§4「実行中と言ってよいのは観測したときだけ」を新設しました。"),
        stop("ANNOUNCE に「を修正します」「を回します」を足しました。"),
        # 「〜中に」「〜中の」は状態の主張でなく修飾
        stop("作業中に見つけた別件は打ち切りました。"),
        stop("検査中の不具合も直しました。"),
        # 同じ文の中の否定は打ち消しとして数える
        stop("検査は実行中ではありません。"),
        # ユーザー宛の待ち表明を平叙で書いた停止
        stop("ご指示を待っています。"),
        # ユーザーの判断・確認を待つ停止
        stop("どちらで進めますか。1. フックを作る 2. 文書だけにする"),
        stop("この方針でよろしいですか。"),
        stop("次はフックを書きますが、先に方針を決めてください。"),
        stop("レビューを実行しますか？"),
        stop("内容をご確認ください。問題なければ次の修正に進みます。"),
        # 予約済みの起床が在る正しい待ち(背景処理に載らない外部ジョブを含む)
        stop("レビューの完了を待っています。", tasks=[task], crons=[cron]),
        stop("外部の CI の完了を待っています。", crons=[cron]),
        stop("検査を実行中です。", tasks=[task], crons=[cron]),
        # 既にブロック済みの停止サイクル
        stop("これからフックを書きます。", active=True),
        # 宣言が本文の途中にあるだけ(末尾は報告)
        stop("フックを書きます、と前のターンで述べていました。" + "あ" * TAIL_WINDOW),
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
    if "--selftest" in sys.argv:
        selftest()
    main()
