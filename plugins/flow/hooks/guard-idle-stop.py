#!/usr/bin/env python3
"""Stop フック: 停止を既定で禁じ、末尾行が停止宣言のものだけを許可する。

宣言は4種。`待機` は終端でない `background_tasks` が在るときだけ通す——手番が戻る経路の無いまま止まるのを
防ぐ。`応答` は、問われたことに答えた回答を届けるために手番を返す場合に使う——完了を主張しないので
完了の判定は掛からない。`要判断` と `応答` は音を鳴らす。

併せてこのセッションが起動した背景処理を数え、生存しているものが残ったままの `待機` 以外の宣言と、
生存している `wait.py` が2つ以上ある `待機` をブロックする。

codex のジョブ記録も同じように見る。進行の実体を失ったまま実行中として残った記録はどの宣言でも
ブロックする——残せば以後そのスレッドを継ぐ起動が拒否され続ける。進行中と分かる記録は
`待機` 以外の宣言をブロックする。どちらとも決められない記録はブロックしない。

`要判断` は区分の申告行と、区分外に当たらないことを確かめた旨の1行を、`応答` は残っている指示を
名指しする1行を要求する。書かれた内容の真偽は検査できないので、書かせること自体を条件にする——
残りを名指しできない `応答` は完了の突き合わせを避ける経路になり、区分を当てられない `要判断` は
ユーザーが手を入れるまで作業が進まない停止になる。区分外の確認の1行は常時の文脈に載らないため、
初めて要判断で止まろうとした停止は必ずここで弾かれ、この deny が区分外の列挙を渡す。1手番を
費やすが、止まるべきでない停止はその1手番で消える。

`応答` はさらに、**直近のユーザー発言より後に成果物へ手を出していないこと**を転写で確かめる。
調べるための読み取りは答えるうちだが、編集・サブエージェントへの委譲と継続・書き換えるコマンドが
入っていれば、その手番は作業の途中である。コマンドは語の位置で見るので、引用の中の言及・捨て場への
リダイレクト・空振りの指定(`--dry-run` 等)は当たらない。残りを名指しできたことは、それをいま実行できることを
意味する——申告行だけを条件にすると、この宣言が作業を先送りする口実になる。手を出した事実は
取り消せないので、この条件で弾かれた手番は宣言を書き直しても通らない。

判定は末尾行の等値比較。応答本文を渡さないハーネスでは判定せず通す——判定できないことを不許可の
理由にすると、何を書いても抜けられない恒久ブロックになる。

使い方: プラグインルートを第1引数に渡す Stop フックとして登録する。--selftest で自己テスト。
宣言を偽らずに発火させたいときは、宣言を書かずに手番を返せば、宣言の無い停止として弾かれる。
"""
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DONE = "[停止: 完了]"
DECISION = "[停止: 要判断]"
WAIT = "[停止: 待機]"
RESPOND = "[停止: 応答]"
MARKERS = (DONE, DECISION, WAIT, RESPOND)
CHIME = (DECISION, RESPOND)

WAIT_SCRIPT = "wait.py"
CODEX_REAPER = "reap_codex_jobs.py"
STOP_DOC = "defect-followthrough.md"

DECISION_FIELD = "要判断の区分"
DECISION_CONFIRM = "区分外に当たらないことを確かめた"
DECISION_KINDS = ("要求仕様", "指示不明", "停止規定", "操作承認")
RESPOND_FIELD = "残っている指示"
RESPOND_EMPTY = frozenset((
    "なし", "無し", "無い", "ない", "特になし", "特に無し", "特にない", "該当なし", "該当無し",
    "ありません", "特にありません", "ございません", "残っていません", "残りなし", "0件", "0",
    "すべて完了", "全て完了", "完了", "完了済み", "済み", "none", "n/a", "na", "nothing",
))
RESPOND_TRIM = "*_`「」()()。．.、,-・ 　"
MATERIAL = ("scripts", "goal_material.py")
GIT_WRITE_HOOK = "guard-git-write.py"
WORK_TOOLS = ("Edit", "Write", "NotebookEdit", "Agent", "Task", "SendMessage")
WORK_COMMANDS = ("tee", "cp", "mv", "rm", "mkdir", "touch", "truncate", "patch", "dd", "install")
GIT_WRITE = (
    "commit", "push", "pull", "merge", "rebase", "reset", "restore", "checkout", "switch",
    "add", "rm", "mv", "revert", "cherry-pick", "am", "apply", "clean",
)
WRITE_SCRIPTS = ("stamp_plugin_version.py", "trash.py")
INTERPRETERS = ("python", "python3", "py", "node", "bash", "sh", "pwsh", "powershell", "perl")
REDIRECTS = (">", ">>")
DISCARDS = ("/dev/null", "nul", "$null")
SEPARATORS = (";", "&&", "||", "|", "&", "(", ")")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DRY_RUN = {
    "apply": ("--check", "--stat", "--numstat", "--summary"),
    "clean": ("-n", "--dry-run"), "add": ("-n", "--dry-run"), "rm": ("-n", "--dry-run"),
    "mv": ("-n", "--dry-run"), "push": ("-n", "--dry-run"), "commit": ("--dry-run",),
}
DECISION_CONFIRM_LINE = re.compile(
    rf"^\s*[>*_\-\s]*{re.escape(DECISION_CONFIRM)}[*_\s。．.]*$"
)
DECISION_KIND_LINE = re.compile(
    rf"^\s*[>*_\-\s]*{re.escape(DECISION_FIELD)}[*_\s]*(?:は)?[*_\s]*[::]?[*_\s]*(.+?)\s*$"
)
RESPOND_LINE = re.compile(
    rf"^\s*[>*_\-\s]*{re.escape(RESPOND_FIELD)}[*_\s]*(?:は)?[*_\s]*[::]?[*_\s]*(.+?)\s*$"
)
KIND_LEAD = "*_`「(("
DECISION_EXCLUDED = (
    "実装の設計", "段取り", "作業量", "レビュアーが諮れと述べたこと", "既定や選択肢を書けること",
    "裏取りを用意できないこと",
)
ENDED_RUN = ("gone", "stalled")
LIVE = ("running", "pending", "backgrounded")
ENDED = ("completed", "failed", "killed", "cancelled", "canceled", "timeout")


