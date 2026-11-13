#!/usr/bin/env python3
"""Stop: 台帳・コード・テストが食い違ったまま、または後退を残したまま、作業を終えさせない。

速くて結果が毎回同じ検査だけを行う。テストの実行などの重いゲートは
完了時（change close）・コミット前・CI が受け持つ。

1. tracekit check（台帳・コード・テストの整合）
2. 基準のブランチ（既定は main）から先の変更が、変更記録の範囲に入っているか。
   コミットした変更も、していない変更も、新しいファイルも、消したファイルも見る。
   pre_edit hook は Edit / Write しか見ないので、シェルのコマンドやスクリプトで書き換えた変更は、ここで見つける。
3. 基準のブランチと比べて、承認されていない後退（要件・受け入れ条件・テストの喪失など）がないか。

同じ指摘で止め続けないよう、止めるのは1回だけ（stop_hook_active のときは通す）。
この hook が内部エラーで終わったときも、1回は止める（守りが黙って外れないようにする）。
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
from project_root import project_root, use_tools_of  # noqa: E402
MAX_SHOWN = 15


def read_input() -> dict:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main(data: dict) -> int:
    root = project_root(data.get("cwd"))  # worktree で作業していれば、そちら
    use_tools_of(root)
    import tracekit

    tracekit.setup_output("utf-8")
    if os.environ.get("TRACEKIT_PROBE"):  # doctor からの呼び出し。この hook が実際に呼ばれることを示す
        print(tracekit.PROBE_MARK, file=sys.stderr)
        return 0
    if data.get("stop_hook_active"):
        return 0

    try:
        project = tracekit.Project(root)
        findings = tracekit.check(project, matrix=False)
    except tracekit.ConfigError as exc:
        print(f"台帳を読めません。直してから終了してください: {exc}", file=sys.stderr)
        return 2
    errors = [f for f in findings if f.level == "error"]
    outside, lost, notes, approvals = [], [], [], []
    rev = tracekit.base_revision(project)  # 基準のブランチがなければ None（直前のコミットとだけ比べる）
    try:
        changed = tracekit.git_changed(root, base=rev)
        with tracekit.snapshot(root, rev) as base:
            # 要件の変更は、変更記録を作る前の工程で行うことがあるので、ここでは見ない（コミット前に見る）
            outside = tracekit.guard(project, changed, diff_files=changed, check_requirements=False,
                                     base_rev=rev or "HEAD", base=base)
            report = tracekit.regress(project, base)
        lost = [item for item in report["items"] if item["blocks"]]
        if report["broken"]:
            lost.append({"id": "比較の基準", "what": report["reason"]})
        notes, approvals = report["notes"], report["approvals"]
    except tracekit.ConfigError:
        pass  # git の管理下にない、最初のコミットがまだない、など。コミット前の検査に任せる
    if not errors and not outside and not lost:
        # 止めないが、エージェントが報告に含められるように知らせる（利用者は、これを読んで取り込むかを決める）
        if notes:
            print("確かめてほしい点（python3 tools/tracekit.py regress）:\n  " + "\n  ".join(notes[:MAX_SHOWN]))
        if approvals:
            print("この作業で承認として記録されているもの（利用者が本当に承認したものか、報告で示すこと）:\n  "
                  + "\n  ".join(f"{a['change']}（承認: {a['approved_by']}）: "
                                 + ", ".join(a["accepts"] + a["quality_base"]) for a in approvals))
        return 0
    if errors:
        print(f"トレーサビリティ検査でエラーが {len(errors)} 件あります。直すか、直せない理由と残るリスクを"
              "利用者に報告してから終了してください:", file=sys.stderr)
        for finding in errors[:MAX_SHOWN]:
            print("  " + finding.render(), file=sys.stderr)
        if len(errors) > MAX_SHOWN:
            print(f"  …ほか {len(errors) - MAX_SHOWN} 件（python3 tools/tracekit.py check）", file=sys.stderr)
    if outside:
        print(f"変更記録の範囲に入っていない変更（本番コード・既存のテスト・品質基盤）が {len(outside)} 件あります。"
              "自分の変更なら、本番コードとテストは影響分析をして変更記録に含め、品質基盤は利用者の承認を得て"
              "変更記録に記録してください。そうでなければ利用者に報告してから終了してください:",
              file=sys.stderr)
        for rel, message in outside[:MAX_SHOWN]:
            print(f"  {rel}: {message}", file=sys.stderr)
        if len(outside) > MAX_SHOWN:
            print(f"  …ほか {len(outside) - MAX_SHOWN} 件（python3 tools/tracekit.py guard --base "
                  f"{project.cfg['base_branch']}）", file=sys.stderr)
    if lost:
        print(f"{project.cfg['base_branch']} と比べて、承認されていない後退が {len(lost)} 件あります。"
              "意図していないなら元に戻してください。意図した変更なら、何をなぜ失うのかを利用者に説明して承認を得てから、"
              "変更記録の accepts にその ID を、approved_by に承認した人の名前を書いてください:", file=sys.stderr)
        for item in lost[:MAX_SHOWN]:
            print(f"  {item['id']}: {item['what']}", file=sys.stderr)
        if len(lost) > MAX_SHOWN:
            print(f"  …ほか {len(lost) - MAX_SHOWN} 件（python3 tools/tracekit.py regress）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    payload = read_input()
    try:
        sys.exit(main(payload))
    except Exception as exc:
        if payload.get("stop_hook_active") or os.environ.get("TRACEKIT_PROBE"):
            sys.exit(0)  # 止め続けて終われなくなることはない
        print(f"on_stop hook の内部エラーのため、終了前の検査ができませんでした: {exc!r}。"
              "`python3 tools/tracekit.py check` と `python3 tools/tracekit.py regress` を自分で実行し、"
              "結果を利用者に報告してから終了してください", file=sys.stderr)
        sys.exit(2)
