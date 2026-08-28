#!/usr/bin/env python3
"""PreToolUse hook: 成果物衛生・文書記述規約が禁じる記述の新規混入を Edit/Write の時点で deny する。

書き込み前の全文と書き込み後の全文を突き合わせ、禁止記述(原子)ごとの出現数が増えたときだけ
deny する。既存の違反を含むファイルへの無関係な編集や、違反の削除・ファイル内の移動は通す。原子
ごとに数えるのは、禁止語を別の禁止語へ置き換える編集を素通りさせないため。断片ではなく適用後の
全文で数えるのは、原子が編集境界をまたいで生まれる形を見逃さないため。

検査するかどうかはファイルパスの文字列照合だけで決める(git は起動しない)。正当な用法が実在する
層——指示層の実行時相対表現と禁止語の引用・パスを扱うフックが持つ例示パス・テストデータなど——は
原子の種類ごとに除く。

Usage: configured as an Edit/Write PreToolUse hook. Run with --selftest.
"""
import json
import re
import sys
from collections import Counter, namedtuple
from pathlib import Path

TARGET_TOOLS = ("Edit", "Write")
# 候補行の抜粋の上限と、抜粋の窓を一致箇所の前後へ広げる文字数。
EXCERPT_LIMIT = 3
WINDOW = 40

HYGIENE_WORK = "artifact-hygiene.md §2"
HYGIENE_ENV = "artifact-hygiene.md §3"
AUTHORING_SCOPE = "document-authoring.md §2"

REMEDY_SCRATCH = "参照先を恒久的な正本に差し替えるか、内容をこの文書内に自己完結で書く"
REMEDY_RELATIVE = "作業の時点に依存しない自己完結の記述に書き直す(例: 挙動そのものを述べる)"
REMEDY_STATE = "到達状態(最終的な規約・挙動)だけを述べる形に書き直す"
REMEDY_PATH = "リポジトリ相対パスか汎用のダミー値に置き換える"

# 後読みは `.scratch` がパス要素の先頭であることを要求する(`cache.scratch/x.md` には一致しない)。
# 区切りの直後をファイル名文字に限るので、置き場そのものへの言及(`.scratch/` に置く)は通る。
SCRATCH_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9._-])\.scratch[/\\][A-Za-z0-9._-][A-Za-z0-9._/\\-]*"
)
# ユーザー名セグメントの捕捉を単語文字・ハイフン・語中のドットに限るので、直後のバッククォート・
# 閉じ括弧・文末のドットなどの表示区切りが捕捉へ入らず、許可リスト照合が区切り記号で外れない。
HOME_PATH = re.compile(r"[a-z]:[/\\]users[/\\]([\w-]+(?:\.[\w-]+)*)", re.IGNORECASE)
# 規約が推奨する汎用ダミー値と、個人を特定しない標準プロファイル。
HOME_PLACEHOLDERS = frozenset({"user", "username", "example", "public", "default", "ユーザー名"})

INSTRUCTION_LAYER = re.compile(r"(?:^|/)plugins/[^/]+/(?:skills|agents|docs|hooks)/")
HOOK_LAYER = re.compile(r"(?:^|/)plugins/[^/]+/hooks/")
INSTRUCTION_NAMES = ("claude.md", "claude.local.md")
# ホーム直下の .claude ディレクトリと .claude.json。個人設定・自動メモリの置き場で、実ホーム
# パスを書かないと動かない。消費リポジトリの .claude は home との間にリポジトリのセグメントが
# 挟まるので一致しない。
HOME_CLAUDE = re.compile(r"(?:^[a-z]:)?/(?:users|home)/[^/]+/\.claude(?:/|\.json)")
# バージョン見出し下の記述と、調整手順書の進行状態。どちらも到達状態のみの範囲外。
VERSION_LOG_PREFIXES = ("changelog", "tuning")
PROSE_SUFFIXES = (".md", ".markdown")

# group は対象パス判定での免除の単位、keep は一致を違反として数えるかの述語。
Atom = namedtuple("Atom", "group label pattern source remedy keep")


