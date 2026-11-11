#!/usr/bin/env python3
"""Bash と PowerShell の PreToolUse フック。Claude Code 専用で、deny ではなく ask を返す。

使い方: Bash・PowerShell の PreToolUse フックとして登録する。--selftest で自己テスト。
"""
import json
import re
import sys

from hook_input import read_command
from shell_words import command_positions, head_name, segments, strip_heredocs

READ_ONLY_GROUPS = {
    "attestation", "browse", "co", "completion", "help", "licenses", "preview", "ruleset", "search",
    "status", "version",
}
READ_ONLY_SUBCOMMANDS = {
    "agent-task": {"list", "view"},
    "alias": {"list"},
    "auth": {"status", "token"},
    "cache": {"list"},
    "codespace": {"code", "jupyter", "list", "logs", "view"},
    "config": {"clear-cache", "get", "list"},
    "discussion": {"list", "view"},
    "extension": {"browse", "list", "search"},
    "gist": {"clone", "list", "view"},
    "gpg-key": {"list"},
    "issue": {"list", "status", "view"},
    "label": {"list"},
    "org": {"list"},
    "pr": {"checkout", "checks", "co", "diff", "list", "status", "view"},
    "project": {"field-list", "item-list", "list", "view"},
    "release": {"download", "list", "verify", "verify-asset", "view"},
    "repo": {"clone", "gitignore", "license", "list", "read-dir", "read-file", "view"},
    "run": {"download", "list", "view", "watch"},
    "secret": {"list"},
    "skill": {"list", "preview", "search", "show"},
    "ssh-key": {"list"},
    "variable": {"get", "list"},
    "workflow": {"list", "view"},
}
WRITE_NESTED = {
    ("codespace", "ports"): {"visibility"},
    ("repo", "autolink"): {"create", "delete"},
    ("repo", "deploy-key"): {"add", "delete"},
}
GROUP_ALIASES = {
    "agent": "agent-task", "agent-tasks": "agent-task", "agents": "agent-task", "at": "attestation",
    "cs": "codespace", "ext": "extension", "extensions": "extension", "rs": "ruleset", "skills": "skill",
}
# これらのグループの -h は --hostname・--homepage・--host の短形式で、ヘルプではない。
VALUED_H_GROUPS = {"auth", "config", "repo"}
REPO_FLAGS = {"-R", "--repo"}
SHELLS = {
    "bash", "cmd", "dash", "eval", "iex", "invoke-expression", "powershell", "pwsh", "sh", "zsh",
}
SUBSTITUTION = re.compile(r"(?<!\\)\$\(((?:[^()]|\([^()]*\))*)\)|(?<!\\)`([^`]*)`")
UNQUOTED_HEREDOC = re.compile(r"<<-?\s*[A-Za-z_]")
QUOTED_SPAN = re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'')
API_FLAG = re.compile(
    r"(?:-i*(?P<short>[XfF])|--(?P<long>method|field|raw-field|input)(?=$|=))=?(?P<value>.*)", re.S)
GRAPHQL_NOISE = re.compile(r'"""[\s\S]*?"""|"(?:[^"\\]|\\.)*"|#[^\n]*|;')
READ_QUERY = re.compile(r"\s*(?:query\b|fragment\b|\{)")
MUTATION_OPERATION = re.compile(r"(?:^|\})\s*mutation\b")
GH_WORD = re.compile(r"(?<![\w-])gh(?:\.exe)?(?![\w-])", re.I)


def positional_words(args):
    words, skip_next = [], False
    for arg in args:
        if skip_next:
            skip_next = False
        elif arg in REPO_FLAGS:
            skip_next = True
        elif not arg.startswith("-"):
            words.append(arg)
    return words


def graphql_query_reads(query):
    operations = GRAPHQL_NOISE.sub("", query)
    return READ_QUERY.match(operations) is not None and MUTATION_OPERATION.search(operations) is None


def asks_for_help(args, group):
    for index, arg in enumerate(args):
        if arg == "--help" or (arg == "-h" and group not in VALUED_H_GROUPS):
            if index == 0 or not args[index - 1].startswith("-"):
                return True
    return False


def api_request_writes(args):
    method, fields, has_body = None, [], False
    for index, arg in enumerate(args):
        match = API_FLAG.fullmatch(arg)
        if match is None:
            continue
        name = match["short"] or match["long"]
        value = match["value"] or "".join(args[index + 1:index + 2])
        if name in ("X", "method"):
            method = value
            continue
        has_body = True
        if name != "input":
            fields.append(value)
    if "graphql" in args:
        queries = [field.split("=", 1)[1] for field in fields if field.startswith("query=")]
        return not queries or not all(graphql_query_reads(query) for query in queries)
    if method is not None:
        return method.upper() not in ("GET", "HEAD")
    return has_body


