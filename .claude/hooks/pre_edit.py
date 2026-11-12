#!/usr/bin/env python3
"""PreToolUse (Edit / Write / NotebookEdit): 影響分析のない本番コードの編集を止める。

本番コード（trace/config.toml の source_roots 配下）を編集できるのは、
そのコンポーネントを範囲に含む「進行中の変更記録」があるときだけ。
テスト・ドキュメント・台帳・プロジェクト外のファイルは止めない。

終了コード 2 で編集をブロックし、標準エラーの内容がエージェントに渡る。
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "tools"))


def main() -> int:
    import tracekit

    tracekit.setup_output("utf-8")
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except ValueError:
        return 0
    tool_input = data.get("tool_input") or {}
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
        if rel.startswith(("trace/", "docs/", ".claude/", "tools/")):
            return 0  # 壊れた台帳を直す編集は通す
        print(f"台帳を読めないため、編集を止めました。先に台帳を直してください: {exc}", file=sys.stderr)
        return 2
    problems = tracekit.guard(project, [rel])
    if not problems:
        return 0
    for file, message in problems:
        print(f"編集を止めました（{file}）: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # hook 自体の不具合で作業を止めない。ただし黙らない
        print(f"pre_edit hook の内部エラー（編集は止めていません）: {exc!r}", file=sys.stderr)
        sys.exit(1)
