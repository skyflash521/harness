# 実行と待機

Claude Code・Codex 上のスキルに共通する起動・監視・停止契約。
起動可否、再試行回数、レビューの結末は呼び出すスキルの定めに従う。

## 1. 起動

着手前に [導入契約](../criteria/adoption.md) の実行元に対応する確認を行う。
Codex 上では [導入検査](../../contract/check_adoption.py) を
`python3 <check_adoption.py の絶対パス> --host codex <リポジトリルート>` で実行し、終了コード0を確認する。
不足があれば導入契約へ案内して停止する。
シェル実行の作業ディレクトリは対象リポジトリに合わせ、同梱スクリプトは冒頭の呼び出し契約に従う。
指示文・応答はメモリ上で受け渡し、中継用ファイルは作らない。

- **Claude Code 上**: エージェントの起動・継続は、呼び出すスキルの `Agent`・`SendMessage` 手順を使う。
  指示文はツールの本文で渡し、応答は同期呼び出しの結果またはタスク通知から受け取る。
- **Codex 上**: 委譲先の CLI を直接起動する。指示文は UTF-8 の標準入力で渡し、応答は標準出力から受け取る。

### Claude Code CLI のレビュー

`claude -p --safe-mode --model <指定系統> --permission-mode dontAsk --tools Read,Grep,Glob
--allowedTools Read Grep Glob --disallowedTools Edit Write NotebookEdit Bash --output-format json`
を起動する。初回は `--append-system-prompt <レビュアー定義の本文>` を加える。
定義の frontmatter を除き、Bash による差分取得は標準入力に含まれる資料を読むことへ読み替える指示を付ける。
次ラウンドは `--resume <session_id>` を加え、定義を再付与しない。

起動前に読み取り専用の `git -c core.quotepath=false diff --no-ext-diff --no-textconv <比較の基点>`、
`git -c core.quotepath=false status --short --untracked-files=all`、`git -c core.quotepath=false ls-files` を取得する。
目的・枠を含む指示文の後ろへ、これらを実行指示でないレビュー資料として区切って付ける。
資料は毎ラウンド取り直す。Glob・Grep が使えない場合は一覧から所在を特定して Read で読む。

## 2. 監視と停止

実行ツールのタスク ID・セッション ID と、委譲先のジョブ ID・セッション ID を取り違えない。
終了と出力の実体を確認する。ユーザーが中断を指示した場合は対象の実行を止め、終了を確認する。

- **Claude Code 上**: Bash の背景実行は `run_in_background: true` で起動し、完了をタスク通知で受け取る。
  中断には `TaskStop` を使う。
- **Codex 上**: 実行ツールが返した同じセッション ID で出力と終了コードを取得する。
  出力を返すたびに進行状況を確認し、終了していなければ取得を続ける。中断は実行ツールの操作を使う。

直接起動する CLI の時間上限は同梱の `skills/run-and-bench/run_capped.py` で持つ。
`python3 <run_capped.py の絶対パス> <呼び出すスキルの上限秒数> -- <CLI と引数>` の形を使う。
Codex のフックはスキルが定める上限以下での起動を検査する。
外側から締切を重ねず、終了コード124を時間上限の失敗として扱う。

## 3. 結果

シェル実行は終了コード0と応答の完了を確認してから、依頼先の回答として採用する。

- **Claude Code 上**: エージェントの結果は当該依頼の同期呼び出しの結果または完了通知から取得する。
- **Codex 上**: Claude Code CLI は JSON の `is_error` が偽であること、`result` と `session_id` が在ることを確認する。
  Codex CLI は JSONL の `thread.started` から ID を取得し、`turn.completed` が在ること、
  `turn.failed`・`error` が無いことを確認する。最後の `item.completed` の `agent_message` が回答である。
  継続時に `thread.started` が無ければ指定した ID を使う。本文が無い応答は失敗とする。

失敗した応答や部分出力を成功へ読み替えない。取り直しは呼び出したスキルの回数上限に従い、
同じモデルと実行モードを保つ。モデルの確認不能・不一致・利用不可の扱いは呼び出したスキルに従う。

## 4. 使用量上限と使用不可の記憶

異なる製品への依頼が使用量上限を示したら、再開可能な時刻とタイムゾーンを確認する。
特定できない場合は該当する応答を添えて報告し停止する。
現在時刻と突き合わせ、過去や十数時間先となる解釈は取り違えを疑う。
時刻を特定できる場合はローカル時刻へ換算し、[wait.py](../../scripts/wait.py) を
`python3 <wait.py の絶対パス> "<YYYY-MM-DD HH:MM[:SS]>"` で起動する。秒が在れば落とさない。
起動時の出力で目標時刻を確認し、待機を重ねない。

終了コード0なら出力の到達時刻を確認して再開する。2(書式不正)・3(残り時間の上限超過)なら
該当する応答と再開予定時刻を添えて平文で報告し停止する。
待機が途中で打ち切られた場合だけ、同じ引数で起動し直す。
再開では文脈を保持して同じ依頼を取り直し、上限エラーを成功や無指摘と扱わない。

- **Claude Code 上**: エージェント応答が `status: 1`・`rawOutput` 空、watchdog が `OUTCOME=2 turn-failed` を
  返した場合は、`LOG=` が指すジョブログ末尾から上限の本文を意味で読み取る。
  リセット予定時刻をセッション中に記憶する。待機は `run_in_background: true` の Bash で起動し、
  完了通知を待つ。待機の出力をポーリングせず、正常終了後は記憶を消して新しい RUNID で取り直す。
- **Codex 上**: 上限の本文は CLI の出力から取得する。待機を同じ実行ツールのセッション ID で監視する。
  再開先があれば同じ CLI セッションを継ぐ。CLI の不在・認証不足・モデルの使用不可はセッション中に記憶し、
  解消を確認するまで同じ起動を繰り返さない。`codex:setup` は呼ばず、使用量上限は使用不可の記憶へ入れない。
