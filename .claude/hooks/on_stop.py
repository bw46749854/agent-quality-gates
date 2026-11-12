#!/usr/bin/env python3
"""Stop: 台帳・コード・テストが食い違ったまま作業を終えさせない。

速くて結果が毎回同じ検査だけを行う。テストの実行などの重いゲートは
完了時（change close）・コミット前・CI が受け持つ。

1. tracekit check（台帳・コード・テストの整合）
2. まだコミットしていない本番コード・品質基盤の変更が、変更記録の範囲に入っているか。
   pre_edit hook は Edit / Write しか見ないので、シェルのコマンドで書き換えた変更はここで見つける。

同じ指摘で止め続けないよう、止めるのは1回だけ（stop_hook_active のときは通す）。
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "tools"))
MAX_SHOWN = 15


def main() -> int:
    import tracekit

    tracekit.setup_output("utf-8")
    if os.environ.get("TRACEKIT_PROBE"):  # doctor からの呼び出し。この hook が実際に呼ばれることを示す
        print(tracekit.PROBE_MARK, file=sys.stderr)
        return 0
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except ValueError:
        data = {}
    if data.get("stop_hook_active"):
        return 0
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HERE.parents[2]).resolve()

    try:
        project = tracekit.Project(root)
        findings = tracekit.check(project, matrix=False)
    except tracekit.ConfigError as exc:
        print(f"台帳を読めません。直してから終了してください: {exc}", file=sys.stderr)
        return 2
    errors = [f for f in findings if f.level == "error"]
    try:
        uncommitted = tracekit.git_changed(root, base="HEAD")
        # 要件の変更は、変更記録を作る前の工程で行うことがあるので、ここでは見ない（コミット前に見る）
        outside = tracekit.guard(project, uncommitted, diff_files=uncommitted, check_requirements=False)
    except tracekit.ConfigError:
        outside = []  # git の管理下にない、最初のコミットがまだない、など。コミット前の検査に任せる
    if not errors and not outside:
        return 0
    if errors:
        print(f"トレーサビリティ検査でエラーが {len(errors)} 件あります。直すか、直せない理由と残るリスクを"
              "利用者に報告してから終了してください:", file=sys.stderr)
        for finding in errors[:MAX_SHOWN]:
            print("  " + finding.render(), file=sys.stderr)
        if len(errors) > MAX_SHOWN:
            print(f"  …ほか {len(errors) - MAX_SHOWN} 件（python3 tools/tracekit.py check）", file=sys.stderr)
    if outside:
        print(f"変更記録の範囲に入っていない変更（本番コード、または品質基盤）が {len(outside)} 件、"
              "コミットされずに残っています。自分の変更なら、本番コードは影響分析をして変更記録に含め、"
              "品質基盤は利用者の承認を得て変更記録に記録してください。そうでなければ利用者に報告してから終了してください:",
              file=sys.stderr)
        for rel, message in outside[:MAX_SHOWN]:
            print(f"  {rel}: {message}", file=sys.stderr)
        if len(outside) > MAX_SHOWN:
            print(f"  …ほか {len(outside) - MAX_SHOWN} 件（python3 tools/tracekit.py guard --base HEAD）",
                  file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"on_stop hook の内部エラー（終了は止めていません）: {exc!r}", file=sys.stderr)
        sys.exit(1)