def _outside_placeholder(match):
    return match.group(1).lower() not in HOME_PLACEHOLDERS


def _word(group, word, source, remedy):
    return Atom(group, word, re.compile(re.escape(word)), source, remedy, None)


# この並び順が deny メッセージの列挙順になる。
ATOMS = (
    Atom("P1", "スクラッチ配下ファイルへの参照", SCRATCH_REFERENCE, HYGIENE_WORK,
         REMEDY_SCRATCH, None),
    # 裸の「今回」は「今回のリクエスト」のような実行時の概念を指す正当用法と区別できないので、
    # 作業過程の名詞と結合した複合形だけを採る。
    *(_word("P2", word, HYGIENE_WORK, REMEDY_RELATIVE) for word in (
        "改修前", "改修後", "今回の変更", "今回の修正", "今回の対応", "今回のレビュー",
        "今回の指摘", "今回のコミット", "今回の作業")),
    *(_word("P3", word, AUTHORING_SCOPE, REMEDY_STATE) for word in (
        "現時点では", "当面", "初期実装では", "段階導入", "先行導入")),
    Atom("P5", "ドライブ文字付きユーザーホームパス", HOME_PATH, HYGIENE_ENV,
         REMEDY_PATH, _outside_placeholder),
)

HEADER = ("[guard-artifact-hygiene] 書き込もうとしたテキストに次の記述が新たに含まれています。"
          "当該記述の全出現を点検し、すべて書き直してください(1 箇所だけ直しても再び deny されます)。")
FOOTER = "既存の同種記述を消して数を合わせることはこの deny の対処ではありません。"

# 自己テストが使うファイルパス。検査対象かどうかはパス文字列だけで決まるので、実在は要らない。
DOC = "docs/module.md"                                # 検査対象の恒久仕様書
CODE_FILE = "scripts/tool.py"                         # 検査対象のコード
SKILL_DOC = "plugins/flow/skills/sample/SKILL.md"     # 指示層のスキル文書
CONVENTION_DOC = "plugins/flow/docs/sample.md"        # 指示層の規約文書
AGENT_DOC = "plugins/flow/agents/sample.md"           # 指示層のエージェント定義
HOOK_FILE = "plugins/flow/hooks/sample.py"            # パスを扱うフック
TEST_DOC = "plugins/flow/tests/fixtures/sample.md"    # テストデータ
REPO = "C:/Users/user/repo"                           # 消費リポジトリの絶対パス
WIN_DOC = "C:\\Users\\user\\repo\\docs\\module.md"    # 区切りがバックスラッシュの絶対パス
# 許可リストに無いユーザー名。P5 が deny になる形を表す。
HOME_LINE = 'root = "C:/Users/alice/x"\n'


def _edit(path, old, new, replace_all=False):
    """Edit の tool_input。replace_all は真のときだけ入れる(既定では key ごと現れない)。"""
    tool_input = {"file_path": path, "old_string": old, "new_string": new}
    if replace_all:
        tool_input["replace_all"] = True
    return ("Edit", tool_input)


def _write(path, content):
    return ("Write", {"file_path": path, "content": content})


