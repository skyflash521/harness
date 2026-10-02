#!/usr/bin/env python3
"""自己テストの対象になる契約検査スクリプト。

呼び出し形: python3 check_codex_skills.py [--selftest]
終了コード: 0=契約が揃っている、1=欠落または自己テスト失敗。
モデルが指示に従うかは実ハーネスで別途確認する。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXECUTION = "docs/guidance/codex-execution.md"
REQUIREMENTS = {
    EXECUTION: (
        "終了コード", "modelUsage", "turn.completed", "wait.py",
    ),
    "skills/codex-consult/SKILL.md": (
        "codex-execution.md", "codex exec", "作業ディレクトリ", "標準入力",
        "workspace-write", "正常終了", "起動失敗", "時間上限", "usage-limit-response.md",
    ),
    "skills/codex-watchdog/SKILL.md": (
        "codex-execution.md",
    ),
    "skills/run-and-bench/SKILL.md": (
        "codex-execution.md",
    ),
    "skills/autonomous-dev/SKILL.md": (
        "codex-execution.md", "review-loop-judgement/SKILL.md", "commit/SKILL.md",
    ),
    "docs/guidance/usage-limit-response.md": (
        "codex-execution.md#4-",
    ),
}


def codex_section(text):
    match = re.search(r"^## Codex 上で実行するとき\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        return None
    return match.group(1)


def check_documents(documents):
    problems = []
    for path, required in REQUIREMENTS.items():
        text = documents.get(path)
        if text is None:
            problems.append(f"{path}: 文書が無い")
            continue
        if path.startswith("skills/"):
            text = codex_section(text)
            if text is None:
                problems.append(f"{path}: Codex 分岐が無い")
                continue
        for clause in required:
            if clause not in text:
                problems.append(f"{path}: 契約の記述が無い: {clause}")
    return problems


def read_documents():
    return {path: (ROOT / path).read_text(encoding="utf-8") for path in REQUIREMENTS if (ROOT / path).is_file()}


def selftest():
    documents = read_documents()
    failures = check_documents(documents)
    for path, clauses in REQUIREMENTS.items():
        for clause in clauses:
            changed = dict(documents)
            changed[path] = changed.get(path, "").replace(clause, "")
            if not check_documents(changed):
                failures.append(f"{path}: 欠落を検出しない: {clause}")
        changed = dict(documents)
        changed.pop(path, None)
        if not check_documents(changed):
            failures.append(f"{path}: 文書の欠落を検出しない")
    for path in REQUIREMENTS:
        if path.startswith("skills/"):
            changed = dict(documents)
            changed[path] = changed.get(path, "").replace("## Codex 上で実行するとき", "## Claude Code 上の経路")
            if not check_documents(changed):
                failures.append(f"{path}: Codex 分岐の欠落を検出しない")
    for failure in failures:
        print("FAIL " + failure)
    print("ALL PASS" if not failures else "SOME FAILED")
    return 1 if failures else 0


def main(argv):
    if argv == ["--selftest"]:
        return selftest()
    if argv:
        print(__doc__)
        return 1
    problems = check_documents(read_documents())
    for problem in problems:
        print(problem)
    print("Codex スキル契約 OK" if not problems else "Codex スキル契約に不備あり")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
