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

千日手は止まる結末で、進めてよいと決められるのはユーザーだけである。申告の非0を通すのは、レビュアーが
`結末: 千日手` を宣言し、**その宣言より後に**ユーザーが出した指示が `[[[`/`]]]` の中に在るときに限る。
どちらを欠いても通さない。

見るのは `subagent_type` がコミットワーカーの `Agent` 起動だけで、他は何も出力せず通す。

使い方: `Agent` の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
TRANSCRIPT = ("scripts", "transcript.py")

WORKER = "commit-worker"
BLOCK_OPEN = "<<<"
BLOCK_CLOSE = ">>>"
OVERRIDE_OPEN = "[[["
OVERRIDE_CLOSE = "]]]"
DECLARATION = re.compile(
    r"^\s*[>*_\-\s]*未解決の指摘[*_\s]*(?:は)?[*_\s]*[::]?[*_\s]*(\d+)\s*件"
)
OUTCOME = re.compile(r"^\s*[>*_\-\s]*結末[*_\s]*[::][*_\s]*(\S+)", re.MULTILINE)
STALEMATE = "千日手"
GUIDANCE = (
    "収束の不在は入力を直して出し直せる不備ではない。レビューがまだ終わっていないなら収束させ、"
    "千日手で終わっていたなら、レビュアーの `結末: 千日手` を含む応答原文と、**その宣言より後に**"
    "ユーザーが出したコミット指示を囲んで添える。要ユーザー判断なら諮る。"
    "**申告行も宣言も囲みの中身も自分で書いて通すな**——申告と宣言はレビュアーが、指示はユーザーが"
    "出すもので、呼び出し元の地の文は判定の入力にならない。"
)


def fenced(lines, open_mark, close_mark):
    """開きと閉じで囲まれた原文の行と、囲みが占める範囲を返す。囲みが無ければ None。"""
    opens = [i for i, line in enumerate(lines) if line.strip() == open_mark]
    closes = [i for i, line in enumerate(lines) if line.strip() == close_mark]
    if not opens or not closes or closes[-1] <= opens[0]:
        return None
    return lines[opens[0] + 1:closes[-1]], (opens[0], closes[-1])


def verdict_block(prompt):
    """`<<<` と `>>>` で囲まれたレビュー応答原文の行を返す。囲みが無ければ None。"""
    found = fenced(prompt.splitlines(), BLOCK_OPEN, BLOCK_CLOSE)
    return None if found is None else found[0]


def override_text(prompt):
    """`[[[`/`]]]` で囲んだコミット指示の本文。囲みがレビュー応答原文の外に無ければ空。

    レビュアーが自分の応答でこの記号を使っていても、それは呼び出し元が渡した指示ではない。"""
    lines = prompt.splitlines()
    verdict = fenced(lines, BLOCK_OPEN, BLOCK_CLOSE)
    if verdict is not None:
        start, end = verdict[1]
        lines = lines[:start] + lines[end + 1:]
    found = fenced(lines, OVERRIDE_OPEN, OVERRIDE_CLOSE)
    return flat("".join(found[0])) if found else ""


def flat(text):
    """照合のために表記の揺れを畳む。空白と装飾は引き写しで落ちても同じ発言を指す。"""
    return "".join(str(text).split()).replace("*", "").replace("`", "")


def user_said(data):
    """レビュアーが千日手を宣言した後のユーザー発言の本文。読めなければ None。"""
    try:
        spec = importlib.util.spec_from_file_location("_t", HOOKS.parent / Path(*TRANSCRIPT))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (OSError, AttributeError, ImportError, SyntaxError, ValueError):
        return None
    rows = module.rows_of(data.get("transcript_path"))
    return None if rows is None else module.instructions_after(rows, OUTCOME, STALEMATE)


def vouched(said, data):
    """その文が、千日手の宣言より後のユーザー発言に実在するか。読めないときは False。"""
    says = user_said(data)
    return bool(said) and says is not None and any(said in flat(one) for one in says)


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
            "「指摘は無い」と読めても結末の証拠にならない。" + GUIDANCE
        )
    outcomes = [m.group(1) for m in map(OUTCOME.match, block) if m]
    declared = bool(outcomes) and outcomes[-1].startswith(STALEMATE)
    if counts[-1] != 0 and not (declared and vouched(override_text(prompt), data)):
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


def launch(prompt, subagent_type="flow:commit-worker", transcript_path=None):
    return {"tool_name": "Agent", "transcript_path": transcript_path, "tool_input": {
        "subagent_type": subagent_type, "prompt": prompt, "name": "committer",
    }}


