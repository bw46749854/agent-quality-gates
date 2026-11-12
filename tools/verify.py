#!/usr/bin/env python3
"""品質ゲートの一括実行。

トレーサビリティ検査を最初に行い、続けて trace/gates.toml のゲートを順に実行する。
完了時（tracekit.py change close）・コミット前・CI は、すべてこの同じ定義を使う。

結果の意味:
  PASS   コマンドが終了コード 0 で終わった
  FAIL   コマンドが失敗した
  SKIP   検証していない。コマンドが空で、理由（skip_reason）と承認した人（skip_approved_by）が
         書かれていて、期限（skip_until。任意）を過ぎていない
  UNSET  コマンドも理由も空。黙って飛ばさないために、不合格として扱う

SKIP は「合格」ではなく「承認つきで検証を外した」状態。全体の合否は止めないが、
検証していないものとして、合格とは分けて表示・記録する。
"""
from __future__ import annotations

import argparse
import datetime
import json
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

    def record(gate_id, name, status, detail, output=""):
        results.append({"id": gate_id, "name": name, "status": status, "detail": detail})
        echo(f"[{status}] {gate_id}（{name}）: {detail}")
        if status == "FAIL" and output:
            echo("\n".join("    " + line for line in _tail(output).splitlines()))

    if not only or "trace" in only:
        try:
            findings = tracekit.check(tracekit.Project(root))
            errors = sum(1 for f in findings if f.level == "error")
            record("trace", "トレーサビリティ検査", "FAIL" if errors else "PASS",
                   f"エラー {errors} 件、警告 {len(findings) - errors} 件",
                   "\n".join(f.render() for f in findings))
        except tracekit.ConfigError as exc:
            record("trace", "トレーサビリティ検査", "FAIL", "台帳を読めません", str(exc))

    try:
        gates = load_gates(root)
    except tracekit.ConfigError as exc:
        record("gates", "ゲートの定義", "FAIL", "trace/gates.toml を読めません", str(exc))
        gates = []

    for gate in gates:
        gate_id, name = str(gate["id"]), str(gate.get("name", gate["id"]))
        if only and gate_id not in only:
            continue
        state, cmd = tracekit.gate_state(gate)
        if state != "RUN":
            record(gate_id, name, state, cmd)
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
            if done.returncode == 0:
                record(gate_id, name, "PASS", f"`{cmd}`（{seconds}）")
            else:
                record(gate_id, name, "FAIL", f"`{cmd}` が終了コード {done.returncode} で失敗（{seconds}）",
                       done.stdout + done.stderr)

    ok = bool(results) and all(r["status"] in ("PASS", "SKIP") for r in results)
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
    if args.report:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({
            "ok": ok,
            "partial": bool(only),  # True: 一部のゲートだけを実行した。全体の合否ではない
            "unverified": [r["id"] for r in results if r["status"] == "SKIP"],  # 承認つきで検証を外したゲート
            "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "commit": tracekit.git_head(root),
            "gates": results,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "FAIL", "SKIP", "UNSET")}
    summary = "、".join(f"{status} {count}" for status, count in counts.items() if count)
    skipped = [r["id"] for r in results if r["status"] == "SKIP"]
    print(f"\n品質ゲート: {'合格' if ok else '不合格'}（{summary}）"
          + (f"　※SKIP の {len(skipped)} 件（{', '.join(skipped)}）は検証していません" if skipped else "")
          + ("　※一部のゲートだけを実行しました" if only else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    tracekit.setup_output()
    sys.exit(main())
