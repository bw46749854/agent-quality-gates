#!/usr/bin/env python3
"""配布用の ZIP を作り、その ZIP を展開した先で動くことを確かめる（テンプレートを配る人のための道具）。

フォルダをそのまま ZIP にすると、`.git/`・作業用の worktree・`__pycache__` が入り、実行権限が落ち、
コミットしていない別のブランチの修正は入らない。このスクリプトは git のコミットから書き出すので、
「いま配ろうとしているもの」と「確かめたもの」が同じになる。

  python3 tools/make_dist.py                    # HEAD から dist/quality-template.zip を作って確かめる
  python3 tools/make_dist.py --ref main         # 別のコミットから作る

確かめること:
  1. 入ってはいけないものが入っていない（.git、worktree、キャッシュ、このスクリプト自身）
  2. コミットにあるファイルが、同じ内容ですべて入っている（古い版が混ざっていない）
  3. コミット前の検査（pre-commit）に実行権限が付いている
  4. 展開した先で、検査ツール自身のテスト・サンプルの検査・サンプルの品質ゲートが通る
  5. 展開した先で、Claude Code の hook と CI の設定が有効と判定される

このファイルは配布物には入らない（.gitattributes の export-ignore）。
"""
from __future__ import annotations

import argparse
import hashlib
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_PARTS = {".git", "__pycache__", "worktrees", "dist"}
FORBIDDEN_FILES = {"tools/make_dist.py", "tools/tests/test_make_dist.py", ".gitattributes"}
MUST_BE_EXECUTABLE = ("tools/git-hooks/pre-commit",)


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout.decode("utf-8")


def uncommitted() -> list[str]:
    return [line for line in _git("status", "--porcelain").splitlines() if line.strip()]


def build(ref: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    _git("archive", "--format=zip", "--output", str(output.resolve()), ref)


def committed_blobs(ref: str) -> dict[str, str]:
    """コミットにあるファイル → その内容のハッシュ（配布しないと決めたものを除く）。"""
    blobs = {}
    for entry in _git("ls-tree", "-r", "-z", ref).split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            if path not in FORBIDDEN_FILES:
                blobs[path] = meta.split()[2]
    return blobs


def inspect(archive: Path, expected: dict[str, str] | None = None) -> list[str]:
    """ZIP の中身を調べ、問題の一覧を返す。expected を渡すと、コミットの内容との一致も調べる。"""
    problems = []
    with zipfile.ZipFile(archive) as bundle:
        files = {info.filename: info for info in bundle.infolist() if not info.is_dir()}
        for name in files:
            if FORBIDDEN_PARTS & set(name.split("/")) or name in FORBIDDEN_FILES or name.endswith(".pyc"):
                problems.append(f"入ってはいけないものが入っています: {name}")
        for name in MUST_BE_EXECUTABLE:
            info = files.get(name)
            if info is None:
                problems.append(f"入っていません: {name}")
            elif not (info.external_attr >> 16) & stat.S_IXUSR:
                problems.append(f"実行権限が付いていません: {name}（git はこの検査を黙って飛ばします）")
        if expected is not None:
            for name in sorted(set(expected) - set(files)):
                problems.append(f"コミットにあるのに、入っていません: {name}")
            for name in sorted(set(files) - set(expected)):
                problems.append(f"コミットにないものが入っています: {name}")
            for name in sorted(set(files) & set(expected)):
                data = bundle.read(name)
                blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
                if blob != expected[name]:
                    problems.append(f"コミットの内容と違います（古い版が混ざっている可能性）: {name}")
    return problems


def extract(archive: Path, target: Path) -> None:
    """実行権限を保って展開する（zipfile の extractall は権限を落とす）。"""
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            path = Path(bundle.extract(info, target))
            mode = info.external_attr >> 16
            if mode and not info.is_dir():
                path.chmod(stat.S_IMODE(mode))


def verify_extracted(target: Path, echo=print) -> list[str]:
    """展開した先で、受け取った人が最初に実行するものを実行する。"""
    problems = []

    def run(label: str, *args: str, expect: int = 0) -> str:
        done = subprocess.run([sys.executable, *args], cwd=target, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        tail = (done.stdout + done.stderr).strip().splitlines()[-1:] or [""]
        ok = done.returncode == expect
        echo(f"  [{'OK' if ok else 'NG'}] {label}: {tail[0]}")
        if not ok:
            problems.append(f"{label} が終了コード {done.returncode} で終わりました:\n{done.stdout}{done.stderr}")
        return done.stdout

    run("検査ツール自身のテスト", "-m", "unittest", "discover", "-s", "tools/tests")
    run("サンプルの検査", "tools/tracekit.py", "--root", "examples/shop", "check")
    run("サンプルの品質ゲート", "tools/verify.py", "--root", "examples/shop")
    # 展開しただけのテンプレートは未設定なので、doctor は対処が必要（終了コード 1）と答える。それが正しい
    report = run("導入の確認（未設定のテンプレートとして NG が出ること）", "tools/tracekit.py", "doctor", expect=1)
    for item in ("Claude Code の hook", "CI"):
        if f"[OK  ] {item}" not in report:
            problems.append(f"展開した先で「{item}」が有効と判定されません:\n{report}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配布用の ZIP を作り、展開した先で動くことを確かめる")
    parser.add_argument("--ref", default="HEAD", help="配布するコミット（既定: HEAD）")
    parser.add_argument("--output", default="dist/quality-template.zip")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="コミットしていない変更があっても作る（その変更は配布物に入らない）")
    args = parser.parse_args(argv)

    dirty = uncommitted()
    if dirty and not args.allow_dirty:
        print("コミットしていない変更があります。配布物は git のコミットから作るので、この変更は入りません。\n"
              "コミットしてからやり直してください（入れなくてよいなら --allow-dirty）:")
        print("\n".join("  " + line for line in dirty[:20]))
        return 1

    output = (ROOT / args.output).resolve()
    commit = _git("rev-parse", "--short", args.ref).strip()
    build(args.ref, output)
    print(f"{output} を作りました（コミット {commit}）")
    problems = inspect(output, committed_blobs(args.ref))
    print(f"  [{'OK' if not problems else 'NG'}] 中身の確認（余計なもの・古い版・実行権限）")
    if not problems:
        with tempfile.TemporaryDirectory() as tmp:
            extract(output, Path(tmp))
            problems = verify_extracted(Path(tmp))
    if problems:
        print("\n配布できません:")
        print("\n".join("- " + problem for problem in problems))
        return 1
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"\n配布できます: {output}\n  コミット {commit}／SHA-256 {digest}")
    print("受け取った側が unzip 以外（Windows のエクスプローラーなど）で展開すると、実行権限は付きません。"
          "README の導入の手順 5 を参照してください。")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
