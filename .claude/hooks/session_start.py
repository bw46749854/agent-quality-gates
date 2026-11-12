#!/usr/bin/env python3
"""SessionStart: 進行中の変更と検査の状態を、セッションの冒頭でエージェントに伝える。

標準出力の内容が、そのままエージェントの文脈に入る。
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "tools"))


def main() -> int:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HERE.parents[2]).resolve()

    import tracekit

    tracekit.setup_output("utf-8")
    if os.environ.get("TRACEKIT_PROBE"):  # doctor からの呼び出し。この hook が実際に呼ばれることを示す
        print(tracekit.PROBE_MARK)
        return 0
    try:
        print(tracekit.render_status(tracekit.Project(root)), end="")
    except tracekit.ConfigError as exc:
        print(f"## 品質ゲートの現在地\n- 台帳を読めません。最初に直してください: {exc}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"session_start hook の内部エラー: {exc!r}", file=sys.stderr)
        sys.exit(0)
