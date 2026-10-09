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
EXECUTION = "docs/guidance/execution.md"
REQUIREMENTS = {
    EXECUTION: (
        "Claude Code 上", "Codex 上", "終了コード", "turn.completed", "wait.py",
    ),
    "skills/opus-review-loop/SKILL.md": ("modelUsage", "出力トークン数が最大", "claude-opus-*"),
    "skills/fable-review-loop/SKILL.md": ("modelUsage", "出力トークン数が最大", "claude-fable-*"),
    "skills/codex-consult/SKILL.md": (
        "execution.md", "codex exec", "作業ディレクトリ", "標準入力",
        "workspace-write", "正常終了", "起動失敗", "時間上限", "usage-limit-response.md",
        "モデルが利用不可なら代替せず停止して報告する",
    ),
    "skills/codex-watchdog/SKILL.md": (
        "execution.md",
    ),
    "skills/run-and-bench/SKILL.md": (
        "execution.md",
    ),
    "skills/autonomous-dev/SKILL.md": (
        "execution.md", "review-loop-judgement/SKILL.md", "commit/SKILL.md",
    ),
    "docs/guidance/usage-limit-response.md": (
        "execution.md#4-",
    ),
}


def constant(path, name):
    """path の Python ソースが `name = <整数>` で定める値。見つからなければ None。"""
    match = re.search(rf"^{name} = (\d+)$", (ROOT / path).read_text(encoding="utf-8"), re.M)
    return int(match.group(1)) if match else None


STALL = constant("scripts/reap_codex_jobs.py", "STALL_SECONDS")
WAIT_CAP = constant("hooks/guard-idle-stop.py", "WAIT_CAP_SECS")
ROUND_CAP = f"[1ラウンドの上限](../review-loop-judgement/SKILL.md#1ラウンドが時間内に終わらないとき) の{WAIT_CAP}秒"
THRESHOLDS = {
    "scripts/reap_codex_jobs.py": (f"既定と同じ{STALL}秒",),
    "skills/codex-watchdog/watchdog.sh": (f'STALL_SECS="${{1:-{STALL}}}"', f'WALL_CAP_SECS="${{2:-{WAIT_CAP}}}"'),
    "skills/codex-watchdog/SKILL.md": (f"`STALL_SECS={STALL}`・`WALL_CAP_SECS={WAIT_CAP}`",),
    "skills/review-loop-judgement/SKILL.md": (f"1ラウンドをハングと判断する時間は{WAIT_CAP}秒",),
    "skills/codex-review-loop/SKILL.md": (ROUND_CAP,),
    "skills/opus-review-loop/SKILL.md": (ROUND_CAP,),
    "skills/fable-review-loop/SKILL.md": (ROUND_CAP,),
    "skills/codex-consult/SKILL.md": (f"単発相談の時間上限は{WAIT_CAP}秒",),
}


def check_thresholds(documents):
    if STALL is None or WAIT_CAP is None:
        return ["閾値の正本(STALL_SECONDS・WAIT_CAP_SECS)を読めない"]
    problems = []
    for path, required in THRESHOLDS.items():
        text = documents.get(path)
        if text is None:
            problems.append(f"{path}: 文書が無い")
            continue
        problems += [f"{path}: 閾値が正本と一致しない: {clause}" for clause in required if clause not in text]
    return problems


TWINS = (
    ("skills/opus-review-loop/SKILL.md", "skills/fable-review-loop/SKILL.md"),
    ("agents/opus-reviewer.md", "agents/fable-reviewer.md"),
)
MODELS = ("opus", "fable")


def without_own_model(path, text):
    model = next(name for name in MODELS if name in path)
    return re.sub(model, "", text, flags=re.IGNORECASE)


def check_twins(documents):
    problems = []
    for left, right in TWINS:
        if left not in documents or right not in documents:
            problems.append(f"{left} と {right}: 文書が無い")
        elif without_own_model(left, documents[left]) != without_own_model(right, documents[right]):
            problems.append(f"{left} と {right}: モデル名以外の本文が食い違う、または他方のモデル名が混入している")
    return problems


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
        full_text = text
        if path.startswith("skills/"):
            text = codex_section(text)
            if text is None:
                problems.append(f"{path}: Codex 分岐が無い")
                continue
        for clause in required:
            contract_text = full_text if clause == "execution.md" else text
            if clause not in contract_text:
                problems.append(f"{path}: 契約の記述が無い: {clause}")
    return problems


def read_documents():
    paths = [*REQUIREMENTS, *THRESHOLDS, *(path for twin in TWINS for path in twin)]
    return {path: (ROOT / path).read_text(encoding="utf-8") for path in paths if (ROOT / path).is_file()}


def selftest():
    documents = read_documents()
    failures = check_documents(documents) + check_twins(documents) + check_thresholds(documents)
    for left, right in TWINS:
        changed = dict(documents)
        changed[left] += "追記"
        if not check_twins(changed):
            failures.append(f"{left}: 双子とのずれを検出しない")
        changed = dict(documents)
        changed[left] = changed[left].replace("opus", "fable", 1)
        if not check_twins(changed):
            failures.append(f"{left}: 他方のモデル名への置換を検出しない")
        changed = dict(documents)
        changed.pop(right)
        if not check_twins(changed):
            failures.append(f"{right}: 双子の欠落を検出しない")
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
    for path, clauses in THRESHOLDS.items():
        for clause in clauses:
            for value in (STALL, WAIT_CAP):
                if str(value) not in clause:
                    continue
                changed = dict(documents)
                changed[path] = changed[path].replace(clause, clause.replace(str(value), str(value + 1)))
                if not check_thresholds(changed):
                    failures.append(f"{path}: 正本とずれた値を検出しない: {clause}")
        changed = dict(documents)
        changed.pop(path)
        if not check_thresholds(changed):
            failures.append(f"{path}: 閾値の文書の欠落を検出しない")
    for name in ("STALL", "WAIT_CAP"):
        saved = globals()[name]
        globals()[name] = None
        try:
            if not check_thresholds(documents):
                failures.append(f"{name}: 正本を読めない回を検出しない")
        finally:
            globals()[name] = saved
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
    documents = read_documents()
    problems = check_documents(documents) + check_twins(documents) + check_thresholds(documents)
    for problem in problems:
        print(problem)
    print("Codex スキル契約 OK" if not problems else "Codex スキル契約に不備あり")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
