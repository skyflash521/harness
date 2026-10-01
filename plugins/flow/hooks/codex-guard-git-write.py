#!/usr/bin/env python3
"""Codex の PreToolUse フック: シェルから直接発行された履歴を書き換える操作を deny する。

Codex では、フックの入力が呼び出し元のエージェントを識別しない。そのためコミットは
scripts/codex_commit.py だけが成立させ、このフックはそれ以外の経路——シェルへ直接書いた
コミット・リセット・リベースなど——を拒否する。ファイルを指定するだけのステージは通す。

コマンド文字列をシェルの構文どおりに語へ分け、命令ごとに版管理コマンドの副コマンドを読む。引用符で
囲んだ値とエスケープした文字は語の一部なので、値に空白や区切り文字があっても副コマンドの位置と
混ざらない。シェルが文字列を命令として読む呼び出し(bash -c・pwsh -Command・cmd /c・eval)に
渡された語だけを、命令として読み直す。

使い方: Codex の PreToolUse フックとして登録する(matcher は Bash)。--selftest で自己テスト。
"""
import json
import re
import subprocess
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


def shell(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


DENIES = (
    ("コミット", "git commit -m x"),
    ("リセット", "git reset --hard"),
    ("リベース", "git rebase main"),
    ("スタッシュ", "git stash"),
    ("履歴の削除", "git rm a.txt"),
    ("-C を挟む", "git -C /repo commit -m x"),
    ("-c を挟む", "git -c user.name=a commit -m x"),
    ("空白を含む引用符付きの -C", 'git -C "C:\\Work Projects\\repo" commit -m x'),
    ("空白と & を含むディレクトリ名", 'git -C "Research & Development" commit -m x'),
    ("バックスラッシュのエスケープ", "git -C Research\\ \\&\\ Development commit -m x"),
    ("バッククォートのエスケープ", "git -C Research` Development commit -m x"),
    ("値を取る長いオプション", "git --git-dir /a/.git commit -m x"),
    ("単引用符の空白を含む値", "git --git-dir='/a b/.git' commit -m x"),
    ("二重引用符内のエスケープされた引用符", 'git -C "a\\"b" commit -m x'),
    ("拡張子付きの実行名", "git.exe commit -m x"),
    ("引用符付きの実行名", '"git" commit -m x'),
    ("パス付きの拡張子付き", '"C:/Program Files/Git/cmd/git.exe" commit -m x'),
    ("バックスラッシュのパス", "C:\\PF\\Git\\cmd\\git.exe commit -m x"),
    ("絶対パスの実行名", "/usr/bin/git commit -m x"),
    ("PowerShell の呼び出し演算子", '& "C:/Program Files/Git/cmd/git.exe" commit -m x'),
    ("連結", "git status && git commit -m x"),
    ("セミコロンの後", "git status; git commit -m x"),
    ("パイプの後", "echo x | git commit -F -"),
    ("改行の後", "git status\ngit commit -m x"),
    ("env を前置", "env GIT_TRACE=1 git commit -m x"),
    ("環境変数の代入を前置", "FOO=1 git commit -m x"),
    ("sudo を前置", "sudo git reset --hard"),
    ("前置きの後の絶対パス", "sudo /usr/bin/git commit -m x"),
    ("time を前置", "time git commit -m x"),
    ("xargs 経由", "echo a | xargs git rm"),
    ("then の後", "if true; then git commit -m x; fi"),
    ("do の後", "for f in a b; do git rm x; done"),
    ("find の実行オプション", "find . -name x -exec git rm {} +"),
    ("bash -c の入れ子", 'bash -c "git commit -m x"'),
    ("入れ子の中の2命令", 'bash -c "echo a; git commit -m x"'),
    ("pwsh -Command の入れ子", 'pwsh -Command "git commit -m x"'),
    ("単引用符の入れ子", "pwsh -Command 'git commit -m x'"),
    ("eval", 'eval "git commit -m x"'),
    ("拡張子付きのシェル", 'bash.exe -c "git reset --hard"'),
    ("絶対パスのシェル", '/bin/bash -c "git commit -m x"'),
    ("cmd /c の引用符なし", "cmd /c git commit -m x"),
    ("cmd /c の引用符つき", 'cmd /c "git commit -m x"'),
    ("構文として読めないコマンド", "git commit -m 'unbalanced"),
)
PASSES = (
    ("ファイルを指定するステージ", "git add -- a.txt"),
    ("状態の確認", "git status --short"),
    ("差分の確認", "git diff --cached"),
    ("ログ", "git log --oneline -1"),
    ("ログの検索", "git log --grep=commit"),
    ("引用符内の禁止語を含む検索", 'git log --grep="fix commit"'),
    ("引用符で囲んだ禁止語の引数", 'git diff -- "commit"'),
    ("ファイル名に禁止語を含む差分", "git diff commit-worker.md"),
    ("別の命令にだけ禁止語がある", "git status && echo commit"),
    ("区切り文字を含む -C の値での読み取り", 'git -C "a & b" status --short'),
    ("エスケープされた引用符を含む値での読み取り", 'git -C "a\\"b" status'),
    ("絶対パスの読み取り", "/usr/bin/git status --short"),
    ("専用経路の起動", "python3 plugins/flow/scripts/codex_commit.py --repo . --files a"),
    ("表示の引数", 'echo "git commit -m x"'),
    ("表示の空白なしの引数", "echo git commit"),
    ("検索の引数", 'rg "git commit" .'),
    ("検索の空白なしの引数", "rg git commit"),
    ("grep の引数", 'grep -r "git reset" docs'),
    ("絶対パスの別コマンドの引数", "/usr/bin/rg git commit"),
)


def selftest():
    failures = []
    for label, command in DENIES:
        if decide(shell(command)) is None:
            failures.append(f"deny するはずが通した: {label}")
    for label, command in PASSES:
        if decide(shell(command)) is not None:
            failures.append(f"通すはずが deny: {label}")
    others = (
        ("シェル以外のツール", {"tool_name": "apply_patch", "tool_input": {"command": "git commit"}}),
        ("辞書でない入力", []),
        ("tool_input が無い", {"tool_name": "Bash"}),
        ("command が文字列でない", {"tool_name": "Bash", "tool_input": {"command": 1}}),
    )
    for label, data in others:
        if decide(data) is not None:
            failures.append(f"通すはずが deny: {label}")
    payload = json.dumps(shell(DENIES[0][1]), ensure_ascii=False).encode("utf-8")
    result = subprocess.run([sys.executable, __file__], input=payload, stdout=subprocess.PIPE, check=False)
    try:
        decision = json.loads(result.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"]
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
        decision = None
    if decision != "deny":
        failures.append("ハーネスと同じ形の入力で deny が出ない")
    quiet = subprocess.run(
        [sys.executable, __file__], input=json.dumps(shell(PASSES[1][1])).encode("utf-8"),
        stdout=subprocess.PIPE, check=False)
    if quiet.stdout.strip():
        failures.append("通す入力で標準出力に何か出た")
    for failure in failures:
        print(f"FAIL {failure}")
    print("ALL PASS" if not failures else "SOME FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
