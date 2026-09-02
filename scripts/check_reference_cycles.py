"""ドキュメント間の参照に循環が無いかを検査する。

参照はすべてMarkdownリンクとして書く規約(正本は flow プラグイン同梱の cross-references.md)が
あるので、追跡下の Markdown を頂点、リンクを辺とする有向グラフがそのまま参照グラフになる。
循環——辿って元の文書へ戻る経路——を強連結成分の検出で見つけ、経路を挙げて終了コード1で終わる。
どちらが正本かの判断は要らず、経路の有無だけで違反が決まるので機械で確定できる。

対象は追跡下の `.md` どうしのリンクに限る。同一文書内のアンカーだけを指すリンク、リポジトリ外
(http・mailto)、追跡下に無いパス、コードブロック・インラインコードの中の例示は辺にしない。

使い方: python3 scripts/check_reference_cycles.py
自己テスト: python3 scripts/check_reference_cycles.py --selftest
終了コード: 循環が無ければ 0、1件でもあれば 1。
"""
import posixpath
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

INLINE_LINK_RE = re.compile(r'\[((?:[^\[\]\n]|\n(?!\n))*)\]\(([^)\n]*)\)')
FENCE_RE = re.compile(r'```.*?```', re.DOTALL)
INLINE_CODE_RE = re.compile(r'`[^`\n]+`')


def git_ls_files(*args) -> list[str]:
    out = subprocess.run(
        ['git', 'ls-files', '-z', *args], cwd=REPO_ROOT,
        capture_output=True, check=True,
    ).stdout.decode('utf-8')
    return [line for line in out.split('\0') if line]


def blank_code(text: str) -> str:
    """コードブロック・インラインコードの中の例示リンクを辺に数えない。行数と桁は保つ。"""
    without_fences = FENCE_RE.sub(lambda m: '\n' * m.group(0).count('\n'), text)
    return INLINE_CODE_RE.sub(lambda m: ' ' * len(m.group(0)), without_fences)


def edges_of(rel_path: str, text: str, tracked_md: set) -> set:
    """1ファイルが張る参照先(追跡下の別 Markdown)を返す。"""
    targets = set()
    for match in INLINE_LINK_RE.finditer(blank_code(text)):
        target = match.group(2).split('#')[0].strip()
        if not target or target.startswith(('http://', 'https://', 'mailto:')):
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel_path), target))
        if resolved in tracked_md and resolved != rel_path:
            targets.add(resolved)
    return targets


def find_cycles(graph: dict) -> list:
    """強連結成分のうち頂点2つ以上のものを、成分ごとの経路として返す(Tarjan)。"""
    index: dict = {}
    low: dict = {}
    on_stack: set = set()
    stack: list = []
    counter = [0]
    components: list = []

    def strongconnect(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in index:
                strongconnect(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component = []
            while True:
                popped = stack.pop()
                on_stack.discard(popped)
                component.append(popped)
                if popped == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            strongconnect(node)
    return components


def build_graph(files: list, read) -> dict:
    tracked_md = set(files)
    return {rel: edges_of(rel, read(rel), tracked_md) for rel in files}


def selftest() -> int:
    failures = []

    def graph_of(sources: dict) -> dict:
        return build_graph(sorted(sources), lambda rel: sources[rel])

    two_way = graph_of({
        'a.md': '[b](b.md)\n',
        'b.md': '[a](a.md)\n',
    })
    if len(find_cycles(two_way)) != 1:
        failures.append('FAIL 2文書の相互参照が検出されない')

    three_hop = graph_of({
        'a.md': '[b](b.md)\n',
        'b.md': '[c](c.md)\n',
        'c.md': '[a](a.md)\n',
    })
    if len(find_cycles(three_hop)) != 1:
        failures.append('FAIL 3文書を経由する循環が検出されない')

    one_way = graph_of({
        'a.md': '[b](b.md) [c](c.md)\n',
        'b.md': '[c](c.md)\n',
        'c.md': '本文だけ\n',
    })
    if find_cycles(one_way):
        failures.append('FAIL 片方向の参照を循環と誤検出')

    anchor_only = graph_of({'a.md': '[節](#1-見出し)\n'})
    if find_cycles(anchor_only) or anchor_only['a.md']:
        failures.append('FAIL 同一文書内のアンカー参照を辺にしている')

    fenced = graph_of({
        'a.md': '```\n[b](b.md)\n```\n',
        'b.md': '[a](a.md)\n',
    })
    if find_cycles(fenced):
        failures.append('FAIL コードブロック内のリンクを辺にしている')

    external = graph_of({'a.md': '[外部](https://example.com/a.md)\n'})
    if external['a.md']:
        failures.append('FAIL 外部リンクを辺にしている')

    inline_code = graph_of({
        'a.md': '書き方の例: `[表示](b.md)`\n',
        'b.md': '[a](a.md)\n',
    })
    if find_cycles(inline_code) or inline_code['a.md']:
        failures.append('FAIL インラインコード内のリンクを辺にしている')

    untracked = graph_of({'a.md': '[未追跡](missing.md)\n'})
    if untracked['a.md']:
        failures.append('FAIL 追跡下に無いパスを辺にしている')

    if failures:
        print('\n'.join(failures))
        return 1
    print('ALL PASS (8 cases)')
    return 0


def main() -> None:
    files = git_ls_files('*.md')
    graph = build_graph(files, lambda rel: (REPO_ROOT / rel).read_text(encoding='utf-8'))
    components = find_cycles(graph)
    if components:
        for component in components:
            print('循環: ' + ' / '.join(component))
            for node in component:
                for nxt in sorted(graph[node]):
                    if nxt in component:
                        print(f'  {node} -> {nxt}')
        print(f'\n{len(components)}件の循環が見つかった。どちらが正本かを決めて片方向にすること。')
        sys.exit(1)
    print(f'参照の循環検査 OK({len(files)}ファイル)')


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(selftest())
    main()
