#!/usr/bin/env python3
"""トレーサビリティ台帳の検査・影響分析・変更管理。

  要件 (docs/requirements/*.md)
    ⇔ コンポーネント (trace/components.toml)
    ⇔ コード (置き場所で決まる)
    ⇔ テスト (テストファイルに書かれた要件ID)

の対応を機械的に調べる。標準ライブラリだけで動く (Python 3.11 以上)。
hook・pre-commit・CI は、すべてこのファイルの同じ検査を呼ぶ。
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import re
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("tools/tracekit.py には Python 3.11 以上が必要です")
import tomllib

TOOLS_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = TOOLS_DIR.parent

REQ_ID = re.compile(r"REQ-\d{3,}\Z")
COMP_ID = re.compile(r"COMP-[A-Z0-9]+(?:-[A-Z0-9]+)*\Z")
CHG_ID = re.compile(r"CHG-\d{3,}\Z")
# テストファイル中の参照。 "REQ-001" または "REQ-001/AC-2"
REQ_REF = re.compile(r"(?<![A-Za-z0-9-])(REQ-\d{3,})(?:/(AC-\d+))?")
# 受け入れ条件の行。 "- **AC-1**: ..."
AC_LINE = re.compile(r"^\s*[-*]\s+\*\*(AC-\d+)\*\*", re.M)
# 受け入れ条件を書いたつもりに見えるが、上の形になっていない行（`- AC-1:`、`- **AC-1:**`、`1. **AC-1**:` など）。
# 黙って見落とすと「テストのない受け入れ条件」を検出できなくなるので、書式のエラーにする。
AC_LIKE = re.compile(r"^[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+)?[*_]*(AC-\d+)[*_]*[ \t]*[:：]", re.M)

REQ_STATUSES = ("draft", "approved", "implemented", "deprecated")
CHG_STATUSES = ("active", "done", "abandoned")
CHG_KINDS = ("feature", "fix", "refactor", "chore")
REQ_KEYS = {"id", "title", "status", "priority", "components", "verify"}
CHG_KEYS = {"id", "title", "status", "kind", "requirements", "components"}
COMP_KEYS = {"id", "name", "responsibility", "paths", "depends_on",
             "reference_patterns", "status", "shared"}
CHG_REQUIRED_SECTIONS = ("目的", "変更内容", "台帳に現れない影響の調査",
                         "回帰テスト計画", "不確実な点・残るリスク")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             "target", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
MAX_SCAN_BYTES = 2_000_000
# reference_patterns のうち、1つの正規表現にまとめると意味が変わる書き方:
# 文字列の端（\A \Z）、番号での後方参照、名前つきグループ、式全体にかかるフラグ。
NOT_COMBINABLE = re.compile(r"\\[AZ1-9]|\(\?P|\(\?[aiLmsux]+\)")
MAX_SHOWN_PER_CODE = 10  # 同じ種類の警告を、既定でいくつまで表示するか

DEFAULT_CONFIG = {
    "source_roots": ["src"],
    "ignore": [],
    "requirements_dir": "docs/requirements",
    "changes_dir": "docs/changes",
    "matrix_file": "docs/traceability.md",
    "tests": {
        "unit": ["tests/unit/**"],
        "integration": ["tests/integration/**"],
        "e2e": ["tests/e2e/**"],
        "contract": ["tests/contract/**"],
    },
    "granularity": {
        "min_requirements": 5,
        "max_requirement_share": 0.6,
        "max_components_per_requirement": 4,
    },
}


class ConfigError(Exception):
    """台帳そのものが読めない（構文エラーなど）。検査を続けられない。"""


# --------------------------------------------------------------------------- 基本部品

@dataclass
class Finding:
    level: str  # "error" | "warning"
    code: str
    message: str
    where: str = ""

    def render(self) -> str:
        tag = "ERROR" if self.level == "error" else "WARN "
        loc = f" [{self.where}]" if self.where else ""
        return f"{tag} {self.code}: {self.message}{loc}"


@dataclass
class Component:
    id: str
    name: str
    responsibility: str
    paths: list[str]
    depends_on: list[str]
    status: str = "active"
    shared: bool = False
    path_res: list[re.Pattern] = field(default_factory=list)
    ref_res: list[re.Pattern] = field(default_factory=list)
    ref_combinable: bool = True  # False: ほかの正規表現とまとめられない書き方。1行ずつ個別に調べる


@dataclass
class Requirement:
    id: str
    title: str
    status: str
    priority: str
    components: list[str]
    verify: list[str]
    acs: list[str]
    path: str


@dataclass
class Change:
    id: str
    title: str
    status: str
    kind: str
    requirements: list[str]
    components: list[str]
    path: str


def glob_to_re(glob: str) -> re.Pattern:
    """`*` は1階層、`**` は複数階層にマッチする glob を正規表現にする。"""
    g = glob.strip().removeprefix("./")
    if g.endswith("/"):
        g += "**"
    out, i = "", 0
    while i < len(g):
        if g.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif g.startswith("**", i):
            out += ".*"
            i += 2
        elif g[i] == "*":
            out += "[^/]*"
            i += 1
        elif g[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(g[i])
            i += 1
    return re.compile(out + r"\Z")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def split_frontmatter(text: str) -> tuple[dict, str]:
    """`---` で囲んだ先頭部分を読む。`キー: 値` と `キー: [a, b]` だけを受け付ける。"""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise ValueError("先頭に `---` で始まるフロントマターがありません")
    end = text.find("\n---", 3)
    if end == -1:
        raise ValueError("フロントマターを閉じる `---` がありません")
    meta: dict = {}
    for number, raw in enumerate(text[4:end].split("\n"), 2):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"{number}行目が `キー: 値` の形になっていません")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key != "title":
            value = re.sub(r"\s+#.*$", "", value)
        if value.startswith("["):
            if not value.endswith("]"):
                raise ValueError(f"{number}行目のリストが `]` で閉じていません")
            meta[key] = [_unquote(x) for x in value[1:-1].split(",") if x.strip()]
        else:
            meta[key] = _unquote(value)
    body = text[end + 4:]
    return meta, body.lstrip("\n")


def read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML の構文エラー: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: 読めません: {exc}") from exc


def list_files(root: Path) -> list[str]:
    """ルート配下のファイル一覧。git の管理下なら .gitignore に従う。

    ルートが git のリポジトリの最上位でなくてもよい（モノレポの中の1プロジェクトなど）。
    ルートそのものが無視の対象になっている場合は、git に聞いても何も返らないので、自分で数える。
    """
    try:
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "."],
                                 capture_output=True).returncode
        if ignored == 1:  # 1: 管理下にあり、無視されていない／0: 無視されている／128: git の外
            done = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                capture_output=True)
            if done.returncode == 0:
                out = done.stdout.decode("utf-8", "replace")
                return sorted(p for p in out.split("\0") if p and (root / p).is_file())
    except OSError:
        pass
    found = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            found.append(Path(directory, name).relative_to(root).as_posix())
    return sorted(found)


def _as_list(value, what: str) -> list[str]:
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        raise ValueError(f"`{what}` は `[a, b]` の形のリストで書いてください")
    return [str(v) for v in value]


def _csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def load_gates(root: Path) -> list[dict]:
    """trace/gates.toml のゲートの定義。verify.py と、検査（レベルが実行されるか）の両方が使う。"""
    path = Path(root) / "trace" / "gates.toml"
    if not path.is_file():
        return []
    gates = read_toml(path).get("gate", [])
    seen = set()
    for gate in gates:
        gate_id = str(gate.get("id", ""))
        if not gate_id or gate_id in seen or gate_id == "trace":
            raise ConfigError(f"trace/gates.toml: ゲートの id `{gate_id}` が空か、重複しています")
        seen.add(gate_id)
        levels = gate.get("levels", [])
        if not isinstance(levels, list) or not all(isinstance(v, str) for v in levels):
            raise ConfigError(f"trace/gates.toml: ゲート `{gate_id}` の levels は文字列のリストで書いてください")
    return gates


# --------------------------------------------------------------------------- 台帳の読み込み

class Project:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or DEFAULT_ROOT).resolve()
        self.findings: list[Finding] = []
        self.cfg = self._load_config()
        self.source_roots = [s.strip().removeprefix("./").strip("/")
                             for s in self.cfg["source_roots"]]
        self.test_levels = {level: [glob_to_re(g) for g in globs]
                            for level, globs in self.cfg["tests"].items()}
        self.ignore = [glob_to_re(g) for g in self.cfg["ignore"]]
        self.components: dict[str, Component] = self._load_components()
        self.requirements: dict[str, Requirement] = self._load_requirements()
        self.changes: dict[str, Change] = self._load_changes()
        self.prod_files: list[str] = []
        self.test_files: dict[str, str] = {}
        self.test_refs: dict[str, set[tuple[str, str]]] = {}
        self.owned: dict[str, list[str]] = {cid: [] for cid in self.components}
        self.req_tests: dict[str, dict[str, set[str]]] = {}
        self.ac_tests: dict[tuple[str, str], set[str]] = {}
        self._scan()

    # ---- 読み込み

    def _err(self, code: str, message: str, where: str = "") -> None:
        self.findings.append(Finding("error", code, message, where))

    def _load_config(self) -> dict:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        path = self.root / "trace" / "config.toml"
        if path.is_file():
            data = read_toml(path)
            for key, value in data.items():
                if key not in cfg:
                    raise ConfigError(f"trace/config.toml: 知らない設定 `{key}` があります")
                if key == "granularity":
                    cfg[key].update(value)
                else:
                    cfg[key] = value
        return cfg

    def _load_components(self) -> dict[str, Component]:
        path = self.root / "trace" / "components.toml"
        result: dict[str, Component] = {}
        if not path.is_file():
            return result
        where = "trace/components.toml"
        for raw in read_toml(path).get("component", []):
            cid = str(raw.get("id", ""))
            if not COMP_ID.match(cid):
                self._err("COMP-FORMAT", f"コンポーネントID `{cid}` が `COMP-名前`（英大文字・数字・ハイフン）の形ではありません", where)
                continue
            if cid in result:
                self._err("COMP-DUP", f"{cid} が重複しています", where)
                continue
            unknown = set(raw) - COMP_KEYS
            if unknown:
                self._err("COMP-FORMAT", f"{cid}: 知らない項目 {sorted(unknown)} があります（書き間違いの可能性）", where)
            comp = Component(
                id=cid,
                name=str(raw.get("name", "")),
                responsibility=str(raw.get("responsibility", "")),
                paths=[str(p) for p in raw.get("paths", [])],
                depends_on=[str(d) for d in raw.get("depends_on", [])],
                status=str(raw.get("status", "active")),
                shared=bool(raw.get("shared", False)),
            )
            if comp.status not in ("active", "planned"):
                self._err("COMP-FORMAT", f"{cid}: status は active か planned です", where)
            if not comp.responsibility.strip():
                self._err("COMP-FORMAT", f"{cid}: responsibility（責務）が書かれていません", where)
            if not comp.paths:
                self._err("COMP-FORMAT", f"{cid}: paths（コードの置き場所）が書かれていません", where)
            comp.path_res = [glob_to_re(p) for p in comp.paths]
            for pattern in raw.get("reference_patterns", []):
                try:
                    comp.ref_res.append(re.compile(pattern))
                    if NOT_COMBINABLE.search(pattern):
                        comp.ref_combinable = False
                except re.error as exc:
                    self._err("COMP-FORMAT", f"{cid}: reference_patterns の正規表現 `{pattern}` が不正です: {exc}", where)
            result[cid] = comp
        return result

    def _docs(self, directory: str) -> list[Path]:
        base = self.root / directory
        if not base.is_dir():
            return []
        return [p for p in sorted(base.rglob("*.md"))
                if p.name.lower() != "readme.md" and not p.name.startswith("_")]

    def _load_requirements(self) -> dict[str, Requirement]:
        result: dict[str, Requirement] = {}
        for path in self._docs(self.cfg["requirements_dir"]):
            rel = path.relative_to(self.root).as_posix()
            try:
                meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
                components = _as_list(meta.get("components"), "components")
                verify = _as_list(meta.get("verify"), "verify")
            except (ValueError, UnicodeDecodeError, OSError) as exc:
                self._err("REQ-FORMAT", str(exc), rel)
                continue
            rid = str(meta.get("id", ""))
            if not REQ_ID.match(rid):
                self._err("REQ-FORMAT", f"要件ID `{rid}` が `REQ-001` の形ではありません", rel)
                continue
            if rid in result:
                self._err("REQ-DUP", f"{rid} が {result[rid].path} と重複しています", rel)
                continue
            unknown = set(meta) - REQ_KEYS
            if unknown:
                self._err("REQ-FORMAT", f"{rid}: 知らない項目 {sorted(unknown)} があります", rel)
            status = str(meta.get("status", ""))
            if status not in REQ_STATUSES:
                self._err("REQ-FORMAT", f"{rid}: status は {' / '.join(REQ_STATUSES)} のどれかです", rel)
                continue
            if not str(meta.get("title", "")).strip():
                self._err("REQ-FORMAT", f"{rid}: title がありません", rel)
            if not path.name.startswith(rid):
                self._err("REQ-FORMAT", f"{rid}: ファイル名を `{rid}` で始めてください", rel)
            acs = AC_LINE.findall(body)
            malformed = sorted(set(AC_LIKE.findall(body)) - set(acs))
            if malformed:
                self._err("REQ-AC-FORMAT", f"{rid}: 受け入れ条件 {malformed} が `- **AC-1**: …` の形に"
                          "なっていないため、検査の対象になっていません。箇条書き（`-`）で始め、"
                          "`**AC-番号**` と太字にしてください", rel)
            duplicated = sorted({a for a in acs if acs.count(a) > 1})
            if duplicated:
                self._err("REQ-FORMAT", f"{rid}: 受け入れ条件 {duplicated} が重複しています", rel)
            result[rid] = Requirement(
                id=rid, title=str(meta.get("title", "")), status=status,
                priority=str(meta.get("priority", "")), components=components,
                verify=verify, acs=list(dict.fromkeys(acs)), path=rel,
            )
        return result

    def _load_changes(self) -> dict[str, Change]:
        result: dict[str, Change] = {}
        for path in self._docs(self.cfg["changes_dir"]):
            rel = path.relative_to(self.root).as_posix()
            try:
                meta, _ = split_frontmatter(path.read_text(encoding="utf-8"))
                requirements = _as_list(meta.get("requirements"), "requirements")
                components = _as_list(meta.get("components"), "components")
            except (ValueError, UnicodeDecodeError, OSError) as exc:
                self._err("CHG-FORMAT", str(exc), rel)
                continue
            cid = str(meta.get("id", ""))
            if not CHG_ID.match(cid):
                self._err("CHG-FORMAT", f"変更ID `{cid}` が `CHG-001` の形ではありません", rel)
                continue
            if cid in result:
                self._err("CHG-DUP", f"{cid} が {result[cid].path} と重複しています", rel)
                continue
            unknown = set(meta) - CHG_KEYS
            if unknown:
                self._err("CHG-FORMAT", f"{cid}: 知らない項目 {sorted(unknown)} があります", rel)
            status, kind = str(meta.get("status", "")), str(meta.get("kind", ""))
            if status not in CHG_STATUSES:
                self._err("CHG-FORMAT", f"{cid}: status は {' / '.join(CHG_STATUSES)} のどれかです", rel)
                continue
            if kind not in CHG_KINDS:
                self._err("CHG-FORMAT", f"{cid}: kind は {' / '.join(CHG_KINDS)} のどれかです", rel)
                continue
            result[cid] = Change(id=cid, title=str(meta.get("title", "")), status=status,
                                 kind=kind, requirements=requirements,
                                 components=components, path=rel)
        return result

    # ---- パスの分類

    def is_ignored(self, rel: str) -> bool:
        if any(part in SKIP_DIRS for part in rel.split("/")):
            return True
        return any(r.match(rel) for r in self.ignore)

    def test_level(self, rel: str) -> str | None:
        for level, patterns in self.test_levels.items():
            if any(r.match(rel) for r in patterns):
                return level
        return None

    def in_source_root(self, rel: str) -> bool:
        return any(rel == s or rel.startswith(s + "/") for s in self.source_roots)

    def is_production(self, rel: str) -> bool:
        """本番コードか。まだ存在しないファイルでも、パスだけで判定できる。"""
        return (self.in_source_root(rel) and not self.is_ignored(rel)
                and self.test_level(rel) is None)

    def owners(self, rel: str) -> list[Component]:
        return [c for c in self.components.values() if any(r.match(rel) for r in c.path_res)]

    def read_text(self, rel: str) -> str:
        path = self.root / rel
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                return ""
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _scan(self) -> None:
        for rel in list_files(self.root):
            if self.is_ignored(rel):
                continue
            level = self.test_level(rel)
            if level:
                self.test_files[rel] = level
                refs = set(REQ_REF.findall(self.read_text(rel)))
                self.test_refs[rel] = refs
                for rid, ac in refs:
                    self.req_tests.setdefault(rid, {}).setdefault(level, set()).add(rel)
                    if ac:
                        self.ac_tests.setdefault((rid, ac), set()).add(rel)
            elif self.in_source_root(rel):
                self.prod_files.append(rel)
                for comp in self.owners(rel):
                    self.owned[comp.id].append(rel)

    # ---- 依存グラフ

    def dependents(self, seeds: set[str]) -> dict[str, int]:
        """seeds に（直接・間接に）依存しているコンポーネントと、その距離。"""
        reverse: dict[str, set[str]] = {cid: set() for cid in self.components}
        for comp in self.components.values():
            for dep in comp.depends_on:
                if dep in reverse:
                    reverse[dep].add(comp.id)
        distance: dict[str, int] = {}
        queue = deque((s, 0) for s in seeds if s in reverse)
        seen = set(seeds)
        while queue:
            current, d = queue.popleft()
            for nxt in sorted(reverse[current]):
                if nxt not in seen:
                    seen.add(nxt)
                    distance[nxt] = d + 1
                    queue.append((nxt, d + 1))
        return distance

    def find_cycle(self) -> list[str] | None:
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(cid: str) -> list[str] | None:
            state[cid] = 1
            stack.append(cid)
            for dep in self.components[cid].depends_on:
                if dep not in self.components:
                    continue
                if state.get(dep) == 1:
                    return stack[stack.index(dep):] + [dep]
                if dep not in state:
                    found = visit(dep)
                    if found:
                        return found
            stack.pop()
            state[cid] = 2
            return None

        for cid in self.components:
            if cid not in state:
                found = visit(cid)
                if found:
                    return found
        return None

    def reqs_of(self, component_ids: set[str]) -> list[Requirement]:
        return [r for r in self.requirements.values()
                if r.status != "deprecated" and component_ids & set(r.components)]


# --------------------------------------------------------------------------- 検査

def check(proj: Project, matrix: bool = True) -> list[Finding]:
    findings = list(proj.findings)

    def err(code, message, where=""):
        findings.append(Finding("error", code, message, where))

    def warn(code, message, where=""):
        findings.append(Finding("warning", code, message, where))

    comps, reqs = proj.components, proj.requirements

    # -- コンポーネントの宣言
    for comp in comps.values():
        for dep in comp.depends_on:
            if dep == comp.id:
                err("COMP-CYCLE", f"{comp.id} が自分自身に依存しています", "trace/components.toml")
            elif dep not in comps:
                err("COMP-DEP-UNKNOWN", f"{comp.id} の依存先 {dep} は宣言されていません", "trace/components.toml")
    cycle = proj.find_cycle()
    if cycle:
        err("COMP-CYCLE", "依存が循環しています: " + " → ".join(cycle), "trace/components.toml")

    # -- コード ⇔ コンポーネント
    for rel in proj.prod_files:
        owners = proj.owners(rel)
        if not owners:
            err("CODE-ORPHAN", "どのコンポーネントにも属さない本番コードです。"
                "trace/components.toml の paths に含めるか、置き場所を見直してください", rel)
        elif len(owners) > 1:
            err("COMP-OVERLAP", "複数のコンポーネントの paths に一致します: "
                + ", ".join(o.id for o in owners), rel)
    for comp in comps.values():
        if comp.status == "active" and not proj.owned[comp.id]:
            warn("COMP-EMPTY", f"{comp.id} にコードがありません（設計段階なら status = \"planned\" にする）",
                 "trace/components.toml")

    # -- 宣言していない依存（reference_patterns による、言語に依らない検査）
    used: dict[str, set[str]] = {cid: set() for cid in comps}
    targets = [c for c in comps.values() if c.ref_res]
    # 「全行 × 全コンポーネントの正規表現」は大きなリポジトリで遅い。すべての正規表現を1つにまとめて
    # ファイル全体を1回だけ走査し、当たった行だけを、どのコンポーネントへの参照かを1行ずつ確かめる。
    fast = [t for t in targets if t.ref_combinable]
    slow = [t for t in targets if not t.ref_combinable]
    combined = None
    if fast:
        try:
            combined = re.compile("|".join(f"(?:{r.pattern})" for t in fast for r in t.ref_res), re.M)
        except re.error:
            fast, slow = [], targets
    for comp in comps.values():
        if not any(t.id != comp.id for t in targets):
            continue
        for rel in proj.owned[comp.id]:
            text = proj.read_text(rel)
            lines = None
            first_hit: dict[str, int] = {}  # 参照先のコンポーネント → 最初に現れる行
            if combined is not None:
                candidates: set[int] = set()
                for match in combined.finditer(text):
                    begin = text.count("\n", 0, match.start())
                    candidates.update(range(begin, begin + text.count("\n", match.start(), match.end()) + 1))
                if candidates:
                    lines = text.split("\n")
                    for index in sorted(candidates):
                        if index >= len(lines):
                            continue
                        line = lines[index].rstrip("\r")
                        for other in fast:
                            if (other.id != comp.id and other.id not in first_hit
                                    and any(r.search(line) for r in other.ref_res)):
                                first_hit[other.id] = index + 1
            for other in slow:
                if other.id == comp.id:
                    continue
                if lines is None:
                    lines = text.split("\n")
                number = next((n for n, line in enumerate(lines, 1)
                               if any(r.search(line.rstrip("\r")) for r in other.ref_res)), None)
                if number is not None:
                    first_hit[other.id] = number
            for other_id, number in sorted(first_hit.items()):
                used[comp.id].add(other_id)
                if other_id not in comp.depends_on:
                    err("DEP-UNDECLARED",
                        f"{comp.id} が {other_id} を参照していますが、depends_on に宣言されていません。"
                        "依存をなくすか、設計として認めるなら台帳と設計書を更新してください",
                        f"{rel}:{number}")
    for comp in comps.values():
        if not proj.owned[comp.id]:
            continue
        for dep in comp.depends_on:
            if dep in comps and comps[dep].ref_res and dep not in used[comp.id]:
                warn("DEP-UNUSED", f"{comp.id} は {dep} への依存を宣言していますが、参照が見つかりません",
                     "trace/components.toml")

    # -- テスト ⇔ 要件
    for test_file, refs in sorted(proj.test_refs.items()):
        if not refs:
            warn("TEST-UNLINKED", "要件IDが書かれていないテストファイルです（どの要件を検証するのか不明）",
                 test_file)
        for rid, ac in sorted(refs):
            req = reqs.get(rid)
            if req is None:
                err("TEST-DANGLING", f"存在しない要件 {rid} を参照しています", test_file)
            elif ac and ac not in req.acs:
                err("TEST-DANGLING", f"{rid} に受け入れ条件 {ac} はありません", test_file)
            elif req.status == "deprecated":
                warn("TEST-DEPRECATED", f"廃止された要件 {rid} を参照しています", test_file)

    # -- 要件
    for req in reqs.values():
        for cid in req.components:
            if cid not in comps:
                err("REQ-COMP-UNKNOWN", f"{req.id} が参照するコンポーネント {cid} は宣言されていません", req.path)
        for level in req.verify:
            if level not in proj.test_levels:
                err("REQ-LEVEL-UNKNOWN", f"{req.id} の verify にあるレベル `{level}` は "
                    "trace/config.toml の [tests] にありません", req.path)
        if req.status in ("approved", "implemented"):
            if not req.acs:
                err("REQ-NO-AC", f"{req.id} に受け入れ条件（`- **AC-1**: ...`）がありません", req.path)
            if not req.components:
                err("REQ-NO-COMP", f"{req.id} を実現するコンポーネントが指定されていません", req.path)
            if not req.verify:
                err("REQ-NO-VERIFY", f"{req.id} をどのレベルのテストで検証するか（verify）が指定されていません", req.path)
        if req.status == "implemented":
            for ac in req.acs:
                if not proj.ac_tests.get((req.id, ac)):
                    err("REQ-AC-UNTESTED", f"{req.id}/{ac} を検証するテストがありません"
                        f"（テストファイルに `{req.id}/{ac}` と書く）", req.path)
            for level in req.verify:
                if level in proj.test_levels and not proj.req_tests.get(req.id, {}).get(level):
                    err("REQ-LEVEL-MISSING", f"{req.id} は {level} での検証を求めていますが、"
                        f"{level} のテストがありません", req.path)
            for cid in req.components:
                if cid in comps and not proj.owned[cid]:
                    err("REQ-NO-CODE", f"{req.id} は implemented ですが、{cid} にコードがありません", req.path)

    # -- 検証レベル ⇔ ゲート: テストがあっても、実行されなければ検証していない
    try:
        gates = load_gates(proj.root)
    except ConfigError as exc:
        gates = []
        err("GATE-FORMAT", str(exc), "trace/gates.toml")
    run_levels: set[str] = set()
    for gate in gates:
        gate_id = str(gate["id"])
        declared = gate.get("levels")
        for level in declared or []:
            if level not in proj.test_levels:
                err("GATE-FORMAT", f"ゲート `{gate_id}` の levels にある `{level}` は "
                    "trace/config.toml の [tests] にありません", "trace/gates.toml")
        levels = declared if declared is not None else ([gate_id] if gate_id in proj.test_levels else [])
        if str(gate.get("cmd", "")).strip():
            run_levels.update(levels)
    for level in proj.test_levels:
        if level in run_levels:
            continue
        needed_by = sorted(r.id for r in reqs.values() if r.status == "implemented" and level in r.verify)
        count = sum(1 for lv in proj.test_files.values() if lv == level)
        how = (f"trace/gates.toml で id が `{level}` のゲートに cmd を書くか、"
               f"そのレベルも実行しているゲートに `levels = [..., \"{level}\"]` と書いてください")
        if needed_by:
            names = ", ".join(needed_by[:5]) + (f" ほか {len(needed_by) - 5} 件" if len(needed_by) > 5 else "")
            err("LEVEL-NOT-RUN", f"{names} は {level} での検証を求めていますが、"
                f"{level} のテストを実行するゲートがありません（未設定か、skip_reason で外されている）。{how}",
                "trace/gates.toml")
        elif count:
            warn("LEVEL-NOT-RUN", f"{level} のテストファイルが {count} 件ありますが、実行するゲートがありません。{how}",
                 "trace/gates.toml")

    # -- 逆方向: 要件のないコンポーネント
    for comp in comps.values():
        if not comp.shared and not proj.reqs_of({comp.id}):
            warn("COMP-NOREQ", f"{comp.id} はどの要件にも結びついていません（不要な設計か、要件の書き漏れ）",
                 "trace/components.toml")

    # -- 変更記録
    for change in proj.changes.values():
        for cid in change.components:
            if cid not in comps:
                err("CHG-REF-UNKNOWN", f"{change.id} が参照するコンポーネント {cid} は宣言されていません", change.path)
        for rid in change.requirements:
            if rid not in reqs:
                err("CHG-REF-UNKNOWN", f"{change.id} が参照する要件 {rid} は存在しません", change.path)
        if (change.kind in ("feature", "fix") and change.status != "abandoned"
                and not change.requirements):
            err("CHG-NO-REQ", f"{change.id}: kind が {change.kind} の変更には、対象の要件が必要です", change.path)

    # -- 粒度（粗すぎ・散らばりすぎ）
    gran = proj.cfg["granularity"]
    live = [r for r in reqs.values() if r.status in ("approved", "implemented")]
    if len(live) >= gran["min_requirements"]:
        for comp in comps.values():
            if comp.shared:
                continue
            reach = {comp.id} | set(proj.dependents({comp.id}))
            share = sum(1 for r in live if reach & set(r.components)) / len(live)
            if share > gran["max_requirement_share"]:
                warn("GRAN-COARSE",
                     f"{comp.id} を変えると要件の {share:.0%} に影響します。境界が粗いか、依存が集まりすぎています"
                     "（分割するか、意図した共有基盤なら shared = true）", "trace/components.toml")
        for req in live:
            if len(req.components) > gran["max_components_per_requirement"]:
                warn("GRAN-SCATTERED",
                     f"{req.id} が {len(req.components)} 個のコンポーネントにまたがっています。"
                     "要件が大きすぎるか、責務が散らばっています", req.path)

    # -- 生成物のマトリクスが古くないか
    if matrix:
        path = proj.root / proj.cfg["matrix_file"]
        if path.is_file():
            current = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            if current != render_matrix(proj):
                err("MATRIX-STALE", "マトリクスが台帳と一致しません。"
                    "`python3 tools/tracekit.py matrix --write` で作り直してください",
                    proj.cfg["matrix_file"])

    findings.sort(key=lambda f: (f.level != "error",))
    return findings


# --------------------------------------------------------------------------- マトリクス

def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_matrix(proj: Project) -> str:
    levels = list(proj.test_levels)
    base = os.path.dirname(proj.cfg["matrix_file"])

    def link(label: str, target: str) -> str:
        return f"[{label}]({os.path.relpath(target, base or '.').replace(os.sep, '/')})"

    out = [
        "<!-- 生成物です。`python3 tools/tracekit.py matrix --write` で作り直します。手で編集しないでください。 -->",
        "# トレーサビリティ・マトリクス",
        "",
        "## 要件 → コンポーネント → テスト",
        "",
    ]
    reqs = sorted(proj.requirements.values(), key=lambda r: r.id)
    if not reqs:
        out.append("（要件はまだありません）")
    else:
        out.append("| 要件 | 状態 | コンポーネント | " + " | ".join(levels) + " | AC の網羅 |")
        out.append("|---|---|---|" + "---|" * (len(levels) + 1))
        for req in reqs:
            cells = []
            for level in levels:
                count = len(proj.req_tests.get(req.id, {}).get(level, ()))
                if count:
                    cells.append(str(count))
                else:
                    cells.append("**なし**" if level in req.verify else "—")
            covered = sum(1 for ac in req.acs if proj.ac_tests.get((req.id, ac)))
            out.append(f"| {link(req.id, req.path)} {_cell(req.title)} | {req.status} | "
                       f"{', '.join(req.components) or '—'} | " + " | ".join(cells)
                       + f" | {covered}/{len(req.acs)} |")
        out += ["", "数字はその要件を参照しているテストファイルの数。"
                "**なし** は、要件が求める検証レベルにテストがないことを示す。"]

    out += ["", "## コンポーネント → 要件（逆引き）", ""]
    comps = sorted(proj.components.values(), key=lambda c: c.id)
    if not comps:
        out.append("（コンポーネントはまだありません）")
    else:
        out.append("| コンポーネント | 責務 | 依存先 | 依存元 | 実現する要件 | 変更時に影響しうる要件 |")
        out.append("|---|---|---|---|---|---|")
        for comp in comps:
            dependents = proj.dependents({comp.id})
            own = sorted(r.id for r in proj.reqs_of({comp.id}))
            reach = sorted(r.id for r in proj.reqs_of({comp.id} | set(dependents)))
            out.append(f"| {comp.id} {_cell(comp.name)} | {_cell(comp.responsibility)} | "
                       f"{', '.join(comp.depends_on) or '—'} | {', '.join(sorted(dependents)) or '—'} | "
                       f"{', '.join(own) or '—'} | {', '.join(reach) or '—'} |")

    out += ["", "## 受け入れ条件 → テスト", ""]
    rows = [(req, ac) for req in reqs for ac in req.acs]
    if not rows:
        out.append("（受け入れ条件はまだありません）")
    else:
        out.append("| 受け入れ条件 | テスト |")
        out.append("|---|---|")
        for req, ac in rows:
            tests = sorted(proj.ac_tests.get((req.id, ac), ()))
            shown = "<br>".join(f"`{t}`（{proj.test_files[t]}）" for t in tests) or "**なし**"
            out.append(f"| {req.id}/{ac} | {shown} |")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- 影響分析

def compute_impact(proj: Project, files=(), components=(), requirements=()) -> dict:
    direct: set[str] = set()
    unknown_components = [c for c in components if c not in proj.components]
    direct.update(c for c in components if c in proj.components)
    changed_reqs = {r for r in requirements if r in proj.requirements}
    unknown_requirements = [r for r in requirements if r not in proj.requirements]
    unowned, changed_tests, ledger_changed = [], [], False
    req_by_path = {r.path: r.id for r in proj.requirements.values()}

    for rel in files:
        if proj.is_ignored(rel):
            continue
        if proj.test_level(rel):
            changed_tests.append(rel)
        elif proj.in_source_root(rel):
            owners = proj.owners(rel)
            if owners:
                direct.update(o.id for o in owners)
            else:
                unowned.append(rel)
        elif rel in req_by_path:
            changed_reqs.add(req_by_path[rel])
        elif rel.startswith("trace/"):
            ledger_changed = True

    dependents = proj.dependents(direct)
    direct_reqs = {r.id for r in proj.reqs_of(direct)} | changed_reqs
    indirect_reqs = {r.id for r in proj.reqs_of(set(dependents))} - direct_reqs

    def tests_for(req_ids: set[str]) -> set[str]:
        return {t for rid in req_ids for files_ in proj.req_tests.get(rid, {}).values() for t in files_}

    tests_direct = tests_for(direct_reqs) | {t for t in changed_tests if (proj.root / t).is_file()}
    tests_indirect = tests_for(indirect_reqs) - tests_direct
    untested = sorted(rid for rid in direct_reqs | indirect_reqs if not proj.req_tests.get(rid))
    return {
        "direct_components": sorted(direct),
        "dependent_components": dict(sorted(dependents.items())),
        "direct_requirements": sorted(direct_reqs),
        "indirect_requirements": sorted(indirect_reqs),
        "tests_direct": sorted(tests_direct),
        "tests_indirect": sorted(tests_indirect),
        "unowned_files": sorted(unowned),
        "untested_requirements": untested,
        "ledger_changed": ledger_changed,
        "unknown_components": unknown_components,
        "unknown_requirements": unknown_requirements,
    }


def render_impact(proj: Project, impact: dict, heading: str = "## 影響分析（台帳からの機械抽出）") -> str:
    def req_line(rid: str) -> str:
        req = proj.requirements[rid]
        return f"- {rid} {req.title}（{req.status}）"

    def test_line(path: str) -> str:
        refs = sorted({rid for rid, _ in proj.test_refs.get(path, ())})
        return f"- `{path}`（{proj.test_files.get(path, '?')}）— {', '.join(refs) or '要件IDなし'}"

    out = [heading, ""]
    out.append("### 直接変更するコンポーネント")
    out += [f"- {cid} {proj.components[cid].name} — {proj.components[cid].responsibility}"
            for cid in impact["direct_components"]] or ["- なし"]
    out += ["", "### 依存をたどって影響しうるコンポーネント"]
    out += [f"- {cid} {proj.components[cid].name}（距離 {d}）"
            for cid, d in impact["dependent_components"].items()] or ["- なし"]
    out += ["", "### 関係する要件", "直接:"]
    out += [req_line(r) for r in impact["direct_requirements"]] or ["- なし"]
    out.append("間接（依存元のコンポーネントが実現している要件）:")
    out += [req_line(r) for r in impact["indirect_requirements"]] or ["- なし"]
    out += ["", "### 実行すべきテスト（回帰テストの候補）", "直接:"]
    out += [test_line(t) for t in impact["tests_direct"]] or ["- なし"]
    out.append("間接:")
    out += [test_line(t) for t in impact["tests_indirect"]] or ["- なし"]

    notes = []
    if impact["unowned_files"]:
        notes.append("どのコンポーネントにも属さないファイル: " + ", ".join(impact["unowned_files"]))
    if impact["untested_requirements"]:
        notes.append("テストが1つもない要件（回帰を検出できない）: " + ", ".join(impact["untested_requirements"]))
    if impact["ledger_changed"]:
        notes.append("台帳（trace/）自体が変わっています。全体の再検証が必要です")
    if impact["unknown_components"]:
        notes.append("台帳にないコンポーネントが指定されました: " + ", ".join(impact["unknown_components"]))
    if impact["unknown_requirements"]:
        notes.append("存在しない要件が指定されました: " + ", ".join(impact["unknown_requirements"]))
    notes.append("この抽出は、台帳に宣言された対応だけに基づきます。共有データ・イベント・設定値・"
                 "外部システムなど、宣言に現れない結合は別に調べてください")
    out += ["", "### 注意"] + [f"- {n}" for n in notes]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- git

def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return done.stdout.decode("utf-8", "replace")


def git_changed(root: Path, base: str | None = None, staged: bool = False) -> list[str]:
    """変更されたファイル（ルートからの相対パス）。

    base を指定したときは、base と HEAD の分かれ目からの差分を取る。base の側だけが先に進んで
    いても、その変更をこちらの変更として数えない（CI で `origin/main` を指定する場合のため）。
    """
    def names(*args: str) -> list[str]:
        # -z: 日本語などのファイル名を、エスケープされていない形で受け取る
        return [name for name in _git(root, *args, "-z").split("\0") if name]

    try:
        if staged:
            return sorted(set(names("diff", "--name-only", "--cached", "--relative")))
        start = base or "HEAD"
        try:
            start = _git(root, "merge-base", start, "HEAD").strip() or start
        except subprocess.CalledProcessError:
            pass  # 共通の祖先がない（履歴を浅く取っている、など）。指定された base とそのまま比べる
        found = names("diff", "--name-only", "--relative", start)
        found += names("ls-files", "--others", "--exclude-standard")
        return sorted(set(found))
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        raise ConfigError(f"git の差分を取得できません: {detail.strip()}") from exc


def git_head(root: Path) -> str | None:
    """検証した時点を記録するための、現在のコミット。git の外なら None。"""
    try:
        head = _git(root, "rev-parse", "--short", "HEAD").strip()
        dirty = bool(_git(root, "status", "--porcelain").strip())
    except (OSError, subprocess.CalledProcessError):
        return None
    return f"{head}（未コミットの変更あり）" if dirty else head


# --------------------------------------------------------------------------- 変更の範囲の番人

def guard(proj: Project, files, diff_files=None, strict: bool = False,
          check_requirements: bool | None = None) -> list[tuple[str, str]]:
    """変更が、影響分析済みの変更記録の範囲に入っているかを調べる。

    - diff_files なし（編集の時点。hook）: 進行中の変更記録だけが有効。
    - diff_files あり（コミット前・CI）: 同じ差分に含まれる完了済みの変更記録も有効とみなす。
      さらに、内容の固まった要件（draft 以外）の変更が、変更記録の requirements に入っているかも調べる
      （check_requirements=False で外せる）。
    - strict（プルリクエスト）: 進行中の変更記録も、この差分で追加・更新されたものだけを有効とみなす。
      閉じ忘れた変更記録が、あとの無関係な変更まで通してしまうのを防ぐ。
    """
    changed = set(diff_files or ())
    if check_requirements is None:
        check_requirements = diff_files is not None
    active = [c for c in proj.changes.values() if c.status == "active"]
    untouched = {}
    if strict:
        untouched = {cid: c.id for c in active if c.path not in changed for cid in c.components}
        active = [c for c in active if c.path in changed]
    covering = active
    if diff_files is not None:
        covering = covering + [c for c in proj.changes.values()
                               if c.status == "done" and c.path in changed]
    not_ready: dict[str, list[str]] = {}
    for change in covering:
        drafts = [rid for rid in change.requirements
                  if rid in proj.requirements and proj.requirements[rid].status == "draft"]
        if drafts:
            not_ready[change.id] = drafts
    scope = {cid for change in covering if change.id not in not_ready for cid in change.components}
    waiting = {cid: change.id for change in covering if change.id in not_ready
               for cid in change.components}
    problems = []
    if check_requirements:
        recorded = {rid for change in covering for rid in change.requirements}
        by_path = {r.path: r for r in proj.requirements.values()}
        for rel in files:
            req = by_path.get(rel)
            if req is not None and req.status != "draft" and req.id not in recorded:
                problems.append((rel, f"{req.id}（{req.status}）が変更されていますが、どの変更記録の "
                                      "requirements にも入っていません。要件の変更は影響分析の対象です。"
                                      f"変更記録の requirements に {req.id} を加え、この要件を参照するテストと"
                                      "コンポーネントへの影響を調べてください"
                                      "（手順: .claude/skills/analyze-impact/SKILL.md）"))
    for rel in files:
        if not proj.is_production(rel):
            continue
        owners = proj.owners(rel)
        blocked_by = next((waiting[o.id] for o in owners if o.id in waiting), None)
        if owners and blocked_by and not any(o.id in scope for o in owners):
            drafts = ", ".join(not_ready[blocked_by])
            problems.append((rel, f"{blocked_by} の対象の要件 {drafts} が draft のままです。受け入れ条件・"
                                  "担当コンポーネント（components）・検証レベル（verify）を埋めて approved に"
                                  "してから実装に入ってください（手順: .claude/skills/develop/SKILL.md）"))
            continue
        if not owners:
            problems.append((rel, "どのコンポーネントにも属さない場所です。新しい責務なら、先に設計として "
                                  "trace/components.toml にコンポーネント（paths・責務・依存先）を宣言してください"
                                  "（手順: .claude/skills/design-architecture/SKILL.md）"))
        elif not any(o.id in scope for o in owners) and any(o.id in untouched for o in owners):
            change_id = next(untouched[o.id] for o in owners if o.id in untouched)
            problems.append((rel, f"{change_id} が範囲に含んでいますが、その変更記録がこの差分で更新されて"
                                  f"いません。この変更が {change_id} の続きなら、変更記録に今回の内容を書き足して"
                                  "ください。別の変更なら、新しい変更記録を作ってください"
                                  "（閉じ忘れた変更記録が、無関係な変更を通してしまうのを防ぐための確認です）"))
        elif not any(o.id in scope for o in owners):
            ids = ", ".join(o.id for o in owners)
            problems.append((rel, f"{ids} を範囲に含む進行中の変更記録がありません。先に影響分析を行い、"
                                  f"`python3 tools/tracekit.py change new --title \"…\" --kind … "
                                  f"--components {owners[0].id} --requirements REQ-…` で変更記録を作ってください"
                                  "（手順: .claude/skills/analyze-impact/SKILL.md）。"
                                  "進行中の変更の範囲が広がった場合は、その記録の components と影響分析を更新してください"))
    return problems


# --------------------------------------------------------------------------- 変更記録

CHANGE_TEMPLATE = """---
id: {id}
title: {title}
status: active
kind: {kind}
requirements: [{requirements}]
components: [{components}]
---

