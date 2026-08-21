#!/usr/bin/env python3
"""プラグインのバージョン刻印スクリプト。plugin.json の version をコミット時刻から機械生成する。

version は「年.月日.時分秒」の3数値。各数値はゼロ埋めしない整数とする(semver の数値識別子は
先頭ゼロを許さないため。例: 2026年8月13日 9時4分55秒 なら 2026.813.90455)。単調性の保証:
生成値が既存 version 以下になる場合(同一秒の連続コミット・時計巻き戻り)は、既存の第3数値に
1を足した値を使う。既存 version には作業ツリーと HEAD のうち大きい方を採る。

呼び出し形:

    stamp_plugin_version.py                  明示刻印(冪等)。ステージに変更を含むプラグインを
                                             検出し、plugin.json を刻印してステージする
    stamp_plugin_version.py --verify-staged  pre-commit の検査のみ。刻印漏れがあれば実行すべき
                                             刻印コマンドを案内して非0終了(ステージは書き換えない)
    stamp_plugin_version.py --check RANGE    CI の検査。RANGE は base..head、または単一リビジョン
                                             (単一なら到達可能な全コミットが対象)。コミット単位・
                                             version 値基準で検査し、違反をコミットとプラグインの
                                             組で列挙して非0終了。親が複数のコミット(マージ)は
                                             検査対象外
    stamp_plugin_version.py --bump NAME      回復用。指定プラグインの plugin.json の version だけを
                                             現在時刻で刻印してステージする
    stamp_plugin_version.py --selftest       生成・比較・検出の各判定ロジックの自己テスト

明示刻印のプラグインごとの評価順:

1. ステージ境界の保護: 刻印対象の plugin.json に未ステージの差分が既にある場合は、書き換え・
   ステージを行わずエラーで終了する(ステージがその未ステージ差分まで取り込み、意図しない内容が
   コミットへ向かうため。スキップ判定より先に評価する)。
2. 冪等スキップ: ステージ済み diff で version が既に HEAD から増加しているプラグインはスキップする。
3. 刻印してステージする。

HEAD が存在しない(リポジトリ最初のコミット前)、または HEAD に該当 plugin.json が無い
(新規プラグイン)場合は比較基準なしとして扱う: ステージ済み plugin.json に有効な3数値 version が
あればスキップ、無ければ現在時刻で刻印する。--verify-staged も同じ条件では「有効な3数値 version が
ステージに存在すること」の要求に切り替わる。
"""
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = "plugins"
HOOKS_DIR = ".githooks"
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
STAMP_COMMAND = "python3 scripts/stamp_plugin_version.py"


def run_git(*args, check=True):
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        print("git コマンドが失敗した: " + " ".join(args))
        print(result.stderr.strip())
        raise SystemExit(1)
    return result


def split_z(stdout):
    return [line for line in stdout.split("\0") if line]


def plugin_json_path(name):
    return f"{PLUGINS_DIR}/{name}/.claude-plugin/plugin.json"


def parse_version(text):
    """有効な3数値 version なら (a, b, c) を、無効なら None を返す。"""
    if not isinstance(text, str):
        return None
    match = VERSION_RE.match(text)
    return tuple(int(group) for group in match.groups()) if match else None


def format_version(version):
    return f"{version[0]}.{version[1]}.{version[2]}"


def blob_exists(rev, path):
    return run_git("cat-file", "-e", f"{rev}:{path}", check=False).returncode == 0


def version_of(spec):
    """git の rev:path 指定で plugin.json を読み version タプルを返す。読めなければ None。"""
    result = run_git("show", spec, check=False)
    if result.returncode != 0:
        return None
    try:
        return parse_version(json.loads(result.stdout).get("version"))
    except (json.JSONDecodeError, AttributeError):
        return None


