#!/usr/bin/env python3
"""PreToolUse フック: 規約ファイルの所在を渡さないレビュー専用エージェントの起動を deny する。

レビュアーの定義は自分が従う規約の正本を相対リンクで指すが、その相対パスは定義ファイル基準で
あり、レビュアーは自分の定義ファイルの所在を知らない。所在を渡さずに起動すると、レビュアーは
正本を開けないまま走り、観点を欠いたレビューが「確認済み」として返る。

判定は、依頼文から取り出した候補が**実在するファイルを指す絶対パスか**で行う。フックはレビュアーが
その正本を開くのと同じマシンで走るので、実在は判定に使える事実であり、地の文を巻き込んだ候補は
存在しないパスになって落ちる。

見るのは `subagent_type` がレビュー専用エージェントの `Agent` 起動だけで、他は何も出力せず通す。
2ラウンド目以降の継続(`SendMessage`)は、ラウンド1で開いた正本をレビュアーが保つので対象外。

使い方: `Agent` の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REQUIRED_FILES = ("review-viewpoints.md", "review-response.md", "review-request.md")
REVIEWERS = ("opus-reviewer", "fable-reviewer")

GUIDANCE = (
    "レビュー指示文に、レビュアーが自分の定義から正本として参照する規約ファイルの**絶対パス**を"
    "書くこと(常設観点の規約・レビュー応答の規約・レビュー依頼の規約)。所在の提示は読む量・"
    "読み方の指定ではないので、レビュアーが読める範囲を狭めることにはならない。"
)


def is_absolute_path_to(prompt, filename):
    """依頼文が、そのファイルを実在する絶対パスで示しているか。"""
    for line in prompt.splitlines():
        end = line.find(filename)
        while end >= 0:
            end += len(filename)
            for start in range(end - len(filename)):
                candidate = line[start:end]
                if os.path.isabs(candidate) and os.path.isfile(candidate):
                    return True
            end = line.find(filename, end)
    return False


def decide(data):
    """deny する理由を返す。対象の起動でなければ None(pass-through)。"""
    if not isinstance(data, dict) or data.get("tool_name") != "Agent":
        return None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    subagent_type = tool_input.get("subagent_type")
    if not isinstance(subagent_type, str) or subagent_type.split(":")[-1] not in REVIEWERS:
        return None
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""
    missing = [name for name in REQUIRED_FILES if not is_absolute_path_to(prompt, name)]
    if not missing:
        return None
    return (
        "[guard-reviewer-launch] {} の起動に、実在する規約ファイルの絶対パスが無い: {}。"
        "レビュアーは自分の定義ファイルの所在を知らないので、定義に書かれた相対リンクを作業"
        "ディレクトリからは解決できない。このまま起動すると、レビュアーは正本を開けないまま走り、"
        "常設観点を欠いたレビューが確認済みとして返る。{}"
    ).format(subagent_type, "・".join(missing), GUIDANCE)


def main():
    # ハーネスが渡す JSON は UTF-8 で、既定の符号化では復号できずに落ちる。
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


def launch(subagent_type, prompt):
    return {"tool_name": "Agent", "tool_input": {
        "subagent_type": subagent_type, "prompt": prompt, "name": "reviewer",
    }}


def rules_dir(root):
    """<root>/docs/rules に規約ファイルを作り、その絶対パスの置き場を返す。"""
    made = Path(root) / "docs" / "rules"
    made.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_FILES:
        (made / name).write_text("", encoding="utf-8")
    return made


def prompt_with(directory, *names, separator="/", template="正本: {}\n"):
    return "".join(
        template.format(str(Path(directory) / name).replace("\\", separator).replace("/", separator))
        for name in names
    )


def selftest():
    plugin_rules = Path(__file__).resolve().parents[1] / "docs" / "rules"
    with tempfile.TemporaryDirectory() as tmp:
        spaced = rules_dir(Path(tmp) / "John Doe")
        japanese = rules_dir(Path(tmp) / "ユーザー")
        existing = str(plugin_rules).replace("\\", "/")
        # バックスラッシュ区切りのパスが実在するのは Windows だけなので、その環境でだけ通す側に置く。
        native = (
            ("バックスラッシュ区切り",
             launch("flow:fable-reviewer",
                    prompt_with(plugin_rules, *REQUIRED_FILES, separator="\\"))),
            ("日本語のユーザー名を含むパス(バックスラッシュ区切り)",
             launch("flow:opus-reviewer",
                    prompt_with(japanese, *REQUIRED_FILES, separator="\\"))),
        ) if os.sep == "\\" else ()
        passes = native + (
            ("導入済みプラグインの絶対パス3件",
             launch("flow:opus-reviewer", prompt_with(plugin_rules, *REQUIRED_FILES))),
            ("ホームディレクトリに空白を含む環境",
             launch("flow:opus-reviewer", prompt_with(spaced, *REQUIRED_FILES))),
            ("日本語のユーザー名を含むパス",
             launch("flow:fable-reviewer", prompt_with(japanese, *REQUIRED_FILES))),
            ("日本語の記号で囲んだ絶対パス",
             launch("flow:fable-reviewer",
                    prompt_with(plugin_rules, *REQUIRED_FILES, template="「{}」\n"))),
            ("レビュー専用でないエージェント", launch("codex:codex-rescue", "レビューさせよ")),
            ("探索エージェント", launch("Explore", "呼び出し元を探せ")),
            ("継続ラウンドの送信", {"tool_name": "SendMessage", "tool_input": {"to": "reviewer"}}),
            ("Agent 以外のツール", {"tool_name": "Bash", "tool_input": {"command": "git diff"}}),
            ("tool_input が辞書でない", {"tool_name": "Agent", "tool_input": []}),
            ("空の入力", {}),
            ("辞書でない入力", []),
        )
        denies = (
            ("絶対パスが1件も無い", launch("flow:opus-reviewer", "この変更をレビューせよ"),
             REQUIRED_FILES),
            ("相対パスで示している",
             launch("flow:fable-reviewer", "正本は ../docs/rules/review-viewpoints.md にある"),
             REQUIRED_FILES),
            ("常設観点の正本だけ渡している",
             launch("flow:opus-reviewer", prompt_with(plugin_rules, "review-viewpoints.md")),
             ("review-response.md", "review-request.md")),
            ("同じ行に絶対パスと日本語を挟んだリポジトリ相対パスが並ぶ",
             launch("flow:opus-reviewer",
                    "正本の置き場: {}。対象: plugins/flow/docs/rules/review-viewpoints.md".format(existing)),
             REQUIRED_FILES),
            ("同じ行に絶対パスと助詞で繋いだリポジトリ相対パスが並ぶ",
             launch("flow:fable-reviewer",
                    "{} の plugins/flow/docs/rules/review-viewpoints.md".format(existing)),
             REQUIRED_FILES),
            ("同じ行に絶対パスと全角括弧で括ったリポジトリ相対パスが並ぶ",
             launch("flow:opus-reviewer",
                    "{}(plugins/flow/docs/rules/review-viewpoints.md)".format(existing)),
             REQUIRED_FILES),
            ("実在しない絶対パスを渡している",
             launch("flow:fable-reviewer",
                    prompt_with(Path(tmp) / "無い場所" / "docs" / "rules", *REQUIRED_FILES)),
             REQUIRED_FILES),
            ("プラグイン名を伴わないエージェント名", launch("opus-reviewer", ""), REQUIRED_FILES),
            ("prompt が無い",
             {"tool_name": "Agent", "tool_input": {"subagent_type": "flow:fable-reviewer"}},
             REQUIRED_FILES),
        )
        failures = []
        for label, data in passes:
            reason = decide(data)
            if reason is not None:
                failures.append("通すはずが deny: {} :: {}".format(label, reason))
        for label, data, needles in denies:
            reason = decide(data)
            if reason is None:
                failures.append("deny するはずが通した: " + label)
            else:
                for needle in needles:
                    if needle not in reason:
                        failures.append("理由が不足を名指ししない: {} :: {}".format(label, needle))
        if not roundtrip_ok():
            failures.append("ハーネスと同じ形の起動で deny が出ない")
    if failures:
        for line in failures:
            print("FAIL:", line)
        sys.exit(1)
    print("ALL PASS ({} 件)".format(len(passes) + len(denies) + 1))


def roundtrip_ok():
    """ハーネスと同じ形(UTF-8 の JSON を標準入力へ)で起動して deny を確かめる。"""
    payload = json.dumps(
        launch("flow:opus-reviewer", "日本語のレビュー依頼"), ensure_ascii=False,
    ).encode("utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
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
    main()
