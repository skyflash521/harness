---
name: opus-review-loop
description: flow:opus-reviewer(Opusモデル、read-only)にレビューさせ、各指摘を実コードで検証して修正/反証/受容/保留に仕分け、再レビューを反復する。未解決ゼロ・千日手・要ユーザー判断のいずれかで終了。その時点で有効なレビュアーの指定がOpusのときだけ使う。有効な指定が無い場合の既定はCodex版のループで、Claudeの判断でこちらへ切り替えない。
---

# Opus 反復レビュー・ループ

`flow:opus-reviewer` サブエージェント(read-only, Opusモデル)にレビューさせ、Claude が各指摘を
実コードで検証して修正/反証/受容/保留に仕分け、再レビューさせる反復ループ。日本語で報告する。

このループを使ってよい条件(ユーザーの指定と、指定が無いときの既定)は
[flow:review-loop-judgement のレビュアーの選定](../review-loop-judgement/SKILL.md#レビュアーの選定どのループを使うか) が正本。

レビュアーの起動契約と、1ラウンドを回して結末を出すまでの手順は
[flow:review-loop-subagent](../review-loop-subagent/SKILL.md) スキル、レビュアーの正体に依らない
判断ロジックは [flow:review-loop-judgement](../review-loop-judgement/SKILL.md) スキルを見よ。

このスキルが定めるのは、[`flow:review-loop-subagent` が呼び出し元に委ねている項目](../review-loop-subagent/SKILL.md#呼び出し元が定める項目):

- **レビュアーエージェント**: `flow:opus-reviewer`(`subagent_type: "flow:opus-reviewer"`)
- **固定モデル**: `opus`。表示名は Opus(`Opus` / `claude-opus-*`)

## Codex 上で実行するとき

Codex CLI 上でこのループを使う場合([レビュアーの選定](../review-loop-judgement/SKILL.md#レビュアーの選定どのループを使うか)が
既定として Opus を指す場面を含む)は、`Agent` ツールと `SendMessage` を使えない。レビュアーは、同梱の
[claude_review.py](../../scripts/claude_review.py) が起動する Claude Code CLI の非対話セッション(固定モデル
`opus`)である。手順0・手順2〜4と、レビュー指示文の要件・失敗の扱いは
[flow:review-loop-subagent](../review-loop-subagent/SKILL.md) に従い、手順1の起動と継続だけを次に置き換える。

- **指示文はファイルに書いて渡す**。要件は
  [ラウンド1(新規起動)](../review-loop-subagent/SKILL.md#ラウンド1新規起動)と同じで、規約ファイルの絶対パスを
  含める(このスキルファイルから見て `../../docs/criteria/` にある)。**差分は指示文に貼らない**——スクリプトが
  比較の基点から作業ツリーまでの差分をファイルへ書いてレビュアーへ渡す。
- **ラウンド1**: `python3 <claude_review.py の絶対パス> --family opus --cwd <リポジトリルート> --base <比較の基点>
  --prompt-file <指示文のファイル>` を実行する。標準出力の JSON が結果で、`session_id` を控える。
- **ラウンド2以降**: 同じ引数に `--resume <直前のラウンドの session_id>` を足して、前ラウンドの文脈
  (対応結果・今回問うこと)だけを書いた指示文を渡す。常設観点を毎ラウンドすべて適用させる点は
  [ラウンド2以降](../review-loop-subagent/SKILL.md#ラウンド2以降同じエージェントを継続する)と同じ。
- **時間上限**: スクリプトは 900 秒で打ち切る(値の正本は
  [1ラウンドが時間内に終わらないとき](../review-loop-judgement/SKILL.md#1ラウンドが時間内に終わらないとき))。
  待つ側の締切を別に重ねない。
- **結果の `status` で分ける**。`ok` は `result` をレビュアーの応答として手順2へ回す(`result` 末尾の
  モデル表示名が Opus であることも確かめる)。**`unavailable`(モデルが使えない・応答したモデルが
  Opus でない)は、代替せず、原因を「Opus が使用不可」として示してユーザーへ報告して停止する。**
  `usage_limit` は[使用量上限に当たったとき](../review-loop-subagent/SKILL.md#使用量上限に当たったとき)に従う。
  `timeout` と `failed` は失敗ラウンドで、`--resume` を付けずに新規セッションで取り直し(前ラウンドまでの
  文脈と指示文一式を改めて渡す)、2回連続したら
  [失敗時の扱い](../review-loop-subagent/SKILL.md#失敗時の扱い)の停止規定に従う。
- 起動が `ECONNREFUSED` で終わったときは、Codex のサンドボックスがネットワークを閉じている。
  サンドボックスを自分で切らず、`failed` として報告する。