def plugin_root():
    roots = [arg for arg in sys.argv[1:] if arg != "--selftest"]
    return roots[0] if roots else ""


def plugin_script(name):
    """誘導先スクリプトの絶対パス。プラグインルートを渡されない起動では名前だけを返す。"""
    root = plugin_root()
    if not root:
        return f"<flow プラグイン同梱の scripts/{name}>"
    return Path(root, "scripts", name).as_posix()


def wait_script():
    return plugin_script(WAIT_SCRIPT)


def reaper_script():
    return plugin_script(CODEX_REAPER)


def stop_doc():
    """諮ってよい場面を定める規約の絶対パス。プラグインルートを渡されない起動では名前だけを返す。"""
    root = plugin_root()
    if not root:
        return f"<flow プラグイン同梱の docs/guidance/{STOP_DOC}>"
    return Path(root, "docs", "guidance", STOP_DOC).as_posix()


FOLD = {"：": ":", "［": "[", "］": "]", " ": "", "　": "", "\t": ""}

TAG = "[guard-idle-stop]"

# 音の再生には OS の音声機能を使う。端末のベル(BEL)は鳴ったかどうかをフックの側から確かめられない。
SOUND_COMMANDS = {
    "Windows": [
        sys.executable, "-c",
        "import winsound; [winsound.Beep(f, 130) for f in (880, 1175, 1568)]",
    ],
    "Darwin": ["afplay", "/System/Library/Sounds/Glass.aiff"],
    "Linux": ["canberra-gtk-play", "--id=dialog-question"],
}

_HOW = (
    "停止するには、応答の**末尾行を停止宣言だけ**にすること。前後に文や囲み記号を付けない。"
    f"{DONE} — 依頼された作業が終わり、手番を返す。"
    f"{DECISION} — ユーザーの判断が要り、それ無しでは進めない。"
    "何を選ぶのかを確定的に書いたうえで付ける。"
    f"{WAIT} — 何かの完了を待つ。手番が戻る経路として、登録された背景処理が在るときだけ使える。"
    f"{RESPOND} — 問われたことに答えたので手番を返す。まだ済んでいない指示が残っている。"
)

