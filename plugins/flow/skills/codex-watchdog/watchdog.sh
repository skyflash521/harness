#!/usr/bin/env bash
# codex-watchdog — 読み取り専用。codex:codex-rescue エージェントを起動するスキルが、そのラウンドを
# 無限に待たないために使う。codex companion のジョブログを見張り、codex の結末を終了コードで返す。
# 書き込みは一切しないので、単一のコマンドとして許可リストに載せられる。
#
#   exit 0  正常終了
#   exit 2  失敗
#   exit 3  停滞(ジョブログの更新時刻が STALL_SECS 進まない。ヒューリスティック)
#   exit 4  codex が始まらない、または上限に達した
#             (a) STARTUP_GRACE_SECS 以内にこのラウンドのジョブログが現れない。遅い起動や探索の
#                 失敗でも当たる、再試行を優先するヒューリスティック。
#             (b) ログは特定できたが WALL_CAP_SECS を超えても終局しない。
#
# 終了前に標準出力へ次を出す。呼び出し側はこれを読めば、どのログに結果があるかを推測せずに済む。
#     LOG=<選んだログのパス>   (特定できなければ空)
#     OUTCOME=<コード> <理由>
#
# 引数(すべて省略可、位置指定): $1=STALL_SECS  $2=WALL_CAP_SECS  $3=STATE_ROOT
#                               $4=STARTUP_GRACE_SECS  $5=RUNID(相関トークン)
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
WALL_CAP_SECS="${2:-1200}"
STATE_ROOT="${3:-${STATE_ROOT:-${HOME:-}/.claude/plugins/data/codex-openai-codex/state}}"
STARTUP_GRACE_SECS="${4:-240}"
# RUNID は英数と _ と - に限る。ERE のメタ文字を持ち込ませないため。
RUNID="${5:-}"
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
  if [ -z "$log" ] && [ $(( $(now) - start )) -ge "$STARTUP_GRACE_SECS" ]; then
    report "$log" 4 "no-start (no job log within ${STARTUP_GRACE_SECS}s)"
    exit 4
  fi
  if [ $(( $(now) - start )) -ge "$WALL_CAP_SECS" ]; then
    report "$log" 4 "wall-cap"
    exit 4
  fi
  if [ -n "$log" ] && [ -f "$log" ]; then
    if grep -qE "$fail_re" "$log" 2>/dev/null; then report "$log" 2 "turn-failed"; exit 2; fi
    if grep -qE "$done_re" "$log" 2>/dev/null; then report "$log" 0 "completed";  exit 0; fi
    m=$(mtime "$log")
    if [ -n "$m" ] && [ $(( $(now) - m )) -ge "$STALL_SECS" ]; then
      report "$log" 3 "stall"
      exit 3
    fi
  fi
  sleep "$POLL"
done
