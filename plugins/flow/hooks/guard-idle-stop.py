#!/usr/bin/env python3
"""Stop フック: 停止を既定で禁じ、末尾行が停止宣言のものだけを許可する。

宣言は4種。`待機` は終端でない `background_tasks` が在るときだけ通す——手番が戻る経路の無いまま止まるのを
防ぐ。`応答` は、問われたことに答えた回答を届けるために手番を返す場合に使う——完了を主張しないので
完了の判定は掛からない。`要判断` と `応答` は音を鳴らす。

併せてこのセッションが起動した背景処理を数え、生存しているものが残ったままの `待機` 以外の宣言と、
生存している `wait.py` が2つ以上ある `待機` をブロックする。**`wait.py` が1つも無い `待機` も
ブロックする**——待つ対象が生きていることは通知が届くことを意味せず、届かなければ手番が戻る経路が
無いまま止まり続ける。時間で起きる `wait.py` がその起点になり、締切が来れば進み具合を確かめて
待ち続けるか取り直すかを決められる。

**その締切が長すぎる `待機` もブロックする**——待つ対象が別に在る場での締切には上限が定まっており、
それより後ろへ置けば、対象が結末を出せないまま落ちた回に、超えたぶんだけ手番が戻らずに止まる。
対象側の監視が持つ閾値へ余裕を足して置く形が、長い締切の出所になる。**時間そのものを待つ形
(`wait.py` だけが残る待機)は長さを問わない**——使用量上限のリセット待ちがそれで、そこは長いのが
正しい。

codex のジョブ記録も同じように見る。進行の実体を失ったまま実行中として残った記録はどの宣言でも
ブロックする——残せば以後そのスレッドを継ぐ起動が拒否され続ける。進行中と分かる記録は
`待機` 以外の宣言をブロックする。どちらとも決められない記録はブロックしない。

`要判断` は区分の申告行と、区分外に当たらないことを確かめた旨の1行を、`応答` は**ユーザーが問うた
ことを復唱する1行**を要求する。復唱は直近のユーザー発言と突き合わせるので、**発言に無い文字列は
書けない**——ただし確かめられるのはその発言に在ることまでで、**写した部分が問いかどうかは判定して
いない**。区分を当てられない `要判断` は
ユーザーが手を入れるまで作業が進まない停止になる。**区分が要求仕様のときは、変わる先が実在する
ファイルで名指しされていることも確かめる**——名指しできないなら、その停止は文書に書かれた要件の
変更ではない。決まっていないことをいま決める場面なら止まる理由にならず、ユーザーが会話で出した
禁止・明示指定が妨げているなら区分は停止規定である。区分外の確認の1行は常時の文脈に載らないため、
初めて要判断で止まろうとした停止は必ずここで弾かれ、この deny が区分外の列挙を渡す。1手番を
費やすが、止まるべきでない停止はその1手番で消える。

**`応答` が問うのは、問われたかどうかだけである。** 済んでいない指示が残っているかは条件に入れない
——問いだけを受けた手番でも、答えを届けるために止まってよい。**代わりに、答えたことを済ませたことに
数えない**: 復唱した発言は消化の突き合わせ(`guard-goal-completion.py`)で未消化のまま残るので、
指摘へ答えるのに `応答` を使っても、その指摘は `完了` の前に `対応済み` として片付ける必要がある。

`応答` はさらに、**直近のユーザー発言より後に成果物へ手を出していないこと**を転写で確かめる。
調べるための読み取りは答えるうちだが、編集・サブエージェントへの委譲と継続・書き換えるコマンドが
入っていれば、その手番は作業の途中である。コマンドは語の位置で見るので、引用の中の言及・捨て場への
リダイレクト・空振りの指定(`--dry-run` 等)は当たらない。窓は次のユーザー発言まで閉じないので、
**この条件で弾かれたら、そのユーザー発言に対して `応答` はもう使えない**。だから deny は残る出口
——受けたものが済んでいるなら `完了`、残りがあるなら続行——を示す。示さないと、止まれなくなった
エージェントが受けていない作業へ進む。

どの宣言でも、**残っている作業を指示として名乗る行**は弾く。そこへ書かれるのは自分が見立てた残件で
あることが多く、ユーザーはそれを自分が頼んだものとして受け取る。どの宣言もこの行を要求していない
——要求しているのは区分の申告と問いの復唱だけである。見るのは行頭の定型のラベルだけなので、本文で
指示に触れる書き方は当たらない。

判定は末尾行の等値比較。応答本文を渡さないハーネスでは判定せず通す——判定できないことを不許可の
理由にすると、何を書いても抜けられない恒久ブロックになる。

使い方: プラグインルートを第1引数に渡す Stop フックとして登録する。--selftest で自己テスト。
宣言を偽らずに発火させたいときは、宣言を書かずに手番を返せば、宣言の無い停止として弾かれる。
"""
import datetime
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
WAIT_CAP_SECS = 900
CODEX_REAPER = "reap_codex_jobs.py"
STOP_DOC = "defect-followthrough.md"

