#!/usr/bin/env python3
"""PreToolUse フック: 成果物衛生・文書記述規約が禁じる記述の新規混入を Edit/Write の時点で deny する。

書き込み前の全文と書き込み後の全文を突き合わせ、禁止記述(原子)ごとの出現数が増えたときだけ
deny する。既存の違反を含むファイルへの無関係な編集や、違反の削除・ファイル内の移動は通す。原子
ごとに数えるのは、禁止語を別の禁止語へ置き換える編集を素通りさせないため。断片ではなく適用後の
全文で数えるのは、原子が編集境界をまたいで生まれる形を見逃さないため。

検査するかどうかはファイルパスの文字列照合だけで決める(git は起動しない)。正当な用法が実在する
層——指示層の実行時相対表現と禁止語の引用・パスを扱うフックが持つ例示パス・テストデータなど——は
原子の種類ごとに除く。

使い方: Edit/Write の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import json
import re
import sys
from collections import Counter, namedtuple
from pathlib import Path

TARGET_TOOLS = ("Edit", "Write")
EXCERPT_LIMIT = 3
EXCERPT_WINDOW = 40

HYGIENE_WORK = "artifact-hygiene.md §2"
HYGIENE_ENV = "artifact-hygiene.md §3"
AUTHORING_SCOPE = "document-authoring.md §2"

REMEDY_SCRATCH = "参照先を恒久的な正本に差し替えるか、内容をこの文書内に自己完結で書く"
REMEDY_RELATIVE = "作業の時点に依存しない自己完結の記述に書き直す(例: 挙動そのものを述べる)"
REMEDY_STATE = "到達状態(最終的な規約・挙動)だけを述べる形に書き直す"
REMEDY_PATH = "リポジトリ相対パスか汎用のダミー値に置き換える"

SCRATCH_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9._-])\.scratch[/\\][A-Za-z0-9._-][A-Za-z0-9._/\\-]*"
)
HOME_PATH = re.compile(r"[a-z]:[/\\]users[/\\]([\w-]+(?:\.[\w-]+)*)", re.IGNORECASE)
HOME_PLACEHOLDERS = frozenset({"user", "username", "example", "public", "default", "ユーザー名"})

RELATIVE_EN = re.compile(r"\b(?:before|after) this (?:change|fix)\b", re.IGNORECASE)

INSTRUCTION_LAYER = re.compile(r"(?:^|/)plugins/[^/]+/(?:skills|agents|docs|hooks)/")
HOOK_LAYER = re.compile(r"(?:^|/)plugins/[^/]+/hooks/")
INSTRUCTION_NAMES = ("claude.md", "claude.local.md")
# ホーム直下の .claude は個人設定と自動メモリの置き場で、実ホームパスを書かないと動かない。
HOME_CLAUDE = re.compile(r"(?:^[a-z]:)?/(?:users|home)/[^/]+/\.claude(?:/|\.json)")
VERSION_LOG_PREFIXES = ("changelog", "tuning")
DOC_SUFFIXES = (".md", ".markdown")

Atom = namedtuple("Atom", "exempt_group label pattern source remedy is_violation")


def _outside_placeholder(match):
    return match.group(1).lower() not in HOME_PLACEHOLDERS


def _word(group, word, source, remedy):
    return Atom(group, word, re.compile(re.escape(word)), source, remedy, None)


ATOMS = (
    Atom("P1", "スクラッチ配下ファイルへの参照", SCRATCH_REFERENCE, HYGIENE_WORK,
         REMEDY_SCRATCH, None),
    *(_word("P2", word, HYGIENE_WORK, REMEDY_RELATIVE) for word in (
        "改修前", "改修後", "今回の変更", "今回の修正", "今回の対応", "今回のレビュー",
        "今回の指摘", "今回のコミット", "今回の作業", "今回の改修", "今回の実装")),
    Atom("P2", "この変更を基準にした英語の相対参照", RELATIVE_EN, HYGIENE_WORK,
         REMEDY_RELATIVE, None),
    *(_word("P3", word, AUTHORING_SCOPE, REMEDY_STATE) for word in (
        "現時点では", "当面", "初期実装では", "段階導入", "先行導入")),
    Atom("P5", "ドライブ文字付きユーザーホームパス", HOME_PATH, HYGIENE_ENV,
         REMEDY_PATH, _outside_placeholder),
)

HEADER = ("[guard-artifact-hygiene] 書き込もうとしたテキストに次の記述が新たに含まれています。"
          "当該記述の全出現を点検し、すべて書き直してください(1 箇所だけ直しても再び deny されます)。")
FOOTER = ("既存の同種記述を消して数を合わせることはこの deny の対処ではありません。Bash の\n"
          "リダイレクト・heredoc・sed 等、別の手段で同じ書き込みを回避して実行しないこと。")

DOC = "docs/module.md"
CODE_FILE = "scripts/tool.py"
INSTRUCTION_SKILL_DOC = "plugins/flow/skills/sample/SKILL.md"
INSTRUCTION_CONVENTION_DOC = "plugins/flow/docs/sample.md"
INSTRUCTION_AGENT_DOC = "plugins/flow/agents/sample.md"
HOOK_FILE = "plugins/flow/hooks/sample.py"
TEST_DOC = "plugins/flow/tests/fixtures/sample.md"
TEST_CODE = "plugins/flow/tests/fixtures/sample.py"
CONSUMER_REPO = "C:/Users/user/repo"
BACKSLASH_DOC = "C:\\Users\\user\\repo\\docs\\module.md"
DENIED_HOME_LINE = 'root = "C:/Users/alice/x"\n'


def _edit(path, old, new, replace_all=False):
    """Edit の tool_input。replace_all は真のときだけ入れる(既定では key ごと現れない)。"""
    tool_input = {"file_path": path, "old_string": old, "new_string": new}
    if replace_all:
        tool_input["replace_all"] = True
    return ("Edit", tool_input)


def _write(path, content):
    return ("Write", {"file_path": path, "content": content})


Case = namedtuple("Case", "tool_name tool_input before expected")
CaseGroup = namedtuple("CaseGroup", "why cases")
CASE_GROUPS = (
    CaseGroup("各原子が新たに増えたときの deny", [
        Case(*_write(DOC, "詳しくは .scratch/plan.md を参照する。\n"), "", "deny"),
        Case(*_write(DOC, "詳しくは .scratch\\plan.md を参照する。\n"), "", "deny"),
        Case(*_write(DOC, "改修前の値は 3。\n"), "", "deny"),
        Case(*_write(DOC, "改修後の挙動を述べる。\n"), "", "deny"),
        Case(*_write(DOC, "今回の変更点を述べる。\n"), "", "deny"),
        Case(*_write(DOC, "今回の修正で入れた分岐。\n"), "", "deny"),
        Case(*_write(DOC, "今回の対応の範囲。\n"), "", "deny"),
        Case(*_write(DOC, "今回のレビューで挙がった点。\n"), "", "deny"),
        Case(*_write(DOC, "今回の指摘に沿って直す。\n"), "", "deny"),
        Case(*_write(DOC, "今回のコミットに含める。\n"), "", "deny"),
        Case(*_write(DOC, "今回の作業で足した節。\n"), "", "deny"),
        Case(*_write(DOC, "今回の改修で入れた分岐。\n"), "", "deny"),
        Case(*_write(DOC, "今回の実装の範囲。\n"), "", "deny"),
        Case(*_write(DOC, "Before this change the flag was unset.\n"), "", "deny"),
        Case(*_write(DOC, "after this fix the branch is gone.\n"), "", "deny"),
        Case(*_write(DOC, "現時点では未対応。\n"), "", "deny"),
        Case(*_write(DOC, "当面はこの形で運用する。\n"), "", "deny"),
        Case(*_write(DOC, "初期実装では未採用。\n"), "", "deny"),
        Case(*_write(DOC, "段階導入の順序を決める。\n"), "", "deny"),
        Case(*_write(DOC, "先行導入した範囲から広げる。\n"), "", "deny"),
        Case(*_write(DOC, "ログは C:/Users/alice/logs にある。\n"), "", "deny"),
        Case(*_write(DOC, "ログは d:\\users\\alice\\logs にある。\n"), "", "deny"),
    ]),
    CaseGroup("境界の許可", [
        Case(*_write(DOC, "一時ドキュメントは .scratch/ に置く。\n"), "", "allow"),
        Case(*_write(DOC, "置き場は .scratch/、退避先は別にする。\n"), "", "allow"),
        Case(*_write(DOC, "cache.scratch/report.md を読む。\n"), "", "allow"),
        Case(*_write(DOC, "詳細は .scratch/.draft.md を読む。\n"), "", "deny"),
        Case(*_write(DOC, "詳細は .scratch/a.md、.scratch/b.md を参照。\n"), "", "deny"),
        Case(*_write(DOC, "scripts/run_selftests.py を実行する。\n"), "", "allow"),
        Case(*_write(CODE_FILE, "# 段階導入の順序を決める\n"), "", "allow"),
    ]),
    CaseGroup("コードのコメント・docstring も作業過程の相対表現の対象になる", [
        Case(*_write(CODE_FILE, "# 改修後の値を返す\n"), "", "deny"),
        Case(*_write(CODE_FILE, '"""今回の変更で足した関数。"""\n'), "", "deny"),
        Case(*_edit(CODE_FILE, "x = 1", "x = 2  # 改修前は 1 だった"), "x = 1\n", "deny"),
        Case(*_write("scripts/tool.sh", "# 今回の対応で足した分岐\n"), "", "deny"),
        Case(*_write("config/app.yml", "# 改修後の既定値\n"), "", "deny"),
        Case(*_write(CODE_FILE, "# 今回の実装で足した分岐\n"), "", "deny"),
        Case(*_write(CODE_FILE, "# before this change we retried twice\n"), "", "deny"),
    ]),
    CaseGroup("指示層・テスト層のコードは、コードでも作業過程の相対表現の免除が続く", [
        Case(*_write(HOOK_FILE, "# 改修後の挙動を返す\n"), "", "allow"),
        Case(*_write(TEST_CODE, "# 改修後の挙動を返す\n"), "", "allow"),
    ]),
    CaseGroup("「before this call」のように this の後が change・fix でない形は許可のままとする", [
        Case(*_write(DOC, "before this call の戻り値を使う。\n"), "", "allow"),
    ]),
    CaseGroup("P1・P5 の適用は Markdown に限らない(拡張子による免除は P3 だけ)", [
        Case(*_write(CODE_FILE, DENIED_HOME_LINE), "", "deny"),
        Case(*_write(CODE_FILE, "# 詳細は .scratch/plan.md を見る\n"), "", "deny"),
    ]),
    CaseGroup("複合形の境界", [
        Case(*_write(DOC, "今回のリクエストで指定した ID を使う。\n"), "", "allow"),
        Case(*_write(DOC, "現時点のカーソル位置を返す。\n"), "", "allow"),
    ]),
    CaseGroup("前後全文の比較", [
        Case(*_edit(DOC, "改修前", "改修後"), "改修前の値は 3。\n", "deny"),
        Case(*_edit(DOC, "他の行", "別の行"), "改修後の値は 3。\n他の行\n", "allow"),
        Case(*_edit(DOC, "改修後の値は 3。\n", ""), "改修後の値は 3。\n本文\n", "allow"),
        Case(*_write(DOC, "A\nB\n改修後\n"), "改修後\nA\nB\n", "allow"),
    ]),
    CaseGroup("原子が編集境界をまたいで生まれる形", [
        Case(*_edit(DOC, "仕様", "変更"), "今回の仕様を述べる。\n", "deny"),
    ]),
    CaseGroup("Edit ツール自体が成立しない入力は検査しない", [
        Case(*_edit(DOC, "無い文字列", "改修後"), "本文\n", "allow"),
        Case(*_edit(DOC, "無い文字列", "改修後", replace_all=True), "本文\n", "allow"),
        Case(*_edit(DOC, "A", "改修後"), "A\nA\n", "allow"),
        Case(*_edit(DOC, "A", "改修後", replace_all=True), "A\nA\n", "deny"),
    ]),
    CaseGroup("P5 の許可リストとプレースホルダ表記", [
        Case(*_write(DOC, "設定は C:/Users/ユーザー名/.claude にある。\n"), "", "allow"),
        Case(*_write(DOC, "設定は C:/Users/user/.claude にある。\n"), "", "allow"),
        Case(*_write(DOC, "設定は C:/Users/<user>/.claude にある。\n"), "", "allow"),
        Case(*_write(DOC, "設定は C:/Users/{user}/.claude にある。\n"), "", "allow"),
    ]),
    CaseGroup("P5 の表示区切り", [
        Case(*_write(DOC, "パスは `C:/Users/user` を使う。\n"), "", "allow"),
    ]),
    CaseGroup("許可リストの照合は大文字小文字を区別しない", [
        Case(*_write(DOC, "共有は C:/Users/Public/Documents にある。\n"), "", "allow"),
        Case(*_write(DOC, "パスは (C:/Users/user) と C:/Users/user. の形。\n"), "", "allow"),
    ]),
    CaseGroup("原子分類別のパス判定", [
        Case(*_write(".scratch/plan.md", "改修後 C:/Users/alice/x .scratch/a.md\n"), "", "allow"),
        Case(*_write("C:/Users/alice/AppData/Local/Temp/claude/x/note.md", "改修後\n"), "", "allow"),
        Case(*_write("/tmp/note.md", "改修後\n"), "", "allow"),
    ]),
    CaseGroup("macOS のセッション一時領域($TMPDIR。実パスは /private を前置した形)", [
        Case(*_write("/var/folders/ab/cd/T/claude/x/note.md", "改修後\n"), "", "allow"),
        Case(*_write("/private/var/folders/ab/cd/T/claude/x/note.md", "改修後\n"), "", "allow"),
        Case(*_write("/private/tmp/note.md", "改修後\n"), "", "allow"),
    ]),
    CaseGroup("POSIX 形のホームパスは原子に採らない", [
        Case(*_write(DOC, "ログは /Users/alice/logs にある。\n"), "", "allow"),
    ]),
    CaseGroup("適用範囲外の置き場と、拡張子による免除の境界", [
        Case(*_write("C:/Users/alice/.claude/projects/x/memory/note.md",
                 "改修後 C:/Users/alice/x\n"), "", "allow"),
        Case(*_write("src/tmp/note.md", "改修後の値。\n"), "", "deny"),
        Case(*_write("docs/note.markdown", "改修後の値。\n"), "", "deny"),
    ]),
    CaseGroup("指示層とフック層の免除", [
        Case(*_write(INSTRUCTION_SKILL_DOC, "改修後の挙動。\n"), "", "allow"),
        Case(*_write(INSTRUCTION_SKILL_DOC, "ログは C:/Users/alice/logs。\n"), "", "deny"),
        Case(*_write(INSTRUCTION_SKILL_DOC, "詳しくは .scratch/plan.md を参照する。\n"), "", "allow"),
        Case(*_write(INSTRUCTION_CONVENTION_DOC, "「改修前」「改修後」「今回」などの相対参照。\n"), "", "allow"),
        Case(*_write(INSTRUCTION_CONVENTION_DOC, "ログは C:/Users/alice/logs。\n"), "", "deny"),
        Case(*_write(INSTRUCTION_AGENT_DOC, "改修後の挙動。\n"), "", "allow"),
        Case(*_write(HOOK_FILE, DENIED_HOME_LINE), "", "allow"),
    ]),
    CaseGroup("指示層はスクラッチ配下にファイルを作らせる生成指示を持つ", [
        Case(*_write(HOOK_FILE, "# 詳細は .scratch/plan.md を見る\n"), "", "allow"),
        Case(*_write("CLAUDE.md", "一時ファイルは .scratch/note.md に置く。\n"), "", "allow"),
    ]),
    CaseGroup("消費リポジトリの .claude 配下は成果物なので検査する", [
        Case(*_write(f"{CONSUMER_REPO}/.claude/agents/sample.md", "改修後の挙動。\n"), "", "allow"),
        Case(*_write(f"{CONSUMER_REPO}/.claude/agents/sample.md", "ログは C:/Users/alice/logs。\n"), "", "deny"),
        Case(*_write(f"{CONSUMER_REPO}/.claude/hooks/sample.py", DENIED_HOME_LINE), "", "allow"),
    ]),
    CaseGroup("ホーム直下の .claude と .claude.json の免除", [
        Case(*_write("C:/Users/alice/.claude/settings.json", DENIED_HOME_LINE), "", "allow"),
        Case(*_write("C:/Users/alice/.claude/CLAUDE.md", "ログは C:/Users/alice/logs。\n"), "", "allow"),
        Case(*_write("/home/alice/.claude/agents/sample.md", "ログは C:/Users/alice/logs。\n"),
         "", "allow"),
        Case(*_write("C:/Users/alice/.claude.json", DENIED_HOME_LINE), "", "allow"),
    ]),
    CaseGroup("指示ファイル名・テストデータ・バージョン記録の免除", [
        Case(*_write("CLAUDE.md", "改修後の挙動。\n"), "", "allow"),
        Case(*_write("CLAUDE.md", "ログは C:/Users/alice/logs。\n"), "", "deny"),
        Case(*_write(TEST_DOC, "改修後 C:/Users/alice/x .scratch/a.md\n"), "", "allow"),
        Case(*_write("TUNING.md", "段階導入の手順。\n"), "", "allow"),
        Case(*_write("CHANGELOG.md", "段階導入の記録。\n"), "", "allow"),
        Case(*_write("CHANGELOG.md", "改修後の挙動。\n"), "", "allow"),
    ]),
    CaseGroup("パスの正規化(バックスラッシュ→スラッシュ・小文字化)を経て初めて成立する判定", [
        Case(*_write(BACKSLASH_DOC, "改修後の値。\n"), "", "deny"),
        Case(*_write("C:\\Users\\user\\repo\\plugins\\flow\\skills\\sample\\SKILL.md",
                 "改修後の挙動。\n"), "", "allow"),
        Case(*_write("C:\\Users\\user\\AppData\\Local\\Temp\\claude\\x\\note.md", "改修後\n"), "", "allow"),
        Case(*_write(f"{CONSUMER_REPO}/.scratch/plan.md", "改修後 .scratch/a.md\n"), "", "allow"),
    ]),
    CaseGroup("検査対象外のツール・読めない入力", [
        Case("MultiEdit", {"file_path": DOC, "edits": [{"old_string": "A", "new_string": "改修後"}]},
         "A\n", "allow"),
        Case("Bash", {"command": "ls"}, "", "allow"),
        Case("Write", {}, "", "allow"),
        Case("Write", {"file_path": DOC}, "", "allow"),
        Case("Edit", {"file_path": DOC}, "本文\n", "allow"),
        Case("Write", {"file_path": "", "content": "改修後\n"}, "", "allow"),
    ]),
)

ReadFailureCase = namedtuple("ReadFailureCase", "tool_name error expected")
READ_FAILURE_CASES = [
    ReadFailureCase("Write", FileNotFoundError(), "empty"),
    ReadFailureCase("Edit", FileNotFoundError(), "allow"),
    ReadFailureCase("Write", PermissionError(), "allow"),
    ReadFailureCase("Write", IsADirectoryError(), "allow"),
    ReadFailureCase("Edit", PermissionError(), "allow"),
]

MessageCase = namedtuple("MessageCase", "why tool_name tool_input before needles")
MESSAGE_CASES = [
    MessageCase("追加した側の行が候補行として出る", *_edit(DOC, "改修後の値は 3。\n", "改修後の値は 3。\n新しい改修後の行\n"),
     "改修後の値は 3。\n",
     ["(増加 1 件)", "新しい改修後の行", "根拠: flow の artifact-hygiene.md §2"]),
    MessageCase("同一原子が複数増えたら増加数と各行を示す", *_write(DOC, "詳細は .scratch/a.md。\n続きは .scratch/b.md。\n"), "",
     ["スクラッチ配下ファイルへの参照", "(増加 2 件)", ".scratch/a.md", ".scratch/b.md"]),
    MessageCase("候補行は先頭から 3 行まで、以降は省略した行数で示す", *_write(DOC, "".join(f"{i} 番目は .scratch/f{i}.md。\n" for i in range(5))), "",
     ["(増加 5 件)", "候補行 他 2 行"]),
    MessageCase("文面の骨格と document-authoring の根拠表示", *_write(DOC, "現時点では未対応。\n"), "",
     ["[guard-artifact-hygiene]", "当該記述の全出現を点検し、すべて書き直してください",
      "根拠: flow の document-authoring.md §2",
      "既存の同種記述を消して数を合わせることはこの deny の対処ではありません。",
      "別の手段で同じ書き込みを回避して実行しないこと。"]),
    MessageCase("artifact-hygiene の根拠表示", *_write(DOC, "ログは C:/Users/alice/logs にある。\n"), "",
     ["ドライブ文字付きユーザーホームパス", "C:/Users/alice",
      "根拠: flow の artifact-hygiene.md §3"]),
]


def _out_of_scope(path):
    """成果物衛生規約の適用範囲外の置き場か。作業過程の記述がそこでは正当になる。"""
    return (
        "/.scratch/" in path or path.startswith(".scratch/")
        or "/appdata/local/temp/" in path or "/var/folders/" in path
        or path.startswith(("/tmp/", "/private/tmp/"))
        or "/.claude/projects/" in path or bool(HOME_CLAUDE.search(path))
    )


def _is_instruction(path, name):
    """実行時の相対表現と禁止語の引用が正当に現れる指示層か。"""
    return bool(INSTRUCTION_LAYER.search(path)) or "/.claude/" in path or name in INSTRUCTION_NAMES


def _is_test(path):
    return "/tests/" in path or "/test/" in path or path.startswith(("tests/", "test/"))


def applicable_atoms(file_path):
    """このパスで検査する原子。免除はその原子の正当用法が実在する層だけに効かせる。"""
    path = file_path.replace("\\", "/").lower()
    if _out_of_scope(path):
        return ()
    name = path.rsplit("/", 1)[-1]
    exempt = set()
    if _is_test(path):
        exempt.update(atom.exempt_group for atom in ATOMS)
    if _is_instruction(path, name):
        exempt.update(("P1", "P2", "P3"))
    if name.startswith(VERSION_LOG_PREFIXES):
        exempt.update(("P2", "P3"))
    if not name.endswith(DOC_SUFFIXES):
        exempt.add("P3")
    if HOOK_LAYER.search(path) or "/.claude/hooks/" in path:
        exempt.add("P5")
    return tuple(atom for atom in ATOMS if atom.exempt_group not in exempt)


def _matches(atom, text):
    return [hit for hit in atom.pattern.finditer(text) if atom.is_violation is None or atom.is_violation(hit)]


def _after_text(tool_name, tool_input, before_text):
    """ツール入力を適用した変更後全文。ツール自体が成立しない入力なら None。"""
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None
    old = tool_input.get("old_string")
    new = tool_input.get("new_string", "")
    if not isinstance(old, str) or not isinstance(new, str) or not old:
        return None
    hits = before_text.count(old)
    # 一致が無い、または一意でないまま replace_all を伴わない Edit は、ツール自体が失敗する。
    if hits == 0 or (hits > 1 and not tool_input.get("replace_all")):
        return None
    return before_text.replace(old, new)


def _changed_lines(before_text, after_text):
    """変更が及んだ行を変更後の並びで返す。無変更の既存行は前後に同数あり差に残らない。"""
    remaining = Counter(after_text.splitlines()) - Counter(before_text.splitlines())
    lines = []
    for line in after_text.splitlines():
        if remaining[line] > 0:
            remaining[line] -= 1
            lines.append(line)
    return lines


def evaluate(tool_name, tool_input, before_text):
    """禁止記述の出現数が増えたなら deny 理由を返す。増えていない・検査対象外なら None。"""
    if tool_name not in TARGET_TOOLS or not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    atoms = applicable_atoms(file_path)
    if not atoms:
        return None
    after_text = _after_text(tool_name, tool_input, before_text)
    if after_text is None:
        return None
    increases = []
    for atom in atoms:
        before_hits = len(_matches(atom, before_text))
        after_hits = len(_matches(atom, after_text))
        if after_hits > before_hits:
            increases.append((atom, before_hits, after_hits))
    if not increases:
        return None
    return _format_reason(increases, _changed_lines(before_text, after_text))


def read_failure_action(tool_name, error):
    """変更前全文の読み取りに失敗したときの続け方。

    "empty" なら変更前を空文字列として検査を続け、"allow" なら検査せず許可する。
    """
    if tool_name == "Write" and isinstance(error, FileNotFoundError):
        return "empty"
    return "allow"


def _window(line, match):
    return line[max(0, match.start() - EXCERPT_WINDOW):match.end() + EXCERPT_WINDOW]


def _format_reason(increases, changed_lines):
    """増えた原子の一覧と変更が及んだ行から deny 理由の文面を組み立てる。"""
    parts = [HEADER]
    for atom, before_hits, after_hits in increases:
        excerpts = [(hits[0].group(0), _window(line, hits[0]))
                    for line, hits in ((line, _matches(atom, line)) for line in changed_lines)
                    if hits]
        source = f"根拠: flow の {atom.source}。対処: {atom.remedy}"
        if not excerpts:
            parts.append(f"- 「{atom.label}」 — "
                         f"出現が {before_hits} 件から {after_hits} 件に増えました。{source}")
            continue
        parts.append(f"- 「{atom.label}」(増加 {after_hits - before_hits} 件) — {source}")
        parts.extend(f"  候補行: 「{hit}」 {window}" for hit, window in excerpts[:EXCERPT_LIMIT])
        if len(excerpts) > EXCERPT_LIMIT:
            parts.append(f"  候補行 他 {len(excerpts) - EXCERPT_LIMIT} 行")
    parts.append(FOOTER)
    return "\n".join(parts)


def selftest():
    failures = []
    for group in CASE_GROUPS:
        for case in group.cases:
            actual = "deny" if evaluate(case.tool_name, case.tool_input, case.before) else "allow"
            if actual != case.expected:
                failures.append(f"{group.why}: expected={case.expected} actual={actual}: "
                                f"{case.tool_name} {case.tool_input!r}")
    for case in READ_FAILURE_CASES:
        actual = read_failure_action(case.tool_name, case.error)
        if actual != case.expected:
            failures.append(
                f"expected={case.expected} actual={actual}: {case.tool_name} {case.error!r}")
    for case in MESSAGE_CASES:
        reason = evaluate(case.tool_name, case.tool_input, case.before) or ""
        for needle in case.needles:
            if needle not in reason:
                failures.append(f"{case.why}: missing {needle!r}: {reason!r}")

    reason = evaluate(*_write(DOC, "改修後の値。\n詳細は .scratch/c.md。\n"), "") or ""
    if "スクラッチ配下ファイルへの参照" not in reason or "改修後" not in reason:
        failures.append(f"複数原子が列挙されていない: {reason!r}")
    elif reason.index("スクラッチ配下ファイルへの参照") > reason.index("改修後"):
        failures.append(f"原子の列挙が定数表の並び順でない: {reason!r}")

    reason = evaluate(*_edit(DOC, "改修後の値は 3。\n", "改修後の値は 3。\n新しい改修後の行\n"),
                      "改修後の値は 3。\n") or ""
    if "改修後の値は 3。" in reason:
        failures.append(f"変更が及んでいない既存行が候補行に出ている: {reason!r}")

    long_line = "あ" * 100 + " .scratch/far.md " + "い" * 100
    reason = evaluate(*_write(DOC, long_line + "\n"), "") or ""
    if ".scratch/far.md" not in reason:
        failures.append(f"長い行の一致箇所が抜粋に入っていない: {reason!r}")
    if "あ" * 60 in reason:
        failures.append("抜粋が窓幅を超えている")

    fallback = _format_reason([(ATOMS[0], 1, 2)], ["この行に原子は無い"])
    if "出現が 1 件から 2 件に増えました" not in fallback:
        failures.append(f"候補行が無い場合の代替文面が出ていない: {fallback!r}")

    if failures:
        for line in failures:
            print(f"FAIL {line}")
        raise SystemExit(1)
    total = (sum(len(group.cases) for group in CASE_GROUPS)
             + len(READ_FAILURE_CASES) + len(MESSAGE_CASES))
    print(f"ALL PASS ({total} cases + 列挙順・候補行・窓幅・代替文面の 4 検査)")


def main():
    # 既定の標準出力コーデック(日本語 Windows では cp932)には理由文の記号が無く、落ちる。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
        return
    # ハーネスが渡す JSON は UTF-8。cp932 で読むと日本語が化けて一致しない。
    # 閉じた標準入力では sys.stdin が None になる。
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    tool_name, tool_input = data.get("tool_name"), data.get("tool_input")
    if tool_name not in TARGET_TOOLS or not isinstance(tool_input, dict):
        return
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path or not applicable_atoms(file_path):
        return
    try:
        before_text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        if read_failure_action(tool_name, error) != "empty":
            return
        before_text = ""
    reason = evaluate(tool_name, tool_input, before_text)
    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))


if __name__ == "__main__":
    main()
