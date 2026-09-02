#!/usr/bin/env python3
"""PreToolUse フック: 規約の形から外れた codex companion の起動を deny する。

起動の作法は codex-watchdog スキルが定めるが、守られるかは起動する側の読解に依存していた。
外れた起動は、失敗してラウンドを1つ捨てるか、そのまま完走してリポジトリの外へ副作用を残す。判定に
要るのは起動コマンドの文字列と、サンドボックス無効化を示すツールの引数だけなので、呼ぶその場で弾ける。

見るのは `codex-companion.mjs` を含むコマンドだけで、他は何も出力せず通す。判定できない形
(引用が閉じていない等)も通す——読み取れないことを不許可の理由にすると、正しい起動まで巻き込む。
実行モード(`--write`)は呼び出し元スキルが場面ごとに決めるので判定しない。

使い方: Bash の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import json
import re
import shlex
import sys

COMPANION = "codex-companion.mjs"
# 中間変数・コマンド置換・ヒアドキュメントは値が argv まで届かない原因になる。
FORBIDDEN_CHARS = (("$", "ドル記号"), ("`", "バッククォート"), ("<<", "ヒアドキュメント演算子"))
RUNID_LINE = re.compile(r"^TASK-RUNID: [A-Za-z0-9_-]+$")

STANDARD_FORM = (
    "標準形: node \"<companion の絶対パス>\" task --cwd=\"<対象リポジトリの絶対パス。"
    "スラッシュ区切り>\" -- \"<タスク指示文>\""
)


def is_absolute(path):
    """スラッシュ区切りの POSIX 絶対パスか、ドライブ文字付きの絶対パスか。"""
    return path.startswith("/") or bool(re.match(r"^[A-Za-z]:[/\\]", path))


def is_enabled(token, name):
    """companion の真偽オプションが有効か。`--name` と `--name=<false 以外>` の両方を受け取る。"""
    return token == name or (token.startswith(name + "=") and token[len(name) + 1:] != "false")


def problem(command, disable_sandbox=False):
    """規約から外れていれば理由を返す。適合または判定対象外なら None。"""
    if not isinstance(command, str) or COMPANION not in command:
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None  # 引用が閉じていない等。読み取れない形は判定しない
    index = next((i for i, t in enumerate(tokens) if t.endswith(COMPANION)), None)
    if index is None:
        return None  # 語の一部としてしか現れない
    if tokens[0].rsplit("/", 1)[-1] not in ("node", "node.exe"):
        return None  # node の起動ではない(ログの検索など)
    # 禁止記号の検査は起動と分かってから当てる。先に当てると、記号を含む対象外のコマンドまで
    # 巻き込む。
    for char, name in FORBIDDEN_CHARS:
        if char in command:
            return (
                "起動コマンドに{}が含まれている。中間変数・コマンド置換・ヒアドキュメントを"
                "経由すると値が argv まで届かないことがある。確定済みの値を直接埋め込むこと。"
            ).format(name)
    if not is_absolute(tokens[index]):
        return (
            "companion のパスが絶対パスでない。プラグインの導入場所は環境で変わるので、相対パスや"
            "推測したパスは MODULE_NOT_FOUND で即失敗する。実パスを確定させて渡すこと。" + STANDARD_FORM
        )

    rest = tokens[index + 1:]
    if not rest or rest[0] != "task":
        return None  # task 以外のサブコマンドはこのフックの対象外

    if "--" not in rest:
        return (
            "オプション終端の -- が無い。タスク指示文の中の -m や --write などが companion 自身の"
            "オプションとして誤認識されうる。--cwd の後に -- を置くこと。" + STANDARD_FORM
        )
    head = rest[:rest.index("--")]
    task_args = rest[rest.index("--") + 1:]

    if disable_sandbox:
        return (
            "サンドボックスを無効化して起動している。codex はサンドボックス内で動かす。"
            "無効化するとリポジトリの外へ副作用が出うる。"
        )
    if any(is_enabled(token, "--background") for token in head):
        return (
            "--background が付いている。companion がジョブIDだけを返して即座に戻るため、"
            "結果を受け取らないまま先へ進む。フォアグラウンドで実行して結果を受け取ること。"
        )

    if "--cwd" in head:
        return (
            "--cwd の値がスペース区切りの別トークンになっている。この形で値が node まで届かな"
            "かった事例がある。--cwd=<パス> と = で1トークンに連結すること。" + STANDARD_FORM
        )
    cwds = [t for t in head if t.startswith("--cwd=")]
    if not cwds:
        return (
            "--cwd が無い。省略すると companion は自分のプロセスの作業ディレクトリで状態を解決し、"
            "新規ディレクトリ作成がサンドボックスに当たって EPERM になりうる。" + STANDARD_FORM
        )
    if len(cwds) > 1:
        return (
            "--cwd が複数ある。どれが採られるかは companion の解析順に委ねられ、先に書いた値を"
            "確認しても実際に使われる値とは限らない。1つだけ渡すこと。" + STANDARD_FORM
        )
    value = cwds[0][len("--cwd="):]
    if "\\" in value:
        return (
            "--cwd の値がバックスラッシュ区切りになっている。サンドボックスが不正パスへ誤解決し、"
            "プロセス生成前に失敗する。スラッシュ区切りで渡すこと。" + STANDARD_FORM
        )
    if not is_absolute(value):
        return "--cwd の値が絶対パスでない。対象リポジトリの絶対パスを渡すこと。" + STANDARD_FORM

    if not task_args:
        return "-- の後にタスク指示文が無い。" + STANDARD_FORM
    if len(task_args) > 1:
        return (
            "-- の後が複数の引数に割れている。タスク指示文は単一の引数として渡すこと"
            "(割れると companion は先頭の引数だけをタスクとして受け取る)。" + STANDARD_FORM
        )
    if not RUNID_LINE.match(task_args[0].split("\n", 1)[0]):
        return (
            "タスク指示文の最先頭行が TASK-RUNID のマーカーになっていない。watchdog はこの"
            "マーカーでジョブログを自ラウンドのものと特定するので、行がずれていると他のラウンドの"
            "ログを掴みうる。前置き・空行・前後の空白を入れず、最初の行を TASK-RUNID: <トークン> "
            "にすること。"
        )
    return None


def main():
    # 既定の標準出力コーデック(日本語 Windows では cp932)には理由文の記号が無く、出力時の
    # UnicodeEncodeError でフックが無出力のまま落ちる。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
        return
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    reason = problem(tool_input.get("command"),
                     disable_sandbox=bool(tool_input.get("dangerouslyDisableSandbox")))
    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))


COMPANION_PATH = "/plugins/codex/scripts/" + COMPANION
GOOD = (
    'node "' + COMPANION_PATH + '" task --cwd="/repo" -- "TASK-RUNID: r1\nレビューせよ"'
)


def selftest():
    passes = (
        ("標準形", GOOD),
        ("--json を挟む形", GOOD.replace("task --cwd", "task --json --cwd")),
        ("--write を挟む形(実行モードは呼び出し元が決める)",
         GOOD.replace("task --cwd", "task --write --cwd")),
        ("--background=false の形", GOOD.replace("task --cwd", "task --background=false --cwd")),
        ("ドライブ文字付きの絶対パス",
         'node "C:/plugins/scripts/' + COMPANION + '" task --cwd="D:/repo" -- "TASK-RUNID: r1\nx"'),
        ("companion を含まないコマンド", "node other.mjs task -- x"),
        ("task 以外のサブコマンド", 'node "' + COMPANION_PATH + '" status'),
        ("引用が閉じていない形は判定しない", 'node "' + COMPANION_PATH),
        ("node の起動でないコマンド", "grep " + COMPANION + " log.txt"),
        ("禁止記号を含む対象外のコマンド", 'grep "$pattern" ' + COMPANION),
        ("禁止記号を含む引用未閉鎖の入力", 'node "$root/' + COMPANION),
    )
    denies = (
        ("中間変数",
         'node "$root/' + COMPANION + '" task --cwd="/repo" -- "TASK-RUNID: r1\nx"', "ドル記号"),
        ("コマンド置換", 'node "`which x`/' + COMPANION + '" task -- x', "バッククォート"),
        ("ヒアドキュメント", 'node "' + COMPANION_PATH + '" task --cwd="/repo" -- <<EOF', "ヒアドキュメント"),
        ("相対パス", 'node ' + COMPANION + ' task --cwd="/repo" -- "TASK-RUNID: r1\nx"', "絶対パス"),
        ("-- が無い", 'node "' + COMPANION_PATH + '" task --cwd="/repo" "TASK-RUNID: r1"', "-- が無い"),
        ("--cwd がスペース区切り",
         'node "' + COMPANION_PATH + '" task --cwd "/repo" -- "TASK-RUNID: r1\nx"', "スペース区切り"),
        ("--cwd が無い", 'node "' + COMPANION_PATH + '" task -- "TASK-RUNID: r1\nx"', "--cwd が無い"),
        ("--cwd が複数ある",
         'node "' + COMPANION_PATH + '" task --cwd="/repo" --cwd="relative" -- "TASK-RUNID: r1\nx"',
         "複数ある"),
        ("--cwd の2つの形が混ざっている",
         'node "' + COMPANION_PATH + '" task --cwd="/repo" --cwd "/other" -- "TASK-RUNID: r1\nx"',
         "スペース区切り"),
        ("--cwd がバックスラッシュ",
         'node "' + COMPANION_PATH + '" task --cwd="C:\\repo" -- "TASK-RUNID: r1\nx"', "バックスラッシュ"),
        ("--cwd が相対パス",
         'node "' + COMPANION_PATH + '" task --cwd="repo" -- "TASK-RUNID: r1\nx"', "絶対パスでない"),
        ("タスク指示文が無い", 'node "' + COMPANION_PATH + '" task --cwd="/repo" --', "タスク指示文が無い"),
        ("RUNID マーカーが無い",
         'node "' + COMPANION_PATH + '" task --cwd="/repo" -- "レビューせよ"', "TASK-RUNID"),
        ("RUNID が2行目にある",
         'node "' + COMPANION_PATH + '" task --cwd="/repo" -- "前置き\nTASK-RUNID: r1"', "TASK-RUNID"),
        ("RUNID の前に空行がある",
         'node "' + COMPANION_PATH + '" task --cwd="/repo" -- "\nTASK-RUNID: r1\nx"', "TASK-RUNID"),
        ("RUNID 行に前後の空白がある",
         'node "' + COMPANION_PATH + '" task --cwd="/repo" -- " TASK-RUNID: r1 \nx"', "TASK-RUNID"),
        ("タスク指示文が複数の引数に割れている",
         'node "' + COMPANION_PATH + '" task --cwd="/repo" -- "TASK-RUNID: r1" "本文"', "複数の引数"),
        ("--background が付いている",
         GOOD.replace("task --cwd", "task --background --cwd"), "--background"),
        ("--background=true の形",
         GOOD.replace("task --cwd", "task --background=true --cwd"), "--background"),
    )
    sandbox = (
        ("無効化して起動", GOOD, True, "サンドボックス"),
        ("無効化せず起動", GOOD, False, None),
    )
    failures = []
    for label, command in passes:
        reason = problem(command)
        if reason is not None:
            failures.append("通すはずが deny: {} :: {}".format(label, reason))
    for label, command, needle in denies:
        reason = problem(command)
        if reason is None:
            failures.append("deny するはずが通した: " + label)
        elif needle not in reason:
            failures.append("理由が想定と違う: {} :: {}".format(label, reason))
    for label, command, disabled, needle in sandbox:
        reason = problem(command, disable_sandbox=disabled)
        if needle is None:
            if reason is not None:
                failures.append("通すはずが deny: {} :: {}".format(label, reason))
        elif reason is None:
            failures.append("deny するはずが通した: " + label)
        elif needle not in reason:
            failures.append("理由が想定と違う: {} :: {}".format(label, reason))
    if failures:
        for line in failures:
            print("FAIL:", line)
        sys.exit(1)
    print("ALL PASS ({} 件)".format(len(passes) + len(denies) + len(sandbox)))


if __name__ == "__main__":
    main()
