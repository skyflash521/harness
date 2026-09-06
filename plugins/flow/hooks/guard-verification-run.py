#!/usr/bin/env python3
"""PreToolUse フック: 確定の段に入る前の検査の実行を deny する。

リポジトリの検証手順書が定める検査は、変更を確定させる前に通すものである。編集のたびに回しても
判定は確定に使われず、待ち時間と出力だけが積み上がる。

確定の段に入ったかは、機械が生む2つの状態のどちらかで見る。

- インデックスに変更がステージされている(コミットで空になり、閉じる)。
- 直近のユーザー発言より後に、レビューループ・コミット・自律開発のスキル、またはレビュアー・
  コミットワーカーを起動している(工程の終わりでは閉じず、次のユーザー発言まで開く)。

止めるのは、検証手順書がコマンドとして書いた検査を実際に走らせる呼び出しに限る。読み取れなかった
検査も、見分けられなかった呼び出しも素通りさせる——止め損なった検査は次の機会に止まるが、無関係な
コマンドを deny すると、許可を求める経路が無いぶんその場で回復できない。

使い方: プラグインルートを第1引数に渡す Bash と PowerShell の PreToolUse フックとして登録する。
--selftest で自己テスト。
"""
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

VERIFICATION_DOC = ("docs", "conventions", "verification.md")
TRANSCRIPT = ("scripts", "transcript.py")
TOOLS = {"Bash", "PowerShell"}
# 数字の接尾辞(python3・py3)を落としてから引く。
SCRIPT_RUNNERS = {"python", "py", "node", "bash", "sh", "pwsh", "powershell", "ruby", "perl",
                  "uv", "uvx", "poetry", "pipenv", "pdm", "rye", "hatch",
                  "npx", "npm", "pnpm", "yarn", "deno", "bun"}
