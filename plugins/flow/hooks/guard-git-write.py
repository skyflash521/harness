#!/usr/bin/env python3
"""PreToolUse フック: コミットスキルが発する git の書き込みを検査する。

無言で通るのは次の2つだけ。

    git add -- <明示したファイル>...
    git commit -m <メッセージ>          (件名に日本語が1文字以上あること)

件名が ASCII だけの形は deny し、日本語で起草し直させる。起草されたメッセージではありえない件名も
deny する——使い捨ての語だけの件名と、コマンド自身の先頭が引数へ紛れ込んだ件名。メッセージの行構造も
検査する。

これら以外の add/commit は deny するので、エージェントはユーザーへ許可を求めず通常の形で出し直す。
add/commit と認識できない形(フックが起動子として知らない語や bash -c の中の git)はここを素通りするが、
git add・git commit で始まらないので設定の許可 glob にも一致せず、許可プロンプトが出るだけで自動
実行はされない。無関係なコマンドは通る。

git reset はどの形も deny する。インデックスと HEAD を書き換え、安全に許せる変種が無いため。

整った git add も、そのコマンドが名指ししていないファイルが既にステージされていれば deny する。
インデックスは作業ツリー単位の共有状態で git commit はその全体を取るので、他のセッションが使って
いるインデックスへステージすると、相手の分を自分のコミットへ巻き込むか、自分の分を相手へ持って
行かれる——書いた側からは見えない。コミットが取るステージ集合は、意図して名指ししたものだけでなければ
ならない。

使い方: Bash の PreToolUse フックとして登録する。--selftest で自己テスト。
"""

import json
import os
import re
import shlex
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

CONTROL_CHARS = ";&|<>\n"
GLOB_CHARS = "*?[]{}"
JAPANESE_CHAR = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")
TARGET_WORD = re.compile(r"(?<![\w-])(add|commit)(?![\w-])")
ESCAPE_NEWLINE = "\\n"
TRAILER_PREFIX = "co-authored-by:"
TRAILER_SHAPE = re.compile(
    r"co-authored-by:\s*claude\s+[^<>\s][^<>]*\s<noreply@anthropic\.com>", re.IGNORECASE
)
PLACEHOLDER_SUBJECT = re.compile(
    r"(?:テスト|てすと|ﾃｽﾄ|試験|試行|試し|動作確認|ダミー|だみー|サンプル|仮|あ+)"
    r"(?:行|番|目|文|コミット|メッセージ)?"
)
# Python の \w は日本語を含むので、\W は約物だけを残す。
SUBJECT_FILLER = re.compile(r"[\s\d\W_]+")
LEAKED_COMMAND_PREFIX = re.compile(r"^(?:git\s*commit\b|git(?=[^\x00-\x7f])|-m\b)", re.IGNORECASE)
# Claude Code は許可リスト照合の前にこれらの一部(2.1.x では env・time・timeout・nice・nohup・stdbuf)を剥がす。
# 剥がされる語がこの集合から漏れると、内側の add/commit が許可 glob に一致して自動実行される。
WRAPPERS = {
    "command", "builtin", "env", "exec", "time", "timeout", "nice", "ionice",
    "nohup", "setsid", "stdbuf", "xargs", "sudo", "doas", "taskset", "chrt",
    "!", "coproc",
}
# シェルの制御演算子・cd・サブシェルの開き括弧の後は、次の git が新しいコマンドの先頭になる。
CONTROL_OR_WRAPPER = {"cd", "&&", "||", ";", "|", "&", "("}
GIT_VALUE_OPTIONS = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace",
    "--super-prefix", "--exec-path", "--config-env",
}
REPOSITION_OPTIONS = {"-C", "--git-dir", "--work-tree"}
# diff・log・show は除く。対象リポジトリの設定が指す textconv・ext-diff・gpg を無フラグで起動しうる。
REPOSITION_READONLY = {
    "status", "blame", "grep", "cat-file", "ls-files", "ls-tree",
    "rev-parse", "rev-list", "merge-base", "describe", "shortlog",
}
# grep・cat-file の --textconv と --filters は外部ドライバを起動する。git は一意な短縮形も受け付ける。
REPOSITION_UNSAFE_LONG = ("--open-files-in-pager", "--filters", "--textconv")


