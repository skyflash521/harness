---
name: fable-review-loop
description: flow:fable-reviewer(Fableモデル、read-only)にレビューさせ、各指摘を実コードで検証して修正/反証/受容/保留に仕分け、再レビューを反復する。未解決ゼロ・千日手・要ユーザー判断のいずれかで終了。その時点で有効なレビュアーの指定がFableのときだけ使う。有効な指定が無い場合の既定はCodex版のループで、Claudeの判断でこちらへ切り替えない。
---

# Fable 反復レビュー・ループ

`flow:fable-reviewer` サブエージェント(read-only, Fableモデル)にレビューさせ、Claude が各指摘を
実コードで検証して修正/反証/受容/保留に仕分け、再レビューさせる反復ループ。日本語で報告する。

このループを使ってよい条件(ユーザーの指定と、指定が無いときの既定)は
[flow:review-loop-judgement のレビュアーの選定](../review-loop-judgement/SKILL.md#レビュアーの選定どのループを使うか) が正本。

レビュアーの起動契約と、1ラウンドを回して結末を出すまでの手順は
[flow:review-loop-subagent](../review-loop-subagent/SKILL.md) スキル、レビュアーの正体に依らない
判断ロジックは [flow:review-loop-judgement](../review-loop-judgement/SKILL.md) スキルを見よ。

このスキルが定めるのは、[`flow:review-loop-subagent` が呼び出し元に委ねている項目](../review-loop-subagent/SKILL.md#呼び出し元が定める項目):

- **レビュアーエージェント**: `flow:fable-reviewer`(`subagent_type: "flow:fable-reviewer"`)
- **固定モデル**: `fable`。表示名は Fable(`Fable` / `claude-fable-*`)

## Codex 上で実行するとき

時間上限は [1ラウンドの上限](../review-loop-judgement/SKILL.md#1ラウンドが時間内に終わらないとき) の900秒とする。
Codex の起動フックも、この上限以下であることを検査する。

Codex 上では Claude Code CLI を直接起動して、固定モデル `fable` の非対話セッションへ委譲する。
起動可否・照合・結末・失敗時の処置は
[レビュー起動の共通契約](../review-loop-subagent/SKILL.md)に従う。
起動と継続は [Claude Code CLI のレビュー](../../docs/guidance/codex-execution.md#claude-code-cli-のレビュー) を使い、
[レビュアー定義](../../agents/fable-reviewer.md)を `--append-system-prompt` に渡す。

実行モデルは JSON の `modelUsage` で出力トークン数が最大のモデルと、応答末尾のモデル表示を照合し、
`claude-fable-*` 系統であることを確認する。モデル情報が無い・系統が違う・固定モデルが使えない場合は
代替せず停止する。使用量上限は
[使用量上限に当たったとき](../review-loop-subagent/SKILL.md#使用量上限に当たったとき)に従う。

再開先が無い場合は失敗ラウンドに数えず、前ラウンドまでの文脈と指示文一式を渡して新規セッションで取り直す。
時間上限または起動失敗は新規セッションで取り直し、2回連続したら停止する。
