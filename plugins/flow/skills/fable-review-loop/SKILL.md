---
name: fable-review-loop
description: flow:fable-reviewer(Fableモデル、read-only)にレビューさせ、各指摘を実コードで検証して修正/反証/受容/保留に仕分け、再レビューを反復する。未解決ゼロ・千日手・要ユーザー判断のいずれかで終了。その時点で有効なレビュアーの指定がFableのときだけ使う。有効な指定が無い場合の既定はCodex版のループで、Claudeの判断でこちらへ切り替えない。
---

# Fable 反復レビュー・ループ

`flow:fable-reviewer` サブエージェント(read-only, Fableモデル)にレビューさせ、Claude が各指摘を
実コードで検証して修正/反証/受容/保留に仕分け、再レビューさせる反復ループ。日本語で報告する。

このループを使ってよい条件(ユーザーの指定と、指定が無いときの既定)は
[flow:review-loop-judgement のレビュアーの選定](../review-loop-judgement/SKILL.md#レビュアーの選定どのループを使うか) が正本。

## 導入契約の確認(最初に行う)

本スキルの実行を開始したら、他の何よりも先に[導入契約の確認スクリプト](../../contract/check_adoption.py)を
絶対パスで実行する。**このスキルファイルから見て `../../contract/check_adoption.py` にある**。

```sh
python3 <check_adoption.py の絶対パス>
```

**非0で終わったら、その出力をそのまま示して停止し、[導入契約](../../docs/adoption.md)へ案内する。**
確認する条項とその内容はスクリプトが持つので、ここには書かない。

レビュアーの起動契約と、1ラウンドを回して結末を出すまでの手順は
[flow:review-loop-subagent](../review-loop-subagent/SKILL.md) スキル、レビュアーの正体に依らない
判断ロジックは [flow:review-loop-judgement](../review-loop-judgement/SKILL.md) スキルを見よ。

このスキルが定めるのは、[`flow:review-loop-subagent` が呼び出し元に委ねている項目](../review-loop-subagent/SKILL.md#呼び出し元が定める項目):

- **レビュアーエージェント**: `flow:fable-reviewer`(`subagent_type: "flow:fable-reviewer"`)
- **固定モデル**: `fable`。表示名は Fable(`Fable` / `claude-fable-*`)
