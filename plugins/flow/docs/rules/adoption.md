# 導入契約(flow を有効化する条件)

flow プラグインを有効化するリポジトリが満たす条件と、その導入手順の正本。flow のスキル・エージェントはこの文書を
参照し、同梱のフックが必須条項をスキルの起動時に機械確認する。
guard プラグインはこの契約を課さない。

## 必須条項

### 1. 検証手順書

そのリポジトリで変更を確定させる前に通す検査の一覧と合格条件を定める文書を
`docs/conventions/verification.md` に置く。言語・検査器は各リポジトリが選ぶ。flow のスキルはこの文書を
「リポジトリの検証手順書」として参照する。

### 2. スクラッチ置き場

リポジトリ直下の `.scratch/` を除外設定に加える。使い捨てのスクリプト・一時ドキュメントの置き場と
する。判定は `git check-ignore` で行う。

### 3. sandbox

同梱の [required-settings.json](../../contract/required-settings.json) が持つ
`sandbox.excludedCommands` の全エントリを、`.claude/settings.json`・`.claude/settings.local.json`・
`~/.claude/settings.json` のいずれかに登録する。

登録が必要なエントリの正本はそのファイルであり、この文書はエントリを列挙しない。

## 任意条項

- レビューで照合させたい規約(用語規約表など)を `docs/conventions/` に置く。flow のレビュー
  ループは、該当文書が存在する場合にだけ読ませて照合する。
- 節参照・裸のファイル参照の検査、参照の循環の検査を採用する場合は、検証手順書に登録する。

## 前提環境

契約条項ではなく実行環境の前提。[起動時の契約確認](#起動時の契約確認)の対象には含めない。

- guard・flow が動作するOSは Windows・macOS・Linux とする。
- 許可モードは、Bash の実行を自動承認する `auto` を前提とする。
- guard・flow の全フックと補助スクリプトは bash シェル経由で python3 を起動するため、各マシンに
  python3 と(Windows では)Git Bash が必要。
- codex 系スキルを使う場合は Node.js・Codex CLI・Codex プラグインの導入が別途必要。セットアップ
  未完了は実行時に検知して報告する機構を codex 系スキルが持つ。

## 導入手順

導入するリポジトリのルートで次を順に行う。

1. **条項1**: 検証手順書 `docs/conventions/verification.md` を作る。そのリポジトリで通す検査の
   一覧と合格条件を書く。
2. **条項2**: 除外設定に `.scratch/` を加える。
3. **条項3**: [required-settings.json](../../contract/required-settings.json) を読み、その
   `sandbox.excludedCommands` の各エントリを設定の同じキーへ加える。
4. marketplace を登録して flow を導入する。harness の所在(リポジトリの URL)は、エージェントが
   ユーザーに尋ねて依頼文へ入れる。経路は2つある。
   - `/plugin marketplace add <harness の所在>` と `/plugin install`。**ユーザー操作なので、
     エージェントは依頼して待つ。** 環境によっては `/plugin` が使えない。
   - `claude plugin marketplace add <harness の所在>` と
     `claude plugin install <プラグイン>@harness --scope project`。ターミナルから実行する。

   `--scope` の既定は `user` で、そのマシンの全リポジトリでフックが発火する。対象を1つに絞るなら
   `project` を明示する。
5. [検証](#導入の検証)を実行し、終了コード0を確認する。

## 導入の検証

同梱の確認スクリプトを絶対パスで実行する。必須条項を機械確認し、欠けていれば条項ごとに何が
足りないかを列挙して非0で終わる。

```sh
python3 <flow プラグインの contract/check_adoption.py の絶対パス> [対象リポジトリのルート]
```

ルートを省いた場合は `CLAUDE_PROJECT_DIR`、それも無ければカレントディレクトリを対象にする。

## 起動時の契約確認

`flow:` で始まるスキルの起動を同梱の [guard-adoption.py](../../hooks/guard-adoption.py) が
PreToolUse フックで受け、[この確認スクリプト](#導入の検証)を実行する。非0で終わったら起動を deny し、
欠けた条項とこの文書の所在を示す。ハーネス側に置くのは、スキル本文に書くと呼び出しのたびに会話の
ツール往復が1つ増え、スキルのネストで積み上がるため。

flow:commit-worker は直接起動への防御として、不正コミット防止チェックでも条項1を確認する。
