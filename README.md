# harness

複数リポジトリで共有するAIハーネスを提供する Claude Code 用のプラグイン。

- **guard**: 言語非依存の安全装置。取り返しのつかない操作と暴走する操作を拒否し、可逆で
  安全な代替へ誘導する。
- **flow**: 開発ワークフロー。実装・レビュー・コミットの進め方を規律づける。
  [導入契約](plugins/flow/docs/rules/adoption.md)を満たすリポジトリで有効化する。

## 対応環境

**Windows・macOS・Linux で動作することを要件とする。** 配布するプラグインも、このリポジトリ自身の
スクリプトも、いずれか1つのOSでしか動かない書き方(OS固有コマンドの決め打ち、パス区切りやドライブ
文字の直書きなど)を採らない。3つのOSで同じ手段を採れない処理は、OSごとの分岐として実装側で吸収し、
利用者から見た挙動を揃える。

Windows では bash を Git Bash が提供する。プラグインを使うマシンが満たす前提は
[導入契約の前提環境](plugins/flow/docs/rules/adoption.md#前提環境)が定める。

## プラグインの利用

### リポジトリへの導入

プラグインの実体はマシンのキャッシュに置かれ、有効化はリポジトリの追跡される
`.claude/settings.json` の `enabledPlugins` が決める。**そのマシンで user スコープの導入を済ませて
いるなら、このキーを真にするだけでよい。** 済んでいるかは `claude plugin list` が
`Scope: user` と表示するかで分かる。

済んでいないなら、対象リポジトリのルートで次を実行する。

```sh
claude plugin marketplace add skyflash521/harness
claude plugin install guard@harness --scope project
claude plugin install flow@harness --scope project
```

marketplace の登録先はマシン側の状態なので、`marketplace add` に `--scope` を付けない。
`--scope project` を付けると追跡される設定に `extraKnownMarketplaces` が書かれるが、それだけでは
プラグインは取得も導入もされない。

`install` の `--scope` は既定が `user`(そのマシンの全リポジトリへ適用)なので、このリポジトリだけに
効かせるには `project` を明示する。この操作が `enabledPlugins` も書き込む。

プラグインが読み込まれるのはセッションの開始時なので、有効化の反映は次のセッションからになる。

#### 導入したのに読み込まれない場合

project スコープの導入記録はプロジェクトの絶対パスで引き当てられ、照合はドライブ文字の大小を
区別する。**導入をターミナルの CLI で行い、利用は VSCode 拡張のセッション**という組み合わせでは、
同じフォルダを CLI が `C:\...`、拡張が `c:\...` と識別するため一致せず、プラグインが読み込まれない
(`claude plugin list` には導入済みと表示されるので、状態からは気付けない)。

その場合は user スコープで導入し、直後に既定を無効へ戻す。user スコープの記録は絶対パスを持たない
ので、この不一致が起きない。

```sh
claude plugin marketplace add skyflash521/harness
claude plugin install guard@harness --scope user
claude plugin install flow@harness --scope user
claude plugin disable guard@harness --scope user
claude plugin disable flow@harness --scope user
```

有効化は変わらずリポジトリの `enabledPlugins` が行い、それがユーザー設定を上書きする。末尾の
`disable` を省くと全リポジトリでフックが発火する。導入済みの状態で `install` を再実行すると有効化が
戻るため、繰り返す場合も `disable` まで通す。

**flow を有効化するリポジトリは[導入契約](plugins/flow/docs/rules/adoption.md)の条項も満たす。**
満たしていないと flow のスキルは起動時に停止する。guard だけなら契約は要らない。満たしているかを
確かめる手順は、その文書が定める。

### 更新の反映

各マシンで実行する。marketplace のカタログを取り直しても、導入済みプラグインは古いバージョンの
ままなので、プラグインごとに更新する。一括で更新する手段は無い。反映は次のセッションから。

```sh
claude plugin marketplace update harness
claude plugin update guard@harness
claude plugin update flow@harness
```

`update` の `--scope` は導入したスコープに合わせる(既定は `user`)。`marketplace update` に
このオプションは無い。

プラグインの内容が変わっても `version` が上がっていなければ、更新の対象にならない。
このリポジトリでは刻印スクリプトが `version` を生成し、CI が刻印漏れを検出する。

## harness の開発環境

このリポジトリ自体を開発する場合。開発用の clone で、刻印漏れとコミットメッセージを検査する
git フックを有効化する。リポジトリローカル設定なので、marketplace のキャッシュや他のリポジトリでは
設定しない。

```sh
git config core.hooksPath .githooks
```

`ruff` と `rumdl` は pip で個別に導入する。harness は配布物を持たないため `pyproject.toml` を
置かず、開発依存の宣言機構を使わない。

```sh
pip install ruff rumdl
```

`lychee` は pip では入らないため各自で導入する(配布物のバイナリを入れるか、パッケージマネージャを
使う)。

**Python は 3.9 以上**を要求する。スクリプトが型注釈で組み込みジェネリクス(`list[str]` 等)を
使うため。

検査器が採用する規則・外す規則とその理由は [.ruff.toml](.ruff.toml) と
[.rumdl.toml](.rumdl.toml) が持つ。

通す検査の一覧と合格条件は[検証手順](docs/conventions/verification.md)が定める。