DECISION_FIELD = "要判断の区分"
DECISION_CONFIRM = "区分外に当たらないことを確かめた"
DECISION_KINDS = ("要求仕様", "指示不明", "停止規定", "操作承認")
DECISION_SPEC_KIND = "要求仕様"
DECISION_SPEC_FIELD = "変わる要求仕様"
RESPOND_FIELD = "答えた質問"
RESPOND_EMPTY = frozenset((
    "なし", "無し", "無い", "ない", "特になし", "特に無し", "特にない", "該当なし", "該当無し",
    "ありません", "特にありません", "ございません", "0件", "0",
    "質問なし", "質問は無い", "問われていない", "none", "n/a", "na", "nothing",
))
RESPOND_TRIM = "*_`「」()()。．.、,-・ 　"
INSTRUCTION_WORD = "指示"
TRANSCRIPT = ("scripts", "transcript.py")
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
    rf"^\s*[>*_\-\s]*{re.escape(DECISION_CONFIRM)}[*_\s。．.]*"
    r"(?:[(（].*[)）][*_\s。．.]*)?$"
)
DECISION_SPEC_LINE = re.compile(
    rf"^\s*[>*_\-\s]*{re.escape(DECISION_SPEC_FIELD)}[*_\s]*(?:は)?[*_\s]*[::]?[*_\s]*(.+?)\s*$"
)
PATH_SPLIT = re.compile(r"[\s、,。「」『』()（）\[\]`*＿]+")
PATH_TRIM = re.compile(r"(?:#[^/\\]*)?(?::\d+(?:-\d+)?)?$")
DECISION_KIND_LINE = re.compile(
    rf"^\s*[>*_\-\s]*{re.escape(DECISION_FIELD)}[*_\s]*(?:は)?[*_\s]*[::]?[*_\s]*(.+?)\s*$"
)
RESPOND_LINE = re.compile(
    rf"^\s*[>*_\-\s]*{re.escape(RESPOND_FIELD)}[*_\s]*(?:は)?[*_\s]*[::]?[*_\s]*(.+?)\s*$"
)
INSTRUCTION_LINE = re.compile(
    rf"^\s*[>*_\-\s]*[^\s::]{{0,12}}{INSTRUCTION_WORD}[*_\s]*(?:は)?[*_\s]*[::]"
)
WAIT_NAME = re.compile(rf"(?:^|[/\s'\"]){re.escape(WAIT_SCRIPT)}")
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
    f"{WAIT} — 何かの完了を待つ。手番が戻る経路として、登録された背景処理と、"
    f"締切になる {WAIT_SCRIPT} の背景実行がどちらも在るときだけ使える。"
    f"{RESPOND} — ユーザーに問われたことへ答えたので、答えを届けるために手番を返す。"
    "指示が残っているかどうかは問わない。"
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
    f"取るべき行動は、待つ対象を実際に起動し、締切として {wait_script()} も"
    "run_in_background の Bash で起動すること(時間そのものを待つなら締切だけでよい)。"
    "待たずにその作業を自分で済ませられるなら、そちらのほうが早い。"
    f"作業が終わっているなら {DONE}、ユーザーの判断が要るなら {DECISION}、"
    f"ユーザーに問われたことへ答えたのなら {RESPOND} を使う。"
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
REASON_WAIT_NO_DEADLINE = (
    f"{WAIT} と宣言しているが、**いつ手番が戻るかの締切が張られていない**。"
    "待つ対象が生きていても通知は落ちることがあり、落ちれば"
    "**ユーザーが促すまで何も起きない停止になる**。対象が自分で締切を持っていても同じ。\n"
    "取るべき行動は、対象の所要に見合った秒数で "
    f'`python3 "{wait_script()}" <秒数>` を run_in_background の Bash から起こし、宣言し直すこと。'
    "**締切に達したのに対象が終わっていない回で、締切だけを張り直さない**"
    "——ハングを捕まえるための締切が、ハングを見逃す装置になる。"
)
REASON_WAIT_TOO_LONG = (
    f"{WAIT} の締切が長すぎる: {{tasks}} が残り{{secs}}秒を張っている。待つ対象が別に在る場で、"
    f"{WAIT_CAP_SECS}秒を超える締切は張らない。"
    f"レビュー1ラウンドを待つ回では、{WAIT_CAP_SECS}秒がハングと判断する時間として定まっている"
    "(正本はレビューループの判断ロジック)。**対象が自分でハングを判定する仕組みを持っていても、"
    "その閾値に余裕を足して後ろへ置かない**——締切が担うのは通知の取りこぼしで、対象側の判定が"
    "結末を出すまでの時間ではない。後ろへ置けば、その仕組みが結末を出せずに落ちた回に、"
    "置いたぶんだけ手番が戻らないまま止まる。\n"
    "レビュー以外の待ちでも同じで、締切は対象の所要に見合わせる。"
    f"取るべき行動は、{WAIT_CAP_SECS}秒以内の締切を張り直し、"
    "先の締切を TaskStop で止めてから宣言し直すこと。"
    "時間そのものを待つ場合(使用量上限のリセット待ち等)はこの上限を受けない。"
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
    "**要求仕様**(ユーザーの決めた値・方針・スコープ・受入条件が変わる。"
    "変わる先を実在する文書の箇所として名指しできることが条件)・"
    "**指示不明**(対象・入力がユーザーにしか無く、推測では別のものを作る)・"
    "**停止規定**(規約またはスキルが命じる停止が実際に発火した。"
    "ユーザーが出した禁止や明示指定が作業を妨げ、その解除・変更なしには進めない場面もこれに当たる)・"
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
    f"ユーザーに問われたことへ答えたのなら {RESPOND}。"
)
REASON_DECISION_UNCONFIRMED = (
    f"{DECISION} と区分「{{kind}}」が申告されているが、区分外に当たらないことを確かめた旨が無い。"
    "**次のどれかに当たらないかを確かめること**: "
    f"{'・'.join(DECISION_EXCLUDED)}。"
    "どれかに当たるなら止まる場面ではない——自分で決めて進み、決めた理由を報告に残す。"
    f"どれにも当たらないと確かめたなら、区分の行に続けて「{DECISION_CONFIRM}」の1行を置いて宣言し直す。"
    "確かめた根拠を添えるなら、その行の末尾に括弧で書く。"
)
REASON_DECISION_NO_SPEC = (
    f"{DECISION} と区分「{DECISION_SPEC_KIND}」が申告されているが、"
    "**どの要求仕様が変わるのかが、実在するファイルで名指しされていない**。"
    f"この区分に当たるのは、ユーザーが決めた値・方針・スコープ・受入条件が変わるときだけである"
    "——変わる先が在るなら、それはどこかの仕様書・計画書・台帳に書かれている。"
    f"取るべき行動は、末尾行の前に「{DECISION_SPEC_FIELD}: <ファイル>の<箇所>を<どう変えるか>」の"
    "1行を置いて宣言し直すこと。ファイルは作業ディレクトリから辿れる実在のパスで書く"
    "(行番号や見出しを添えてよい)。\n"
    "**名指しできないなら、変わる要求仕様は無い。** 決まっていないことを自分で決めるのは実装の設計で、"
    "それは止まる理由にならない——選択肢を書けることも、どちらが良いか迷うことも同じである。"
    "自分で決めて進み、決めた理由を報告に残すこと。\n"
    "**ユーザーが会話で出した禁止や明示指定が作業を妨げていて、その解除・変更を諮りたいのであれば、"
    "区分は「停止規定」である**——文書に書かれていない指定は名指しできないので、この区分では通らない。"
)
REASON_RESPOND_UNSUBSTANTIATED = (
    f"{RESPOND} と宣言しているが、**ユーザーに何を問われたのかの復唱が無い**。"
    f"{RESPOND} は「ユーザーに問われたことへ答えたので、答えを届けるために手番を返す」ことを述べる"
    "宣言で、問われていないのにこれを書くと、作業の途中で手番を返す口実になる。"
    f"問われているなら、末尾行の前に「{RESPOND_FIELD}: <ユーザーが問うたこと>」の1行を置いて"
    "宣言し直す。**書くのはユーザーが実際に発した問いの復唱**で、自分が report したい内容・"
    "自分で立てた論点は書かない。"
    f"「なし」のように問いが無いと述べる申告は復唱に当たらない。"
    "**復唱はユーザーの発言から原文のまま引く**——書いた文字列が直近のユーザー発言に見つからなければ"
    "弾かれるので、言い換えず、問いに当たる部分をそのまま写す。"
    f"問われていないなら、この宣言は使えない——作業が終わっているなら {DONE}、"
    f"ユーザーの判断が要るなら {DECISION}、待ちが発生したなら {WAIT} を使い、"
    "どれでもないなら止まらずに作業を続ける。"
)
REASON_RESPOND_UNQUOTED = (
    f"{RESPOND} と宣言しているが、**復唱した文字列が直近のユーザー発言に見つからない**。"
    "この宣言が成り立つのはユーザーに問われたときだけなので、復唱はその発言から原文のまま引かせる。"
    "言い換え・要約・自分で立てた論点は一致しない。"
    f"直近の発言に問いが在るなら、その部分を**原文のまま**「{RESPOND_FIELD}: 」の行へ写して"
    "宣言し直すこと。"
    "**ここで確かめているのは、写した文字列がその発言に在ることだけである**——"
    "問いでない部分を写せば検査は通るが、それは問われたことにはならない。"
    "**問われていないのに問われたことにして止まらない**——"
    "作業の区切りで報告したいだけなら、それは手番を返してよい理由にならない。"
)
REASON_RESPOND_AFTER_WORK = (
    f"{RESPOND} と宣言しているが、直近のユーザー発言より後に成果物へ手を出している"
    "(編集・サブエージェントへの委譲と継続・書き換えるコマンドのいずれか)。"
    f"{RESPOND} は**問われたことに答えたので、回答を届けるために手番を返す**宣言で、"
    "調べるための読み取りは答えるうちだが、手を出したならその手番は作業の途中である。\n"
    "**出口は2つある。** (1) 受けたものが全部済んでいるなら、その記録を残して"
    f"{DONE}——調べろ・確かめろと言われて調べ終えたなら、これに当たる。"
    "(2) 受けた指示にまだ残りがあるなら、止まらずにそれを続ける。\n"
    "**(2)で続けてよいのは受けた指示の範囲だけである。** 調べて分かったこと・妨げが無いと"
    "確かめたことは、**指示されていない作業を始めてよい根拠にならない**"
    "——希望や背景として述べられた事柄は、着手の指示ではない。"
    f"**ここで弾かれたことも根拠にならない**——{RESPOND} を使えないことは、"
    "受けていない作業へ進んでよいという意味ではない。"
    "順序の指定(「まず」「先に」)も、済んだところで止まってよいという意味ではない。"
    "**ユーザーがその作業の着手を禁じているなら進んではならない。この deny は、ユーザーが"
    f"出した禁止を解除しない**——解除を諮るために {DECISION} を使う"
    f"(待ちが発生したなら {WAIT})。"
)
REASON_FABRICATED_INSTRUCTION = (
    f"手番を返す応答に、残っている作業を**「{INSTRUCTION_WORD}」として名乗る行**がある。"
    "そこへ書くのは自分が見立てた残件であることが多く、**ユーザーが出していない作業をそこへ置けば、"
    "受けていないものを受けた指示にすることになる**——読み手はそれを自分が頼んだものとして受け取る。"
    "**この行はどの停止宣言も要求していない。** 要求しているのは"
    f"{DECISION} の区分の申告と {RESPOND} の問いの復唱だけで、残っている作業の列挙を求める宣言は無い。"
    "取るべき行動は、その行を消して宣言し直すこと。"
    "残件そのものを伝えたいなら、**自分が見立てた作業として**本文に書く——"
    f"ユーザーが出した言葉を引くのであれば、それは {RESPOND} の復唱の行が引き受ける。"
    "そのうえで、**列挙できるということは片付けられるということである**——"
    "受けた指示の残りならそのまま続け、自分が見立てた残件なら着手するか打ち切るかを決める。"
    "止まってよい場面に当たるかを確かめ、当たらないなら止まらないこと。"
)
REASON_CODEX_RUNNING = (
    "このセッションが起こした codex が実行中のまま手番を返そうとしている: {jobs}。"
    "途中で止めた実行は成果ゼロで費用だけが残るので、殺して片付けない。結果を受け取るまで待つこと"
    f"——待つなら締切として {WAIT_SCRIPT} を背景で起こしてから {WAIT} を使う。"
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


def names_instruction(message):
    """残っている作業を指示として名乗る行が在るか。文中の言及と区別するため行頭のラベルだけを見る。"""
    return any(INSTRUCTION_LINE.match(line) for line in message.splitlines())


def quoted_questions(message):
    """復唱行に書かれた文字列。文中の言及と区別するため行単位で見る。無いと述べた申告は数えない。"""
    found = []
    for line in message.splitlines():
        matched = RESPOND_LINE.match(line)
        if not matched:
            continue
        named = matched.group(1).strip().strip(RESPOND_TRIM)
        if named and named.lower() not in RESPOND_EMPTY:
            found.append(named)
    return found


def condensed(text):
    """照合のために表記の揺れを畳む。空白と装飾は復唱で落ちても同じ発言を指す。"""
    return "".join(str(text).split()).strip(RESPOND_TRIM).replace("*", "").replace("`", "")


def answered(message, data):
    """復唱された問いが、直近のユーザー発言に実在するか。転写を読めなければ None(判定しない)。"""
    if not quoted_questions(message):
        return False
    module = transcript_module()
    if module is None:
        return None
    rows = module.rows_of(data.get("transcript_path"))
    said = None if rows is None else module.latest_instruction(rows)
    if not said:
        return None
    return any(condensed(q) in condensed(said) for q in quoted_questions(message))


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


def transcript_module():
    """転写の読み取りを持つ側を取り込む。読めなければ None。"""
    try:
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("_transcript", Path(root, *TRANSCRIPT))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def worked_since_instruction(data):
    """直近のユーザー発言より後に手を出したか。転写から判定できなければ None。"""
    module = transcript_module()
    if module is None:
        return None
    try:
        rows = module.rows_of(data.get("transcript_path"))
        calls = None if rows is None else module.calls_since_last_instruction(rows)
    except Exception:
        return None
    return None if calls is None else any(is_work(block) for block in calls)


def confirmed(message):
    """区分外の確認が申告されているか。文中の言及と区別するため行単位で見る。"""
    return any(DECISION_CONFIRM_LINE.match(line) for line in message.splitlines())


def names_file(text, cwd):
    """その申告が実在するファイルを指しているか。行番号・アンカーは落として見る。"""
    base = Path(cwd) if cwd else Path.cwd()
    for token in PATH_SPLIT.split(str(text)):
        candidate = PATH_TRIM.sub("", token)
        while candidate and candidate not in (".", ".."):
            try:
                if Path(candidate).is_file() or (base / candidate).is_file():
                    return True
            except (OSError, ValueError):
                pass
            candidate = candidate[:-1]
    return False


def spec_named(message, cwd):
    """変わる要求仕様が、実在するファイルを指して申告されているか。"""
    return any(
        names_file(matched.group(1), cwd)
        for matched in (DECISION_SPEC_LINE.match(line) for line in message.splitlines())
        if matched
    )


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


def is_wait(task):
    """その背景処理が wait.py か。**起動の形は問わず名前の境界で見る**——絶対パスで渡す形も
    直に名前を書く形も同じ待機で、取りこぼすと締切を張っているのに無いことにしてしまう。"""
    fields = " ".join(str(task.get(key, "")) for key in ("command", "description"))
    return WAIT_NAME.search(fields.replace("\\", "/")) is not None


def waits_in(tasks):
    """その並びに在る wait.py の件数。"""
    return sum(1 for t in tasks if is_wait(t))


def live_waits(data):
    """生存と分かる wait.py の件数。余分な待機を弾く側はこちらで見る。"""
    return waits_in(live_now(data))


def pending_waits(data):
    """終端と分かるものを除いた wait.py の件数。**締切が在るかはこちらで見る**——状態の語彙に
    無い値を数え落とすと、締切を張っているのに弾かれて、何を直しても抜けられなくなる。"""
    return waits_in(live_tasks(data))


def wait_module():
    """締切の引数を解釈する側(wait.py)を取り込む。読めなければ None。

    **取り込み先は自分の位置から辿る**——プラグインルートは誘導文に出すパスの都合で渡されない
    起動があり、そこで取り込めないと締切の長さを見ないまま通す。"""
    try:
        spec = importlib.util.spec_from_file_location(
            "_wait", Path(__file__).resolve().parent.parent / "scripts" / WAIT_SCRIPT,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def deadline_text(task):
    """その起動が wait.py へ渡した引数。**command からしか採らない**——description の文言は
    起動の実体ではなく、そこから読んだ秒数で締切の長さを決めると、実際より短い値で通してしまう。"""
    command = str(task.get("command", "")).replace("\\", "/")
    found = re.search(rf"{re.escape(WAIT_SCRIPT)}[\"']?\s+(.*)$", command)
    if not found:
        return ""
    rest = found.group(1).strip()
    if not rest:
        return ""
    if rest[0] in "\"'":
        end = rest.find(rest[0], 1)
        return rest[1:end] if end > 0 else ""
    return rest.split()[0]


def deadline_left(task, now, module):
    """その締切の残り秒数。引数を解釈できなければ None(判定しない)。"""
    text = deadline_text(task)
    if module is None or not text:
        return None
    target = module.parse_target(text, now)
    if target is None:
        return None
    return module.remaining_seconds(target, now)


def overlong_deadline(data):
    """待つ対象が別に在るのに長すぎる締切。無ければ None、在れば `(対象の一覧, 最長の残り秒数)`。

    **wait.py だけが残る待機は見ない**——時間そのものを待つ形(使用量上限のリセット待ち)で、
    そこは長いのが正しい。解釈できない引数も見ない。判定できないことを不許可の理由にすると、
    何を張り直しても抜けられなくなる。**見るのは生存が確定している集合に限る**——弾く側の判定なので、
    語彙に無い状態をここへ含めると、終わった処理の残骸のせいで長い待ちが塞がり、止める相手も縮める
    締切も無いまま抜けられなくなる(締切が在るかを見る側は広い集合を使うが、あちらは広げるほど
    通る側へ倒れるので向きが逆である)。"""
    tasks = live_now(data)
    if not any(not is_wait(t) for t in tasks):
        return None
    module = wait_module()
    now = datetime.datetime.now()
    overlong = []
    for task in tasks:
        if not is_wait(task):
            continue
        left = deadline_left(task, now, module)
        if left is not None and left > WAIT_CAP_SECS:
            overlong.append((task, int(left)))
    if not overlong:
        return None
    return label([task for task, _ in overlong]), max(secs for _, secs in overlong)


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
        if not pending_waits(data):
            return None, REASON_WAIT_NO_DEADLINE
        overlong = overlong_deadline(data)
        if overlong is not None:
            tasks, secs = overlong
            return None, REASON_WAIT_TOO_LONG.format(tasks=tasks, secs=secs)
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
        if not quoted_questions(message):
            return None, REASON_RESPOND_UNSUBSTANTIATED
        if answered(message, data) is False:
            return None, REASON_RESPOND_UNQUOTED
        if worked_since_instruction(data):
            return None, REASON_RESPOND_AFTER_WORK
    if found[0] == DECISION:
        kind = declared_kind(message)
        if not kind:
            return None, REASON_DECISION_UNCLASSIFIED.format(doc=stop_doc())
        if not confirmed(message):
            return None, REASON_DECISION_UNCONFIRMED.format(kind=kind)
        if kind == DECISION_SPEC_KIND and not spec_named(message, data.get("cwd")):
            return None, REASON_DECISION_NO_SPEC
    if names_instruction(message):
        return None, REASON_FABRICATED_INSTRUCTION
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

    here = Path(__file__).resolve().as_posix()

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
    waiting_bare = dict(waiting, id="b8", command="python3 wait.py 300")
    waiting_long = dict(waiting, id="b9", command="python3 /p/flow/scripts/wait.py 1320")
    waiting_cap = dict(waiting, id="b10",
                       command=f"python3 /p/flow/scripts/wait.py {WAIT_CAP_SECS}")
    waiting_far = dict(waiting, id="b11",
                       command='python3 "/p/flow/scripts/wait.py" "2099-01-01 00:00"')
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
        (stop("回答しました。\n答えた質問: プッシュはまだか\n\n[停止: 応答]", tasks=[task]),
         REASON_TASK_LEFT_RUNNING.format(tasks="b1")),
        (stop("回答しました。\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n答えた質問: なし\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n答えた質問は無い\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n**答えた質問**: 特になし。\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n答えた質問: 特にありません\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n答えた質問: 質問なし\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。答えた質問は無い。\n\n[停止: 応答]"), REASON_RESPOND_UNSUBSTANTIATED),
        (stop("回答しました。\n答えた質問: プッシュはまだか\n\n[停止: 応答]"), codex_running("j1"), [codex_alive]),
        (stop("どちらで進めますか。1. フックを作る 2. 文書だけにする\n\n[停止: 要判断]"),
         unclassified()),
        (stop("諮ります。\n\n要判断の区分: 実装方針\n\n[停止: 要判断]"), unclassified()),
        (stop("諮ります。区分は要求仕様です。\n\n[停止: 要判断]"), unclassified()),
        (stop("受入条件が変わります。\n\n要判断の区分: 要求仕様\n\n[停止: 要判断]"),
         unconfirmed("要求仕様")),
        (stop("要判断の区分: 要求仕様\n区分外に当たらないことを確かめたわけではない。\n\n[停止: 要判断]"),
         unconfirmed("要求仕様")),
        (stop("どちらも計画に無い変更です。\n\n要判断の区分: 要求仕様\n"
              "区分外に当たらないことを確かめた\n\n[停止: 要判断]"), REASON_DECISION_NO_SPEC),
        (stop("方針が変わります。\n\n要判断の区分: 要求仕様\n区分外に当たらないことを確かめた\n"
              f"{DECISION_SPEC_FIELD}: 公開面を絞らないという要件\n\n[停止: 要判断]"),
         REASON_DECISION_NO_SPEC),
        (stop("方針が変わります。\n\n要判断の区分: 要求仕様\n区分外に当たらないことを確かめた\n"
              f"{DECISION_SPEC_FIELD}: docs/specs/no-such-file.md の受入条件\n\n[停止: 要判断]"),
         REASON_DECISION_NO_SPEC),
        (stop("禁止の解除が要ります。\n\n要判断の区分: 要求仕様\n区分外に当たらないことを確かめた\n"
              f"{DECISION_SPEC_FIELD}: ユーザーが出した着手禁止\n\n[停止: 要判断]"),
         REASON_DECISION_NO_SPEC),
        (stop("明示指定を守れません。\n\n要判断の区分: 要求仕様\n区分外に当たらないことを確かめた\n"
              f"{DECISION_SPEC_FIELD}: ユーザーが会話で指定したテスト基盤\n\n[停止: 要判断]"),
         REASON_DECISION_NO_SPEC),
        (stop("これからフックを書きます。", active=True), REASON_NO_MARKER),
        (stop("コミットしました。ハッシュは 90d8326 です。"), REASON_NO_MARKER, [codex_ghost]),
        (stop("作業は終わりました。\n\n[停止: 完了]"), codex_stale("j2"), [codex_ghost]),
        (stop("どちらで進めますか。\n\n[停止: 要判断]"), codex_stale("j2"), [codex_ghost]),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[waiting, task]),
         codex_stale("j2"), [codex_ghost]),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[task]), REASON_WAIT_NO_DEADLINE),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[other_test]), REASON_WAIT_NO_DEADLINE),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[waiting_long, task]),
         REASON_WAIT_TOO_LONG.format(tasks="b9", secs=1320)),
        (stop("作業は終わりました。\n\n[停止: 完了]"), codex_stale("j2"), [codex_alive, codex_ghost]),
        (stop("作業は終わりました。\n\n[停止: 完了]"), codex_running("j1"), [codex_alive]),
        (stop("どちらで進めますか。\n\n[停止: 要判断]"), codex_running("j1"), [codex_alive]),
        (stop("作業は終わりました。\n\n[停止: 完了]"), codex_stale("j4"), [codex_stalled]),
        (stop("コミットしました。\n残っている指示: ステップ2〜52の自律進行\n\n[停止: 完了]"),
         REASON_FABRICATED_INSTRUCTION),
        (stop("レビューを回しています。\n**残っている指示**: 完了条件2を満たすまでの残り98行"
              "\n\n[停止: 待機]", tasks=[waiting, task]), REASON_FABRICATED_INSTRUCTION),
        (stop("回答です。\n答えた質問: どこまで進んだ\n残りの指示: 計画書の未了2件\n\n[停止: 応答]"),
         REASON_FABRICATED_INSTRUCTION),
        (stop("諮ります。\n\n要判断の区分: 指示不明\n区分外に当たらないことを確かめた\n"
              "- 未了の指示: 仕様書の追記\n\n[停止: 要判断]"), REASON_FABRICATED_INSTRUCTION),
        (stop("終わりました。\n指示:全ステップの自律進行\n\n[停止: 完了]"),
         REASON_FABRICATED_INSTRUCTION),
    ]
    pass_cases = [
        (stop("コミットしました。ハッシュは 90d8326 です。\n\n[停止: 完了]"), "[停止: 完了]"),
        (stop("受入条件が変わります。\n\n要判断の区分: 要求仕様\n区分外に当たらないことを確かめた"
              f"\n{DECISION_SPEC_FIELD}: {here} の受入条件を緩める\n\n[停止: 要判断]"),
         "[停止: 要判断]"),
        (stop("受入条件が変わります。\n\n要判断の区分: 要求仕様\n区分外に当たらないことを確かめた"
              f"\n{DECISION_SPEC_FIELD}: {here}の受入条件を緩める\n\n[停止: 要判断]"),
         "[停止: 要判断]"),
        (stop("禁止の解除が要ります。\n\n要判断の区分: 停止規定\n区分外に当たらないことを確かめた"
              "\n\n[停止: 要判断]"), "[停止: 要判断]"),
        (stop("受入条件が変わります。\n\n要判断の区分: 要求仕様(受入条件が変わる)\n"
              "区分外に当たらないことを確かめた\n"
              f"**{DECISION_SPEC_FIELD}**: `{here}:61` の区分の定義を差し替える\n\n[停止: 要判断]"),
         "[停止: 要判断]"),
        (stop("対象のファイルが分かりません。\n\n**要判断の区分**: 指示不明\n"
              "**区分外に当たらないことを確かめた**\n\n[停止: 要判断]"), "[停止: 要判断]"),
        (stop("千日手で終わりました。\n\n- 要判断の区分:停止規定\n- 区分外に当たらないことを確かめた"
              "\n\n[停止: 要判断]"), "[停止: 要判断]"),
        (stop("千日手で終わりました。\n\n要判断の区分: 停止規定\n"
              "区分外に当たらないことを確かめた(止まる根拠はレビュアーの発言ではなく規定であり、"
              "実装の設計・段取り・作業量のいずれでもない)\n\n[停止: 要判断]"), "[停止: 要判断]"),
        (stop("諮ります。\n\n要判断の区分: 指示不明\n"
              "**区分外に当たらないことを確かめた(対象がユーザーにしか無い)**\n\n[停止: 要判断]"),
         "[停止: 要判断]"),
        (stop("プッシュしてよいか確かめます。\n\n要判断の区分:操作承認\n"
              "区分外に当たらないことを確かめた\n\n[停止: 要判断]"), "[停止: 要判断]"),
        (stop("作業は終わりました。\n\n[停止：完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止:完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n［停止：完了］"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止：　完了]"), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止:\t完了]"), "[停止: 完了]"),
        ({"hook_event_name": "Stop", "last_assistant_message": None}, None),
        ({"hook_event_name": "Stop", "stop_hook_active": False}, None),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[waiting, task]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting, unknown]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[dict(waiting, status="mystery")]),
         "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting_bare]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting_far]), "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting_long]), "[停止: 待機]"),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[waiting_cap, task]),
         "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]",
              tasks=[dict(waiting_far, status="mystery"), waiting_seconds, task]),
         "[停止: 待機]"),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]",
              tasks=[waiting_long, dict(task, id="b12", status="mystery")]),
         "[停止: 待機]"),
        (stop("上限明けを待ちます。\n\n[停止: 待機]", tasks=[waiting, task]), "[停止: 待機]"),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[waiting_ended]), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止: 完了]", tasks=[unknown]), "[停止: 完了]"),
        (stop("作業は終わりました。\n\n[停止: 完了]", active=True), "[停止: 完了]"),
        (stop("ご質問への回答です。\n答えた質問: どこまで進んだ\n\n[停止: 応答]"), "[停止: 応答]"),
        (stop("回答です。\n答えた質問: None を渡すとどうなる\n\n[停止: 応答]"), "[停止: 応答]"),
        (stop("回答です。\n答えた質問: - プッシュはまだか\n\n[停止: 応答]"), "[停止: 応答]"),
        (stop("回答しました。\n**答えた質問**: プッシュはまだか\n\n[停止: 応答]", tasks=[waiting_ended]), "[停止: 応答]"),
        (stop("レビューの完了を待ちます。\n\n[停止: 待機]", tasks=[waiting, task]),
         "[停止: 待機]", [codex_alive]),
        (stop("作業は終わりました。\n\n[停止: 完了]"), "[停止: 完了]", [codex_unknown]),
        (stop("指示された作業は全部終わりました。\n\n[停止: 完了]"), "[停止: 完了]"),
        (stop("ご指示のとおり、まとめて1コミットにしました。\n\n[停止: 完了]"), "[停止: 完了]"),
        (stop("回答です。\n答えた質問: 残っている指示をやれ\n\n[停止: 応答]"), "[停止: 応答]"),
        (stop("対象が分かりません。\n\n要判断の区分: 指示不明\n区分外に当たらないことを確かめた"
              "\n\n[停止: 要判断]"), "[停止: 要判断]"),
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
    if not _deadline_text_ok():
        ok = False
    total = len(block_cases) + len(pass_cases) + 4
    print("ALL PASS" if ok else "SOME FAILED", f"({total} cases)")
    sys.exit(0 if ok else 1)


