#!/usr/bin/env bash
# codex-watchdog — codex:codex-rescue エージェントを起動するスキルが、そのラウンドを無限に待たない
# ために使う。codex companion のジョブログを見張り、codex の結末を終了コードで返す。
# 外へ及ぶ操作は wall-cap に達した回の停止だけで、それ以外は読み取りに徹する。停滞(exit 3)では
# 止めない——ログの更新が途切れただけの正常なラウンドにも当たるヒューリスティックで、誤検知した回は
# codex がその後完走している。止めてよいのは、終局しないことが時間で確定した回に限る。
#
#   exit 0  正常終了
#   exit 2  失敗
#   exit 3  停滞(ジョブログの更新時刻が STALL_SECS 進まない。ヒューリスティック)
#   exit 4  codex が始まらない、または上限に達した
#             (a) STARTUP_GRACE_SECS 以内にこのラウンドのジョブログが現れない。遅い起動や探索の
#                 失敗でも当たる、再試行を優先するヒューリスティック。
#             (b) ログは特定できたが WALL_CAP_SECS を超えても終局しない。この回は companion の
#                 cancel でジョブを止める。止めないと codex はターンを続け、結果を受け取る側が
#                 居ないまま費用だけが増える。
#   exit 5  companion が結末を書かずに消えた(ジョブ記録は pid を持ったまま、そのプロセスが無い)。
#             既知の原因は、同じ作業ディレクトリの別セッションの終了が共有の broker を止めること。
#             companion は接続を失うと何も出力せず終了コード0で終わるので、停滞を待たずにここで知らせる。
#
# 終了前に標準出力へ次を出す。呼び出し側はこれを読めば、どのログに結果があるかを推測せずに済む。
#     LOG=<選んだログのパス>   (特定できなければ空)
#     OUTCOME=<コード> <理由>
#
# 引数(位置指定。$1-$5 は省略可、$6 は必須): $1=STALL_SECS  $2=WALL_CAP_SECS  $3=STATE_ROOT
#                               $4=STARTUP_GRACE_SECS  $5=RUNID(相関トークン)
#                               $6=COMPANION(codex companion の絶対パス)
#
# COMPANION が無ければ wall-cap で止められないので、省略と別スクリプトの指定は bad-arg で弾く。
# 停止の結果は OUTCOME の理由へ括弧書きで添える。cancelled=ターンの中断まで確認できた、
# cancelled-record-only=記録は終局したがターンの中断は確認できない、cancel-failed=cancel が失敗、
# cancel-timeout=cancel が時間内に戻らない。**後ろ3つは codex が走り続けている可能性が残る**ので、
# 呼び出し側はそのラウンドの費用を止められていない前提で扱う。
#
# RUNID を渡すと、そのトークンを含むジョブログだけをこのラウンドのものとして選ぶ。時刻にも起動順にも
# 依存しないので、同じリポジトリで複数のセッションを同時に回してもログを取り違えない。
#
# RUNID を渡さない場合は、起動時点に存在しなかった最新のログを選ぶ。**呼び出し側はエージェントより
# 先にこれを起動すること。** 同じ名前のリポジトリを2つチェックアウトしていると、もう一方で同時に走る
# codex のログを選びうる。結果の主チャネルはエージェントの応答なので、この誤選択は呼び出し側の再試行に
# 縮退し、誤った修正には至らない。

set -u

STALL_SECS="${1:-420}"
WALL_CAP_SECS="${2:-900}"
STATE_ROOT="${3:-${STATE_ROOT:-${CLAUDE_CONFIG_DIR:-${HOME:-}/.claude}/plugins/data/codex-openai-codex/state}}"
STARTUP_GRACE_SECS="${4:-240}"
# RUNID は英数と _ と - に限る。ERE のメタ文字を持ち込ませないため。
RUNID="${5:-}"
COMPANION="${6:-}"
# 名前まで確かめる。ここが素通りすると、承認済みのこの起動が任意の node スクリプトを走らせる口になる。
# 区切りはスラッシュへ寄せてから見る。Windows の呼び出し側はバックスラッシュ区切りで渡しうる。
case "${COMPANION//\\//}" in
  '') printf 'LOG=\nOUTCOME=4 bad-arg (COMPANION required)\n'; exit 4;;
  */codex-companion.mjs|codex-companion.mjs) ;;
  *) printf 'LOG=\nOUTCOME=4 bad-arg (COMPANION is not codex-companion.mjs: %s)\n' "$COMPANION"; exit 4;;
