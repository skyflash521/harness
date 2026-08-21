"""節参照 `§N` と裸の `.md` パス参照が平文で残っていないかを検査する。

Markdownリンクとして書かれた参照は lychee がリンク切れを検査できるが、平文表記(そもそも
リンクでない)は lychee の検査対象外のため、この再発防止チェッカーで別途検査する。
節参照は必ず `[表示](path.md#アンカー)`、ファイル相互参照は必ず `[表示](path.md)` として書く
規約(正本は flow プラグイン同梱の cross-references.md)への適合を機械的に確認する。
違反があればファイル:行を列挙して終了コード1で終わる。

裸の `.md` パスは、参照元ファイルの親ディレクトリ基準・リポジトリルート基準のどちらかで
追跡ファイル集合(git ls-files)へ解決される場合だけ違反とする。どちらにも解決されない平文パス
——消費リポジトリの契約ファイルパス等、リポジトリ外を指すもの——は書けるようにするため。
バッククォートのインラインコード内は検査対象外とする(消費リポジトリのファイル名と同名を
harness 自身も追跡しているケースを本文中で言及できるようにするため。リンク先の実在は lychee が
別途担保する)。
"""
import posixpath
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

INLINE_LINK_RE = re.compile(r'\[((?:[^\[\]\n]|\n(?!\n))*)\]\(([^)\n]*)\)')
SECTION_TOKEN_RE = re.compile(r'§\s?\d+(?:\.\d+){0,2}')
BARE_MD_RE = re.compile(r'[A-Za-z0-9_./-]+\.md')
FENCE_RE = re.compile(r'```.*?```', re.DOTALL)
INLINE_CODE_RE = re.compile(r'`[^`\n]+`')
FRONTMATTER_RE = re.compile(r'\A---\n.*?\n---\n', re.DOTALL)


def git_ls_files(*args) -> list[str]:
    out = subprocess.run(
        ['git', 'ls-files', '-z', *args], cwd=REPO_ROOT,
        capture_output=True, check=True,
    ).stdout.decode('utf-8')
    return [line for line in out.split('\0') if line]


def line_of(text: str, pos: int) -> int:
    return text.count('\n', 0, pos) + 1


def strip_frontmatter(text: str) -> str:
    """先頭のYAML frontmatter(--- ... ---)はMarkdown本文でなく描画もされないため検査対象外とする。
    行番号を保つため、除いた分を同じ行数の空行に置き換える(削除はしない)。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return text
    return '\n' * m.group(0).count('\n') + text[m.end():]


def find_link_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for m in INLINE_LINK_RE.finditer(text):
        spans.append(m.span(1))
        spans.append(m.span(2))
    return spans


def in_any_span(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def check_section_tokens(rel_path: str, text: str) -> list[str]:
    violations = []
    links = list(INLINE_LINK_RE.finditer(text))

    def covering(pos: int, group: int):
        for lk in links:
            start, end = lk.span(group)
            if start <= pos < end:
                return lk
        return None

    for m in SECTION_TOKEN_RE.finditer(text):
        start = m.start()
        if covering(start, 2) is not None:
            continue  # URL内は対象外(既存リンクとして正当)
        text_link = covering(start, 1)
        if text_link is not None:
            if '#' in text_link.group(2):
                continue  # 表示テキスト内・アンカー付きは正当
            violations.append(
                f'{rel_path}:{line_of(text, start)}: 節参照 {m.group(0)!r} を含むリンクにアンカー(#)が無い'
            )
            continue
        violations.append(f'{rel_path}:{line_of(text, start)}: 平文の節参照 {m.group(0)!r} が残っている')
    return violations


def resolves_to_tracked(candidate: str, rel_path: str, tracked: set[str]) -> bool:
    """裸パスが参照元の親ディレクトリ基準またはリポジトリルート基準で追跡ファイルへ解決されるか。"""
    base = posixpath.dirname(rel_path)
    for path in {candidate, posixpath.join(base, candidate) if base else candidate}:
        normalized = posixpath.normpath(path)
        if normalized == '..' or normalized.startswith('../'):
            continue  # リポジトリ外へ抜ける相対パスは追跡ファイルに解決されない
        if normalized in tracked:
            return True
    return False


def check_bare_md(rel_path: str, text: str, tracked: set[str]) -> list[str]:
    violations = []
    link_spans = find_link_spans(text)
    fence_spans = [m.span() for m in FENCE_RE.finditer(text)]
    code_spans = [m.span() for m in INLINE_CODE_RE.finditer(text)]

    first_line_end = text.find('\n')
    first_line = text[:first_line_end] if first_line_end != -1 else text
    first_line_span = (
        (0, first_line_end if first_line_end != -1 else len(text))
        if first_line.startswith('# ') else None
    )

    for m in BARE_MD_RE.finditer(text):
        start = m.start()
        if in_any_span(start, link_spans):
            continue
        if first_line_span is not None and first_line_span[0] <= start < first_line_span[1]:
            continue
        if in_any_span(start, fence_spans) or in_any_span(start, code_spans):
            continue
        line_start = text.rfind('\n', 0, start) + 1
        prefix = text[line_start:start]
        if '<' in prefix and prefix.count('<') > prefix.count('>'):
            continue
        # `<ツール>/x.md` の `>` 直後、`*/SKILL.md` の `*` 直後はプレースホルダー・globパターンの
        # 断片であり実ファイルではないため対象外とする
        if start > 0 and text[start - 1] in ('>', '*'):
            continue
        if not resolves_to_tracked(m.group(0), rel_path, tracked):
            continue
        violations.append(f'{rel_path}:{line_of(text, start)}: 裸の.md参照 {m.group(0)!r} がリンク化されていない')
    return violations


def main() -> None:
    all_md = git_ls_files('*.md')
    tracked = set(git_ls_files())

    violations: list[str] = []
    for rel_path in all_md:
        text = strip_frontmatter((REPO_ROOT / rel_path).read_text(encoding='utf-8'))
        violations.extend(check_section_tokens(rel_path, text))
        violations.extend(check_bare_md(rel_path, text, tracked))

    if violations:
        print('\n'.join(violations))
        print(f'\n{len(violations)}件の平文参照が見つかった。Markdownリンクへ書き換えること。')
        sys.exit(1)
    print(f'節参照・ファイル参照の検査 OK({len(all_md)}ファイル)')


if __name__ == '__main__':
    main()