def gh_write_label(args):
    """gh の引数列が書き込みなら表示用のコマンド名を返す。読み取りなら None。"""
    words = positional_words(args)
    group = GROUP_ALIASES.get(words[0], words[0]) if words else None
    if group is None or asks_for_help(args, group):
        return None
    if group == "api":
        return "gh api" if api_request_writes(args) else None
    if group in READ_ONLY_GROUPS:
        return None
    if len(words) == 1:
        return None if group in READ_ONLY_SUBCOMMANDS else f"gh {group}"
    subcommand = "list" if words[1] == "ls" else words[1]
    label = f"gh {group} {subcommand}"
    nested = WRITE_NESTED.get((group, subcommand))
    if nested is not None:
        written = next((word for word in words[2:] if word in nested), None)
        return f"{label} {written}" if written else None
    return None if subcommand in READ_ONLY_SUBCOMMANDS.get(group, ()) else label


def substitutable_text(command):
    if UNQUOTED_HEREDOC.search(command):
        return command
    return QUOTED_SPAN.sub(lambda span: span[0] if span[0][0] == '"' else "", strip_heredocs(command))


def decide(command):
    """コマンド列に gh の書き込みがあれば表示用のコマンド名を返す。無ければ None。"""
    if not isinstance(command, str) or not command.strip():
        return None
    for match in SUBSTITUTION.finditer(substitutable_text(command)):
        label = decide(match[1] or match[2] or "")
        if label:
            return label
    parsed = segments(command)
    if parsed is None:
        return "gh" if GH_WORD.search(command) else None
    for tokens in parsed:
        for index in command_positions(tokens):
            name, args = head_name(tokens[index]), tokens[index + 1:]
            if name == "gh":
                label = gh_write_label(args)
                if label:
                    return label
            elif name in SHELLS:
                for arg in args:
                    label = decide(arg) if " " in arg else None
                    if label:
                        return label
    return None


def main():
    # ハーネスが渡す JSON は UTF-8。既定の符号化で読むと非ASCII が化けて素通りする。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tool, command = read_command()
    if tool is None:
        sys.exit(0)
    label = decide(command)
    if label is None:
        sys.exit(0)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": f"{label} は gh の書き込み操作なので、実行前にユーザーの確認を取る。",
    }}, ensure_ascii=False))