REASON_NO_MARKER = (
    "末尾行が停止宣言になっていない。このハーネスは停止を既定で禁じており、"
    "宣言の無い停止は、作業の途中で手番を返したものとして扱う。"
    "止まらずに作業を続けるか、止まる理由を宣言すること。"
    "宣言を文中で言及しただけ・囲み記号で包んだだけでは許可されない。" + _HOW
)
REASON_WAIT_UNSUBSTANTIATED = (
    f"{WAIT} と宣言しているが、手番が戻る経路になる背景処理が無い(終わった処理は経路にならない)。"
    "このまま止まると再開する手立てが無く、ユーザーが促すまで止まり続けることになる。"
    f"取るべき行動は、待つ対象を実際に起動するか、時間で待つなら {wait_script()} を"
    "run_in_background の Bash で起動するか、待たずにその作業を自分で済ませること。"
    f"作業が終わっているなら {DONE}、ユーザーの判断が要るなら {DECISION}、"
    f"問われたことに答えただけで指示が残っているなら {RESPOND} を使う。"
)
REASON_MULTIPLE = (
    "末尾行に停止宣言が複数ある。どの理由で止まるのかが決まらない。1つだけにすること。" + _HOW
)
REASON_TASK_LEFT_RUNNING = (
    "このセッションが起動した背景処理が残ったまま手番を返そうとしている: {tasks}。"
    "残したものは後で終わって手番を戻し、確認するものが無いターンを1つ作る。"
    "用が済んだものは TaskStop で止めてから宣言し直すこと(対象のIDは起動時の戻り値が示す)。"
    f"まだ待つのであれば、止めずに {WAIT} を使う。"
)
REASON_WAIT_DUPLICATED = (
    f"{WAIT_SCRIPT} が2つ以上動いている。待つ対象は1つなので、先に用が済んだ後も残りが発火し、"
    "確認するものが無いターンを作る。TaskStop で余分な方を止めてから宣言し直すこと。"
)
REASON_CODEX_STALE = (
    "このセッションが起こした codex のジョブが、進行の実体を失ったまま実行中として記録に"
    "残っている: {jobs}。残したままにすると、以後このスレッドを継ぐ起動が同じ記録に当たって拒否され"
    "続け、前ラウンドの文脈を持たない新規スレッドへ縮退する。"
    'python3 "{reaper}" {session} で終局させてから宣言し直すこと。'
)
DECISION_KINDS_TEXT = (
    "**要求仕様**(ユーザーの決めた値・方針・スコープ・受入条件が変わる。守れなくなった明示指定を"
    "含む)・**指示不明**(対象・入力がユーザーにしか無く、推測では別のものを作る)・"
    "**停止規定**(規約またはスキルが命じる停止が実際に発火した)・"
    "**操作承認**(取り消せない操作・外部へ及ぶ操作の承認が要る)。"
)
DECISION_EXCLUDED_TEXT = (  # 使う側が `.format(doc=...)` で埋める。
    f"{'・'.join(DECISION_EXCLUDED)}は、"
    "いずれも区分に当たらない(判定は {doc} が正本)。"
)
REASON_DECISION_UNCLASSIFIED = (
    f"{DECISION} と宣言しているが、どの区分の判断を求めるのかの申告が無い。"
    "止まってよいのは次の4区分だけで、どれにも当てられない停止は、ユーザーが手を入れるまで作業が"
    "進まない状態を作る。"
    + DECISION_KINDS_TEXT
    + DECISION_EXCLUDED_TEXT
    + f"当たる区分が在るなら、末尾行の前に「{DECISION_FIELD}: <区分名>」の1行を置いて宣言し直す。"
    f"当たらないなら止まらずに自分で決めて進み、決めた理由を報告に残す。作業が終わっているなら {DONE}、"
    f"問われたことに答えただけで指示が残っているなら {RESPOND}。"
)
REASON_DECISION_UNCONFIRMED = (
    f"{DECISION} と区分「{{kind}}」が申告されているが、区分外に当たらないことを確かめた旨が無い。"
    "**次のどれかに当たらないかを確かめること**: "
    f"{'・'.join(DECISION_EXCLUDED)}。"
    "どれかに当たるなら止まる場面ではない——自分で決めて進み、決めた理由を報告に残す。"
    f"どれにも当たらないと確かめたなら、区分の行に続けて「{DECISION_CONFIRM}」の1行を置いて宣言し直す。"
)
REASON_RESPOND_UNSUBSTANTIATED = (
    f"{RESPOND} と宣言しているが、何が残っているのかの申告が無い。"
    f"{RESPOND} は「問われたことに答えたが、まだ済んでいない指示が残っている」ことを述べる宣言で、"
    "残りが無いのにこれを書くと、済んでいるものを未了と偽って伝えたうえ、完了に掛かる突き合わせを"
    "受けずに手番を返すことになる。"
    f"残っているものが在るなら、末尾行の前に「{RESPOND_FIELD}: <何が残っているか>」の1行を置いて"
    f"宣言し直す。書くのは**ユーザーの指示のうち済んでいないもの**で、自分で足した作業は書かない。"
    f"「なし」のように残りが無いと述べる申告は名指しに当たらない。残っていないなら {DONE} を使う。"
)
REASON_RESPOND_AFTER_WORK = (
    f"{RESPOND} と宣言しているが、直近のユーザー発言より後に成果物へ手を出している"
    "(編集・サブエージェントへの委譲と継続・書き換えるコマンドのいずれか)。"
    f"{RESPOND} は**問われたことに答えたので、回答を届けるために手番を返す**宣言である——"
    "調べるための読み取りは答えるうちだが、手を出したならそれは作業であって、その手番はまだ"
    "作業の途中である。"
    "**残っている指示を書き出せたということは、それをいま実行できるということである**"
    "——実行できない理由が無いなら、止まらずにその指示へ進む。"
    "順序の指定(「まず」「先に」)は、済んだところで止まってよいという意味ではない。"
    f"手番を返してよいのは、残っている指示が無くなったとき({DONE})、ブロックされたとき({DECISION})、"
    f"待ちが発生したとき({WAIT})である。"
)
REASON_CODEX_RUNNING = (
    "このセッションが起こした codex が実行中のまま手番を返そうとしている: {jobs}。"
    "途中で止めた実行は成果ゼロで費用だけが残るので、殺して片付けない。結果を受け取るまで待つこと"
    f"——待つなら背景処理を起こして {WAIT} を使う。"
    'プロセスが終わったのに記録が実行中のままなら python3 "{reaper}" {session} で終局させる。'
)


