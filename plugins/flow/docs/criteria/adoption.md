# 導入契約(flow を有効化する条件)

flow プラグインを有効化するリポジトリが満たす条件と、その導入手順の正本。flow のスキル・エージェントはこの文書を
参照する。

## 必須条項

条項の中身はここで定め、どのスキルがどの条項を要るかは[条項の区分](#条項の区分)が定める。

### 1. 検証手順書

そのリポジトリで変更を確定させる前に通す検査の一覧と合格条件を定める文書を
`docs/conventions/verification.md` に置く。言語・検査器は各リポジトリが選ぶ。flow のスキルはこの文書を
「リポジトリの検証手順書」として参照する。

### 2. スクラッチ置き場

リポジトリ直下の `.scratch/` を除外設定に加える。使い捨てのスクリプト・一時ドキュメントの置き場と
する。判定は `git check-ignore` で行う。ルートが git 作業ツリーでなければ、この条項は何も要求しない。

### 3. 製品別の設定

#### Claude Code

同梱の [required-settings.json](../../contract/required-settings.json) が持つ
`sandbox.excludedCommands` の全エントリを、`.claude/settings.json`・`.claude/settings.local.json`・
Claude Code の設定ディレクトリ(既定 `~/.claude`、`CLAUDE_CONFIG_DIR` で変更)の `settings.json` の
いずれかに登録する。サンドボックスの無いネイティブ Windows では確認しない。

#### Codex

対象リポジトリで `flow@harness` が導入済み・有効であることを、`codex plugin list --marketplace harness --json`
の `installed` 一覧の `pluginId`・`installed`・`enabled` で確認する。設定はユーザー設定と
信頼済みリポジトリの `.codex/config.toml` の解決結果を使う。

フック機能が有効であり、flow の必須フックすべてが信頼済み・有効であることも必要とする。
guard も導入・有効化している場合は、guard の必須フックも同じ条件で確認する。
実行状態は Codex の `hooks/list` と `config/read` の対象リポジトリでの解決結果を使う。
未信頼・変更後の未承認・無効・欠落・状態の取得失敗は導入不足とする。

## 条項の区分

条項は**基本条項**と**開発条項**に分かれる。判定はプロジェクトの種別でなく、ルートについて観測できる
事実——git 作業ツリーか・検証手順書が在るか・`.scratch/` が除外されているか・製品別の設定が在るか——で行う。

| 区分 | 条項 |
|---|---|
| 基本条項 | 条項3。ルートが git 作業ツリーなら条項2 も |
| 開発条項 | 基本条項のすべてに加え、条項1 と、ルートが git 作業ツリーであること |

ルートが git 作業ツリーであるとは、ルートかその親ディレクトリのいずれかに `.git` が在ることをいう(ディレクトリでも
ファイルでもよい)。判定に使うルートは、Claude Code では `CLAUDE_PROJECT_DIR`(無ければカレントディレクトリ)、
Codex ではイベントの `cwd` である。ルートそのものを親ディレクトリへ探し直すことはしない。

### スキルごとに要る区分

スキルごとに要る区分の正本は、[確認スクリプト](../../contract/check_adoption.py)が持つスキルと区分の対応である。

### 基本条項だけを満たすリポジトリでの振る舞い

- 停止規約・実行規律・単発相談のフックとスキルは、開発条項を満たすリポジトリと同じに働く。
- 成果物衛生の検査([guard-artifact-hygiene.py](../../hooks/guard-artifact-hygiene.py) と、Codex の `apply_patch`
  への検査)は、**書き込み先のファイルが git 作業ツリーの中に無く、かつルートも git 作業ツリーでない**ときだけ
  掛からない。どちらかが git 作業ツリーの中なら、開発条項を満たすリポジトリと同じに掛かる。
- エージェントはコミットしない。コミットを作れるのは flow:commit-worker だけで、それを起動する flow:commit は
  開発条項を要る。git 作業ツリーであっても、開発条項を満たさないリポジトリのコミットはユーザーが行う。
- 作業結果の完了確認と、工程の順序・再開・進捗の管理は、そのリポジトリ側の指示文書とフックが持つ。flow が
  担うのは、停止規約のフックによる中途終了と停止の申告漏れの抑止までで、停止規約のフックは作業の結果を判定しない。

## 任意条項

- レビューで照合させたい規約(用語規約表など)を `docs/conventions/` に置く。flow のレビュー
  ループは、該当文書が存在する場合にだけ読ませて照合する。
- 節参照・裸のファイル参照の検査、参照の循環の検査を採用する場合は、検証手順書に登録する。

## 前提環境

契約条項ではなく実行環境の前提。[起動時の契約確認](#起動時の契約確認)の対象には含めない。

- guard・flow が動作するOSは Windows・macOS・Linux とする。
- Claude Code の許可モードは `auto` を前提とする。auto は Bash の実行を無条件に通すわけではなく、分類器が危険と判断した形は止まる。
- 各マシンに python3 が必要。Claude Code を使う Windows では Git Bash も必要。
- codex 系スキルを使う場合は Node.js・Codex CLI・Codex プラグインの導入が別途必要。セットアップ
  未完了は実行時に検知して報告する機構を codex 系スキルが持つ。
- Codex CLI 上で flow を使う場合は、Claude Code CLI も別途必要になる。加えて Codex の設定(`config.toml`)に
  次の2つを加える。
  - `sandbox_workspace_write.network_access = true`。
  - `sandbox_workspace_write.writable_roots` に、そのリポジトリの管理ディレクトリ(`<リポジトリ>/.git`)を加える。

  Windows の Codex のサンドボックスには、次の制約がある。
  - Python の一時ディレクトリを後始末できず(権限エラー)、一時ディレクトリを使う自己テストを含む検証がサンドボックスの
    中では通らない。
  - 委譲した Claude Code CLI の Glob と Grep が権限エラーで使えない。
  - 委譲した Claude Code CLI のセッションを保存できず、`--resume` で継続できない。

## 導入手順

### 基本条項だけを満たす導入

停止規約・実行規律・単発相談だけを使うフォルダ(git 管理外の作業フォルダを含む)では、ルートで次を順に行う。

1. **条項2**: ルートが git 作業ツリーなら、除外設定に `.scratch/` を加える。git 作業ツリーでなければ不要。
2. **条項3**: [開発条項までの導入](#開発条項までの導入)の手順3・4と同じことを行う。
3. [検証](#導入の検証)を `--tier base` を付けて実行し、終了コード0を確認する。

### 開発条項までの導入

レビュー・コミット・自律進行・計画書のスキルまで使うリポジトリでは、git 作業ツリーのルートで次を順に行う。

1. **条項1**: 検証手順書 `docs/conventions/verification.md` を作る。そのリポジトリで通す検査の
   一覧と合格条件を書く。
2. **条項2**: 除外設定に `.scratch/` を加える。
3. **条項3(Claude Code)**: [required-settings.json](../../contract/required-settings.json) を読み、その
   `sandbox.excludedCommands` の各エントリを設定の同じキーへ加える。サンドボックスの無い
   ネイティブ Windows では不要。
4. [harness の導入・更新手順](https://github.com/skyflash521/harness#プラグインの利用)に従い、
   対象リポジトリで flow を導入・有効化する。
   Codex CLI では新しいセッションで `/hooks` を開き、`Plugin - flow@harness` のフック定義の内容を確認して
   信頼し、有効化する。信頼はフック定義の内容ごとに記録されるため、プラグインの更新で定義が変わったら
   再度確認する。エージェントが操作する場合は、ユーザーの明示指示を受けてから対話式の Codex CLI で
   `/hooks` を開き、表示されたフックを確認して信頼・有効化する。指示が無ければ、ユーザーに操作を
   依頼して待つ。信頼の記録を設定ファイルへ直接書き込まない。
   `codex plugin list` の `installed, enabled` だけではフックの動作は確認できない。
   `/hooks` で `Plugin - flow@harness` の `SessionStart`・`PreToolUse`・`Stop` が有効と表示されることを確かめる。
   さらに Codex CLI のシェルツールへ `git commit --dry-run --no-verify` を渡し、
   `codex-guard-git-write` による `PreToolUse Blocked` が返ることを確かめる。
   この確認は実際のコミットを作らない。Codex CLI の外のシェルで実行してもフックは発火しない。
   これらを確認してから、flow のレビューとコミットを実変更へ適用する。
5. [検証](#導入の検証)を実行し、終了コード0を確認する。

## 導入の検証

同梱の確認スクリプトを絶対パスで実行する。`--tier` が指す区分の条項を機械確認し、欠けていれば条項ごとに
何が足りないかを列挙して非0で終わる。ルートが git 作業ツリーでないことによる不足は「git 管理外」として並べる。

```sh
python3 <flow プラグインの contract/check_adoption.py の絶対パス> [--tier base|dev] [対象リポジトリのルート]
```

Codex では `--host codex` を付ける。

```sh
python3 <flow プラグインの contract/check_adoption.py の絶対パス> --host codex [--tier base|dev] [対象リポジトリのルート]
```

`--tier` の既定は `dev`(開発条項)で、`base` は基本条項だけを確認する。`--host` の既定は `claude`。Claude Code でルートを省いた場合は `CLAUDE_PROJECT_DIR`、それも無ければ
カレントディレクトリを対象にする。Codex で省いた場合はカレントディレクトリを対象にする。
Codex 用の機械検査は、区分が含む条項1・2と、条項3のプラグイン導入・有効化、フック機能と必須フックの
実行状態を扱う。

## 起動時の契約確認

`flow:` で始まるスキルの起動を同梱の [guard-adoption.py](../../hooks/guard-adoption.py) が
PreToolUse フックで受け、[起動するスキルに要る区分](#スキルごとに要る区分)の条項を
[確認スクリプト](#導入の検証)で確認する。不足があれば起動を deny し、欠けた条項とこの文書の所在を示す。

flow:commit-worker は不正コミット防止チェックでも条項1を確認する。

Codex は `Skill` イベントを持たない。スキルの着手前に、[導入の検証](#導入の検証)の Codex 用コマンドを
[着手するスキルに要る区分](#スキルごとに要る区分)の `--tier` を付けて実行し、終了コード0を確認する。
レビュー・相談の CLI とコミットの同梱エントリポイントをシェルから起動する場合は
[codex-flow.py](../../hooks/codex-flow.py) が `PreToolUse` で Codex 用の導入検査を行い、不足を deny する。
コミットの同梱エントリポイントには開発条項を、レビュー・相談の CLI には基本条項を要件とする。