esac
[ -f "$COMPANION" ] || {
  printf 'LOG=\nOUTCOME=4 bad-arg (COMPANION not found: %s)\n' "$COMPANION"; exit 4
}
case "$RUNID" in
  '') ;;
  *[!A-Za-z0-9_-]*) printf 'LOG=\nOUTCOME=4 bad-arg (invalid RUNID: %s)\n' "$RUNID"; exit 4;;
esac
# bash の整数比較は非数値だと黙ってエラーになり、歯止めが外れる。
for _v in "$STALL_SECS" "$WALL_CAP_SECS" "$STARTUP_GRACE_SECS"; do
  case "$_v" in
    ''|*[!0-9]*) printf 'LOG=\nOUTCOME=4 bad-arg (non-integer time value: %s)\n' "$_v"; exit 4;;
  esac
  # bash の整数は64ビット。18桁までなら比較が溢れない。
  [ "${#_v}" -gt 18 ] && { printf 'LOG=\nOUTCOME=4 bad-arg (time value out of range: %s)\n' "$_v"; exit 4; }
done
[ "$STARTUP_GRACE_SECS" -gt "$WALL_CAP_SECS" ] && STARTUP_GRACE_SECS="$WALL_CAP_SECS"
POLL=5

mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }
now() { date +%s; }

# companion 自身のログ行は完全な ISO 時刻で始まる。応答本文の中の同じ語を終局と誤らないための錨。
ts_re='^\[[0-9]{4}-[0-9]{2}-[0-9]{2}T[^]]*\] '
done_re="${ts_re}(Turn completed\.|Final output)"
fail_re="${ts_re}Turn failed\."
report() { printf 'LOG=%s\nOUTCOME=%s %s\n' "$1" "$2" "$3"; }

# wall-cap に達した回の codex を止める。ジョブIDはログのファイル名がそのまま持つ。companion の
# cancel はターンの中断・プロセスツリーの終了・記録の終局を順に行うが、**中断に失敗しても続行して
# 正常終了する**ので、応答の turnInterrupted で区別する。応答しない app-server を待ち続けると
# WALL_CAP の保証そのものが消えるため、待ちには上限を置く。
CANCEL_WAIT_SECS=60
cancel_job() {
  local lg="$1" id out rc runner
  [ -n "$lg" ] || { printf 'cancel-failed'; return; }
  id=$(basename "$lg"); id="${id%.log}"
  runner="$(dirname "${BASH_SOURCE[0]}")/../run-and-bench/run_capped.py"
  out=$(python3 "$runner" "$CANCEL_WAIT_SECS" -- node "$COMPANION" cancel "$id" --json 2>/dev/null)
  rc=$?
  if [ "$rc" -eq 124 ]; then
    printf 'cancel-timeout'
    return
  fi
  if [ "$rc" -ne 0 ]; then
    printf 'cancel-failed'
    return
  fi
  if printf '%s\n' "$out" | grep -Eq '"turnInterrupted"[[:space:]]*:[[:space:]]*true'; then
    printf 'cancelled'
  else
    printf 'cancelled-record-only'
  fi
}

# companion はリポジトリ名を接頭辞にした状態ディレクトリを作る。合わなければ何も見つからない。
repo=$(basename "$PWD" 2>/dev/null || printf '')
list_logs() {
  local d
  find "$STATE_ROOT" -mindepth 1 -maxdepth 1 -type d -name "${repo}-*" 2>/dev/null \
  | while IFS= read -r d; do
      [ -n "$d" ] && find "$d/jobs" -type f -name 'task-*.log' 2>/dev/null
    done
}

in_baseline() { printf '%s\n' "$baseline" | awk -v k="$1" '$0==k{f=1} END{exit !f}'; }

# companion はタスク指示文の冒頭をジョブの .json の summary へ保存する。無ければログ本文を見る。
# 一致は境界付きで取る。短いトークンが長いトークンの一部に当たらないようにするため。
has_runid() {
  local lg="$1" js="${1%.log}.json" pat
  pat='TASK-RUNID:[[:space:]]*'"${RUNID}"'([^A-Za-z0-9_-]|$)'
  if [ -f "$js" ]; then
    grep -Eq -- "$pat" "$js" 2>/dev/null
    return
  fi
  grep -Eq -- "$pat" "$lg" 2>/dev/null
}