def fold(text):
    for src, dst in FOLD.items():
        text = text.replace(src, dst)
    return text


def last_line(message):
    for line in reversed(message.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def play_sound():
    """起動しっぱなしにして待たない。鳴らせない環境では黙って諦める。"""
    command = SOUND_COMMANDS.get(platform.system())
    if not command:
        return
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def declared_kind(message):
    """申告された区分。申告が無い・語彙に無い語であれば None。"""
    for line in message.splitlines():
        matched = DECISION_KIND_LINE.match(line)
        if not matched:
            continue
        declared = matched.group(1).lstrip(KIND_LEAD)
        for kind in DECISION_KINDS:
            if declared.startswith(kind):
                return kind
    return None


def remaining(message):
    """残っている指示が名指しされているか。文中の言及と区別するため行単位で見る。無いと述べた申告は
    名指しではないので数えない。"""
    for line in message.splitlines():
        matched = RESPOND_LINE.match(line)
        if not matched:
            continue
        named = matched.group(1).strip().strip(RESPOND_TRIM).lower()
        if named and named not in RESPOND_EMPTY:
            return True
    return False


def git_write_hook():
    """語彙とトークナイザを持つ側を取り込む。読めなければ None。"""
    try:
        spec = importlib.util.spec_from_file_location(
            "_guard_git_write", Path(__file__).resolve().parent / GIT_WRITE_HOOK,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def discarded(tokens, index):
    """リダイレクト先が捨て場か。記述子の複製と数字は行き先ではないので読み飛ばす。"""
    for token in tokens[index + 1:index + 3]:
        if token == "&" or token.isdigit():
            continue
        return token.lower() in DISCARDS
    return True


def git_verb(module, tokens, index):
    """`git` の直後に来るサブコマンド。値を取るオプションは値ごと読み飛ばす。"""
    pos = index + 1
    while pos < len(tokens) and tokens[pos].startswith("-"):
        option = tokens[pos].split("=", 1)[0]
        pos += 1
        if option in module.GIT_VALUE_OPTIONS and "=" not in tokens[pos - 1]:
            pos += 1
    return tokens[pos] if pos < len(tokens) else ""


def segments(module, tokens):
    """区切りで節へ割る。1つの呼び出しの判定に、別の呼び出しの語を混ぜないため。"""
    found, current = [], []
    for token in tokens:
        if token in SEPARATORS or token in module.CONTROL_OR_WRAPPER:
            if current:
                found.append(current)
            current = []
            continue
        current.append(token)
    if current:
        found.append(current)
    return found


def segment_writes(module, tokens):
    """1つの呼び出しが成果物を書き換えうるか。"""
    for index, token in enumerate(tokens):
        if token in REDIRECTS and not discarded(tokens, index):
            return True
        if token.startswith("<<"):
            return True
    head = 0
    while head < len(tokens) and (
        tokens[head] in module.WRAPPERS or ASSIGNMENT.match(tokens[head])
        or tokens[head].startswith("-") or tokens[head].isdigit()
    ):
        head += 1
    if head >= len(tokens):
        return False
    rest = tokens[head + 1:]
    name = tokens[head].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in WORK_COMMANDS:
        return True
    if name == "sed" and any(t.startswith("-i") or t == "--in-place" for t in rest):
        return True
    if name in INTERPRETERS and any(
        t.replace("\\", "/").rsplit("/", 1)[-1] in WRITE_SCRIPTS for t in rest
    ):
        return True
    if name not in ("git", "git.exe"):
        return False
    verb = git_verb(module, tokens, head)
    return verb in GIT_WRITE and not any(t in DRY_RUN.get(verb, ()) for t in rest)


def writes(command):
    """成果物を書き換えうるコマンドか。語として現れる位置で見るので、引用の中の言及は当たらない。
    判定できない入力は False——判定できないことを不許可の理由にしない。"""
    module = git_write_hook()
    if module is None:
        return False
    try:
        tokens = module._tokens(command.replace("\n", " ; "))
    except ValueError:
        return False
    return any(segment_writes(module, part) for part in segments(module, tokens))


def is_work(block):
    """調べるためでなく手を出すための呼び出しか。編集・委譲と、書き換えるコマンドを見る。"""
    if block.get("name") in WORK_TOOLS:
        return True
    command = (block.get("input") or {}).get("command")
    return isinstance(command, str) and writes(command)


def worked_since_instruction(data):
    """直近のユーザー発言より後に手を出したか。転写から判定できなければ None。"""
    root = Path(__file__).resolve().parent.parent
    try:
        spec = importlib.util.spec_from_file_location("_goal_material", Path(root, *MATERIAL))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows = module.rows_of(data.get("transcript_path"))
        calls = None if rows is None else module.calls_since_last_instruction(rows)
    except Exception:
        return None
    return None if calls is None else any(is_work(block) for block in calls)


def confirmed(message):
    """区分外の確認が申告されているか。文中の言及と区別するため行単位で見る。"""
    return any(DECISION_CONFIRM_LINE.match(line) for line in message.splitlines())


def tasks_of(data):
    tasks = data.get("background_tasks")
    return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []


def live_tasks(data):
    """手番が戻る経路になりうる背景処理。終端と分かるものだけを除く。"""
    return [t for t in tasks_of(data) if t.get("status") not in ENDED]


def label(tasks):
    """deny 文で残っている処理を名指しするための一覧。多いときは残りを件数で補う。"""
    shown = ", ".join(str(t.get("id", "?")) for t in tasks[:5])
    return shown if len(tasks) <= 5 else f"{shown} ほか{len(tasks) - 5}件"


def live_now(data):
    """生存と分かる背景処理。語彙に無い状態は数えない——読めない値を生存とみなすと、
    既に終わった処理を止めようがないまま停止が塞がり続ける。語彙が増えて漏れても、
    漏れは素通り側にだけ倒れる。"""
    return [t for t in tasks_of(data) if t.get("status") in LIVE]


def live_waits(data):
    """生存と分かる wait.py の件数。"""
    def is_wait(task):
        fields = " ".join(str(task.get(key, "")) for key in ("command", "description"))
        return f"/{WAIT_SCRIPT}" in fields.replace("\\", "/")
    return sum(1 for t in live_now(data) if is_wait(t))


def codex_jobs(data):
    """このセッションが起こした、記録上まだ終局していない codex ジョブ。判定できない環境では空。"""
    session = data.get("session_id")
    root = plugin_root()
    if not isinstance(session, str) or not session or not root:
        return []
    try:
        spec = importlib.util.spec_from_file_location(
            "_reap_codex_jobs", Path(root, "scripts", CODEX_REAPER),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.active_jobs(session)
    except Exception:
        return []


def decide(data, codex=()):
    """`(通した宣言, block する理由)` を返す。両方 None なら判定しない入力。"""
    message = data.get("last_assistant_message")
    if message is None:
        return None, None
    line = fold(last_line(message))
    found = [m for m in MARKERS if fold(m) in line]
    if len(found) > 1:
        return None, REASON_MULTIPLE
    if len(found) != 1 or line != fold(found[0]):
        return None, REASON_NO_MARKER
    if found[0] == WAIT:
        if not live_tasks(data):
            return None, REASON_WAIT_UNSUBSTANTIATED
        if live_waits(data) > 1:
            return None, REASON_WAIT_DUPLICATED
    elif live_now(data):
        return None, REASON_TASK_LEFT_RUNNING.format(tasks=label(live_now(data)))
    stale = [job for job in codex if job.get("state") in ENDED_RUN]
    live = [job for job in codex if job.get("state") == "live"]
    blocked = stale or (live if found[0] != WAIT else [])
    if blocked:
        reason = REASON_CODEX_STALE if stale else REASON_CODEX_RUNNING
        return None, reason.format(
            jobs=label(blocked), reaper=reaper_script(), session=data.get("session_id"),
        )
    if found[0] == RESPOND:
        if not remaining(message):
            return None, REASON_RESPOND_UNSUBSTANTIATED
        if worked_since_instruction(data):
            return None, REASON_RESPOND_AFTER_WORK
    if found[0] == DECISION:
        kind = declared_kind(message)
        if not kind:
            return None, REASON_DECISION_UNCLASSIFIED.format(doc=stop_doc())
        if not confirmed(message):
            return None, REASON_DECISION_UNCONFIRMED.format(kind=kind)
    return found[0], None


def main():
    # UTF-8 を明示する。既定の符号化で読むと宣言が化けて一致せず、全ての停止をブロックし続ける。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    marker, reason = decide(data, codex_jobs(data))
    if reason:
        print(json.dumps({"decision": "block", "reason": f"{TAG} {reason}"}))
        return
    if marker in CHIME:
        play_sound()


def selftest():
    def stop(message, tasks=(), crons=(), active=False):
        return {
            "hook_event_name": "Stop",
            "stop_hook_active": active,
            "last_assistant_message": message,
            "background_tasks": list(tasks),
            "session_crons": list(crons),
            "session_id": "S1",
        }

    def unclassified():
        return REASON_DECISION_UNCLASSIFIED.format(doc=stop_doc())

    def unconfirmed(kind):
        return REASON_DECISION_UNCONFIRMED.format(kind=kind)

    def codex_stale(jobs):
        return REASON_CODEX_STALE.format(jobs=jobs, reaper=reaper_script(), session="S1")

    def codex_running(jobs):
        return REASON_CODEX_RUNNING.format(jobs=jobs, reaper=reaper_script(), session="S1")

    task = {"id": "b1", "type": "shell", "status": "running", "description": "検査"}
    cron = {"id": "c1"}
    waiting = {
        "id": "b2", "type": "shell", "status": "running", "description": "上限明けまで待つ",
        "command": 'python3 "/p/flow/scripts/wait.py" "2026-08-30 21:00"',
    }
    waiting_seconds = dict(waiting, id="b3", command="python3 /p/flow/scripts/wait.py 300")
    waiting_ended = dict(waiting, id="b4", status="completed")
    other_test = dict(waiting, id="b5", description="回帰検査",
                      command="python3 -m pytest tests/test_wait.py")
    unknown = dict(task, id="b6", status="mystery")
    codex_alive = {"id": "j1", "status": "running", "pid": 111, "state": "live"}
    codex_ghost = {"id": "j2", "status": "running", "pid": 222, "state": "gone"}
    codex_stalled = {"id": "j4", "status": "running", "pid": 444, "state": "stalled"}
    codex_unknown = {"id": "j3", "status": "running", "pid": 333, "state": "unknown"}
    pending = dict(task, id="b7", status="pending")
    crowd = [dict(task, id=f"c{n}") for n in range(6)]
    block_cases = [
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[task]),
         REASON_TASK_LEFT_RUNNING.format(tasks="b1")),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[pending]),
         REASON_TASK_LEFT_RUNNING.format(tasks="b7")),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=crowd),
         REASON_TASK_LEFT_RUNNING.format(tasks="c0, c1, c2, c3, c4 ほか1件")),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[other_test]),
         REASON_TASK_LEFT_RUNNING.format(tasks="b5")),
        (stop("コミットしました。ハッシュは 90d8326 です。"), REASON_NO_MARKER),
        (stop("次はレビューを回します。"), REASON_NO_MARKER),
        (stop("お任せいただけるなら、このまま実装します。"), REASON_NO_MARKER),
        (stop("どちらで進めますか。1. フックを作る 2. 文書だけにする"), REASON_NO_MARKER),
        (stop(""), REASON_NO_MARKER),
        (stop("[停止: 完了]\n\n続けて別の作業もあります。"), REASON_NO_MARKER),
        (stop("末尾に [停止: 完了] と書く決まりにしました。"), REASON_NO_MARKER),
        (stop("作業は終わりました。\n\n`[停止: 完了]`"), REASON_NO_MARKER),
        (stop("作業は終わりました。\n\n[停止: 完了] 以上です。"), REASON_NO_MARKER),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]"), REASON_WAIT_UNSUBSTANTIATED),
        (stop("外部の CI の完了を待ちます。\n\n[停止: 待機]", crons=[cron]),
         REASON_WAIT_UNSUBSTANTIATED),
        (stop("外部の CI の完了を待ちます。\n\n[停止: 待機]", tasks=[waiting_ended]),
         REASON_WAIT_UNSUBSTANTIATED),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[waiting]), REASON_TASK_LEFT_RUNNING.format(tasks="b2")),
        (stop("どちらで進めますか。\n\n[停止: 要判断]", tasks=[waiting_seconds]),
         REASON_TASK_LEFT_RUNNING.format(tasks="b3")),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting, waiting_seconds]),
         REASON_WAIT_DUPLICATED),
        (stop("作業は終わりました。\n\n[停止: 完了] [停止: 要判断]"), REASON_MULTIPLE),
        (stop("回答しました。\n残っている指示: プッシュ\n\n[停止: 応答]", tasks=[task]),
         REASON_TASK_LEFT_RUNNING.format(tasks="b1")),
        (stop("回答しました。\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n残っている指示: なし\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n残っている指示は無い\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n**残っている指示**: 特になし。\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n残っている指示: 特にありません\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n残っている指示: すべて完了\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。残っている指示は無い。\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n残っている指示: プッシュ\n\n[停止: 応答]"), codex_running("j1"), [codex_alive]),
        (stop("どちらで進めますか。1. フックを作る 2. 文書だけにする\n\n[停止: 要判断]"),
         unclassified()),
        (stop("諮ります。\n\n要判断の区分: 実装方針\n\n[停止: 要判断]"), unclassified()),
        (stop("諮ります。区分は要求仕様です。\n\n[停止: 要判断]"), unclassified()),
        (stop("受入条件が変わります。\n\n要判断の区分: 要求仕様\n\n[停止: 要判断]"),
         unconfirmed("要求仕様")),
        (stop("要判断の区分: 要求仕様\n区分外に当たらないことを確かめたわけではない。\n\n[停止: 要判断]"),
         unconfirmed("要求仕様")),
        (stop("これからフックを書きます。", active=True), REASON_NO_MARKER),
        (stop("コミットしました。ハッシュは 90d8326 です。"), REASON_NO_MARKER, [codex_ghost]),
        (stop("作業は終わりました。\n\n[停止: 完了]"), codex_stale("j2"), [codex_ghost]),
        (stop("どちらで進めますか。\n\n[停止: 要判断]"), codex_stale("j2"), [codex_ghost]),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[task]), codex_stale("j2"), [codex_ghost]),
        (stop("作業は終わりました。\n\n[停止: 完了]"), codex_stale("j2"), [codex_alive, codex_ghost]),
        (stop("作業は終わりました。\n\n[停止: 完了]"), codex_running("j1"), [codex_alive]),
        (stop("どちらで進めますか。\n\n[停止: 要判断]"), codex_running("j1"), [codex_alive]),
        (stop("作業は終わりました。\n\n[停止: 完了]"), codex_stale("j4"), [codex_stalled]),
    ]
    pass_cases = [
        (stop("コミットしました。ハッシュは 90d8326 です。\n\n[停止: 完了]"), "[停止: 完了]"),
        (stop("受入条件が変わります。\n\n要判断の区分: 要求仕様\n区分外に当たらないことを確かめた"
              "\n\n[停止: 要判断]"), "[停止: 要判断]"),
        (stop("受入条件が変わります。\n\n要判断の区分: 要求仕様(受入条件が変わる)\n"
              "区分外に当たらないことを確かめた\n\n[停止: 要判断]"), "[停止: 要判断]"),
        (stop("対象のファイルが分かりません。\n\n**要判断の区分**: 指示不明\n"
              "**区分外に当たらないことを確かめた**\n\n[停止: 要判断]"), "[停止: 要判断]"),
        (stop("千日手で終わりました。\n\n- 要判断の区分:停止規定\n- 区分外に当たらないことを確かめた"
              "\n\n[停止: 要判断]"), "[停止: 要判断]"),
        (stop("プッシュしてよいか確かめます。\n\n要判断の区分:操作承認\n"
              "区分外に当たらないことを確かめた\n\n[停止: 要判断]"), "[停止: 要判断]"),
        (stop("作業は終わりました。\n\n[停止：完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止:完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n［停止：完了］"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止：　完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止:\t完了]"), "[停止: 完了]"),
        ({"hook_event_name": "Stop", "last_assistant_message": None}, None),
        ({"hook_event_name": "Stop", "stop_hook_active": False}, None),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[task]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[unknown]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting, task]), "[停止: 待機]"),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[waiting_ended]), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[unknown]), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止: 完了]", active=True), "[停止: 完了]"),
        (stop("ご質問への回答です。\n残っている指示: 実装ステップ2以降\n\n[停止: 応答]"), "[停止: 応答]"),
        (stop("回答です。\n残っている指示: None を渡したときの分岐の修正\n\n[停止: 応答]"), "[停止: 応答]"),
        (stop("回答です。\n残っている指示: - プッシュ\n\n[停止: 応答]"), "[停止: 応答]"),
        (stop("回答しました。\n**残っている指示**: プッシュ\n\n[停止: 応答]", tasks=[waiting_ended]), "[停止: 応答]"),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[task]), "[停止: 待機]", [codex_alive]),
        (stop("作業は終わりました。\n\n[停止: 完了]"), "[停止: 完了]", [codex_unknown]),
    ]
    def unpack(case):
        return case if len(case) == 3 else (case[0], case[1], ())

    ok = True
    for case in block_cases:
        data, expected, codex = unpack(case)
        actual = decide(data, codex)
        if actual != (None, expected):
            ok = False
            print(f"FAIL expected block: {data.get('last_assistant_message')!r} -> {actual!r}")
    for case in pass_cases:
        data, expected, codex = unpack(case)
        actual = decide(data, codex)
        if actual != (expected, None):
            ok = False
            print(f"FAIL expected pass: {data.get('last_assistant_message')!r} -> {actual!r}")
    if not _roundtrip_ok(block_cases[0][0], block_cases[0][1]):
        ok = False
    if not _codex_lookup_ok():
        ok = False
    if not _respond_gate_ok():
        ok = False
    total = len(block_cases) + len(pass_cases) + 3
    print("ALL PASS" if ok else "SOME FAILED", f"({total} cases)")
    sys.exit(0 if ok else 1)