def wrap(verdict, tail="\nレビュー済みファイルのリスト:\n- a.md\n", override=None):
    text = "作業ディレクトリ: /repo\n\n最終応答テキスト:\n{}\n{}\n{}\n{}".format(
        BLOCK_OPEN, verdict, BLOCK_CLOSE, tail)
    if override is None:
        return text
    return text + "\nユーザーのコミット指示:\n{}\n{}\n{}\n".format(
        OVERRIDE_OPEN, override, OVERRIDE_CLOSE)


DEFERRED = """## 反証への認否

一部だけ認める。項目としては直す必要があるまま残る。
終わり方は引き続きユーザーへ諮ることでよい。閉じるのは諮った結果が出たときとする。

## 他に未解決の指摘

**無い。**

実行モデル: Opus 5 (1M context)"""


STALEMATE_VERDICT = """## 結末

反証を認めない。同じ理由で再掲する。

未解決の指摘: 2件

結末: 千日手

実行モデル: Opus 5 (1M context)"""

SAID = "残りの2件は直さなくていい、そのままコミットしろ。"


def transcript_file(directory, says, name="transcript.jsonl"):
    """転写を書き、そのパスを返す。`|` で始まる要素はレビュアーの応答の差し込みにする。"""
    path = Path(directory, name)
    rows = []
    for text in says:
        body, meta = text, False
        if text.startswith("|"):
            body, meta = '<agent-message from="reviewer">\n' + text[1:], True
        row = {"type": "user", "isSidechain": False, "isMeta": meta,
               "message": {"role": "user", "content": [{"type": "text", "text": body}]}}
        rows.append(row)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")
    return path.as_posix()


def selftest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run_selftest(tmp)


def run_selftest(tmp):
    told = transcript_file(tmp, ["レビューしろ", "|結末: 千日手", SAID])
    beforehand = transcript_file(
        tmp, [SAID, "レビューしろ", "|結末: 千日手"], "beforehand.jsonl")
    missing = Path(tmp, "no.jsonl").as_posix()
    passes = (
        ("申告が0件", launch(wrap("指摘は無い。\n\n未解決の指摘: 0件"))),
        ("申告が強調と全角コロン",
         launch(wrap("**未解決の指摘: 0件**\n\n実行モデル: Fable 5"))),
        ("引用の非0申告のあとに0件の申告",
         launch(wrap("前ラウンドは 未解決の指摘: 2件 だった。\n\n未解決の指摘: 0件"))),
        ("箇条書きの申告", launch(wrap("- 未解決の指摘: 0件"))),
        ("千日手の宣言の後に出たコミット指示を添える",
         launch(wrap(STALEMATE_VERDICT, override=SAID), transcript_path=told)),
        ("コミット指示の装飾と空白の違いを畳んで照合する",
         launch(wrap(STALEMATE_VERDICT, override="**そのまま コミットしろ。**"), transcript_path=told)),
        ("レビュー応答原文の中の囲みはコミット指示ではない",
         launch(wrap("[[[\n引用した設定\n]]]\n\n未解決の指摘: 0件"))),
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
        ("コミット指示が呼び出し元の代弁",
         launch(wrap(STALEMATE_VERDICT, override="ユーザーが千日手を承知してコミットを指示した"),
                transcript_path=told), "未解決 2件"),
        ("コミット指示が千日手の宣言より前に出ていた",
         launch(wrap(STALEMATE_VERDICT, override=SAID), transcript_path=beforehand), "未解決 2件"),
        ("レビュアーが千日手を宣言していない",
         launch(wrap("直らない。\n\n未解決の指摘: 2件", override=SAID), transcript_path=told),
         "未解決 2件"),
        ("地の文で宣言の形に触れただけ",
         launch(wrap("規約は `結末: 千日手` の1行を求める。\n\n未解決の指摘: 2件", override=SAID),
                transcript_path=told), "未解決 2件"),
        ("コミット指示の囲みが空",
         launch(wrap(STALEMATE_VERDICT, override=""), transcript_path=told), "未解決 2件"),
        ("転写を読めない",
         launch(wrap(STALEMATE_VERDICT, override=SAID), transcript_path=missing), "未解決 2件"),
        ("コミット指示があっても申告が無ければ結末を確かめられない",
         launch(wrap("千日手だ。", override=SAID), transcript_path=told), "申告が無い"),
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
    if not broken_module_denies(told):
        failures.append("共通モジュールを読めない回で deny が出ない")
    if failures:
        for line in failures:
            print("FAIL:", line)
        sys.exit(1)
    print("ALL PASS ({} 件)".format(len(passes) + len(denies) + 2))


def broken_module_denies(told):
    """転写を読む共通モジュールへ届かない回が、素通りでなく deny に倒れるか。"""
    saved = globals()["TRANSCRIPT"]
    globals()["TRANSCRIPT"] = ("scripts", "no_such_module.py")
    try:
        return decide(launch(wrap(STALEMATE_VERDICT, override=SAID),
                             transcript_path=told)) is not None
    finally:
        globals()["TRANSCRIPT"] = saved


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