def _is_unsafe_reposition_arg(arg):
    """読み取り専用のサブコマンドでも外部ヘルパを起動する引数か。"""
    if arg.startswith("-O"):
        return True
    name = arg.split("=", 1)[0]
    return name.startswith("--") and len(name) >= 3 and any(
        long_opt.startswith(name) for long_opt in REPOSITION_UNSAFE_LONG
    )


def _has_shell_syntax(text):
    """引用符の外に制御演算子か展開があるか。"""
    single = double = False
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and not single and i + 1 < len(text):
            i += 2
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif not single and char in "$`":
            return True
        elif not single and not double and char in CONTROL_CHARS:
            return True
        i += 1
    return single or double


def _tokens(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _is_git(token):
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in {"git", "git.exe"}


def _is_plain_git(token):
    return token == "git"


def _mentions_target(tokens):
    """素の形でない add/commit の呼び出しを、安全側に倒して見分ける。"""
    for index, token in enumerate(tokens):
        if not _is_git(token):
            continue
        pos = index + 1
        while pos < len(tokens) and tokens[pos].startswith("-"):
            option = tokens[pos].split("=", 1)[0]
            pos += 1
            if option in GIT_VALUE_OPTIONS and "=" not in tokens[pos - 1]:
                pos += 1
        if pos >= len(tokens):
            continue
        subcommand = tokens[pos]
        if "$" in subcommand or "`" in subcommand:
            return True
        if subcommand not in {"add", "commit"}:
            continue
        prefix = tokens[:index]
        if (
            index == 0
            or not _is_plain_git(token)
            or any(item in WRAPPERS or item in CONTROL_OR_WRAPPER for item in prefix)
            or any(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", item) for item in prefix)
        ):
            return True
    return False


def _mentions_reset(tokens):
    """git が実際に reset を走らせる形か。echo の引数のような言及は含めない。"""
    for index, token in enumerate(tokens):
        if not _is_git(token):
            continue
        pos = index + 1
        while pos < len(tokens) and tokens[pos].startswith("-"):
            option = tokens[pos].split("=", 1)[0]
            pos += 1
            if option in GIT_VALUE_OPTIONS and "=" not in tokens[pos - 1]:
                pos += 1
        if pos >= len(tokens):
            continue
        if tokens[pos] != "reset":
            continue
        prefix = tokens[:index]
        if (
            index == 0
            or not _is_plain_git(token)
            or any(item in WRAPPERS or item in CONTROL_OR_WRAPPER for item in prefix)
            or any(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", item) for item in prefix)
        ):
            return True
    return False


def _mentions_repositioned_git(command, tokens):
    """別のリポジトリへ移した git の呼び出しのうち、読み取り専用と示せないもの。

    読み取り専用としての除外が効くのは、コマンド全体にシェル構文も括弧も無いときだけ。
    除外に外れた再配置は、呼び出しへの到達可能性を調べずに deny する。
    """
    single_invocation = not _has_shell_syntax(command) and "(" not in tokens and ")" not in tokens
    for index, token in enumerate(tokens):
        if not _is_git(token):
            continue
        repositioned = False
        pos = index + 1
        while pos < len(tokens) and tokens[pos].startswith("-"):
            option = tokens[pos].split("=", 1)[0]
            if option in REPOSITION_OPTIONS or option.startswith("-C"):
                repositioned = True
            pos += 1
            if option in GIT_VALUE_OPTIONS and "=" not in tokens[pos - 1]:
                pos += 1
        if not repositioned:
            continue
        if single_invocation and pos < len(tokens) and tokens[pos] in REPOSITION_READONLY:
            rest = tokens[pos + 1:]
            if not any(_is_unsafe_reposition_arg(arg) for arg in rest):
                continue
        return True
    return False


def _tracked_file(root, path):
    """パスが追跡下の1ファイルを正確に指すか。削除済みの登録も含む。"""
    relative = path.relative_to(root).as_posix()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--error-unmatch", "--", relative],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    # git はパスを UTF-8 バイトで出す。text=True はロケールの符号化で壊す。
    entries = [entry for entry in result.stdout.split(b"\0") if entry]
    return result.returncode == 0 and entries == [relative.encode("utf-8")]


def _safe_add(args, root):
    """安全な add が名指ししたパスをリポジトリ相対の POSIX で返す。安全でなければ None。

    シンボリックリンクや、インデックスと大文字小文字の異なる綴りでは、返す綴りがインデックスの持つ
    綴りと一致しない。呼び出し側の比較でステージ項目が名指しされていない側へ落ち、その add は止まる。
    """
    if len(args) < 2 or args[0] != "--":
        return None
    root = Path(root).resolve()
    named = []
    for raw_path in args[1:]:
        if (
            not raw_path
            or Path(raw_path).is_absolute()
            or raw_path.startswith(("~", ":"))
            or any(char in raw_path for char in GLOB_CHARS)
        ):
            return None
        path = (root / raw_path).resolve(strict=False)
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            return None
        if path.is_symlink() or path.is_file():
            named.append(relative)
            continue
        if path.is_dir() or not _tracked_file(root, path):
            return None
        named.append(relative)
    return named


def _staged_paths(root):
    """インデックスに現在ステージされている、名指しできるパス。git が答えられなければ None。

    ステージ済みの削除は含めない——git add はインデックスがもう持たないパスを名指しできない。
    None のときは検査を飛ばす。既に許されていた形を、git が答えられないことで塞がないため。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--cached", "--name-only", "--diff-filter=d", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    # git はパスを UTF-8 で出す。text=True はロケールの符号化で復号してしまう。
    return [entry.decode("utf-8", "replace") for entry in result.stdout.split(b"\0") if entry]


def _unnamed_staged(named, staged):
    """この add が名指ししていないステージ済みパス。staged が None なら空。"""
    if staged is None:
        return []
    already = set(named)
    return sorted(path for path in staged if path not in already)


def _shared_index_reason(unnamed):
    listed = ", ".join(unnamed[:10]) + (", ..." if len(unnamed) > 10 else "")
    return (
        f"The index already holds staged paths this command does not name: {listed}. The index is "
        "one shared state per worktree and git commit takes all of it, so these entries would ride "
        "into your commit, and what you stage now can be swept into a commit made by whoever "
        "staged them. Do not retry until you know where they came from: run git status, and if "
        "they are not yours, stop and tell the user (another session may be working in this "
        "worktree) instead of committing or unstaging them. If they do belong in this commit, "
        "name every intended file in one git add -- <files>."
    )


def _safe_commit(args):
    return len(args) == 2 and args[0] == "-m" and bool(args[1].strip())


def _probe_subject_problem(subject):
    """件名が使い捨ての語だけなら deny 理由を返す。そうでなければ None。

    語彙による判定なので、別の言い回しで書かれた試行コミットはここでは捕まえない。
    """
    core = SUBJECT_FILLER.sub("", subject)
    if not core or not PLACEHOLDER_SUBJECT.fullmatch(core):
        return None
    return (
        "Commit subject is a placeholder, so this reads as a commit issued to try out the command "
        "form rather than to record the drafted message. With changes staged, every git commit is "
        "a real commit, and neither --amend nor git reset can repair the history it leaves. Do not "
        "probe with a commit: re-issue the message drafted from the diff, or stop and report the "
        "deny reason you cannot get past."
    )


def _message_format_problem(message):
    """メッセージの行構造かトレーラが規約から外れていれば deny 理由を返す。そうでなければ None。

    トレーラが無いメッセージも deny する。トレーラを消すことが他の deny の抜け道にならないように。
    """
    if ESCAPE_NEWLINE in message:
        return (
            "Commit message contains escape notation (backslash n). Inside the single-quoted -m "
            "argument a backslash is literal, so those two characters are committed as text "
            "instead of breaking the line. Re-issue the SAME message with real line breaks; do not "
            "shorten it or drop the body or the Co-Authored-By trailer to get past this. To "
            "describe the escape sequence in the prose itself, spell it out in words."
        )
    lines = [line for line in message.split("\n") if line.strip()]
    if len(lines) >= 2 and lines[-1].lower().startswith(TRAILER_PREFIX):
        if TRAILER_SHAPE.fullmatch(lines[-1].rstrip()):
            return None
        return (
            "Co-Authored-By trailer is malformed. Re-issue the SAME message with the trailer "
            "written as the token Co-Authored-By: followed by Claude and then your own model name "
            "as plain text, and then the address noreply@anthropic.com in angle brackets. Every "
            "part is required: keep the Claude prefix and the model name, and do not put the model "
            "name in angle brackets or drop the address."
        )
    if TRAILER_PREFIX in message.lower():
        return (
            "The Co-Authored-By trailer must be the last non-empty line, must start that line, and "
            "must sit below the subject. Here it does not: either the drafted line breaks were "
            "lost, or the trailer is not at the end. Re-issue the SAME message with real line "
            "breaks and the trailer as its final line."
        )
    return (
        "Commit message must end with a Co-Authored-By trailer on its own line, written as the "
        "token Co-Authored-By: followed by Claude and then your own model name as plain text, and "
        "then the address noreply@anthropic.com in angle brackets. Add it -- dropping it is not a "
        "way past another denial."
    )


def classify(command, root=None, staged=None):
    """("deny", 理由) か ("pass", None) を返す。

    staged はインデックスの現在のステージ一覧。None なら共有インデックス検査を飛ばす。
    """
    if not isinstance(command, str) or not command.strip():
        return "pass", None
    try:
        tokens = _tokens(command)
    except ValueError:
        if re.search(r"\bgit\b", command) and re.search(r"\breset\b", command):
            return "deny", (
                "git reset is denied; ask the user to add an allow rule if truly needed. Do not "
                "work around this by using a plumbing equivalent (update-ref, symbolic-ref, "
                "checkout-index, or editing .git/ directly) or any other command."
            )
        if re.search(r"\bgit\b", command) and TARGET_WORD.search(command):
            return "deny", "Unparseable git add/commit; retry with the regular form"
        return "pass", None

    if _mentions_reset(tokens):
        return "deny", (
            "git reset is denied; ask the user to add an allow rule if truly needed. Do not work "
            "around this by using a plumbing equivalent (update-ref, symbolic-ref, checkout-index, "
            "or editing .git/ directly) or any other command."
        )

    if _mentions_repositioned_git(command, tokens):
        return "deny", (
            "Repositioned git (-C/--git-dir/--work-tree) is allowed only for read-only "
            "subcommands. Do not work around this by cd-ing into that other repository (or any "
            "other method) to run the write command there instead -- run write commands only in "
            "THIS repository's cwd."
        )

    plain_target = (
        len(tokens) >= 2
        and _is_plain_git(tokens[0])
        and tokens[1] in {"add", "commit"}
    )
    if not plain_target:
        if _mentions_target(tokens):
            return "deny", "Use plain git add/commit from the repository cwd"
        return "pass", None
    if _has_shell_syntax(command):
        # ANSI-C クォート($'...')はドル記号で deny され、外すとエスケープが文字列として残る。
        if "$'" in command:
            return "deny", (
                "ANSI-C quoting ($'...') is not allowed: the $ makes this an expansion. Pass the "
                "message in a plain single-quoted -m argument holding real line breaks. Do NOT fix "
                "this by deleting the $ and keeping the \\n escapes inside the quotes -- that "
                "commits the two characters as text."
            )
        return "deny", "Do not combine or expand git add/commit commands"

    args = tokens[2:]
    root = root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if tokens[1] == "commit":
        if not _safe_commit(args):
            return "deny", "Retry with git commit -m <message>"
        subject = args[1].split("\n", 1)[0]
        if not JAPANESE_CHAR.search(subject):
            return "deny", (
                "Commit subject must be Japanese (repo convention): the first line has no "
                "Japanese character. Redraft the subject in Japanese and retry."
            )
        problem = _probe_subject_problem(subject)
        if problem:
            return "deny", problem
        if LEAKED_COMMAND_PREFIX.match(subject):
            return "deny", (
                "Commit subject opens with the commit command itself, so the -m argument picked up "
                "the front of the command while it was assembled. Re-issue the subject you "
                "drafted, without the command text. Compare the argument against the draft BEFORE "
                "running it -- once the commit exists neither --amend nor git reset can repair it."
            )
        problem = _message_format_problem(args[1])
        if problem:
            return "deny", problem
        return "pass", None
    named = _safe_add(args, root)
    if named is None:
        return "deny", "Retry with git add -- <explicit-file>..."
    unnamed = _unnamed_staged(named, staged)
    if unnamed:
        return "deny", _shared_index_reason(unnamed)
    return "pass", None


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    # ハーネスが渡す JSON は UTF-8。既定の符号化で読むと非ASCII が化けて素通りする。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if data.get("tool_name") != "Bash":
        return
    command = (data.get("tool_input") or {}).get("command")
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    could_be_add = isinstance(command, str) and "git" in command and "add" in command
    decision, reason = classify(command, root, _staged_paths(root) if could_be_add else None)
    if decision == "deny":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))


def selftest():
    TRAILER = "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
    CaseGroup = namedtuple("CaseGroup", "why cases")
    case_groups = (
        CaseGroup("許可する形と、件名の言語による deny", [
            ("git add -- plugins/flow/hooks/guard-git-write.py", "pass"),
            ("git add plugins/flow/hooks/guard-git-write.py", "deny"),
            ("git add -- plugins/flow/hooks", "deny"),
            ("git add -- ../outside.py", "deny"),
            ("git add -- C:/outside.py", "deny"),
            ("git add -- missing-file.py", "deny"),
            (f"git commit -m '件名\n\n本文 $5 `literal` > text\n\n{TRAILER}'", "pass"),
            (f"git commit -m 'it'\\''s 修正済み\n\n{TRAILER}'", "pass"),
            (f"git commit -m 'ガード追加\n\nadd english body\n\n{TRAILER}'", "pass"),
            (f"git commit -m 'Add CHANGELOG validation\n\n{TRAILER}'", "deny"),
            (f"git commit -m 'English subject\n\n日本語本文\n\n{TRAILER}'", "deny"),
            (f"git commit -m 'English subject、with a stray comma\n\n日本語本文\n\n{TRAILER}'", "deny"),
            (f"git commit -m 'v0.2.0\n\n{TRAILER}'", "deny"),
        ]),
        CaseGroup("エスケープ表記で1行へ詰めたメッセージと、部分的に潰れた形", [
            (f"git commit -m '件名\\n\\n本文\\n\\n{TRAILER}'", "deny"),
            (f"git commit -m '件名\\n\\n本文\n\n{TRAILER}'", "deny"),
            (f"git commit -m '件名\n\n本文\\n\\n{TRAILER}'", "deny"),
        ]),
        CaseGroup("使い捨ての語だけの件名(コマンドの形を試すコミット)", [
            (f"git commit -m 'テスト行1\n\nテスト行2\n\n{TRAILER}'", "deny"),
            (f"git commit -m 'テスト\n\n{TRAILER}'", "deny"),
            (f"git commit -m 'ダミー2\n\n{TRAILER}'", "deny"),
            (f"git commit -m 'あああ\n\n{TRAILER}'", "deny"),
            (f"git commit -m '仮\n\n{TRAILER}'", "deny"),
            (f"git commit -m 'テストコミット\n\n{TRAILER}'", "deny"),
            (f"git commit -m '試行\n\n{TRAILER}'", "deny"),
            (f"git commit -m 'テストを追加\n\n{TRAILER}'", "pass"),
            (f"git commit -m '試行コミットの禁止を追加\n\n{TRAILER}'", "pass"),
        ]),
        CaseGroup("コマンド自身の先頭が引数へ紛れ込んだ件名", [
            (f"git commit -m 'git試行コミットの禁止を追加\n\n{TRAILER}'", "deny"),
            (f"git commit -m 'git commit 疎化の区間分割を見直す\n\n{TRAILER}'", "deny"),
            (f"git commit -m '-m 疎化の区間分割を見直す\n\n{TRAILER}'", "deny"),
            (f"git commit -m 'git のフック設定を見直す\n\n{TRAILER}'", "pass"),
            (f"git commit -m 'front_stage のテスト分割を見直す\n\n{TRAILER}'", "pass"),
            (f"git commit -m '仮引数の既定値を見直す\n\n{TRAILER}'", "pass"),
        ]),
        CaseGroup("トレーラの形が欠けた形", [
            ("git commit -m '件名\n\n本文\n\nCo-Authored-By: Claude <Opus 5> <noreply@anthropic.com>'", "deny"),
            ("git commit -m '件名\n\n本文\n\nCo-Authored-By: Claude <モデル名> <noreply@anthropic.com>'", "deny"),
            ("git commit -m '件名\n\n本文\n\nCo-Authored-By: Claude <Opus 5>'", "deny"),
            ("git commit -m '件名\n\n本文\n\nCo-Authored-By: Opus 5 <noreply@anthropic.com>'", "deny"),
            ("git commit -m '件名\n\n本文\n\nCo-Authored-By: Claude <noreply@anthropic.com>'", "deny"),
            ("git commit -m '件名\n\n本文\n\nCo-Authored-By: Claude   <noreply@anthropic.com>'", "deny"),
            ("git commit -m '件名\n\n本文\n\nCo-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>'", "pass"),
            (f"git commit -m '件名\n\n本文\n\n{TRAILER} '", "pass"),
            (f"git commit -m '件名 {TRAILER}'", "deny"),
            ("git commit -m '件名'", "deny"),
            (f"git commit -m '件名\n\n本文\n {TRAILER}'", "deny"),
            (f"git commit -m '件名\n\n{TRAILER}\n\n追記'", "deny"),
            (f"git commit -m '{TRAILER} 件名'", "deny"),
        ]),
        CaseGroup("行構造を表す改行エスケープだけを拒む", [
            (f"git commit -m '区切りは \\t 文字\n\n{TRAILER}'", "pass"),
            (f"git commit -m '改行を \\n で表す\n\n{TRAILER}'", "deny"),
            (f"git commit -m $'件名\\n\\n本文\\n\\n{TRAILER}'", "deny"),
        ]),
        CaseGroup("許可しない add・commit の形", [
            ("git add", "deny"),
            ("git add .", "deny"),
            ("git add -A", "deny"),
            ("git add 'src/*'", "deny"),
            ("git add -- :/", "deny"),
            ("git commit", "deny"),
            ("git commit -m ''", "deny"),
            ("git commit -m 'x' --amend", "deny"),
            ("git commit -m 'x' -- file.py", "deny"),
            ("git add a.py && git commit -m 'x'", "deny"),
            ("git commit -m \"$MESSAGE\"", "deny"),
            ("git commit -m \"$(date)\"", "deny"),
            ("git add src/{a,b}.py", "deny"),
            ("git add a.py > staged.txt", "deny"),
            ("cd repo && git add a.py", "deny"),
            ("git -C repo commit -m 'x'", "deny"),
            ("VAR=x git commit -m 'x'", "deny"),
            ("command git add a.py", "deny"),
            ("time git commit --amend", "deny"),
            ("stdbuf git add -A", "deny"),
            ("xargs git commit -m 'x'", "deny"),
            ("git.exe commit -m 'x'", "deny"),
            ("/usr/bin/git commit -m 'x'", "deny"),
        ]),
        CaseGroup("git reset はどの形も deny する", [
            ("git reset", "deny"),
            ("git reset --soft HEAD~1", "deny"),
            ("git reset --hard origin/main", "deny"),
            ("git reset -- file.py", "deny"),
            ("git -C repo reset --hard", "deny"),
            ("git.exe reset", "deny"),
            ("/usr/bin/git reset --hard", "deny"),
            ("time git reset --hard", "deny"),
            ("VAR=x git reset", "deny"),
            ("cd repo && git reset --hard", "deny"),
            ("echo git reset", "pass"),
            ("echo 'git reset --hard'", "pass"),
        ]),
        CaseGroup("別のリポジトリへ移した git の読み取り除外と、その境界", [
            ("cd /d && git -C /d/other-repo status --short", "deny"),
            ("git -C repo status", "pass"),
            ("git -C. status", "pass"),
            ("git -C ../other-clone log --oneline -5", "deny"),
            ("git -C ../other-clone diff", "deny"),
            ("git -C ../other-clone show HEAD", "deny"),
            ("git --git-dir=.git --work-tree=. status", "pass"),
            ("git --work-tree /x status", "pass"),
            ("VAR=x git -C repo status", "pass"),
            ("git -C repo push", "deny"),
            ("git -C repo checkout main", "deny"),
            ("git -C repo stash", "deny"),
            ("git -C repo", "deny"),
            ("git -C ../other grep --open-files-in-pager foo", "deny"),
            ("git -C ../other grep -Ovim foo", "deny"),
            ("git -C ../other grep --textconv foo", "deny"),
            ("git -C ../other cat-file --filters HEAD:a.py", "deny"),
            ("git -C ../other cat-file --textconv HEAD:a.py", "deny"),
            ("git -C ../other cat-file --textcon HEAD:a.py", "deny"),
            ("git -C ../other grep --filter=x foo", "deny"),
        ]),
        CaseGroup("シェルのキーワードと制御演算子の後ろに置いた呼び出し", [
            ("! git add a.py", "deny"),
            ("coproc git commit -m 'x'", "deny"),
            ("! git reset --hard", "deny"),
            ("git -C ../other status & git -C ../other checkout main", "deny"),
            ("git -C ../other status\ngit -C ../other checkout main", "deny"),
            ("( git -C ../other checkout main )", "deny"),
            ("! git -C ../other push", "deny"),
            ("coproc git -C ../other checkout main", "deny"),
            ("( git add a.py )", "deny"),
            ("git status & git commit -m 'x'", "deny"),
        ]),
        CaseGroup("ガードの対象外として素通りする形", [
            ("git log -C", "pass"),
            ("git -c user.name=x status", "pass"),
            ("echo git -C repo status", "pass"),
            ("git restore --staged a.py", "pass"),
            ("git status --short", "pass"),
            ("git diff --staged -- a.py", "pass"),
            ("git log --oneline -10", "pass"),
            ("git log commit", "pass"),
            ("echo git commit", "pass"),
            ("echo 'git commit -m x'", "pass"),
            ("", "pass"),
        ]),
    )
    IndexCase = namedtuple("IndexCase", "why command staged expected")
    index_cases = [
        IndexCase("ステージが空なら通る", "git add -- README.md", [], "pass"),
        IndexCase("既にステージ済みのファイルの再 add", "git add -- README.md", ["README.md"], "pass"),
        IndexCase("既にステージ済みのファイルの再 add", "git add -- README.md CLAUDE.md", ["CLAUDE.md"], "pass"),
        IndexCase("別のセッションがステージした項目との衝突", "git add -- README.md", ["CLAUDE.md"], "deny"),
        IndexCase("別のセッションがステージした項目との衝突", "git add -- README.md", ["README.md", "plugins/flow/.claude-plugin/plugin.json"], "deny"),
        IndexCase("拒む形はインデックスの中身によらず拒む", "git add -- missing-file.py", ["CLAUDE.md"], "deny"),
        IndexCase("commit はインデックス全体を取るので対象外", f"git commit -m '件名\n\n本文\n\n{TRAILER}'", ["CLAUDE.md"], "pass"),
    ]
    repo = Path(__file__).resolve().parents[3]
    failures = []
    for group in case_groups:
        for command, expected in group.cases:
            actual, _ = classify(command, repo)
            if actual != expected:
                failures.append((f"{group.why}: {command}", expected, actual))
    for case in index_cases:
        actual, _ = classify(case.command, repo, case.staged)
        if actual != case.expected:
            failures.append((f"{case.why}: {case.command} [staged={case.staged}]",
                             case.expected, actual))
    if failures:
        for command, expected, actual in failures:
            print(f"FAIL expected={expected} actual={actual}: {command!r}")
        raise SystemExit(1)
    if not _tracked_file(repo, repo / "plugins/flow/tests/fixtures/日本語パス検査.txt"):
        print("FAIL 追跡下の非ASCIIパスがバイト比較で一致しない")
        raise SystemExit(1)
    print(f"ALL PASS ({sum(len(group.cases) for group in case_groups) + len(index_cases)} cases + non-ASCII tracked-path check)")


if __name__ == "__main__":
    main()
