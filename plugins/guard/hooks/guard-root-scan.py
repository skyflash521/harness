#!/usr/bin/env python3
"""PreToolUse フック: ドライブルート起点の再帰探索(`find /` 等)を deny する。

Git Bash の `/` は MSYS ルートで、`/c` `/d` … として全ドライブが自動マウントされる。そこを起点に
再帰探索を始めると、走査対象に全ドライブが入る。実測ではこの形の探索が数時間経っても終わらず、
CPU を1コア占有し続けた(所要時間がそこまで伸びた理由は未検証)。`| head -N` はヒットが N 件に届く
場合しか止められない。さらに親のシェルが終了しても Windows は子孫を巻き込まないため、走り続ける
プロセスだけが孤児として残る。

そこで、走査コマンドにルート相当のパスが渡された呼び出しを deny し、走査対象がリポジトリの
管理下に絞られる代替(Glob/Grep ツール・`git ls-files`・`rg --files`)へ誘導する。
`cd /` でルートへ移ってから暗黙のカレントを走査する形も同じ暴走なので併せて見る。

浅い深さで区切れば走査量が小さく収まり実用的な時間で終わるため、`find` に浅い `-maxdepth` が
あるときは通す。これが明示的な迂回手段であり、ルート直下を確かめたいだけの正当な用途はこれで足りる。

PowerShell ツールの発行も同じく見る。見るのは上に挙げた走査コマンドの呼び出しだけで、PowerShell 固有の
再帰列挙(`Get-ChildItem -Recurse`)は対象にしない。

使い方: Bash・PowerShell の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import json
import re
import sys

from hook_input import read_command
from shell_words import command_positions, head_name, segments

ALWAYS_RECURSIVE = {"find", "rg", "ripgrep", "fd", "fdfind", "ag", "ack", "tree", "du"}
# ls の `-r` は逆順であって再帰ではないので、大文字だけを見る。
RECURSIVE_LETTERS = {"grep": "rR", "egrep": "rR", "fgrep": "rR", "ls": "R"}
PATTERN_FIRST = {"grep", "egrep", "fgrep", "rg", "ripgrep", "ag", "ack", "fd", "fdfind"}
VALUE_FLAGS = {"-e", "--regexp", "-f", "--file"}
PATTERN_BY_FLAG = VALUE_FLAGS | {"--files"}
# ルート相当のパス: `/`・`/c`(ドライブの自動マウント)・`/cygdrive/c`・`C:`。
ROOT_RE = re.compile(r"(?:/(?:[a-z]|cygdrive/[a-z])?|[a-z]:)/*", re.IGNORECASE)
MAX_BOUNDED_DEPTH = 2


def is_root_path(token):
    """トークンがドライブルート相当のパスか。"""
    return bool(ROOT_RE.fullmatch(token.replace("\\", "/")))


def has_recursive_flag(name, args):
    """再帰フラグが付いているか。短縮形は `-rn` のようなまとめ書きも見る。"""
    letters = RECURSIVE_LETTERS[name]
    for arg in args:
        if arg == "--recursive":
            return True
        if re.fullmatch(r"-[a-zA-Z]+", arg) and set(arg[1:]) & set(letters):
            return True
    return False


def find_path_operands(args):
    """`find` の探索起点を返す。先頭のオプションを飛ばし、最初の式(`-name` 等)で打ち切る。

    式の引数(`-name adoption.md` の `adoption.md` など)は起点ではないので、混ぜて数えない。
    """
    operands = []
    for arg in args:
        if arg in ("-H", "-L", "-P") or arg.startswith("-O"):
            continue
        if arg.startswith("-"):
            break
        operands.append(arg)
    return operands


def path_operands(name, args):
    """パスとして渡されたオペランドを返す。フラグとその引数・検索パターンは除く。"""
    if name == "find":
        return find_path_operands(args)
    skip_pattern = name in PATTERN_FIRST and not (set(args) & PATTERN_BY_FLAG)
    operands = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in VALUE_FLAGS:
            index += 2
        elif arg.startswith("-") and arg != "-":
            index += 1
        else:
            if skip_pattern:
                skip_pattern = False
            else:
                operands.append(arg)
            index += 1
    return operands


def has_bounded_depth(args):
    """`find` の探索が浅い深さで区切られているか。"""
    for index, arg in enumerate(args):
        if arg == "-maxdepth" and index + 1 < len(args) and args[index + 1].isdigit():
            return int(args[index + 1]) <= MAX_BOUNDED_DEPTH
    return False


def scans_root(command):
    """ドライブルート起点の再帰探索を含むか。"""
    parsed = segments(command)
    if parsed is None:
        return False
    at_root = False
    for tokens in parsed:
        positions = list(command_positions(tokens))
        if positions and head_name(tokens[positions[0]]) == "cd":
            targets = [arg for arg in tokens[positions[0] + 1:] if not arg.startswith("-")]
            at_root = bool(targets) and is_root_path(targets[0])
            continue
        if any(scans_from(head_name(tokens[i]), tokens[i + 1:], at_root) for i in positions):
            return True
    return False


def scans_from(name, args, at_root):
    """name を args で起動すると、ドライブルート起点の再帰探索になるか。"""
    if name not in ALWAYS_RECURSIVE and not (
        name in RECURSIVE_LETTERS and has_recursive_flag(name, args)
    ):
        return False
    if name == "find" and has_bounded_depth(args):
        return False
    operands = path_operands(name, args)
    if any(is_root_path(operand) for operand in operands):
        return True
    return at_root and all(operand.rstrip("/") in ("", ".") for operand in operands)


def main():
    # ハーネスが渡す JSON は UTF-8。既定の符号化で読むと非ASCII が化けて素通りする。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tool, command = read_command("--codex" in sys.argv)
    if tool is None:
        return
    if scans_root(command):
        search_guidance = (
            "ファイル名を探すなら git ls-files・rg --files、内容を探すなら rg を使い、"
            if "--codex" in sys.argv else
            "ファイル名を探すなら Glob ツール・git ls-files・rg --files、内容を探すなら Grep ツールを使い、"
        )
        reason = (
            "ドライブルート起点の再帰探索は禁止。"
            f"{search_guidance}探索範囲はリポジトリ配下に限ってください。"
            "PowerShell の Get-ChildItem -Recurse・python の os.walk・cd でルートへ移ってからの"
            "走査等、別の手段で同じ全走査を回避して実行しないこと。ルート直下だけを見たい場合は"
            "find に -maxdepth 2 以下を付けてください。"
        )
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))


def selftest():
    deny_cases = [
        ("ルート起点の find は拒否する", "find / -iname adoption.md"),
        ("パイプの前段のルート起点の find は拒否する", "find / -iname adoption.md | grep -v node_modules | head -20"),
        ("ドライブの自動マウント起点の find は拒否する", "find /c -name '*.md'"),
        ("cygdrive 起点の find は拒否する", "find /cygdrive/c -name x"),
        ("ドライブ文字起点の find は拒否する", "find 'C:/' -name x"),
        ("バックスラッシュのドライブ文字起点の find は拒否する", "find 'C:\\' -name x"),
        ("深い -maxdepth の find は拒否する", "find / -maxdepth 9 -name x"),
        ("空白を含むパスの find.exe は拒否する", '"C:/Program Files/Git/usr/bin/find.exe" / -iname adoption.md'),
        ("-H の後ろのルート起点は拒否する", "find -H / -name x"),
        ("-L の後ろのルート起点は拒否する", "find -L / -name x"),
        ("-P の後ろのルート起点は拒否する", "find -P / -name x"),
        ("-O の後ろのルート起点は拒否する", "find -O3 / -name x"),
        ("ルートを渡した rg は拒否する", "rg -uuu adoption /"),
        ("--files でパターンを取らない rg は拒否する", "rg --files /c"),
        ("ルートを渡した grep -r は拒否する", "grep -r foo /"),
        ("束ねた短フラグに -r を含む grep は拒否する", "grep -rn foo /d"),
        ("--recursive の grep は拒否する", "grep --recursive foo /"),
        ("大文字の -R の grep は拒否する", "grep -R foo /"),
        ("-e でパターンを渡した grep -r は拒否する", "grep -r -e foo /"),
        ("ルートを渡した ls -R は拒否する", "ls -R /"),
        ("ルートを渡した du は拒否する", "du -sh /"),
        ("ルートを渡した tree は拒否する", "tree /"),
        ("ルートを渡した fd は拒否する", "fd adoption /"),
        ("ルートへ移ってからの find . は拒否する", "cd / && find . -name adoption.md"),
        ("ルートへ移ってからの find ./ は拒否する", "cd / && find ./ -name x"),
        ("ルートへ移ってからの起点の無い find は拒否する", "cd / && find -name x"),
        ("オプション付きの cd でルートへ移ってからの find は拒否する", "cd -P / && find . -name x"),
        ("ドライブへ移ってからのパスの無い rg は拒否する", "cd /c && rg -uuu adoption"),
        ("ルートへ移ってからのパスの無い ripgrep は拒否する", "cd / && ripgrep foo"),
        ("ルートへ移ってからのパスの無い ag は拒否する", "cd / && ag foo"),
        ("ルートへ移ってからのパスの無い ack は拒否する", "cd / && ack foo"),
        ("ルートへ移ってからのパスの無い fd は拒否する", "cd / && fd foo"),
        ("ルートへ移ってからのパスの無い fdfind は拒否する", "cd / && fdfind foo"),
        ("ルートへ移ってからのパスの無い egrep -r は拒否する", "cd / && egrep -r foo"),
        ("ルートへ移ってからのパスの無い fgrep -r は拒否する", "cd / && fgrep -r foo"),
        ("ルートへ移ってから -e を重ねた grep -r は拒否する", "cd / && grep -r -e foo -e bar"),
        ("ルートへ移ってから --regexp を重ねた grep -r は拒否する", "cd / && grep -r --regexp foo --regexp bar"),
        ("ルートへ移ってから -f を重ねた grep -r は拒否する", "cd / && grep -r -f a -f b"),
        ("ルートへ移ってから --file を重ねた grep -r は拒否する", "cd / && grep -r --file a --file b"),
        ("ルートへ移ってからの du は拒否する", "cd / && du -sh"),
        ("改行の後のルート起点の find は拒否する", "echo start\nfind / -name x"),
        ("代入だけのセグメントの後のルート起点の find は拒否する", "X=1; find / -name x"),
        ("here-doc を伴うルート起点の find は拒否する", "find / -name x <<EOF\nx\nEOF"),
        ("timeout で起動するルート起点の find は拒否する", "timeout 60 find / -name x"),
        ("起動子のオプション値の後ろのルート起点の find は拒否する", "sudo -u x find / -name y"),
        ("find -exec で起動するルート起点の grep -r は拒否する", "find . -exec grep -r x / ;"),
    ]
    pass_cases = [
        ("ドライブ配下のディレクトリ起点の find は通す", "find /c/repo -name x"),
        ("-maxdepth 1 の find は通す", "find / -maxdepth 1 -name x"),
        ("境界値の -maxdepth 2 の find は通す", "find /c -maxdepth 2 -name x"),
        ("パスの無い rg は通す", "rg adoption"),
        ("リポジトリ配下の rg は通す", "rg -uuu adoption plugins/"),
        ("ルート直下のディレクトリの grep -r は通す", "grep -r foo /etc"),
        ("パターンとしての / は通す", "grep -r / plugins"),
        ("-e の値としての / は通す", "grep -e / -r plugins"),
        ("再帰しない長フラグの grep は通す", "grep --color foo /"),
        ("再帰しない ls は通す", "ls /"),
        ("逆順の ls -r は通す", "ls -r /"),
        ("起動子の後ろの再帰しない ls は通す", "env ls /"),
        ("引数としての find の言及は通す", "echo find / -iname x"),
        ("ルートでないディレクトリへ移ってからの find は通す", "cd /c/repo && find . -name x"),
        ("引数の無い cd の後の find は通す", "cd && find . -name x"),
        ("ルートへ移ってからの再帰しない ls は通す", "cd / && ls"),
        ("ルートへ移ってからの相対起点の find は通す", "cd / && find plugins -name x"),
        ("find の式の値としての / は通す", "find . -newer /"),
        ("here-doc 本文の find は通す", "cat <<EOF\nfind / -name x\nEOF"),
        ("解釈できない形は通す", "echo 'x"),
    ]
    ok = True
    for why, case in deny_cases:
        if not scans_root(case):
            ok = False
            print(f"FAIL {why}: 拒否されない :: {case!r}")
    for why, case in pass_cases:
        if scans_root(case):
            ok = False
            print(f"FAIL {why}: 拒否された :: {case!r}")
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    main()
