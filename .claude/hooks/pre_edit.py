#!/usr/bin/env python3
"""PreToolUse (Edit / Write / NotebookEdit、任意で Bash): 影響分析のない編集を止め、重要な変更の承認を人に確かめる。

止めるもの（終了コード 2。標準エラーの内容がエージェントに渡る）:
- 本番コード（trace/config.toml の source_roots 配下）の編集で、そのコンポーネントを範囲に含む
  「進行中の変更記録」がないもの。
- 既存のテストの編集で、そのテストが確かめている要件を requirements に含む進行中の変更記録がないもの。
- 品質基盤（ゲートの定義、台帳、検査ツール、hook、CI、行動規約。tracekit.QUALITY_BASE）の編集で、
  そのパスを quality_base に含み、承認した人（approved_by）を記録した進行中の変更記録がないもの
  （コンポーネントを宣言した台帳をまだコミットしていない、導入の途中は止めない）。

人に確かめるもの（Claude Code が利用者に確認のダイアログを出す。エージェントは代わりに答えられない）:
- 変更記録に、承認を記録する編集。品質基盤の変更（quality_base）や後退の受け入れ（accepts）に、
  承認した人（approved_by）を書く、または承認済みの記録に新しい対象を足す編集。
  すでに記録されている対象について、もう一度は確かめない。
- 設定で Bash にも掛けている場合は、シェルのコマンドで承認を書き込もうとする操作。

文書・新しいテスト・プロジェクト外のファイルは止めない。

この hook が内部エラーで終わったときは、編集を通さない（守りが黙って外れないようにする）。
hook が時間切れになった場合など、ここで止められなかった変更は、作業を終えるときの hook・コミット前の検査・
品質ゲート（どれも git の差分を見る）が見つける。
"""
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "tools"))

# 読むだけのコマンド。承認の項目名が出てきても、書き込みではないので確かめない
READ_ONLY = re.compile(r"^\s*(?:grep|rg|cat|head|tail|less|wc|git\s+(?:diff|log|show|status|grep|blame))\b[^>|;&]*$")


def ask(reason: str) -> int:
    """利用者に確認のダイアログを出す。確認できない環境（対話のない実行）では、Claude Code がこの操作を拒む。"""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))
    return 0


def approvals(tracekit, text: str) -> tuple[set[str], str, str]:
    """変更記録の文面から、(承認の対象, 承認した人, 変更記録の見出し) を読む。"""
    try:
        meta, _ = tracekit.split_frontmatter(text)
        items = {f"品質基盤 {p}" for p in tracekit._as_list(meta.get("quality_base"), "quality_base")}
        items |= {f"後退の受け入れ {a}" for a in tracekit._as_list(meta.get("accepts"), "accepts")}
    except ValueError:
        return set(), "", ""
    return items, str(meta.get("approved_by", "")).strip(), f"{meta.get('id', '')}「{meta.get('title', '')}」"


def after_edit(tool_name: str, tool_input: dict, before: str) -> str:
    """この編集のあとの文面。"""
    if tool_name == "Write":
        return str(tool_input.get("content", ""))
    old, new = str(tool_input.get("old_string", "")), str(tool_input.get("new_string", ""))
    if not old or old not in before:
        return before  # この編集は当たらない（道具の側が失敗させる）
    return before.replace(old, new) if tool_input.get("replace_all") else before.replace(old, new, 1)


def main() -> int:
    import tracekit

    tracekit.setup_output("utf-8")
    if os.environ.get("TRACEKIT_PROBE"):  # doctor からの呼び出し。この hook が実際に呼ばれ、止められることを示す
        print(tracekit.PROBE_MARK, file=sys.stderr)
        return 2
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    tool_name = str(data.get("tool_name", ""))
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        raise TypeError("tool_input がオブジェクトではありません")

    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if "approved_by" in command and not READ_ONLY.match(command):
            return ask("シェルのコマンドが、承認した人（approved_by / skip_approved_by）に触れています。"
                       "品質基盤の変更・後退の受け入れ・ゲートの免除を、あなたが承認したことにする記録です。"
                       "承認していない内容なら、許可しないでください")
        return 0

    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return 0
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HERE.parents[2]).resolve()
    path = Path(target)
    if not path.is_absolute():
        path = Path(data.get("cwd") or root) / path
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError:
        return 0  # プロジェクトの外

    try:
        project = tracekit.Project(root)
    except tracekit.ConfigError as exc:
        # 壊れた台帳を直す編集と、文書の編集は通す。ゲートの定義や検査ツールは通さない
        # （台帳を壊しておけば品質基盤を自由に書き換えられる、という抜け道にしない）
        if rel in ("trace/config.toml", "trace/components.toml") or rel.startswith("docs/"):
            return 0
        print(f"台帳を読めないため、編集を止めました。先に台帳を直してください: {exc}", file=sys.stderr)
        return 2
    problems = tracekit.guard(project, [rel])
    if problems:
        for file, message in problems:
            print(f"編集を止めました（{file}）: {message}", file=sys.stderr)
        return 2

    changes_dir = str(project.cfg["changes_dir"]).strip("/") + "/"
    if rel.startswith(changes_dir) and rel.endswith(".md") and tool_name in ("Write", "Edit"):
        try:
            before_text = path.read_text(encoding="utf-8")
        except OSError:
            before_text = ""
        before, approver_before, _ = approvals(tracekit, before_text)
        after, approver, label = approvals(tracekit, after_edit(tool_name, tool_input, before_text))
        if approver and after:
            added = after if approver != approver_before else after - before
            if added:
                return ask(f"変更記録 {label} に、あなた（{approver}）が承認したという記録を書きます。"
                           f"対象: {'、'.join(sorted(added))}。"
                           "検査そのものや、以前あった要件・テストの保証を変える重要な変更です。"
                           "説明を受けて承認した内容と合っている場合だけ、許可してください")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # hook 自体の不具合。守りが黙って外れないよう、編集は通さない
        print(f"pre_edit hook の内部エラーのため、この操作を止めました: {exc!r}。"
              "hook が直るまで Edit / Write は止まります。原因は `python3 tools/tracekit.py doctor` と "
              "`python3 tools/tracekit.py check` で確かめてください", file=sys.stderr)
        sys.exit(2)
