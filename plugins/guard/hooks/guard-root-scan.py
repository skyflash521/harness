#!/usr/bin/env python3
"""PreToolUse hook: deny ドライブルート起点の再帰探索(`find /` 等)。

Git Bash の `/` は MSYS ルートで、`/c` `/d` … として全ドライブが自動マウントされる。そこを起点に
再帰探索を始めると、走査対象に全ドライブが入る。実測ではこの形の探索が数時間経っても終わらず、
CPU を1コア占有し続けた(所要時間がそこまで伸びた理由は未検証)。`| head -N` はヒットが N 件に届く
場合しか止められない。さらに親のシェルが終了しても Windows は子孫を巻き込まないため、走り続ける
プロセスだけが孤児として残る。

そこで、走査コマンドにルート相当のパスが渡された呼び出しを deny し、走査対象がリポジトリの
管理下に絞られる代替(Glob/Grep ツール・`git ls-files`・`rg --files`)へ誘導する。
判定はコマンドのセグメントごとに「走査コマンドか」「そのパスオペランドがルート相当か」で行う。
`cd /` でルートへ移ってから暗黙のカレントを走査する形も同じ暴走なので併せて見る。

浅い深さで区切れば走査量が小さく収まり実用的な時間で終わるため、`find` に浅い `-maxdepth` が
あるときは通す。これが明示的な迂回手段であり、ルート直下を確かめたいだけの正当な用途はこれで足りる。

Usage: configured as a Bash PreToolUse hook. Run with --selftest.
"""
import json
import re
import shlex
import sys

# 常に再帰する走査コマンド。
ALWAYS_RECURSIVE = {"find", "rg", "ripgrep", "fd", "fdfind", "ag", "ack", "tree", "du"}
# 再帰フラグが付いたときだけ走査になるコマンドと、その再帰フラグの短縮形。
# ls の `-r` は逆順であって再帰ではないので、大文字だけを見る。
RECURSIVE_LETTERS = {"grep": "rR", "egrep": "rR", "fgrep": "rR", "ls": "R"}
# 第1オペランドがパスでなく検索パターンであるコマンド。
PATTERN_FIRST = {"grep", "egrep", "fgrep", "rg", "ripgrep", "ag", "ack", "fd", "fdfind"}
# 引数を1つ取り、その引数がパスではないフラグ。
VALUE_FLAGS = {"-e", "--regexp", "-f", "--file"}
# これらがあるとパターンはフラグ側で与えられており、第1オペランドはパスになる。
PATTERN_BY_FLAG = VALUE_FLAGS | {"--files"}
# ルート相当のパス: `/`・`/c`(ドライブの自動マウント)・`/cygdrive/c`・`C:`。
ROOT_RE = re.compile(r"(?:/(?:[a-z]|cygdrive/[a-z])?|[a-z]:)/*", re.IGNORECASE)
# 走査量が小さく収まるとみなす深さ。
MAX_BOUNDED_DEPTH = 2


def is_root_path(token):
    """トークンがドライブルート相当のパスか。"""
    return bool(ROOT_RE.fullmatch(token.replace("\\", "/")))


def head_name(token):
    """コマンド名の比較用の形。パス修飾と `.exe` を落とす。"""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def segments(command):
    """コマンドを `;` `|` `&&` 等で区切ったセグメントのトークン列に分ける。解釈不能なら None。"""
    if "<<" in command:  # here-doc 本文は安全に切り出せないので対象外
        return None
    lexer = shlex.shlex(command.replace("\n", "\n;"), posix=True, punctuation_chars=";()<>|&")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    result, current = [], []
    for token in tokens:
        if token[:1] in ";|&<>(){}":
            if current:
                result.append(current)
            current = []
        else:
            current.append(token)
    if current:
        result.append(current)
    return result


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
            continue  # 起点より前に置くオプション
        if arg.startswith("-"):
            break  # ここから式
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
            index += 2  # フラグ + パスではないその引数
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
    at_root = False  # 直前までの cd でルートへ移っているか
    for tokens in parsed:
        name = head_name(tokens[0])
        args = tokens[1:]
        if name == "cd":
            targets = [arg for arg in args if not arg.startswith("-")]
            at_root = bool(targets) and is_root_path(targets[0])
            continue
        if name not in ALWAYS_RECURSIVE and not (
            name in RECURSIVE_LETTERS and has_recursive_flag(name, args)
        ):
            continue
        if name == "find" and has_bounded_depth(args):
            continue
        operands = path_operands(name, args)
        if any(is_root_path(operand) for operand in operands):
            return True
        # cd でルートへ移った後の、カレント起点(明示・暗黙とも)の走査も同じ暴走になる。
        if at_root and all(operand.rstrip("/") in ("", ".") for operand in operands):
            return True
    return False


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return
    if data.get("tool_name") != "Bash":
        return
    command = (data.get("tool_input") or {}).get("command") or ""
    if scans_root(command):
        reason = (
            "ドライブルート起点の再帰探索は禁止。Git Bash の / は全ドライブを自動マウントするため"
            "走査対象に全ドライブが入り、実測ではこの形が数時間経っても終わりませんでした。親が"
            "終了しても子は回収されず、CPU を1コア占有し続けます。"
            "ファイル名を探すなら Glob ツール・git ls-files・rg --files、"
            "内容を探すなら Grep ツールを使い、探索範囲はリポジトリ配下に限ってください。"
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
        "find / -iname adoption.md",
        "find / -iname adoption.md | grep -v node_modules | head -20",
        "find /c -name '*.md'",
        "find /cygdrive/c -name x",
        "find 'C:/' -name x",
        "find / -maxdepth 9 -name x",
        '"C:/Program Files/Git/usr/bin/find.exe" / -iname adoption.md',
        "rg -uuu adoption /",
        "rg --files /c",
        "grep -r foo /",
        "grep -rn foo /d",
        "ls -R /",
        "du -sh /",
        "tree /",
        "fd adoption /",
        "cd / && find . -name adoption.md",
        "cd /c && rg -uuu adoption",
        "cd / && find -name x",
        "cd / && du -sh",
        "echo start\nfind / -name x",
    ]
    pass_cases = [
        "find . -name '*.md'",
        "find /usr/share -name x",
        "find /c/repo -name x",
        "find / -maxdepth 1 -name x",
        "find /c -maxdepth 2 -name x",
        "rg adoption",
        "rg -uuu adoption plugins/",
        "grep -r foo /etc",
        "grep -r / plugins",
        "grep -e / -r plugins",
        "ls /",
        "ls -la /c",
        "ls -r /",
        "echo find / -iname x",
        "cd /c/repo && find . -name x",
        "cd / && ls",
        "cd / && find plugins -name x",
        "find . -newer /",
        "cat <<EOF\nfind / -name x\nEOF",
        "git ls-files",
    ]
    ok = True
    for case in deny_cases:
        if not scans_root(case):
            ok = False
            print("FAIL expected deny:", repr(case))
    for case in pass_cases:
        if scans_root(case):
            ok = False
            print("FAIL expected pass:", repr(case))
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    main()
