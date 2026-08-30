#!/usr/bin/env python3
"""時間が経つのを待つ唯一の手段。目標時刻か秒数まで待って終わる。read-only。

待つ側は `run_in_background: true` の Bash から起動し、このプロセスの完了通知で手番を取り戻す。
用途は2つ。使用量上限が明けるのを待つ形(`flow:codex-watchdog` が適用する)と、応答を待つだけの
相手に締切を持たせる形である。

目標時刻を受ける形では、待ち時間を秒で決め打ちしたときのような、算出から実行までのずれと丸めの
ぶんの余計な待ち・早すぎる再開が起きない。バッファは足さない。

引数は `YYYY-MM-DD HH:MM[:SS]` のローカル時刻か、今からの秒数(数字のみ)の2形式だけを受ける。
**codex のログの文面をそのまま渡す用途は持たない**——codex 側の文言・時刻の書式は予告なく変わり、
それを解釈する規則をここに置けば書式が変わった日に壊れるため。ログから時刻を読み取って正規化するのは
呼び出し元の責務で、このスクリプトは曖昧さのない形式だけを扱う。解釈した時刻は起動直後に出力するので、
正規化を誤っていれば気付ける。

`sleep` が短く戻った場合は残り秒数を計算し直して待機し直すので、正常終了は「目標時刻に到達した」
ことを意味する。ただしプロセスごと打ち切られる可能性は残るため、呼び出し元は終了後に出力の
到達時刻を確かめ、到達前なら同じ引数で起動し直す。

待てる長さの上限(5時間)はこのスクリプトに固定で、呼び出し側から変えられない。閾値はユーザーが
決めた値で、上限を超える待ちは待たずに止まってユーザーへ諮る側の判断に回すため。

Usage: python3 wait.py "<YYYY-MM-DD HH:MM[:SS]>"
       python3 wait.py <秒数>
       python3 wait.py --selftest
Exit code: 0=目標時刻に到達(過去の時刻を渡した場合も即 0)、2=引数を解釈できない、
           3=残りが上限の5時間を超える。
"""
import datetime
import re
import sys
import time

FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")
SECONDS_RE = re.compile(r"\A\d+\Z")
MAX_SECONDS = 5 * 60 * 60


def parse_target(text, now):
    """`YYYY-MM-DD HH:MM[:SS]` か秒数を目標時刻に直す。どちらでもない表記は None。"""
    cleaned = " ".join(text.strip().split())
    if SECONDS_RE.match(cleaned):
        return now + datetime.timedelta(seconds=int(cleaned))
    for fmt in FORMATS:
        try:
            return datetime.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def remaining_seconds(target, now):
    """今から目標時刻までの秒数。過ぎていれば 0。"""
    return max(0.0, (target - now).total_seconds())


def selftest():
    base = datetime.datetime(2026, 8, 29, 1, 47)
    cases = [
        ("2026-08-29 01:47", datetime.datetime(2026, 8, 29, 1, 47)),
        ("  2026-08-29   01:47  ", datetime.datetime(2026, 8, 29, 1, 47)),
        ("2026-08-29 01:47:30", datetime.datetime(2026, 8, 29, 1, 47, 30)),
        ("300", base + datetime.timedelta(seconds=300)),
        (" 300 ", base + datetime.timedelta(seconds=300)),
        ("0", base),
        # codex のログの文面は受け付けない。書式の解釈を持てば、その書式が変わった日に壊れる。
        ("Aug 29th, 2026 1:47 AM", None),
        ("at Aug 29th, 2026 1:47 AM.", None),
        ("2026-08-29T01:47Z", None),
        ("いつか", None),
        ("300s", None),
        ("-300", None),
    ]
    failures = []
    for text, expected in cases:
        got = parse_target(text, base)
        if got != expected:
            failures.append(f"parse_target({text!r}) -> {got!r}, expected {expected!r}")

    if remaining_seconds(base, base - datetime.timedelta(seconds=90)) != 90:
        failures.append("remaining_seconds: 未来の目標で残り秒数が合わない")
    if remaining_seconds(base, base + datetime.timedelta(seconds=90)) != 0:
        failures.append("remaining_seconds: 過ぎた目標で 0 にならない")

    for line in failures:
        print(f"FAIL {line}")
    print("selftest: " + ("OK" if not failures else f"{len(failures)} 件失敗"))
    return 1 if failures else 0


def main(argv):
    if "--selftest" in argv:
        return selftest()

    if len(argv) != 1:
        print(__doc__.split("Usage:")[1].strip(), file=sys.stderr)
        return 2

    now = datetime.datetime.now()
    target = parse_target(argv[0], now)
    if target is None:
        print(f"YYYY-MM-DD HH:MM のローカル時刻でも秒数でもない: {argv[0]!r}", file=sys.stderr)
        return 2

    remaining = remaining_seconds(target, now)
    if remaining > MAX_SECONDS:
        print(f"残り {int(remaining)} 秒は上限 {MAX_SECONDS} 秒を超える: 待たない", flush=True)
        return 3

    # バックグラウンド実行では標準出力がブロックバッファリングされるため、待機に入る前に
    # 明示的に流す。流さないと、解釈した目標時刻を待機中に確認できない。
    print(f"目標: {target:%Y-%m-%d %H:%M:%S} / 待機秒数: {int(remaining)}", flush=True)
    while remaining > 0:
        time.sleep(remaining)
        remaining = remaining_seconds(target, datetime.datetime.now())
    print(f"到達: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