# ジョブ記録が持つ companion の pid。終局時に companion が null へ書き換えるので、数値が残っていれば
# 記録上はまだ走っている。
job_pid() {
  local js="${1%.log}.json"
  [ -f "$js" ] || return
  grep -Eo '"pid"[[:space:]]*:[[:space:]]*[0-9]+' "$js" 2>/dev/null | head -n 1 | grep -Eo '[0-9]+$'
}

# 消えたと確かめられた回だけ真。問い合わせに失敗した回は生きている側へ倒す(誤って消失と報じない)。
# Windows の pid は Git Bash の kill では問えないので tasklist を使う。
pid_gone() {
  local out
  if command -v tasklist.exe >/dev/null 2>&1; then
    out=$(MSYS_NO_PATHCONV=1 tasklist.exe /FI "PID eq $1" /NH /FO CSV 2>/dev/null) || return 1
    case "$out" in *"\"$1\""*) return 1;; esac
    return 0
  fi
  kill -0 "$1" 2>/dev/null && return 1
  [ -d /proc ] && [ ! -d "/proc/$1" ] && return 0
  ps -p "$1" >/dev/null 2>&1 && return 1
  return 0
}

# companion は終局時に記録の pid を消してからプロセスを終える。消失を確かめた後に記録と終局行を
# 読み直し、まだ同じ pid が残り終局行も無い回だけを消失とする(正常終了との競合を消失と誤らない)。
companion_gone() {
  local lg="$1" p
  p=$(job_pid "$lg"); [ -n "$p" ] || return 1
  pid_gone "$p" || return 1
  [ "$(job_pid "$lg")" = "$p" ] || return 1
  grep -qE "$done_re|$fail_re" "$lg" 2>/dev/null && return 1
  gone_pid="$p"
  return 0
}

baseline=$(list_logs | sort)

start=$(now)

# このラウンドのログを選ぶ。runid は RUNID を含む最新、baseline は起動時点に無かった最新。
# どちらも見つからなければ空を出力する。
pick_active() {
  local mode="$1" p cm best="" bestmt=0
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if [ "$mode" = runid ]; then
      has_runid "$p" || continue
    else
      in_baseline "$p" && continue
    fi
    cm=$(mtime "$p"); [ -n "$cm" ] || continue
    if [ "$cm" -ge "$bestmt" ]; then bestmt=$cm; best=$p; fi
  done < <(list_logs)
  printf '%s' "$best"
}

# RUNID を渡した回は RUNID 一致だけで選ぶ。起動時点との差へ落ちると、同時に走る別セッションの
# ログを選びうる。
select_log() {
  local lg=""
  if [ -n "$RUNID" ]; then
    lg=$(pick_active runid)
  else
    lg=$(pick_active baseline)
  fi
  printf '%s' "$lg"
}

log=""
while :; do
  [ -z "$log" ] && log=$(select_log)
  # 終局は上限より先に見る。同じ反復で両方が成立しうるので、後に置くと直前に終わったラウンドを
  # wall-cap が殺し、書き出し中の成果を失う。
  if [ -n "$log" ] && [ -f "$log" ]; then
    if grep -qE "$fail_re" "$log" 2>/dev/null; then report "$log" 2 "turn-failed"; exit 2; fi
    if grep -qE "$done_re" "$log" 2>/dev/null; then report "$log" 0 "completed";  exit 0; fi
    if companion_gone "$log"; then
      report "$log" 5 "companion-gone (pid ${gone_pid} exited without writing an outcome)"
      exit 5
    fi
  fi
  if [ -z "$log" ] && [ $(( $(now) - start )) -ge "$STARTUP_GRACE_SECS" ]; then
    report "$log" 4 "no-start (no job log within ${STARTUP_GRACE_SECS}s)"
    exit 4
  fi
  if [ $(( $(now) - start )) -ge "$WALL_CAP_SECS" ]; then
    report "$log" 4 "wall-cap ($(cancel_job "$log"))"
    exit 4
  fi
  if [ -n "$log" ] && [ -f "$log" ]; then
    m=$(mtime "$log")
    if [ -n "$m" ] && [ $(( $(now) - m )) -ge "$STALL_SECS" ]; then
      report "$log" 3 "stall"
      exit 3
    fi
  fi
  sleep "$POLL"
done