def _codex_lookup_ok():
    """同梱スクリプトを実際に取り込ませ、残骸1件で block が出るところまでを通す。判定関数へ
    直接リストを渡す検査は取り込み経路を通らないので、そこが壊れていても合格してしまう。"""
    plugin_root_dir = Path(__file__).resolve().parents[1]
    session = "selftest-session"
    job = {
        "id": "task-selftest", "status": "running", "phase": "running", "pid": 0,
        "sessionId": session, "updatedAt": "2000-01-01T00:00:00.000Z",
    }
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp, "plugins", "data", "codex", "state", "repo-0123456789abcdef")
        workspace.mkdir(parents=True)
        (workspace / "state.json").write_text(
            json.dumps({"version": 1, "jobs": [job]}, ensure_ascii=False), encoding="utf-8",
        )
        payload = json.dumps({
            "hook_event_name": "Stop",
            "last_assistant_message": f"作業は終わりました。\n\n{DONE}",
            "background_tasks": [],
            "session_id": session,
        }, ensure_ascii=False).encode("utf-8")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), str(plugin_root_dir)],
            input=payload, capture_output=True, check=False,
            env={**os.environ, "CLAUDE_CONFIG_DIR": tmp, "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp},
        )
    try:
        out = json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"FAIL codex lookup: 出力が JSON でない: {result.stdout[:200]!r}")
        return False
    if out.get("decision") != "block" or "task-selftest" not in out.get("reason", ""):
        print(f"FAIL codex lookup: 残骸を名指しする block が出ない: {out!r}")
        return False
    return True