def selftest():
    cases = [
        ("リポジトリ指定を挟んだ形", "gh pr --repo o/r merge 1", "gh pr merge"),
        ("短いリポジトリ指定を挟んだ形", "gh pr -R o/r close 1", "gh pr close"),
        ("入れ子の書き込み", "gh repo deploy-key add k.pub", "gh repo deploy-key add"),
        ("入れ子の自動リンク作成", "gh repo autolink create X", "gh repo autolink create"),
        ("入れ子の自動リンク削除", "gh repo autolink delete 1", "gh repo autolink delete"),
        ("入れ子の鍵の削除", "gh repo deploy-key delete 1", "gh repo deploy-key delete"),
        ("ポートの公開範囲変更", "gh codespace ports visibility 80:public", "gh codespace ports visibility"),
        ("未知のサブコマンド", "gh pr frobnicate", "gh pr frobnicate"),
        ("拡張コマンド", "gh dash", "gh dash"),
        ("exe 付きの名前", "gh.exe pr merge 1", "gh pr merge"),
        ("パス修飾された名前", "/usr/bin/gh pr merge 1", "gh pr merge"),
        ("空白を含むパス", '"C:/Program Files/GitHub CLI/gh.exe" pr merge 1', "gh pr merge"),
        ("複合コマンドの後段", "cd x && gh pr merge 1", "gh pr merge"),
        ("パイプの後段", "gh pr list | gh pr merge 1", "gh pr merge"),
        ("コマンド置換の内側", "echo `gh pr merge 1`", "gh pr merge"),
        ("呼び出し演算子", "& gh pr merge 1", "gh pr merge"),
        ("環境変数の代入", "GH_TOKEN=x gh pr merge 1", "gh pr merge"),
        ("起動子の後ろ", "sudo gh pr merge 1", "gh pr merge"),
        ("シェルへ渡した文字列", 'bash -c "gh pr merge 1"', "gh pr merge"),
        ("PowerShell へ渡した文字列", "pwsh -Command 'gh pr merge 1'", "gh pr merge"),
        ("sh へ渡した文字列", 'sh -c "gh pr merge 1"', "gh pr merge"),
        ("dash へ渡した文字列", 'dash -c "gh pr merge 1"', "gh pr merge"),
        ("zsh へ渡した文字列", 'zsh -c "gh pr merge 1"', "gh pr merge"),
        ("cmd へ渡した文字列", 'cmd /c "gh pr merge 1"', "gh pr merge"),
        ("Windows PowerShell へ渡した文字列", 'powershell -Command "gh pr merge 1"', "gh pr merge"),
        ("iex へ渡した文字列", 'iex "gh pr merge 1"', "gh pr merge"),
        ("解釈できない形", "gh pr merge 'x", "gh"),
        ("API の POST", "gh api -X POST repos/o/r/issues", "gh api"),
        ("API の連結した POST", "gh api -XPOST repos/o/r/issues", "gh api"),
        ("API の長形式の DELETE", "gh api --method=DELETE repos/o/r", "gh api"),
        ("API の長形式の PATCH", "gh api repos/o/r --method PATCH", "gh api"),
        ("-i を束ねた POST", "gh api -iXPOST repos/o/r", "gh api"),
        ("API の方式が変数", "gh api -X $M repos/o/r", "gh api"),
        ("API の暗黙の POST", "gh api repos/o/r/issues -f title=x", "gh api"),
        ("API の型付きフィールド", "gh api repos/o/r/issues -F n=1", "gh api"),
        ("API の連結フィールド", "gh api repos/o/r/issues -ftitle=x", "gh api"),
        ("API の長形式フィールド", "gh api repos/o/r/issues --raw-field=title=x", "gh api"),
        ("API の本文ファイル", "gh api repos/o/r/issues --input body.json", "gh api"),
        ("GraphQL の mutation", "gh api graphql -f query='mutation{a}'", "gh api"),
        ("query と併記した mutation", "gh api graphql -f query='query A{a} mutation B{b}'", "gh api"),
        ("GraphQL の本文ファイル", "gh api graphql --input q.json", "gh api"),
        ("GraphQL のクエリが変数", 'gh api graphql -f query="$q"', "gh api"),
        ("二重引用内のコマンド置換", 'echo "$(gh pr merge 1 --squash)"', "gh pr merge"),
        ("二重引用内の逆引用", 'echo "`gh pr merge 1`"', "gh pr merge"),
        ("入れ子の置換", 'echo "$(echo $(date); gh pr merge 1)"', "gh pr merge"),
        ("eval へ渡した文字列", 'eval "gh pr merge 1"', "gh pr merge"),
        ("Invoke-Expression へ渡した文字列", "Invoke-Expression 'gh pr merge 1'", "gh pr merge"),
        ("本文の値が --help", "gh issue comment 1 --body '--help'", "gh issue comment"),
        ("引用なし here-doc の置換", "cat <<EOF\n$(gh pr merge 1 --squash)\nEOF", "gh pr merge"),
        ("here-doc 本文の単一引用内の置換", "cat <<EOF\n'$(gh pr merge 1)'\nEOF", "gh pr merge"),
        ("引用ありと引用なしの here-doc の混在",
         "cat <<'A'\nx\nA\ncat <<B\n$(gh pr merge 1)\nB", "gh pr merge"),
        ("二重引用内の単一引用符","""echo "it's $(gh pr merge 1) isn't" """, "gh pr merge"),
        ("単一引用内の置換の言及", "gh search code '$(gh pr merge)'", None),
        ("エスケープした置換の言及", 'gh search code "\\$(gh pr merge)"', None),
        ("引用 here-doc の置換の言及", "cat <<'EOF'\n$(gh pr merge 1)\nEOF", None),
        ("置換の読み取り",'x="$(gh pr view 1 --json title)"', None),
        ("GraphQL の文字列内の語", """gh api graphql -f 'query=query { search(query:"} mutation x") { n } }'""", None),
        ("GraphQL のコメント内の語", "gh api graphql -f 'query=# mutation\nquery { a }'", None),
        ("複数行の GraphQL 読み取り", "gh api graphql -f query='\n  query {\n    a\n  }\n'", None),
        ("複数行の GraphQL の mutation", "gh api graphql -f query='\n  query A { a }\n  mutation B {\n    b\n  }\n'", "gh api"),
        ("GraphQL のフラグメント先頭","gh api graphql -f 'query=fragment F on X { a } query { ...F }'", None),
        ("文字列に隠れない mutation", """gh api graphql -f 'query=query A { a(q:"x") } mutation B { b(q:"y") }'""", "gh api"),
        ("別名のグループの書き込み", "gh cs delete", "gh codespace delete"),
        ("別名の削除", "gh ext uninstall x", "gh extension uninstall"),
        ("ポート指定の後ろの書き込み", "gh codespace ports -c x visibility 80:public",
         "gh codespace ports visibility"),
        ("auth の -h はホスト名", "gh auth login -h github.com", "gh auth login"),
        ("repo edit の -h はホームページ", "gh repo edit -h https://x", "gh repo edit"),
        ("config set の -h はホスト", "gh config set -h x k v", "gh config set"),
        ("PR の一覧の別名", "gh pr ls", None),
        ("リポジトリ一覧の別名", "gh repo ls", None),
        ("チェックアウトの別名", "gh pr co 1", None),
        ("スキル参照の別名", "gh skill show x", None),
        ("codespace の別名", "gh cs ls", None),
        ("extension の別名", "gh ext ls", None),
        ("extension の複数形", "gh extensions ls", None),
        ("ruleset の別名", "gh rs ls", None),
        ("attestation の別名", "gh at verify x", None),
        ("agent-task の別名", "gh agent list", None),
        ("skill の複数形", "gh skills ls", None),
        ("codespace 指定のポート一覧", "gh codespace ports -c x", None),
        ("短いヘルプ", "gh pr create -h", None),
        ("PR の一覧", "gh pr list", None),
        ("PR の参照", "gh pr view 1 --json title", None),
        ("PR の差分", "gh pr diff 1", None),
        ("PR のチェックアウト", "gh pr checkout 1", None),
        ("issue の参照", "gh issue view 1", None),
        ("実行ログの参照", "gh run view 1 --log-failed", None),
        ("実行の監視", "gh run watch 1", None),
        ("認証状態", "gh auth status", None),
        ("リポジトリの参照", "gh repo view o/r", None),
        ("リポジトリの複製", "gh repo clone o/r", None),
        ("入れ子の読み取り", "gh repo deploy-key list", None),
        ("検索", "gh search prs x", None),
        ("全体の状態", "gh status", None),
        ("バージョン表示", "gh --version", None),
        ("書き込みのヘルプ", "gh pr create --help", None),
        ("グループのヘルプ表示", "gh pr", None),
        ("素の gh", "gh", None),
        ("API の既定の GET", "gh api repos/o/r/pulls", None),
        ("API の明示 GET", "gh api -X GET repos/o/r -f per_page=1", None),
        ("API の HEAD", "gh api --method HEAD repos/o/r", None),
        ("ハイフン入りのパス", "gh api repos/o/foo-fix/pulls --paginate", None),
        ("API のヘッダ指定", "gh api repos/o/r -H 'Accept: x'", None),
        ("GraphQL の読み取り", "gh api graphql -f query='query{viewer{login}}'", None),
        ("GraphQL の省略形の読み取り", "gh api graphql -f query='{viewer{login}}'", None),
        ("引用の内側の言及", "git commit -m 'gh pr merge'", None),
        ("引数としての言及", "echo gh pr merge", None),
        ("here-doc の本文", "cat <<EOF\ngh pr merge 1\nEOF", None),
        ("名前が前方一致する別コマンド", "ghq get x", None),
        ("解釈できない形の gh 以外", "echo 'x", None),
        ("シェルへ渡した読み取り", 'bash -c "gh pr view 1"', None),
        ("API の小文字の読み取り方式", "gh api -X get repos/o/r", None),
        ("API の長形式の等号付き GET", "gh api --method=GET repos/o/r", None),
        ("API の長形式の field", "gh api repos/o/r/issues --field title=x", "gh api"),
        ("改行を含む連結フィールド", "gh api repos/o/r/issues -f'body=a\nb'", "gh api"),
        ("変数を伴う GraphQL の読み取り", "gh api graphql -f query='query{a}' -f owner=x", None),
        ("複数の query の片方が mutation", "gh api graphql -f query='query{a}' -f query='mutation{b}'", "gh api"),
        ("query で始まる別の語", "gh api graphql -f query='queryx{a}'", "gh api"),
        ("GraphQL のブロック文字列内の語",
         "gh api graphql -f 'query=query { a(d:\"\"\"x \" } mutation y\"\"\") { n } }'", None),
        ("解釈できない形の後段の gh", "cd x && gh pr merge 'x", "gh"),
        ("解釈できない形の前方一致する別コマンド", "ghq get 'x", None),
        ("解釈できない形の語尾が gh の語", "echo sigh 'x", None),
        ("解釈できない形の大文字の GH", "GH pr merge 'x", "gh"),
        ("エスケープした逆引用の言及", 'gh search code "\\`gh pr merge\\`"', None),
        ("タブ字下げ here-doc の置換", "cat <<-EOF\n\t$(gh pr merge 1)\n\tEOF", "gh pr merge"),
        ("空白を挟んだ here-doc の置換", "cat << EOF\n$(gh pr merge 1)\nEOF", "gh pr merge"),
        ("エスケープした二重引用符を含む二重引用内の置換",
         'echo "it\\"s \' $(gh pr merge 1) \' "', "gh pr merge"),
        ("空のコマンド", "", None),
    ]
    ok = True
    for why, command, want in cases:
        got = decide(command)
        if got != want:
            ok = False
            print(f"FAIL {why}: want={want} got={got} :: {command!r}")
    print("ALL PASS" if ok else "SOME FAILED", f"({len(cases)} cases)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    main()
