#!/usr/bin/env python3
"""PreToolUse フック: 未解決ゼロの申告を伴わないコミットワーカーの起動を deny する。

コミットのレビューゲートは、渡されたレビュー最終応答が収束を示すかの判定に掛かっている。判定の
入力はレビュアーの散文なので、「他に未解決は無い」のように範囲を限った打ち消しと、残る項目を
認めながらの締めくくりが同居していると、読む側は収束と読み違えうる。収束していないものを収束
として扱ったコミットは履歴に残り、後から取り消せない。

散文の読解に代えて、レビュー応答の規約が末尾に求める1行(`未解決の指摘: N件`)だけを見る。この行が
無いか N が0でなければ deny する。見るのは呼び出し元が `<<<`/`>>>` で囲んで渡したレビュー応答の
原文だけで、その外側に書かれた申告は数えない——申告はレビュアーが出すものであり、呼び出し元の
地の文は判定の入力ではない。

見るのは `subagent_type` がコミットワーカーの `Agent` 起動だけで、他は何も出力せず通す。

使い方: `Agent` の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import json
import re
import subprocess
import sys

WORKER = "commit-worker"
BLOCK_OPEN = "<<<"
BLOCK_CLOSE = ">>>"
DECLARATION = re.compile(
    r"^\s*[>*_\-\s]*未解決の指摘[*_\s]*(?:は)?[*_\s]*[::]?[*_\s]*(\d+)\s*件"
)
GUIDANCE = (
    "収束の不在は入力を直して出し直せる不備ではない。レビューがまだ終わっていないなら収束させ、"
    "千日手・要ユーザー判断で終わっていたならユーザーに諮る。**申告行を自分で書き足して通すな**"
    "——申告はレビュアーが自分の応答に出すものである。"
)


def verdict_block(prompt):
    """`<<<` と `>>>` で囲まれたレビュー応答原文の行を返す。囲みが無ければ None。"""
    lines = prompt.splitlines()
    opens = [i for i, line in enumerate(lines) if line.strip() == BLOCK_OPEN]
    closes = [i for i, line in enumerate(lines) if line.strip() == BLOCK_CLOSE]
    if not opens or not closes or closes[-1] <= opens[0]:
        return None
    return lines[opens[0] + 1:closes[-1]]


def decide(data):
    """deny する理由を返す。対象の起動でなければ None(pass-through)。"""
    if not isinstance(data, dict) or data.get("tool_name") != "Agent":
        return None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    subagent_type = tool_input.get("subagent_type")
    if not isinstance(subagent_type, str) or subagent_type.split(":")[-1] != WORKER:
        return None
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""
    block = verdict_block(prompt)
    if block is None:
        return (
            "[guard-commit-gate] レビューの最終応答テキストが {} と {} で囲まれていない。"
            "コミットのレビューゲートは、この囲みの中をレビュアーの応答原文として読む。"
            "flow:commit のプロンプトの型のとおり、原文をそのまま囲んで渡すこと。"
        ).format(BLOCK_OPEN, BLOCK_CLOSE)
    counts = [int(m.group(1)) for m in map(DECLARATION.match, block) if m]
    if not counts:
        return (
            "[guard-commit-gate] レビュー応答原文の末尾に未解決件数の申告が無い"
            "(規約が求める形は `未解決の指摘: N件` の1行)。申告の無い応答は、地の文が"
            "「指摘は無い」と読めても収束の証拠にならない。" + GUIDANCE
        )
    if counts[-1] != 0:
        return (
            "[guard-commit-gate] レビュー応答原文の申告が未解決 {}件。収束していない。"
        ).format(counts[-1]) + GUIDANCE
    return None


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


def launch(prompt, subagent_type="flow:commit-worker"):
    return {"tool_name": "Agent", "tool_input": {
        "subagent_type": subagent_type, "prompt": prompt, "name": "committer",
    }}


def wrap(verdict, tail="\nレビュー済みファイルのリスト:\n- a.md\n"):
    return "作業ディレクトリ: /repo\n\n最終応答テキスト:\n{}\n{}\n{}\n{}".format(
        BLOCK_OPEN, verdict, BLOCK_CLOSE, tail)


DEFERRED = """## 反証への認否

一部だけ認める。項目としては直す必要があるまま残る。
終わり方は引き続きユーザーへ諮ることでよい。閉じるのは諮った結果が出たときとする。

## 他に未解決の指摘

**無い。**

実行モデル: Opus 5 (1M context)"""


def selftest():
    passes = (
        ("申告が0件", launch(wrap("指摘は無い。\n\n未解決の指摘: 0件"))),
        ("申告が強調と全角コロン",
         launch(wrap("**未解決の指摘: 0件**\n\n実行モデル: Fable 5"))),
        ("引用の非0申告のあとに0件の申告",
         launch(wrap("前ラウンドは 未解決の指摘: 2件 だった。\n\n未解決の指摘: 0件"))),
        ("箇条書きの申告", launch(wrap("- 未解決の指摘: 0件"))),
        ("コミットワーカー以外のエージェント", launch("レビューせよ", "flow:opus-reviewer")),
        ("継続の送信", {"tool_name": "SendMessage", "tool_input": {"to": "committer"}}),
        ("Agent 以外のツール", {"tool_name": "Bash", "tool_input": {"command": "git diff"}}),
        ("tool_input が辞書でない", {"tool_name": "Agent", "tool_input": []}),
        ("空の入力", {}),
        ("辞書でない入力", []),
    )
    denies = (
        ("先送りを残したまま範囲を限って打ち消した応答", launch(wrap(DEFERRED)), "申告が無い"),
        ("申告が非0", launch(wrap("未解決の指摘: 1件")), "未解決 1件"),
        ("申告が末尾で非0へ戻る",
         launch(wrap("未解決の指摘: 0件\n\n追加で見つかった。\n\n未解決の指摘: 3件")), "未解決 3件"),
        ("申告が原文の外にある",
         launch(wrap("指摘は無い。") + "\n未解決の指摘: 0件\n"), "申告"),
        ("囲みが無い", launch("最終応答テキスト: 未解決の指摘: 0件"), BLOCK_OPEN),
        ("閉じの囲みが無い", launch("作業ディレクトリ: /repo\n<<<\n未解決の指摘: 0件\n"), BLOCK_CLOSE),
        ("prompt が無い",
         {"tool_name": "Agent", "tool_input": {"subagent_type": "flow:commit-worker"}}, BLOCK_OPEN),
        ("プラグイン名を伴わないエージェント名", launch(wrap("直した"), "commit-worker"), "申告が無い"),
    )
    failures = []
    for label, data in passes:
        reason = decide(data)
        if reason is not None:
            failures.append("通すはずが deny: {} :: {}".format(label, reason))
    for label, data, needle in denies:
        reason = decide(data)
        if reason is None:
            failures.append("deny するはずが通した: " + label)
        elif needle not in reason:
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
    payload = json.dumps(launch(wrap(DEFERRED)), ensure_ascii=False).encode("utf-8")
    result = subprocess.run(
        [sys.executable, __file__], input=payload, stdout=subprocess.PIPE, check=False)
    try:
        output = json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
