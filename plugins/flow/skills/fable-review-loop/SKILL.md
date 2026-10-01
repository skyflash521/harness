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

Codex 上では `Agent` ツールの代わりに、[claude_review.py](../../scripts/claude_review.py) が
Claude Code CLI の非対話セッションを固定モデル `fable` で起動する。起動・照合・結末は
[flow:review-loop-subagent](../review-loop-subagent/SKILL.md) に従い、
レビュアーの起動と継続を次の形にする。

- 指示文をファイルに書き、規約ファイルの絶対パスを含める。差分は貼らず、スクリプトが
  比較の基点から作業ツリーまでの差分と状態を渡す。
- 初回は `python3 <claude_review.py の絶対パス> --family fable --cwd <リポジトリルート>
  --base <比較の基点> --prompt-file <指示文のファイル>` を実行し、JSON の `session_id` を控える。
- 次回以降は `--resume <直前の session_id>` を足し、前回の指摘への対応と今回問うことを渡す。
  常設観点は毎回すべて適用させる。
- `status` が `ok` のときだけ `result` を採り、末尾の実行モデル表示名と JSON の `model` が
  Fable 系統であることを確認する。`unavailable` なら代替せず Fable の使用不可を報告して停止する。
  `usage_limit` は[使用量上限に当たったとき](../review-loop-subagent/SKILL.md#使用量上限に当たったとき)に従う。`resume_unavailable` は、前ラウンドまでの文脈と
  指示文一式を渡し、`--resume` を外した新規セッションで取り直す。
  `timeout` と `failed` は新規セッションで取り直し、2回連続したら
  [失敗時の扱い](../review-loop-subagent/SKILL.md#失敗時の扱い)に従う。
- 時間上限はスクリプトが900秒で管理する。`ECONNREFUSED` で失敗した場合も、別のモデルや
  レビュアーへ切り替えず失敗として扱う。