def _respond_gate_ok():
    """転写を実際に読ませて、道具を呼んだ後の `応答` が弾かれるところまでを通す。転写を渡さない
    検査はこの経路を通らないので、そこが壊れていても合格してしまう。"""
    def row(kind, block):
        return {"type": kind, "isSidechain": False,
                "message": {"role": kind, "content": [block]}}

    asked = row("user", {"type": "text", "text": "不要なエントリは消せ"})
    acted = row("assistant", {"type": "tool_use", "id": "t1", "name": "Agent", "input": {}})
    edited = row("assistant", {"type": "tool_use", "id": "t2", "name": "Edit", "input": {}})
    committed = row("assistant", {"type": "tool_use", "id": "t3", "name": "Bash",
                                  "input": {"command": "git commit -m x"}})
    scripted = row("assistant", {"type": "tool_use", "id": "t6", "name": "Bash",
                                 "input": {"command": "python3 - <<PY"}})
    sent = row("assistant", {"type": "tool_use", "id": "t7", "name": "SendMessage",
                             "input": {"to": "reviewer"}})
    looked = row("assistant", {"type": "tool_use", "id": "t4", "name": "Bash",
                               "input": {"command": 'grep -n "git commit -m" x.py | tail -8'}})
    listed = row("assistant", {"type": "tool_use", "id": "t8", "name": "Bash",
                               "input": {"command": "git stash list && git tag"}})
    discarded = row("assistant", {"type": "tool_use", "id": "t9", "name": "Bash",
                                  "input": {"command": "strings -n 6 x 2>/dev/null | grep -n a"}})
    unparsed = row("assistant", {"type": "tool_use", "id": "ta", "name": "Bash",
                                 "input": {"command": 'ls "C:' + chr(92) + '"'}})
    wrapped = row("assistant", {"type": "tool_use", "id": "tb", "name": "Bash",
                                "input": {"command": "timeout 180 git push origin main"}})
    stamped = row("assistant", {"type": "tool_use", "id": "tc", "name": "Bash",
                                "input": {"command": "python3 scripts/stamp_plugin_version.py"}})
    staged = row("assistant", {"type": "tool_use", "id": "td", "name": "Bash",
                               "input": {"command": "git add -A && git diff --staged --stat"}})
    ranged = row("assistant", {"type": "tool_use", "id": "te", "name": "Bash",
                               "input": {"command": 'sed -n 1,9p x.md && grep -n -i "更新" y.md'}})
    read = row("assistant", {"type": "tool_use", "id": "t5", "name": "Read", "input": {}})
    said = row("assistant", {"type": "text", "text": "お答えします。"})
    message = ("回答しました。\n残っている指示: 規約2本のレビュー\n\n" + RESPOND)
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "transcript.jsonl")
        for rows, expected, label in (
            ([asked, acted], REASON_RESPOND_AFTER_WORK, "委譲した後の応答は弾く"),
            ([asked, edited], REASON_RESPOND_AFTER_WORK, "編集した後の応答は弾く"),
            ([asked, committed], REASON_RESPOND_AFTER_WORK, "コミットした後の応答は弾く"),
            ([asked, scripted], REASON_RESPOND_AFTER_WORK, "スクリプトを流した後の応答は弾く"),
            ([asked, sent], REASON_RESPOND_AFTER_WORK, "委譲を継いだ後の応答は弾く"),
            ([asked, wrapped], REASON_RESPOND_AFTER_WORK, "前置語ごしのプッシュも弾く"),
            ([asked, stamped], REASON_RESPOND_AFTER_WORK, "同梱の書き込みスクリプトも弾く"),
            ([asked, staged], REASON_RESPOND_AFTER_WORK, "後続の節の空振り指定で打ち消されない"),
            ([asked, said], RESPOND, "答えただけの応答は通す"),
            ([asked, looked, listed, read, said], RESPOND, "調べてから答えた応答は通す"),
            ([asked, discarded, unparsed, ranged, said], RESPOND,
             "捨て場へのリダイレクト・読めないコマンド・別の節の -i は通す"),
        ):
            body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
            path.write_text(body + "\n", encoding="utf-8")
            marker, reason = decide({
                "hook_event_name": "Stop", "last_assistant_message": message,
                "transcript_path": path.as_posix(), "background_tasks": [], "session_id": "S1",
            })
            actual = reason if reason else marker
            if actual != expected:
                ok = False
                print(f"FAIL {label}: {actual!r}")
    return ok


def _roundtrip_ok(data, expected):
    """ハーネスと同じ形(UTF-8 の JSON を標準入力へ)で起動する。判定関数を直接叩く検査は標準入力の
    復号を通らないので、そこが壊れていても合格してしまう。"""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=payload, capture_output=True, check=False,
    )
    try:
        out = json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"FAIL stdin roundtrip: 出力が JSON でない: {result.stdout[:200]!r}")
        return False
    if out.get("decision") != "block" or expected not in out.get("reason", ""):
        print(f"FAIL stdin roundtrip: block が出ない: {out!r}")
        return False
    return True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