## 目的
<!-- なぜ変えるのか。どの要件を満たす（または直す）ための変更かを書く。 -->

## 変更内容
<!-- 何をどう変えるか。設計上の判断があれば理由も書く（大きな判断は docs/decisions/ に残す）。 -->

{impact}
## 台帳に現れない影響の調査
<!-- 共有データ・イベント・設定値・外部システム・公開インターフェースの変更など、宣言された依存に現れない結合を調べて書く。なければ「なし（調べた範囲: …）」と書く。 -->

## 回帰テスト計画
<!-- 上の「実行すべきテスト」に加えて、新しく追加・修正するテストを書く。 -->

## 不確実な点・残るリスク
<!-- 影響を特定しきれなかった点。なければ「なし」と書く。 -->
"""


def change_new(proj: Project, title: str, kind: str, components: list[str],
               requirements: list[str]) -> Path:
    problems = [f"コンポーネント {c} は台帳にありません" for c in components if c not in proj.components]
    problems += [f"要件 {r} は存在しません" for r in requirements if r not in proj.requirements]
    if kind in ("feature", "fix") and not requirements:
        problems.append(f"kind が {kind} の変更には --requirements が必要です")
    if problems:
        raise ConfigError("変更記録を作れません: " + " / ".join(problems))
    numbers = [int(cid.split("-")[1]) for cid in proj.changes]
    directory = proj.root / proj.cfg["changes_dir"]
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.glob("CHG-*.md"):
        match = re.match(r"CHG-(\d+)", path.name)
        if match:
            numbers.append(int(match.group(1)))
    change_id = f"CHG-{max(numbers, default=0) + 1:03d}"
    impact = compute_impact(proj, components=components, requirements=requirements)
    path = directory / f"{change_id}.md"
    path.write_text(CHANGE_TEMPLATE.format(
        id=change_id, title=title.replace("\n", " "), kind=kind,
        requirements=", ".join(requirements), components=", ".join(components),
        impact=render_impact(proj, impact)), encoding="utf-8")
    return path


def _sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = None
    for line in body.split("\n"):
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return sections


def _set_status(path: Path, status: str) -> None:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    end = text.find("\n---", 3)
    head = re.sub(r"(?m)^status:.*$", f"status: {status}", text[:end], count=1)
    path.write_text(head + text[end:], encoding="utf-8")


def change_close(proj: Project, change_id: str, echo=print) -> bool:
    change = proj.changes.get(change_id)
    if change is None:
        raise ConfigError(f"変更記録 {change_id} がありません")
    if change.status != "active":
        raise ConfigError(f"{change_id} は進行中ではありません（status: {change.status}）")
    path = proj.root / change.path
    problems = []
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    sections = _sections(body)
    for name in CHG_REQUIRED_SECTIONS:
        content = re.sub(r"<!--.*?-->", "", sections.get(name, ""), flags=re.S).strip()
        if not content:
            problems.append(f"「{name}」が書かれていません")
    for rid in change.requirements:
        req = proj.requirements.get(rid)
        if req is not None and req.status not in ("implemented", "deprecated"):
            problems.append(f"{rid} の status が {req.status} のままです"
                            "（実装とテストが済んだら implemented にする）")
    if problems:
        echo(f"{change_id} を完了にできません:")
        for problem in problems:
            echo(f"  - {problem}")
        return False

    matrix_path = proj.root / proj.cfg["matrix_file"]
    matrix_path.parent.mkdir(parents=True, exist_ok=True)
    matrix_path.write_text(render_matrix(proj), encoding="utf-8")

    import verify  # 循環 import を避けるため、ここで読み込む
    results, ok = verify.run_gates(proj.root, echo=echo)
    if not ok:
        echo(f"\n品質ゲートを通過していないため、{change_id} は進行中のままです。")
        return False
    lines = ["", "## 検証結果", f"- 実施日: {datetime.date.today().isoformat()}"]
    head = git_head(proj.root)
    if head:
        lines.append(f"- 検証した時点のコミット: {head}")
    lines += [f"- [{r['status']}] {r['id']}（{r['name']}）: {r['detail']}" for r in results]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    _set_status(path, "done")
    echo(f"\n{change_id} を完了にしました（{change.path}）。")
    return True


# --------------------------------------------------------------------------- 現在地

def render_status(proj: Project) -> str:
    findings = check(proj, matrix=False)
    errors = sum(1 for f in findings if f.level == "error")
    warnings = len(findings) - errors
    by_status: dict[str, int] = {}
    for req in proj.requirements.values():
        by_status[req.status] = by_status.get(req.status, 0) + 1
    req_summary = "、".join(f"{k} {v}" for k, v in by_status.items()) or "なし"
    out = [
        "## 品質ゲートの現在地",
        f"- 要件: {len(proj.requirements)} 件（{req_summary}）／コンポーネント: {len(proj.components)} 個",
        f"- 台帳の検査: エラー {errors} 件、警告 {warnings} 件"
        + ("（`python3 tools/tracekit.py check` で内容を確認して直す）" if errors else ""),
    ]
    active = [c for c in proj.changes.values() if c.status == "active"]
    if active:
        out.append("- 進行中の変更:")
        out += [f"  - {c.id} {c.title}（範囲: {', '.join(c.components) or 'なし'}／"
                f"要件: {', '.join(c.requirements) or 'なし'}）→ {c.path}" for c in active]
    else:
        out.append("- 進行中の変更: なし。本番コードを編集する前に、影響分析をして変更記録を作ること")
    not_ready = [item for state, item, _ in doctor(proj.root, proj) if state == "NG"]
    if not_ready:
        out.append(f"- 導入の確認: {'、'.join(not_ready)} に対処が必要です"
                   "（`python3 tools/tracekit.py doctor` で内容を確認する）")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- CLI

def _print_findings(findings: list[Finding], show_all: bool = False) -> None:
    """エラーはすべて表示する。警告は、同じ種類が多いときは先頭だけを表示して件数を添える
    （既存のプロジェクトに入れた直後に、数百件の警告で本当に見るべきものが埋もれないように）。"""
    shown: dict[str, int] = {}
    hidden: dict[str, int] = {}
    for finding in findings:
        if finding.level == "warning" and not show_all:
            shown[finding.code] = shown.get(finding.code, 0) + 1
            if shown[finding.code] > MAX_SHOWN_PER_CODE:
                hidden[finding.code] = hidden.get(finding.code, 0) + 1
                continue
        print(finding.render())
    for code, count in hidden.items():
        print(f"WARN  {code}: …ほか {count} 件（`check --all` ですべて表示）")
    errors = sum(1 for f in findings if f.level == "error")
    print(f"検査結果: エラー {errors} 件、警告 {len(findings) - errors} 件")


def setup_output(encoding: str | None = None) -> None:
    """表示できない文字があっても、異常終了しない。

    Windows の日本語環境（cp932）のコンソールは「—」などを表示できず、何もしないと例外で止まる。
    hook の出力はエージェントが UTF-8 として読むので、encoding="utf-8" を指定して呼ぶ。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if encoding:
                stream.reconfigure(encoding=encoding, errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


# --------------------------------------------------------------------------- 導入の確認

def doctor(root: Path, proj: "Project | None" = None) -> list[tuple[str, str, str]]:
    """仕組みが実際に働く状態かを調べる。(OK|NG|WARN|INFO, 項目, 説明) の一覧を返す。

    NG は、そのままでは検査が働かないもの。WARN は、守りの層が1つ欠けているが、ほかの層が残るもの。

    検査の中身が正しくても、hook やコミット前の検査が有効になっていなければ何も止まらない。
    とくに `git config core.hooksPath` は clone ごとの設定なので、メンバーごとに確かめる必要がある。
    """
    root = Path(root).resolve()
    out: list[tuple[str, str, str]] = []

    def add(ok, item: str, good: str, bad: str) -> None:
        out.append(("OK" if ok else "NG", item, good if ok else bad))

    try:
        proj = proj or Project(root)
    except ConfigError as exc:
        return [("NG", "台帳", f"読めません: {exc}")]

    missing = [s for s in proj.source_roots if not (root / s).is_dir()]
    add(not missing, "本番コードの置き場所", "source_roots = " + ", ".join(proj.source_roots),
        f"trace/config.toml の source_roots にある {', '.join(missing)} がありません。"
        "実際の置き場所に合わせてください（合っていないと、本番コードの編集が何も検査されません）")
    add(bool(proj.components), "コンポーネントの宣言", f"{len(proj.components)} 個",
        "trace/components.toml にコンポーネントがありません（.claude/skills/design-architecture/SKILL.md）")

    try:
        gates = load_gates(root)
        unset = [str(g["id"]) for g in gates
                 if not str(g.get("cmd", "")).strip() and not str(g.get("skip_reason", "")).strip()]
        skipped = [str(g["id"]) for g in gates
                   if not str(g.get("cmd", "")).strip() and str(g.get("skip_reason", "")).strip()]
        add(bool(gates) and not unset, "品質ゲートの設定", f"{len(gates)} 個（うち理由つきで実行しないもの: "
            f"{', '.join(skipped) or 'なし'}）",
            f"未設定のゲートがあります: {', '.join(unset) or '（ゲートが1つもありません）'}。"
            "trace/gates.toml に cmd か skip_reason を書いてください（adapters/README.md）")
    except ConfigError as exc:
        out.append(("NG", "品質ゲートの設定", str(exc)))

    try:
        inside = _git(root, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except (OSError, subprocess.CalledProcessError):
        inside = False
    add(inside, "git", "git の管理下にあります",
        "git の管理下にありません。コミット前の検査と、差分からの影響分析が働きません")
    if inside:
        try:
            hooks_path = _git(root, "config", "--get", "core.hooksPath").strip()
        except subprocess.CalledProcessError:
            hooks_path = ""
        hook = root / "tools" / "git-hooks" / "pre-commit"
        enabled = hooks_path.rstrip("/").endswith("tools/git-hooks")
        add(enabled, "コミット前の検査", "core.hooksPath = " + hooks_path,
            "有効になっていません。`git config core.hooksPath tools/git-hooks` を実行してください"
            "（clone ごとの設定なので、メンバー全員に必要です）")
        if enabled and os.name != "nt":
            add(os.access(hook, os.X_OK), "コミット前の検査の実行権限", "あり",
                "tools/git-hooks/pre-commit に実行権限がありません（`chmod +x tools/git-hooks/pre-commit`）")

    settings = [root / ".claude" / name for name in ("settings.json", "settings.local.json")]
    wired = set()
    for path in settings:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        wired.update(name for name in ("session_start.py", "pre_edit.py", "on_stop.py") if name in text)
    not_wired = sorted({"session_start.py", "pre_edit.py", "on_stop.py"} - wired)
    out.append(("OK", "Claude Code の hook", "3本とも設定されています") if not not_wired else
               ("WARN", "Claude Code の hook",
                f".claude/settings.json に {', '.join(not_wired)} が設定されていません。"
                "テンプレートの .claude/settings.json を置くか、その hooks の部分を書き写してください"
                "（Claude Code を使わない場合は不要です。コミット前の検査と CI が守ります）"))

    workflows = root / ".github" / "workflows"
    ci = workflows.is_dir() and any("verify.py" in p.read_text(encoding="utf-8", errors="replace")
                                    for p in workflows.glob("*.y*ml"))
    out.append(("OK" if ci else "INFO", "CI",
                "GitHub Actions で tools/verify.py を実行しています" if ci else
                "GitHub Actions の設定に tools/verify.py が見つかりません。ほかの CI を使っている場合は、"
                "`guard --base … --strict` と `verify.py` を実行していることを確かめてください"))

    stale = [c for c in proj.changes.values() if c.status == "active"]
    out.append(("INFO", "進行中の変更記録", "なし" if not stale else
                "、".join(f"{c.id}（{', '.join(c.components) or '範囲なし'}）" for c in stale)
                + "。終わっているものは `change close`、やめたものは `change abandon` で閉じてください"))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="トレーサビリティ台帳の検査・影響分析・変更管理")
    parser.add_argument("--root", default=None, help="プロジェクトのルート（既定: tools/ の1つ上）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="台帳・コード・テストの整合を検査する")
    p.add_argument("--no-matrix", action="store_true", help="マトリクスの鮮度は調べない")
    p.add_argument("--all", action="store_true", help="同じ種類の警告を省略せず、すべて表示する")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("impact", help="変更の影響範囲と、実行すべきテストを抽出する")
    p.add_argument("--base", help="この git リビジョンとの差分から抽出する")
    p.add_argument("--staged", action="store_true", help="ステージ済みの差分から抽出する")
    p.add_argument("--files", nargs="*", default=[])
    p.add_argument("--components", default="", help="変更予定のコンポーネント（カンマ区切り）")
    p.add_argument("--requirements", default="", help="変更予定の要件（カンマ区切り）")
    p.add_argument("--format", choices=("md", "json", "tests"), default="md")

    p = sub.add_parser("matrix", help="トレーサビリティ・マトリクスを生成する")
    p.add_argument("--write", action="store_true", help="matrix_file に書き出す")

    sub.add_parser("status", help="要件・進行中の変更・検査結果の要約")
    sub.add_parser("doctor", help="hook・コミット前の検査・ゲートが、実際に働く状態かを確かめる")

    p = sub.add_parser("guard", help="本番コードの変更が変更記録の範囲内かを調べる")
    p.add_argument("--file", action="append", default=[])
    p.add_argument("--base", help="この git リビジョンとの分かれ目からの差分を調べる")
    p.add_argument("--staged", action="store_true", help="ステージ済みの差分を調べる")
    p.add_argument("--strict", action="store_true",
                   help="プルリクエスト用。進行中の変更記録も、この差分で追加・更新されたものだけを有効とみなす")

    p = sub.add_parser("change", help="変更記録（影響分析の記録）を扱う")
    csub = p.add_subparsers(dest="change_command", required=True)
    c = csub.add_parser("new")
    c.add_argument("--title", required=True)
    c.add_argument("--kind", required=True, choices=CHG_KINDS)
    c.add_argument("--components", default="")
    c.add_argument("--requirements", default="")
    c = csub.add_parser("close")
    c.add_argument("id")
    c = csub.add_parser("abandon")
    c.add_argument("id")
    c.add_argument("--reason", required=True)
    csub.add_parser("list")

    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            results = doctor(Path(args.root or DEFAULT_ROOT))
            for state, item, detail in results:
                print(f"[{state:<4}] {item}: {detail}")
            bad = sum(1 for state, _, _ in results if state == "NG")
            print("導入の確認: " + (f"{bad} 件の対処が必要です" if bad else "仕組みは働く状態です"))
            return 1 if bad else 0

        proj = Project(args.root)

        if args.command == "check":
            findings = check(proj, matrix=not args.no_matrix)
            if args.json:
                print(json.dumps([f.__dict__ for f in findings], ensure_ascii=False, indent=2))
            else:
                _print_findings(findings, show_all=args.all)
            return 1 if any(f.level == "error" for f in findings) else 0

        if args.command == "impact":
            files = list(args.files)
            if args.staged:
                files += git_changed(proj.root, staged=True)
            elif args.base:
                files += git_changed(proj.root, base=args.base)
            impact = compute_impact(proj, files=files, components=_csv(args.components),
                                    requirements=_csv(args.requirements))
            if args.format == "json":
                print(json.dumps(impact, ensure_ascii=False, indent=2))
            elif args.format == "tests":
                print("\n".join(impact["tests_direct"] + impact["tests_indirect"]))
            else:
                print(render_impact(proj, impact), end="")
            return 0

        if args.command == "matrix":
            text = render_matrix(proj)
            if args.write:
                path = proj.root / proj.cfg["matrix_file"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                print(f"{proj.cfg['matrix_file']} を書き出しました")
            else:
                print(text, end="")
            return 0

        if args.command == "status":
            print(render_status(proj), end="")
            return 0

        if args.command == "guard":
            diff_files = None
            files = list(args.file)
            if args.staged or args.base:
                diff_files = git_changed(proj.root, base=args.base, staged=args.staged)
                files += diff_files
            if args.strict and diff_files is None:
                raise ConfigError("--strict は --base か --staged と一緒に使います")
            problems = guard(proj, files, diff_files, strict=args.strict)
            for rel, message in problems:
                print(f"BLOCK {rel}: {message}")
            if not problems:
                print("変更はすべて、変更記録の範囲内です")
            return 1 if problems else 0

        if args.command == "change":
            if args.change_command == "new":
                path = change_new(proj, args.title, args.kind, _csv(args.components),
                                  _csv(args.requirements))
                print(f"{path.relative_to(proj.root).as_posix()} を作りました。"
                      "空欄（<!-- --> の部分）を埋めてから実装に進んでください")
                return 0
            if args.change_command == "close":
                return 0 if change_close(proj, args.id) else 1
            if args.change_command == "abandon":
                change = proj.changes.get(args.id)
                if change is None or change.status != "active":
                    raise ConfigError(f"進行中の変更記録 {args.id} がありません")
                path = proj.root / change.path
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(f"\n## 中止\n- 日付: {datetime.date.today().isoformat()}\n"
                                 f"- 理由: {args.reason}\n")
                _set_status(path, "abandoned")
                print(f"{args.id} を中止にしました")
                return 0
            if args.change_command == "list":
                for change in sorted(proj.changes.values(), key=lambda c: c.id):
                    print(f"{change.id} [{change.status}] {change.kind}: {change.title} "
                          f"（{', '.join(change.components) or '範囲なし'}）")
                return 0
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(TOOLS_DIR))
    setup_output()
    sys.exit(main())
