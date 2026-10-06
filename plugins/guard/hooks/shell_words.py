"""guard の PreToolUse フックが共有するシェル語の分割モジュール。"""

import shlex

BOUNDARY_CHARS = ";|&<>(){}"


def head_name(token):
    """コマンド名の比較用の形。パス修飾と `.exe` を落として小文字にする。"""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def segments(command, skip_heredoc=True):
    """コマンドを `;` `|` `&&` 等で区切ったセグメントのトークン列に分ける。解釈不能なら None。

    skip_heredoc が真なら here-doc を含むコマンドは安全に切り出せないので None を返す。
    """
    if skip_heredoc and "<<" in command:
        return None
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
