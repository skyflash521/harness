"""guard の PreToolUse フックが共有するシェル語の分割モジュール。"""

import re
import shlex

BOUNDARY_CHARS = ";|&<>(){}"
HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "time"}
LAUNCHERS = {
    "env", "sudo", "doas", "xargs", "nohup", "command", "exec", "builtin", "nice", "timeout", "stdbuf",
}
EXEC_OPTIONS = {"-exec", "-execdir", "-ok", "-okdir"}
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")


def head_name(token):
    """コマンド名の比較用の形。パス修飾と `.exe` を落として小文字にする。"""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def command_positions(tokens):
    """命令として実行されうる語の位置。

    先頭と実行オプションの直後を数える。起動子の後ろはオプションの値と命令を見分けず、全ての語を数える。
    """
    expecting, launched = True, False
    for index, token in enumerate(tokens):
        if expecting or launched:
            if head_name(token) in LAUNCHERS:
                launched = True
            elif not (token in KEYWORDS or token.startswith("-") or token.isdigit()
                      or ASSIGNMENT.match(token)):
                expecting = False
                yield index
        elif token in EXEC_OPTIONS:
            expecting = True


def strip_heredocs(command):
    """here-doc の開始記号と本文を落としたコマンド。引用の内側の `<<` は開始記号と見ない。"""
    output, pending = [], []
    for line in command.splitlines(keepends=True):
        if pending:
            delimiter, tabs = pending[0]
            if (line.lstrip("\t") if tabs else line).rstrip("\r\n") == delimiter:
                pending.pop(0)
            continue
        matches = []
        for match in HEREDOC.finditer(line):
            try:
                shlex.split(line[:match.start()])
            except ValueError:
                continue
            matches.append(match)
        pending.extend((match.group(3), bool(match.group(1))) for match in matches)
        for match in reversed(matches):
            line = line[:match.start()] + line[match.end():]
        output.append(line)
    return "".join(output)


def segments(command):
    """here-doc の本文を落とし、`;` `|` `&&` 等で区切ったセグメントのトークン列に分ける。解釈不能なら None。"""
    command = strip_heredocs(command)
    lexer = shlex.shlex(command.replace("\n", "\n;"), posix=True, punctuation_chars=";()<>|&")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    result, current = [], []
    for token in tokens:
        if token[:1] in BOUNDARY_CHARS:
            if current:
                result.append(current)
            current = []
        else:
            current.append(token)
    if current:
        result.append(current)
    return result