SCRIPT_SUFFIXES = (".py", ".sh", ".ps1", ".js", ".mjs", ".rb", ".pl")
DATA_SUFFIXES = (".md", ".txt", ".toml", ".json", ".yml", ".yaml", ".cfg", ".ini", ".lock", ".log")
COMMAND_NAME = re.compile(r"^[A-Za-z0-9_.+-]+$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
WRAPPERS = {"command", "builtin", "env", "exec", "time", "timeout",
            "nice", "ionice", "nohup", "setsid", "stdbuf", "sudo", "doas"}
OPERATORS = ";&|()<>"
GATE_SKILL_SUFFIXES = ("review-loop", "commit", "autonomous-dev")
GATE_AGENTS = ("commit-worker", "opus-reviewer", "fable-reviewer")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
FENCE = re.compile(r"^\s*(?:```|~~~)")

REASON = (
    "[guard-verification-run] 検証手順書が定める検査 `{}` を、確定の段に入る前に実行しようと"
    "している。この検査は変更を確定させる前に通すもので、編集のたびに回しても判定は確定に使われず、"
    "待ち時間と出力だけが積み上がる。レビューやコミットへ進む段なら、ステージするかその工程の"
    "スキルを起動してから回すこと。そうでなければ実行せずに手番を返す。ユーザーが検査そのものを"
    "指示していてここで止まったなら、止まった旨をそのままユーザーへ伝える。"
)


def _base(word):
    return word.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _looks_like_path(word):
    return "/" in word or "\\" in word or "." in word


def _words(text):
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _segments(command):
    """制御演算子で区切った、それぞれの呼び出しの語列。"""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    segments, current = [], []
    for token in tokens:
        if token and not token.strip(OPERATORS):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _invocation(words):
    """環境変数代入とラッパー(その引数を含む)を飛ばした、呼び出しの語列。"""
    index = 0
    while index < len(words):
        word = words[index]
        if ASSIGNMENT.match(word) or _base(word) in WRAPPERS:
            index += 1
            continue
        if index and (word.startswith("-") or word.replace(".", "", 1).isdigit()):
            index += 1
            continue
        break
    return words[index:]


def _operands(words):
    return [word for word in words if not word.startswith("-")]


def _script_operand(operands):
    """ランナーがスクリプトとして起動する被演算子のファイル名。無ければ None。"""
    for operand in operands:
        name = _base(operand)
        if name.endswith(SCRIPT_SUFFIXES):
            return name
    return None


def signature_of(command):
    """検査コマンド1件を、呼び出しを見分ける署名にする。見分けられなければ None。

    ("script", スクリプトのファイル名) か ("exe", コマンド名, 続くサブコマンドの並び)。
    """
    words = _words(command)
    if len(words) < 2:
        return None
    name = _base(words[0])
    if not COMMAND_NAME.match(name) or name.endswith(DATA_SUFFIXES):
        return None
    operands = _operands(words[1:])
    runner = name.rstrip("0123456789") in SCRIPT_RUNNERS
    if runner:
        script = _script_operand(operands)
        if script:
            return ("script", script)
    subcommands, truncated = [], False
    for operand in operands:
        if _looks_like_path(operand):
            truncated = True
            break
        subcommands.append(operand.lower())
    if runner and (truncated or not subcommands):
        return None
    return ("exe", name, tuple(subcommands))


def code_fragments(text):
    """文書がコードとして書いた断片。コードスパンと、コードブロックの各行。"""
    inside = False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        if inside:
            if line.strip():
                yield line.strip()
            continue
        for span in CODE_SPAN.findall(line):
            yield span.strip()


def signatures(text):
    """検証手順書から検査コマンドの署名を集める。文書の組み立て方は問わない。"""
    found = []
    for fragment in code_fragments(text):
        signature = signature_of(fragment)
        if signature and signature not in found:
            found.append(signature)
    return found


def matches(command, signature):
    """発行しようとしているコマンドが、その署名の検査を走らせるか。"""
    for words in _segments(command):
        invocation = _invocation(words)
        if not invocation:
            continue
        name = _base(invocation[0])
        operands = _operands(invocation[1:])
        if signature[0] == "script":
            if name == signature[1]:
                return True
            if (name.rstrip("0123456789") in SCRIPT_RUNNERS
                    and _script_operand(operands) == signature[1]):
                return True
            continue
        if name.endswith(".exe"):
            name = name[:-len(".exe")]
        if name != signature[1]:
            continue
        wanted = signature[2]
        if [operand.lower() for operand in operands[:len(wanted)]] == list(wanted):
            return True
    return False


def repo_root(start):
    """検証手順書を持つ祖先ディレクトリ。見つからなければ None。"""
    try:
        here = Path(start or ".").resolve()
    except (OSError, ValueError):
        return None
    for candidate in (here, *here.parents):
        if (candidate / Path(*VERIFICATION_DOC)).is_file():
            return candidate
    return None


def staged_changes(root):
    """インデックスに変更がステージされているか。判定できなければ False。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--cached", "--quiet"],
            capture_output=True, check=False,
        )
    except OSError:
        return False
    return result.returncode == 1


def _load_transcript(plugin_root):
    try:
        spec = importlib.util.spec_from_file_location(
            "_transcript", Path(plugin_root, *TRANSCRIPT))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (OSError, AttributeError, ImportError, TypeError, ValueError):
        return None


def gate_started(plugin_root, transcript_path):
    """直近のユーザー発言より後に、確定へ向かう工程を起動しているか。"""
    module = _load_transcript(plugin_root)
    if module is None:
        return False
    rows = module.rows_of(transcript_path)
    if not rows:
        return False
    calls = module.calls_since_last_instruction(rows)
    for call in calls or ():
        args = call.get("input")
        if not isinstance(args, dict):
            continue
        if call.get("name") == "Skill":
            skill = args.get("skill")
            if isinstance(skill, str) and skill.split(":")[-1].endswith(GATE_SKILL_SUFFIXES):
                return True
        elif call.get("name") == "Agent":
            agent = args.get("subagent_type")
            if isinstance(agent, str) and agent.split(":")[-1] in GATE_AGENTS:
                return True
    return False


def decide(data, plugin_root):
    """deny する理由を返す。止める必要が無ければ None(pass-through)。"""
    if not isinstance(data, dict) or data.get("tool_name") not in TOOLS:
        return None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    root = repo_root(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR"))
    if root is None:
        return None
    try:
        text = (root / Path(*VERIFICATION_DOC)).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    hit = next((s for s in signatures(text) if matches(command, s)), None)
    if hit is None:
        return None
    if staged_changes(root) or gate_started(plugin_root, data.get("transcript_path")):
        return None
    return REASON.format(hit[1])


def main(plugin_root):
    # ハーネスが渡す JSON は UTF-8 で、既定の符号化では復号できずに落ちる。
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    reason = decide(data, plugin_root)
    if reason is None:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


MIXED_FORMAT_DOC = """# 検証手順

| 検査 | コマンド | 対象 |
|---|---|---|
| 静的検査 | `ruff check` | Python 全体 |
| 記法検査 | `rumdl check` | Markdown の記法 |

節参照の検査はコードブロックで示す。

```sh
python3 scripts/check_section_references.py
```

- リンク検査: `lychee --config lychee.toml .`(設定は `lychee.toml` が持つ)
- 自己テスト: `python3 scripts/run_selftests.py`
- 依存の検査: `uv run scripts/check_deps.py`
- 書式の検査: `npm run lint`
- 秘密の走査: `uv run ./bin/scan-secrets`

版管理は `git`、CI は `.github/workflows/`、規約は `CLAUDE.md` が持つ。
"""
MIXED_FORMAT_SIGNATURES = [
    ("exe", "ruff", ("check",)),
    ("exe", "rumdl", ("check",)),
    ("script", "check_section_references.py"),
    ("exe", "lychee", ()),
    ("script", "run_selftests.py"),
    ("script", "check_deps.py"),
    ("exe", "npm", ("run", "lint")),
]


def selftest():
    import tempfile

    plugin_root = Path(__file__).resolve().parent.parent
    failures, cases = [], 0

    def check(label, actual, expected):
        nonlocal cases
        cases += 1
        if actual != expected:
            failures.append("{}: {!r} != {!r}".format(label, actual, expected))

    collected = signatures(MIXED_FORMAT_DOC)
    check("組み立て方に依らず検査の署名だけを集める", collected, MIXED_FORMAT_SIGNATURES)

    def runs_a_check(command):
        return any(matches(command, signature) for signature in collected)

    for label, command, expected in (
        ("素の静的検査", "ruff check", True),
        ("対象を絞った記法検査", "rumdl check plugins/flow/docs", True),
        ("スクリプトの検査", "python3 scripts/check_section_references.py", True),
        ("絶対パスのスクリプト", "python3 /repo/scripts/run_selftests.py", True),
        ("スクリプトの直接起動", "./scripts/run_selftests.py", True),
        ("オプション付きのリンク検査", "lychee --config lychee.toml .", True),
        ("連結した後段の検査", "cd /repo && ruff check .", True),
        ("パス付きのコマンド名", "/usr/bin/ruff check", True),
        ("オプションを挟んだサブコマンド", "ruff --isolated check docs", True),
        ("環境変数を前置した検査", "TZ=UTC ruff check", True),
        ("ラッパー越しの検査", "timeout 60 rumdl check", True),
        ("ランナー経由のスクリプト検査", "uv run scripts/check_deps.py", True),
        ("同じランナーの別の用途", "uv run pytest tests/", False),
        ("同じランナーで別のスクリプト", "uv run scripts/build.py", False),
        ("パッケージマネージャ経由の検査", "npm run lint", True),
        ("同じ前置語の別の用途", "npm run build", False),
        ("検査スクリプトをステージする", "git add scripts/run_selftests.py", False),
        ("検査スクリプトを読む", "cat scripts/check_section_references.py", False),
        ("検査器の名前で検索する", "grep -rn lychee docs/", False),
        ("検査器の名前を引数に持つ検索", "grep -n rumdl README.md", False),
        ("検査器の設定を読む", "cat lychee.toml", False),
        ("無関係な読み取り", "git diff --stat", False),
        ("語の一部に検査器名を含む", "cat rumdlcheck.txt", False),
    ):
        check("形の見分け: " + label, runs_a_check(command), expected)

    def payload(command, tool="Bash", cwd=None, transcript=None):
        return {"tool_name": tool, "tool_input": {"command": command},
                "cwd": cwd, "transcript_path": transcript}

    def denied(data):
        return decide(data, plugin_root) is not None

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "repo")
        (root / Path(*VERIFICATION_DOC[:-1])).mkdir(parents=True)
        (root / Path(*VERIFICATION_DOC)).write_text(MIXED_FORMAT_DOC, encoding="utf-8")
        (root / "plugins").mkdir()
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        cwd = str(root)
        for label, data, expected in (
            ("編集の途中の静的検査", payload("ruff check", cwd=cwd), True),
            ("編集の途中の自己テスト",
             payload("python3 scripts/run_selftests.py", cwd=cwd), True),
            ("PowerShell からの検査", payload("rumdl check", tool="PowerShell", cwd=cwd), True),
            ("サブディレクトリからの検査",
             payload("ruff check", cwd=str(root / "plugins")), True),
            ("検査でないコマンド", payload("git status", cwd=cwd), False),
            ("検証手順書を持たない場所", payload("ruff check", cwd=tmp), False),
            ("空のコマンド", payload("   ", cwd=cwd), False),
            ("対象でないツール", payload("ruff check", tool="Read", cwd=cwd), False),
            ("tool_input が辞書でない",
             {"tool_name": "Bash", "tool_input": [], "cwd": cwd}, False),
            ("空の入力", {}, False),
            ("辞書でない入力", [], False),
        ):
            check("判定: " + label, denied(data), expected)

        (root / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True)
        check("ステージ済みなら通す", denied(payload("ruff check", cwd=cwd)), False)
        subprocess.run(["git", "-C", str(root), "reset", "-q"], check=True)
        check("ステージを解いたら戻る", denied(payload("ruff check", cwd=cwd)), True)

        transcript = Path(tmp, "transcript.jsonl")
        for label, calls, expected in (
            ("レビューループの起動", [("Skill", {"skill": "flow:codex-review-loop"})], False),
            ("コミットスキルの起動", [("Skill", {"skill": "flow:commit"})], False),
            ("コミットワーカーの起動",
             [("Agent", {"subagent_type": "flow:commit-worker"})], False),
            ("無関係なスキルの起動", [("Skill", {"skill": "flow:run-and-bench"})], True),
            ("読み取りだけ", [("Bash", {"command": "git diff"})], True),
        ):
            rows = [
                {"type": "user", "isSidechain": False,
                 "message": {"role": "user", "content": [{"type": "text", "text": "直せ"}]}},
            ] + [
                {"type": "assistant", "isSidechain": False, "message": {"role": "assistant",
                 "content": [{"type": "tool_use", "name": name, "input": args}]}}
                for name, args in calls
            ]
            transcript.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8")
            data = payload("ruff check", cwd=cwd, transcript=transcript.as_posix())
            check("転写のゲート: " + label, denied(data), expected)

        check("ハーネスと同じ形の発行で deny が出る", roundtrip_deny(cwd), True)

    if failures:
        for line in failures:
            print("FAIL:", line)
        sys.exit(1)
    print("ALL PASS ({} 件)".format(cases))


def roundtrip_deny(cwd):
    """ハーネスと同じ形(UTF-8 の JSON を標準入力へ)で起動して deny が返るか。"""
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "ruff check"}, "cwd": cwd},
        ensure_ascii=False,
    ).encode("utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()),
         str(Path(__file__).resolve().parent.parent)],
        input=payload, capture_output=True, check=False,
    )
    try:
        out = json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
