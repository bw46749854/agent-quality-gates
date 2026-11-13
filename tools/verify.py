#!/usr/bin/env python3
"""品質ゲートの一括実行。

トレーサビリティ検査を最初に行い、続けて trace/gates.toml のゲートを順に実行する。
完了時（tracekit.py change close）・コミット前・CI は、すべてこの同じ定義を使う。

結果の意味:
  PASS         実行して、合格した（コマンドが終了コード 0 で終わった）
  FAIL         実行して、失敗した。または、ゲートの外し方が認められない
  SKIP:対象外  確かめる対象がない（skip_kind = "not_applicable"）。理由と承認した人が書かれている
  SKIP:免除    対象はあるが、今回は検証していない（skip_kind = "waiver"）。承認された例外で、期限がある
  UNSET        未設定（未導入）。コマンドも理由も空。黙って飛ばさないために、不合格として扱う
  NOTRUN       今回は実行していない（--only で一部だけを実行した、基準のブランチと比べられなかった）

SKIP は「合格」ではない。全体の合否は止めないが、合格とは分けて表示・記録する。
「対象外」と「免除」も分ける。前者は確かめるものがなく、後者は確かめるべきものを確かめていない。
NOTRUN も合否は止めないが、1件も実行していない実行は合格にしない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracekit  # noqa: E402

TAIL_LINES = 60


load_gates = tracekit.load_gates


def _tail(text: str) -> str:
    lines = text.rstrip().splitlines()
    if len(lines) > TAIL_LINES:
        lines = [f"…（先頭 {len(lines) - TAIL_LINES} 行を省略）"] + lines[-TAIL_LINES:]
    return "\n".join(lines)


def run_gates(root: Path, only: list[str] | None = None, echo=print) -> tuple[list[dict], bool]:
    root = Path(root).resolve()
    results: list[dict] = []

    def record(gate_id, name, status, detail, output="", kind=""):
        result = {"id": gate_id, "name": name, "status": status, "detail": detail}
        if status == "SKIP":  # 「対象外」と「免除」を、同じ SKIP としてまとめない
            result["kind"] = kind
            result["label"] = f"SKIP:{tracekit.SKIP_KINDS.get(kind, kind)}"
        results.append(result)
        echo(f"[{result.get('label', status)}] {gate_id}（{name}）: {detail}")
        if status == "FAIL" and output:
            echo("\n".join("    " + line for line in _tail(output).splitlines()))

    def left_out(gate_id, name):
        record(gate_id, name, "NOTRUN", "今回は実行していません（--only の対象に入っていない）")

    project = None
    if not only or "trace" in only:
        try:
            project = tracekit.Project(root)
            findings = tracekit.check(project)
            errors = sum(1 for f in findings if f.level == "error")
            record("trace", "トレーサビリティ検査", "FAIL" if errors else "PASS",
                   f"エラー {errors} 件、警告 {len(findings) - errors} 件",
                   "\n".join(f.render() for f in findings))
        except tracekit.ConfigError as exc:
            record("trace", "トレーサビリティ検査", "FAIL", "台帳を読めません", str(exc))
    else:
        left_out("trace", "トレーサビリティ検査")

    # 変更する前（基準のブランチとの分かれ目）と比べる。いまの台帳が整合していても、要件やテストを消した結果かもしれない。
    # git の外や、導入の途中（変更する前にコンポーネントがない）では比べられない。不合格にはしないが、
    # 「比べていない」ことを結果に残す（NOTRUN）。合格と同じに見えてはいけない。
    # 導入が済んでいるのに比べる相手（基準のブランチ）がないときは、不合格にする
    if not only or "regress" in only:
        try:
            project = project or tracekit.Project(root)
            base_name = str(project.cfg["base_branch"])
            with tracekit.snapshot(root, tracekit.base_revision(project)) as base:
                report = tracekit.regress(project, base)
            if report["broken"]:
                record("regress", f"後退の検出（{base_name} との比較）", "FAIL", "比べられません",
                       report["reason"])
            elif report["compared"]:
                blocking = sum(1 for item in report["items"] if item["blocks"])
                record("regress", f"後退の検出（{base_name} との比較）", "FAIL" if blocking else "PASS",
                       f"承認されていない後退 {blocking} 件、承認済み {len(report['items']) - blocking} 件、"
                       f"確かめてほしい点 {len(report['notes'])} 件"
                       + ("（`python3 tools/tracekit.py regress` で内容を確認する）"
                          if report["items"] or report["notes"] else ""),
                       tracekit.render_regress(report, base_name))
            else:
                record("regress", f"後退の検出（{base_name} との比較）", "NOTRUN",
                       f"比べていません。{report['reason']}")
        except tracekit.ConfigError:
            pass  # 台帳を読めないことは、上の trace が不合格にしている
    else:
        left_out("regress", "後退の検出")

    try:
        gates = load_gates(root)
    except tracekit.ConfigError as exc:
        record("gates", "ゲートの定義", "FAIL", "trace/gates.toml を読めません", str(exc))
        gates = []

    for gate in gates:
        gate_id, name = str(gate["id"]), str(gate.get("name", gate["id"]))
        if only and gate_id not in only:
            left_out(gate_id, name)
            continue
        state, cmd = tracekit.gate_state(gate)
        if state != "RUN":
            record(gate_id, name, state, cmd, kind=str(gate.get("skip_kind", "")).strip())
        else:
            started = time.monotonic()
            try:
                done = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      timeout=gate.get("timeout", 1800))
            except subprocess.TimeoutExpired:
                record(gate_id, name, "FAIL", f"時間切れ: `{cmd}`")
                continue
            seconds = f"{time.monotonic() - started:.1f} 秒"
            output = done.stdout + done.stderr
            pattern = str(gate.get("count_pattern", "") or "")
            if done.returncode == 0 and pattern:
                # 終了コードが 0 でも、テストが1件も走っていなければ、何も確かめていない
                matches = list(re.finditer(pattern, output, re.M))
                ran = (sum(int(m.group(1)) for m in matches if m.group(1).isdigit())
                       if re.compile(pattern).groups else len(matches))
                if not matches:
                    record(gate_id, name, "FAIL", f"`{cmd}` は終了コード 0 で終わりましたが、出力に count_pattern"
                           f"（`{pattern}`）に合う行がありません。テストが実行されたかを確かめられません", output)
                elif ran == 0:
                    record(gate_id, name, "FAIL", f"`{cmd}` は終了コード 0 で終わりましたが、実行されたテストが "
                           "0 件です。何も確かめていません", output)
                else:
                    record(gate_id, name, "PASS", f"`{cmd}`（{seconds}、{ran} 件）")
            elif done.returncode == 0:
                levels = gate.get("levels") or ([gate_id] if project and gate_id in project.test_levels else [])
                record(gate_id, name, "PASS", f"`{cmd}`（{seconds}）"
                       + ("　※実行した件数は確かめていません（count_pattern が未設定）" if levels else ""))
            else:
                record(gate_id, name, "FAIL", f"`{cmd}` が終了コード {done.returncode} で失敗（{seconds}）",
                       done.stdout + done.stderr)

    # 実行していないもの（NOTRUN）は合否を止めないが、何も確かめていない実行は合格にしない
    ok = (any(r["status"] in ("PASS", "SKIP") for r in results)
          and all(r["status"] in ("PASS", "SKIP", "NOTRUN") for r in results))
    return results, ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="品質ゲートを一括で実行する")
    parser.add_argument("--root", default=None, help="プロジェクトのルート（既定: tools/ の1つ上）")
    parser.add_argument("--only", default="", help="実行するゲートの id（カンマ区切り）。指定すると部分的な検証になる")
    parser.add_argument("--report", default="", help="結果を JSON でこのファイルに書き出す（CI の成果物・集計用）")
    args = parser.parse_args(argv)
    only = [x.strip() for x in args.only.split(",") if x.strip()] or None
    root = Path(args.root or tracekit.DEFAULT_ROOT).resolve()
    results, ok = run_gates(root, only=only)

    def ids(status: str, kind: str = "") -> list[str]:
        return [r["id"] for r in results if r["status"] == status and (not kind or r.get("kind") == kind)]

    if args.report:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({
            "ok": ok,
            "partial": bool(only),  # True: 一部のゲートだけを実行した。全体の合否ではない
            "unverified": [r["id"] for r in results if r["status"] == "SKIP"],  # 承認つきで外したゲート（下の2つの合計）
            "not_applicable": ids("SKIP", "not_applicable"),  # 確かめる対象がない
            "waived": ids("SKIP", "waiver"),                  # 対象はあるが、検証していない（期限つきの免除）
            "not_run": ids("NOTRUN"),                         # 今回は実行していない
            "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "commit": tracekit.git_head(root),
            "gates": results,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    groups = {"PASS": ids("PASS"), "FAIL": ids("FAIL"), "対象外": ids("SKIP", "not_applicable"),
              "免除": ids("SKIP", "waiver"), "UNSET": ids("UNSET"), "未実施": ids("NOTRUN")}
    summary = "、".join(f"{name} {len(found)}" for name, found in groups.items() if found)
    notes = [f"免除の {len(groups['免除'])} 件（{', '.join(groups['免除'])}）は、対象があるのに検証していません"
             if groups["免除"] else "",
             f"対象外の {len(groups['対象外'])} 件（{', '.join(groups['対象外'])}）は、確かめる対象がないとして外しています"
             "（合格ではありません）" if groups["対象外"] else "",
             f"未実施の {len(groups['未実施'])} 件（{', '.join(groups['未実施'])}）は、今回は確かめていません"
             if groups["未実施"] else "",
             "一部のゲートだけを実行しました（全体の合否ではありません）" if only else ""]
    print(f"\n品質ゲート: {'合格' if ok else '不合格'}（{summary}）" + "".join(f"　※{note}" for note in notes if note))
    return 0 if ok else 1


if __name__ == "__main__":
    tracekit.setup_output()
    sys.exit(main())