def has_head():
    return run_git("rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0


def staged_paths(head):
    if head:
        return split_z(run_git("diff", "--cached", "--name-only", "-z").stdout)
    # 最初のコミット前は index の全内容がステージ分にあたる
    return split_z(run_git("ls-files", "--cached", "-z").stdout)


def touched_plugins(paths):
    names = set()
    for path in paths:
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == PLUGINS_DIR:
            names.add(parts[1])
    return sorted(names)


def generate_version(now, existing):
    candidate = (
        now.year,
        now.month * 100 + now.day,
        now.hour * 10000 + now.minute * 100 + now.second,
    )
    if existing is not None and candidate <= existing:
        return (existing[0], existing[1], existing[2] + 1)
    return candidate


def worktree_version(name):
    path = REPO_ROOT / plugin_json_path(name)
    try:
        return parse_version(json.loads(path.read_text(encoding="utf-8")).get("version"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def stamp_and_stage(name, now, head):
    """plugin.json の version を刻印してステージし、刻んだ値を返す。

    単調性の基準には作業ツリーと HEAD のうち大きい方の version を使う。作業ツリー側だけを基準に
    すると、version が HEAD より低い値へ手で書き換えられている状態で時計が HEAD の刻印時刻より前を
    指す場合に、刻んだ値が HEAD 以下のままになり、検査が拒否し続けて刻印し直しても収束しない。
    """
    rel = plugin_json_path(name)
    path = REPO_ROOT / rel
    data = json.loads(path.read_text(encoding="utf-8"))
    baselines = [v for v in (worktree_version(name), head_baseline(name, head)) if v is not None]
    version = generate_version(now, max(baselines) if baselines else None)
    data["version"] = format_version(version)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_git("add", "--", rel)
    return version


def head_baseline(name, head):
    """HEAD 側の比較基準 version。HEAD 無し・plugin.json 無し・無効 version は None(基準なし)。"""
    if not head:
        return None
    return version_of(f"HEAD:{plugin_json_path(name)}")


def is_increased(current, baseline):
    """比較基準が無ければ有効な version の存在を、あれば増加を要求する。"""
    if baseline is None:
        return current is not None
    return current is not None and current > baseline


def require_hooks_installed():
    """core.hooksPath が向いていなければ、設定コマンドを案内して False を返す。

    刻印はコミットの直前に行う。この時点で hooksPath が未設定だと、刻印漏れの検査もコミット
    メッセージの検査も無言で素通りする。設定漏れは検出できるのでここで止める。
    """
    configured = run_git("config", "core.hooksPath", check=False).stdout.strip()
    if configured == HOOKS_DIR:
        return True
    state = f"現在: {configured}" if configured else "未設定"
    print(
        f"エラー: core.hooksPath が {HOOKS_DIR} を指していない({state})。"
        f"このままコミットするとゲートが働かない。\n"
        f"  git config core.hooksPath {HOOKS_DIR}"
    )
    return False


def cmd_stamp():
    if not require_hooks_installed():
        return 1
    head = has_head()
    plugins = touched_plugins(staged_paths(head))
    if not plugins:
        print("ステージにプラグイン配下の変更は無い(刻印対象なし)")
        return 0
    now = datetime.now()
    for name in plugins:
        rel = plugin_json_path(name)
        if not (REPO_ROOT / rel).is_file():
            if head and blob_exists("HEAD", rel):
                print(f"{name}: plugin.json が削除されている(プラグイン削除)ためスキップ")
                continue
            print(f"エラー: {rel} が存在しない。プラグインには plugin.json が必要")
            return 1
        # (1) ステージ境界の保護(スキップ判定より先に評価する)
        if run_git("diff", "--name-only", "--", rel).stdout.strip():
            print(
                f"エラー: {rel} に未ステージの差分がある。刻印はステージを書き換えるため、"
                "その差分をステージするか退避してから刻印し直すこと"
            )
            return 1
        # (2) 冪等スキップ
        baseline = head_baseline(name, head)
        staged = version_of(f":{rel}")
        if is_increased(staged, baseline):
            print(f"{name}: 刻印済み({format_version(staged)})のためスキップ")
            continue
        # (3) 刻印してステージ
        version = stamp_and_stage(name, now, head)
        print(f"{name}: version {format_version(version)} を刻印してステージした")
    return 0


def cmd_verify_staged():
    head = has_head()
    violations = []
    for name in touched_plugins(staged_paths(head)):
        rel = plugin_json_path(name)
        if not run_git("ls-files", "--cached", "--", rel).stdout.strip():
            if head and blob_exists("HEAD", rel):
                continue  # プラグイン削除はコミットしてよい
            violations.append(name)
            continue
        if not is_increased(version_of(f":{rel}"), head_baseline(name, head)):
            violations.append(name)
    if violations:
        for name in violations:
            print(
                f"刻印漏れ: プラグイン {name} の変更がステージされているのに、"
                f"{plugin_json_path(name)} の version 増加がステージに含まれていない"
            )
        print(f"明示刻印を実行してからコミットし直すこと: {STAMP_COMMAND}")
        return 1
    return 0


def commit_files(rev, parent):
    if parent:
        return split_z(run_git("diff", "--name-only", "-z", parent, rev).stdout)
    return split_z(run_git("ls-tree", "-r", "--name-only", "-z", rev).stdout)


def commit_exists(rev):
    return run_git("cat-file", "-e", f"{rev}^{{commit}}", check=False).returncode == 0


def resolve_range(range_spec):
    """比較範囲の起点がクローンに無ければ、終点単独へ落として返す。

    履歴を書き換えて force push すると、push イベントの before は書き換え前の先端を指すが、
    その commit は書き換え後のリモートから到達不能でクローンにも入らない。そのまま rev-list へ
    渡すとエラーで落ちるので、新規 ref と同じ扱い(終点から到達可能な全コミット)にする。
    検査対象はむしろ広がるため、取りこぼしは生じない。
    """
    if ".." not in range_spec:
        return range_spec
    base, head = range_spec.split("..", 1)
    if not base or commit_exists(base):
        return range_spec
    print(f"起点 {base[:12]} がクローンに無い(履歴の書き換え等)。終点から到達可能な全コミットを検査する")
    return head


def cmd_check(range_spec):
    # 浅いクローンでは親が graft 境界で切られ、境界のコミットがルート扱い(新規プラグイン規則)に
    # 緩んで未刻印を素通りさせる。検査できないことを赤で知らせる。
    if run_git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        print("エラー: クローンが浅く、コミット単位の検査ができない。checkout の fetch-depth を 0 にすること")
        return 1
    violations = []
    checked = 0
    for rev in run_git("rev-list", resolve_range(range_spec)).stdout.split():
        parents = run_git("rev-list", "--parents", "-n", "1", rev).stdout.split()[1:]
        if len(parents) >= 2:
            continue  # マージコミットは比較親が一意でないため対象外
        checked += 1
        parent = parents[0] if parents else None
        for name in touched_plugins(commit_files(rev, parent)):
            rel = plugin_json_path(name)
            if not blob_exists(rev, rel):
                continue  # このコミットでプラグイン自体が削除されている
            baseline = version_of(f"{parent}:{rel}") if parent else None
            if not is_increased(version_of(f"{rev}:{rel}"), baseline):
                violations.append((rev, name))
    if violations:
        for rev, name in violations:
            print(f"刻印違反: コミット {rev[:12]} のプラグイン {name} で version の3数値が増加していない")
        print(f"回復は {STAMP_COMMAND} --bump プラグイン名 で version を刻み直す")
        print("報告された全プラグインを刻み直すこと。一部を残すと、次の検査範囲が回復コミットだけに"
              "なり、残した未刻印がこの検査を素通りする")
        return 1
    print(f"刻印検査 OK({checked}コミット。マージコミットは対象外)")
    return 0


def cmd_bump(name):
    rel = plugin_json_path(name)
    if not (REPO_ROOT / rel).is_file():
        print(f"エラー: {rel} が存在しない")
        return 1
    version = stamp_and_stage(name, datetime.now(), has_head())
    print(f"{name}: version {format_version(version)} を刻印してステージした")
    return 0


def _selftest():
    ok = True
    generate_cases = [
        ("既存なし", datetime(2026, 8, 22, 4, 9, 52), None, (2026, 822, 40952)),
        ("同一秒", datetime(2026, 8, 22, 4, 9, 52), (2026, 822, 40952), (2026, 822, 40953)),
        ("時計巻き戻り", datetime(2026, 8, 22, 4, 9, 52), (2026, 823, 10000), (2026, 823, 10001)),
        ("既存が小さい", datetime(2026, 8, 22, 4, 9, 52), (2026, 821, 191537), (2026, 822, 40952)),
        ("先頭ゼロなし", datetime(2026, 1, 2, 3, 4, 5), None, (2026, 102, 30405)),
    ]
    for name, now, existing, want in generate_cases:
        got = generate_version(now, existing)
        if got != want:
            ok = False
            print(f"FAIL generate_version {name}: want={want} got={got}")
        if parse_version(format_version(got)) != got:
            ok = False
            print(f"FAIL generate_version {name}: {format_version(got)} が有効な version でない")

    increased_cases = [
        ("基準なし・値あり", (1, 0, 0), None, True),
        ("基準なし・値なし", None, None, False),
        ("増加", (2026, 822, 2), (2026, 822, 1), True),
        ("同値", (2026, 822, 1), (2026, 822, 1), False),
        ("減少", (2026, 821, 9), (2026, 822, 1), False),
        ("値なし", None, (2026, 822, 1), False),
    ]
    for name, current, baseline, want in increased_cases:
        got = is_increased(current, baseline)
        if got != want:
            ok = False
            print(f"FAIL is_increased {name}: want={want} got={got}")

    parse_cases = [("1.2.3", (1, 2, 3)), ("2026.822.40952", (2026, 822, 40952)),
                   ("01.2.3", None), ("1.2", None), ("1.2.3.4", None), ("", None), (None, None)]
    for text, want in parse_cases:
        got = parse_version(text)
        if got != want:
            ok = False
            print(f"FAIL parse_version {text!r}: want={want} got={got}")

    touched_cases = [
        (["plugins/flow/docs/adoption.md", "scripts/x.py", "plugins/guard/hooks/a.py"], ["flow", "guard"]),
        (["scripts/x.py", "README.md"], []),
        (["plugins"], []),
    ]
    for paths, want in touched_cases:
        got = touched_plugins(paths)
        if got != want:
            ok = False
            print(f"FAIL touched_plugins {paths}: want={want} got={got}")

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


def main(argv):
    if argv == ["--selftest"]:
        return _selftest()
    if not argv:
        return cmd_stamp()
    if argv == ["--verify-staged"]:
        return cmd_verify_staged()
    if len(argv) == 2 and argv[0] == "--check":
        return cmd_check(argv[1])
    if len(argv) == 2 and argv[0] == "--bump":
        return cmd_bump(argv[1])
    print("usage: stamp_plugin_version.py "
          "[--verify-staged | --check RANGE | --bump NAME | --selftest]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
