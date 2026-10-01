#!/usr/bin/env python3
"""Codex の PreToolUse フック: シェルから直接発行された履歴を書き換える操作を deny する。

Codex では、フックの入力が呼び出し元のエージェントを識別しない。そのためコミットは
scripts/codex_commit.py だけが成立させ、このフックはそれ以外の経路——シェルへ直接書いた
コミット・リセット・リベースなど——を拒否する。ファイルを指定するだけのステージは通す。

コマンド文字列をシェルの構文どおりに語へ分け、命令ごとに版管理コマンドの副コマンドを読む。引用符で
囲んだ値とエスケープした文字は語の一部なので、値に空白や区切り文字があっても副コマンドの位置と
混ざらない。シェルが文字列を命令として読む呼び出し(bash -c・pwsh -Command・cmd /c・eval)に
渡された語だけを、命令として読み直す。

使い方: Codex の PreToolUse フックとして登録する(matcher は Bash)。
"""
import json
import re
import sys

BLOCKED = frozenset((
    "commit", "reset", "rebase", "cherry-pick", "revert", "am", "restore", "clean", "stash", "rm",
    "update-ref", "filter-branch",
))
OPTIONS_WITH_VALUE = frozenset(("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"))
SHELLS = frozenset((
    "bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd", "eval",
))
WRAPPERS = frozenset((
    "env", "sudo", "xargs", "nohup", "command", "exec", "builtin", "nice", "timeout", "time", "stdbuf",
    "call", "start",
))
KEYWORDS = frozenset(("if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"))
EXEC_OPTIONS = frozenset(("-exec", "-execdir", "-ok", "-okdir"))
ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
SWITCH = re.compile(r"/[A-Za-z]")
OPERATORS = frozenset(";&|<>()\n")
QUOTES = "\"'"
BLANKS = " \t\r"
ESCAPES = "\\`"
MAX_DEPTH = 3
GUIDANCE = (
    "コミットは scripts/codex_commit.py だけが成立させる。レビューの結末と対象範囲を検査してから"
    "コミットするので、flow:commit のコミットワーカーの手順に従うこと。"
)


def words(text):
    """(語, 引用符で囲まれていたか)の並びに分ける。区切り文字は単独の語になる。

    バックスラッシュとバッククォートは、空白・区切り文字・引用符の直前にあるときだけ次の1文字を
    語の一部にするエスケープとして読む。それ以外は、パスの区切りとして文字どおりに読む。
    """
    tokens = []
    current = []
    quote = None
    quoted = False

    def flush():
        nonlocal current, quoted
        if current or quoted:
            tokens.append(("".join(current), quoted))
        current, quoted = [], False

    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if quote:
            if char == "\\" and following == quote:
                current.append(following)
                index += 1
            elif char == quote:
                quote = None
            else:
                current.append(char)
        elif char in ESCAPES and following and following in BLANKS + "".join(OPERATORS) + QUOTES + ESCAPES:
            current.append(following)
            index += 1
        elif char in QUOTES:
            quote, quoted = char, True
        elif char in BLANKS:
            flush()
        elif char in OPERATORS:
            flush()
            tokens.append((char, False))
        else:
            current.append(char)
        index += 1
    if quote:
        raise ValueError("引用符が閉じていない")
    flush()
    return tokens


def program(word):
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def blocked_subcommand(text, depth=0):
    """命令の中の履歴を書き換える副コマンドの名前を返す。無ければ None。"""
    try:
        tokens = words(text)
    except ValueError:
        return loose_match(text)
    segment = []
    for word, quoted in tokens + [("\n", False)]:
        if not quoted and word in OPERATORS:
            found = scan_segment(segment, depth)
            if found:
                return found
            segment = []
        else:
            segment.append(word)
    return None


def skippable(word):
    """命令の頭に置かれても、実行する命令を変えない語。"""
    return (word in KEYWORDS or program(word) in WRAPPERS or word.startswith("-")
            or SWITCH.fullmatch(word) is not None or word.isdigit()
            or ASSIGNMENT.match(word) is not None)


def command_positions(segment):
    """命令として実行される語の位置。先頭と、前置きの語・実行オプションの直後を数える。"""
    expecting = True
    for index, word in enumerate(segment):
        if expecting:
            if skippable(word):
                continue
            expecting = False
            yield index
        elif word in EXEC_OPTIONS:
            expecting = True


def scan_segment(segment, depth):
    for index in command_positions(segment):
        name = program(segment[index])
        if name == "git":
            position = index + 1
            while position < len(segment):
                if segment[position] in OPTIONS_WITH_VALUE:
                    position += 2
                elif segment[position].startswith("-"):
                    position += 1
                else:
                    break
            if position < len(segment) and segment[position] in BLOCKED:
                return segment[position]
        elif name in SHELLS and depth < MAX_DEPTH:
            for word in segment[index + 1:]:
                found = blocked_subcommand(word, depth + 1) if " " in word else None
                if found:
                    return found
            found = scan_segment(segment[index + 1:], depth + 1)
            if found:
                return found
    return None


def loose_match(text):
    """構文として読めないコマンドは、版管理コマンドと禁止する副コマンドの語が共にあれば拒否する。"""
    if re.search(r"\bgit\b", text, re.IGNORECASE):
        for word in sorted(BLOCKED):
            if re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", text):
                return word
    return None


def decide(data):
    """deny する理由を返す。対象でなければ None。"""
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return None
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None
    found = blocked_subcommand(command)
    if found is None:
        return None
    return f"[codex-guard-git-write] git {found} をシェルから直接発行できない。" + GUIDANCE


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    reason = decide(data)
    if reason is None:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    main()