# (tool_name, tool_input, 変更前全文, 期待する判定)。
CASES = [
    # 各原子が新たに増えたときの deny。
    (*_write(DOC, "詳しくは .scratch/plan.md を参照する。\n"), "", "deny"),
    (*_write(DOC, "詳しくは .scratch\\plan.md を参照する。\n"), "", "deny"),
    (*_write(DOC, "改修前の値は 3。\n"), "", "deny"),
    (*_write(DOC, "改修後の挙動を述べる。\n"), "", "deny"),
    (*_write(DOC, "今回の変更点を述べる。\n"), "", "deny"),
    (*_write(DOC, "今回の修正で入れた分岐。\n"), "", "deny"),
    (*_write(DOC, "今回の対応の範囲。\n"), "", "deny"),
    (*_write(DOC, "今回のレビューで挙がった点。\n"), "", "deny"),
    (*_write(DOC, "今回の指摘に沿って直す。\n"), "", "deny"),
    (*_write(DOC, "今回のコミットに含める。\n"), "", "deny"),
    (*_write(DOC, "今回の作業で足した節。\n"), "", "deny"),
    (*_write(DOC, "現時点では未対応。\n"), "", "deny"),
    (*_write(DOC, "当面はこの形で運用する。\n"), "", "deny"),
    (*_write(DOC, "初期実装では未採用。\n"), "", "deny"),
    (*_write(DOC, "段階導入の順序を決める。\n"), "", "deny"),
    (*_write(DOC, "先行導入した範囲から広げる。\n"), "", "deny"),
    (*_write(DOC, "ログは C:/Users/alice/logs にある。\n"), "", "deny"),
    (*_write(DOC, "ログは d:\\users\\alice\\logs にある。\n"), "", "deny"),
    # 境界の許可。
    (*_write(DOC, "一時ドキュメントは .scratch/ に置く。\n"), "", "allow"),
    (*_write(DOC, "置き場は .scratch/、退避先は別にする。\n"), "", "allow"),
    (*_write(DOC, "cache.scratch/report.md を読む。\n"), "", "allow"),
    (*_write(DOC, "詳細は .scratch/.draft.md を読む。\n"), "", "deny"),
    (*_write(DOC, "詳細は .scratch/a.md、.scratch/b.md を参照。\n"), "", "deny"),
    (*_write(DOC, "scripts/run_selftests.py を実行する。\n"), "", "allow"),
    (*_write(CODE_FILE, "# 段階導入の順序を決める\n"), "", "allow"),
    # P1・P5 の適用は Markdown に限らない(拡張子による免除は P2・P3 だけ)。
    (*_write(CODE_FILE, HOME_LINE), "", "deny"),
    (*_write(CODE_FILE, "# 詳細は .scratch/plan.md を見る\n"), "", "deny"),
    # 複合形の境界。裸の語に一致させると実行時の概念を指す正当用法まで止まる。
    (*_write(DOC, "今回のリクエストで指定した ID を使う。\n"), "", "allow"),
    (*_write(DOC, "現時点のカーソル位置を返す。\n"), "", "allow"),
    # 前後全文の比較。
    (*_edit(DOC, "改修前", "改修後"), "改修前の値は 3。\n", "deny"),
    (*_edit(DOC, "他の行", "別の行"), "改修後の値は 3。\n他の行\n", "allow"),
    (*_edit(DOC, "改修後の値は 3。\n", ""), "改修後の値は 3。\n本文\n", "allow"),
    (*_write(DOC, "A\nB\n改修後\n"), "改修後\nA\nB\n", "allow"),
    # 原子が編集境界をまたいで生まれる形。断片はどちらも原子を含まない。
    (*_edit(DOC, "仕様", "変更"), "今回の仕様を述べる。\n", "deny"),
    # Edit ツール自体が成立しない入力は検査しない。
    (*_edit(DOC, "無い文字列", "改修後"), "本文\n", "allow"),
    (*_edit(DOC, "無い文字列", "改修後", replace_all=True), "本文\n", "allow"),
    (*_edit(DOC, "A", "改修後"), "A\nA\n", "allow"),
    (*_edit(DOC, "A", "改修後", replace_all=True), "A\nA\n", "deny"),
    # P5 の許可リストとプレースホルダ表記。
    (*_write(DOC, "設定は C:/Users/ユーザー名/.claude にある。\n"), "", "allow"),
    (*_write(DOC, "設定は C:/Users/user/.claude にある。\n"), "", "allow"),
    (*_write(DOC, "設定は C:/Users/<user>/.claude にある。\n"), "", "allow"),
    (*_write(DOC, "設定は C:/Users/{user}/.claude にある。\n"), "", "allow"),
    # P5 の表示区切り。捕捉に区切り記号が入らないので許可リスト照合が外れない。
    (*_write(DOC, "パスは `C:/Users/user` を使う。\n"), "", "allow"),
    # 許可リストの照合は大文字小文字を区別しない。Windows の共有プロファイルは実在の標準パス。
    (*_write(DOC, "共有は C:/Users/Public/Documents にある。\n"), "", "allow"),
    (*_write(DOC, "パスは (C:/Users/user) と C:/Users/user. の形。\n"), "", "allow"),
    # 原子分類別のパス判定。
    (*_write(".scratch/plan.md", "改修後 C:/Users/alice/x .scratch/a.md\n"), "", "allow"),
    (*_write("C:/Users/alice/AppData/Local/Temp/claude/x/note.md", "改修後\n"), "", "allow"),
    (*_write("/tmp/note.md", "改修後\n"), "", "allow"),
    # macOS のセッション一時領域($TMPDIR。実パスは /private を前置した形になる)。
    (*_write("/var/folders/ab/cd/T/claude/x/note.md", "改修後\n"), "", "allow"),
    (*_write("/private/var/folders/ab/cd/T/claude/x/note.md", "改修後\n"), "", "allow"),
    (*_write("/private/tmp/note.md", "改修後\n"), "", "allow"),
    # POSIX 形のホームパスは原子に採らない。REST の /users/{id} や、規約自身が推奨するダミー値
    # /home/user と衝突し、規約どおり書き直した結果を再び止めてしまうため。
    (*_write(DOC, "ログは /Users/alice/logs にある。\n"), "", "allow"),
    (*_write("C:/Users/alice/.claude/projects/x/memory/note.md",
             "改修後 C:/Users/alice/x\n"), "", "allow"),
    (*_write("src/tmp/note.md", "改修後の値。\n"), "", "deny"),
    (*_write("docs/note.markdown", "改修後の値。\n"), "", "deny"),
    (*_write(SKILL_DOC, "改修後の挙動。\n"), "", "allow"),
    (*_write(SKILL_DOC, "ログは C:/Users/alice/logs。\n"), "", "deny"),
    (*_write(SKILL_DOC, "詳しくは .scratch/plan.md を参照する。\n"), "", "allow"),
    (*_write(CONVENTION_DOC, "「改修前」「改修後」「今回」などの相対参照。\n"), "", "allow"),
    (*_write(CONVENTION_DOC, "ログは C:/Users/alice/logs。\n"), "", "deny"),
    (*_write(AGENT_DOC, "改修後の挙動。\n"), "", "allow"),
    (*_write(HOOK_FILE, HOME_LINE), "", "allow"),
    # 指示層はスクラッチ配下にファイルを作らせる生成指示を持つ。フック自身の保守編集も同じ免除で通る。
    (*_write(HOOK_FILE, "# 詳細は .scratch/plan.md を見る\n"), "", "allow"),
    (*_write("CLAUDE.md", "一時ファイルは .scratch/note.md に置く。\n"), "", "allow"),
    (*_write(f"{REPO}/.claude/agents/sample.md", "改修後の挙動。\n"), "", "allow"),
    (*_write(f"{REPO}/.claude/agents/sample.md", "ログは C:/Users/alice/logs。\n"), "", "deny"),
    (*_write(f"{REPO}/.claude/hooks/sample.py", HOME_LINE), "", "allow"),
    # ホーム直下の .claude は個人設定の置き場で、実ホームパスを書く用法が正当。消費リポジトリの
    # .claude は同じ名前でも成果物なので検査する(直上の 2 件がその形をピンしている)。
    (*_write("C:/Users/alice/.claude/settings.json", HOME_LINE), "", "allow"),
    (*_write("C:/Users/alice/.claude/CLAUDE.md", "ログは C:/Users/alice/logs。\n"), "", "allow"),
    (*_write("/home/alice/.claude/agents/sample.md", "ログは C:/Users/alice/logs。\n"),
     "", "allow"),
    (*_write("C:/Users/alice/.claude.json", HOME_LINE), "", "allow"),
    (*_write("CLAUDE.md", "改修後の挙動。\n"), "", "allow"),
    (*_write("CLAUDE.md", "ログは C:/Users/alice/logs。\n"), "", "deny"),
    (*_write(TEST_DOC, "改修後 C:/Users/alice/x .scratch/a.md\n"), "", "allow"),
    (*_write("TUNING.md", "段階導入の手順。\n"), "", "allow"),
    (*_write("CHANGELOG.md", "段階導入の記録。\n"), "", "allow"),
    # パスの正規化(バックスラッシュ→スラッシュ・小文字化)を経て初めて成立する判定。実際の
    # ツール入力は Windows の絶対パスで届くので、正規化が抜けると免除が総崩れになる。
    (*_write(WIN_DOC, "改修後の値。\n"), "", "deny"),
    (*_write("C:\\Users\\user\\repo\\plugins\\flow\\skills\\sample\\SKILL.md",
             "改修後の挙動。\n"), "", "allow"),
    (*_write("C:\\Users\\user\\AppData\\Local\\Temp\\claude\\x\\note.md", "改修後\n"), "", "allow"),
    (*_write(f"{REPO}/.scratch/plan.md", "改修後 .scratch/a.md\n"), "", "allow"),
    # 検査対象外のツール・読めない入力。
    ("MultiEdit", {"file_path": DOC, "edits": [{"old_string": "A", "new_string": "改修後"}]},
     "A\n", "allow"),
    ("Bash", {"command": "ls"}, "", "allow"),
    ("Write", {}, "", "allow"),
    ("Write", {"file_path": DOC}, "", "allow"),
    ("Edit", {"file_path": DOC}, "本文\n", "allow"),
    ("Write", {"file_path": "", "content": "改修後\n"}, "", "allow"),
]