def _deadline_text_ok():
    """締切の引数を起動の形ごとに取り出せるところを確かめる。ここを取り違えると、
    長い締切を短い値と読んで通す(または短い締切を弾く)。"""
    cases = [
        ('python3 "/p/flow/scripts/wait.py" "2026-08-30 21:00"', "2026-08-30 21:00"),
        ("python3 /p/flow/scripts/wait.py 300", "300"),
        ("python3 wait.py 1320", "1320"),
        (r'python3 "C:\Users\d\scripts\wait.py" "2026-09-14 10:41:00"', "2026-09-14 10:41:00"),
        ("python3 /p/flow/scripts/wait.py", ""),
        ("python3 -m pytest tests/test_wait.py", ""),
    ]
    ok = True
    for command, expected in cases:
        actual = deadline_text({"command": command})
        if actual != expected:
            ok = False
            print(f"FAIL deadline_text({command!r}) -> {actual!r}, expected {expected!r}")
    if deadline_text({"description": "wait.py 60 で待つ"}) != "":
        ok = False
        print("FAIL deadline_text: description に書かれた秒数を引数として採っている")
    module = wait_module()
    if module is None:
        ok = False
        print("FAIL deadline_text: wait.py を取り込めない(締切の長さを見ないまま通す)")
    else:
        now = datetime.datetime.now()
        left = deadline_left({"command": "python3 wait.py 1320"}, now, module)
        if left != 1320:
            ok = False
            print(f"FAIL deadline_left: 秒数の締切が {left!r}")
    return ok


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

    pending = row("user", {"type": "text", "text": "台帳の重複を整理しろ"})
    asked = row("user", {"type": "text", "text": "不要なエントリは消せ。どれが不要か分かるか"})
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
    message = ("回答しました。\n答えた質問: どれが不要か分かるか\n\n" + RESPOND)
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "transcript.jsonl")
        for rows, expected, label in (
            ([pending, asked, acted], REASON_RESPOND_AFTER_WORK, "委譲した後の応答は弾く"),
            ([pending, asked, edited], REASON_RESPOND_AFTER_WORK, "編集した後の応答は弾く"),
            ([pending, asked, committed], REASON_RESPOND_AFTER_WORK, "コミットした後の応答は弾く"),
            ([pending, asked, scripted], REASON_RESPOND_AFTER_WORK, "スクリプトを流した後の応答は弾く"),
            ([pending, asked, sent], REASON_RESPOND_AFTER_WORK, "委譲を継いだ後の応答は弾く"),
            ([pending, asked, wrapped], REASON_RESPOND_AFTER_WORK, "前置語ごしのプッシュも弾く"),
            ([pending, asked, stamped], REASON_RESPOND_AFTER_WORK, "同梱の書き込みスクリプトも弾く"),
            ([pending, asked, staged], REASON_RESPOND_AFTER_WORK, "後続の節の空振り指定で打ち消されない"),
            ([pending, asked, said], RESPOND, "答えただけの応答は通す"),
            ([pending, asked, looked, listed, read, said], RESPOND, "調べてから答えた応答は通す"),
            ([pending, asked, discarded, unparsed, ranged, said], RESPOND,
             "捨て場へのリダイレクト・読めないコマンド・別の節の -i は通す"),
            ([asked, row("assistant", {"type": "text", "text": message})], RESPOND,
             "問いだけを受けて答えた応答は通す"),
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

        path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                  for r in (pending, asked)) + "\n", encoding="utf-8")
        for quoted, expected, label in (
            ("どれが不要か分かるか", RESPOND, "発言にある問いの復唱は通す"),
            ("**「どれが不要か分かるか」**", RESPOND, "囲みを付けた復唱も通す"),
            ("どれが 不要か 分かるか", RESPOND, "空白の入った復唱も通す"),
            ("いつ終わるのか", REASON_RESPOND_UNQUOTED, "発言に無い問いは弾く"),
            ("不要なものを消してよいか確認したいそうですね", REASON_RESPOND_UNQUOTED,
             "言い換えた復唱は弾く"),
            ("エントリの整理について", REASON_RESPOND_UNQUOTED, "自分で立てた論点は弾く"),
        ):
            marker, reason = decide({
                "hook_event_name": "Stop", "background_tasks": [], "session_id": "S1",
                "last_assistant_message": f"回答しました。\n{RESPOND_FIELD}: {quoted}\n\n{RESPOND}",
                "transcript_path": path.as_posix(),
            })
            actual = reason if reason else marker
            if actual != expected:
                ok = False
                print(f"FAIL {label}: {actual!r}")

        missing = Path(tmp, "no.jsonl").as_posix()
        marker, reason = decide({
            "hook_event_name": "Stop", "background_tasks": [], "session_id": "S1",
            "last_assistant_message": f"回答しました。\n{RESPOND_FIELD}: 何か\n\n{RESPOND}",
            "transcript_path": missing,
        })
        if (reason if reason else marker) != RESPOND:
            ok = False
            print(f"FAIL 転写が読めなければ照合しない: {reason!r}")
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