# (tool_name, 読み取りで出た例外, 期待する検査の続け方)。
READ_FAILURE_CASES = [
    ("Write", FileNotFoundError(), "empty"),
    ("Edit", FileNotFoundError(), "allow"),
    ("Write", PermissionError(), "allow"),
    ("Write", IsADirectoryError(), "allow"),
    ("Edit", PermissionError(), "allow"),
]

# (tool_name, tool_input, 変更前全文, deny 理由に必ず含まれる文字列)。
MESSAGE_CASES = [
    # 追加した側の行が候補行として出る(既存の違反行は前後に同数あり行の差に残らない)。
    (*_edit(DOC, "改修後の値は 3。\n", "改修後の値は 3。\n新しい改修後の行\n"),
     "改修後の値は 3。\n",
     ["(増加 1 件)", "新しい改修後の行", "根拠: flow の artifact-hygiene.md §2"]),
    # 同一原子が複数増えたら増加数と各行を示す。
    (*_write(DOC, "詳細は .scratch/a.md。\n続きは .scratch/b.md。\n"), "",
     ["スクラッチ配下ファイルへの参照", "(増加 2 件)", ".scratch/a.md", ".scratch/b.md"]),
    # 候補行は先頭から 3 行まで。4 行目以降は省略した候補行の行数で示す。
    (*_write(DOC, "".join(f"{i} 番目は .scratch/f{i}.md。\n" for i in range(5))), "",
     ["(増加 5 件)", "候補行 他 2 行"]),
    # 文面の骨格と、規約ごとの根拠表示。
    (*_write(DOC, "現時点では未対応。\n"), "",
     ["[guard-artifact-hygiene]", "当該記述の全出現を点検し、すべて書き直してください",
      "根拠: flow の document-authoring.md §2",
      "既存の同種記述を消して数を合わせることはこの deny の対処ではありません。"]),
    (*_write(DOC, "ログは C:/Users/alice/logs にある。\n"), "",
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
        exempt.update(atom.group for atom in ATOMS)
    if _is_instruction(path, name):
        exempt.update(("P1", "P2", "P3"))
    if not name.endswith(PROSE_SUFFIXES) or name.startswith(VERSION_LOG_PREFIXES):
        exempt.update(("P2", "P3"))
    if HOOK_LAYER.search(path) or "/.claude/hooks/" in path:
        exempt.add("P5")
    return tuple(atom for atom in ATOMS if atom.group not in exempt)


def _matches(atom, text):
    return [hit for hit in atom.pattern.finditer(text) if atom.keep is None or atom.keep(hit)]


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
    return line[max(0, match.start() - WINDOW):match.end() + WINDOW]


def _format_reason(increases, changed_lines):
    """増えた原子の一覧と変更が及んだ行から deny 理由の文面を組み立てる。"""
    parts = [HEADER]
    for atom, before_hits, after_hits in increases:
        excerpts = [(hits[0].group(0), _window(line, hits[0]))
                    for line, hits in ((line, _matches(atom, line)) for line in changed_lines)
                    if hits]
        source = f"根拠: flow の {atom.source}。対処: {atom.remedy}"
        if not excerpts:
            # 行の差から候補行を絞れない想定外の形。増加の事実だけを件数で伝える。
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
    for tool_name, tool_input, before, expected in CASES:
        actual = "deny" if evaluate(tool_name, tool_input, before) else "allow"
        if actual != expected:
            failures.append(f"expected={expected} actual={actual}: {tool_name} {tool_input!r}")
    for tool_name, error, expected in READ_FAILURE_CASES:
        actual = read_failure_action(tool_name, error)
        if actual != expected:
            failures.append(f"expected={expected} actual={actual}: {tool_name} {error!r}")
    for tool_name, tool_input, before, needles in MESSAGE_CASES:
        reason = evaluate(tool_name, tool_input, before) or ""
        for needle in needles:
            if needle not in reason:
                failures.append(f"missing {needle!r}: {reason!r}")

    # 複数の原子が増えたら定数表の並び順(スクラッチ参照が先)で列挙する。
    reason = evaluate(*_write(DOC, "改修後の値。\n詳細は .scratch/c.md。\n"), "") or ""
    if "スクラッチ配下ファイルへの参照" not in reason or "改修後" not in reason:
        failures.append(f"複数原子が列挙されていない: {reason!r}")
    elif reason.index("スクラッチ配下ファイルへの参照") > reason.index("改修後"):
        failures.append(f"原子の列挙が定数表の並び順でない: {reason!r}")

    # 候補行は変更が及んだ行に限る。変更前から残る行を抜粋すると、触っていない行の書き直しへ誘導する。
    reason = evaluate(*_edit(DOC, "改修後の値は 3。\n", "改修後の値は 3。\n新しい改修後の行\n"),
                      "改修後の値は 3。\n") or ""
    if "改修後の値は 3。" in reason:
        failures.append(f"変更が及んでいない既存行が候補行に出ている: {reason!r}")

    # 抜粋は一致箇所の前後 40 文字の窓に収める(長い行をそのまま貼らない)。
    long_line = "あ" * 100 + " .scratch/far.md " + "い" * 100
    reason = evaluate(*_write(DOC, long_line + "\n"), "") or ""
    if ".scratch/far.md" not in reason:
        failures.append(f"長い行の一致箇所が抜粋に入っていない: {reason!r}")
    if "あ" * 60 in reason:
        failures.append("抜粋が窓幅を超えている")

    # 候補行から原子が見つからない想定外の形では、件数だけを示す形式へ落とす。
    fallback = _format_reason([(ATOMS[0], 1, 2)], ["この行に原子は無い"])
    if "出現が 1 件から 2 件に増えました" not in fallback:
        failures.append(f"候補行が無い場合の代替文面が出ていない: {fallback!r}")

    if failures:
        for line in failures:
            print(f"FAIL {line}")
        raise SystemExit(1)
    total = len(CASES) + len(READ_FAILURE_CASES) + len(MESSAGE_CASES)
    print(f"ALL PASS ({total} cases + 列挙順・候補行・窓幅・代替文面の 4 検査)")


def main():
    # 既定の標準出力コーデック(日本語 Windows では cp932)には理由文が持つ記号が無く、
    # 出力時の UnicodeEncodeError でフックが無出力のまま落ちる。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
        return
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
