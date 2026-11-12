"""検査ツール自身のテスト。examples/shop を一時フォルダに写し、わざと壊して検出できるかを確かめる。

実行: python3 -m unittest discover -s tools/tests
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import tracekit  # noqa: E402
import verify  # noqa: E402

EXAMPLE = REPO / "examples" / "shop"
HOOKS = REPO / ".claude" / "hooks"


class ShopCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "shop"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self._tmp.cleanup()

    def project(self):
        return tracekit.Project(self.root)

    def codes(self, level="error", **kwargs):
        return [f.code for f in tracekit.check(self.project(), **kwargs) if f.level == level]

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def append(self, rel, text):
        with (self.root / rel).open("a", encoding="utf-8") as handle:
            handle.write(text)

    def replace(self, rel, old, new):
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def new_change(self, components=("COMP-ORDER",), requirements=("REQ-002",), kind="fix"):
        path = tracekit.change_new(self.project(), "テスト用の変更", kind,
                                   list(components), list(requirements))
        return path, tracekit.split_frontmatter(path.read_text(encoding="utf-8"))[0]["id"]

    def approved_change(self, *paths, approved_by="山田"):
        return tracekit.change_new(self.project(), "品質基盤の見直し", "chore", [], [],
                                   quality_base=list(paths), approved_by=approved_by)

    def fill(self, path):
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(r"<!--.*?-->", "記入済み", text, flags=re.S), encoding="utf-8")


class CheckTest(ShopCase):
    def test_example_has_no_findings(self):
        self.assertEqual(tracekit.check(self.project()), [])

    def test_undeclared_dependency_is_detected(self):
        self.append("src/inventory/stock.py", "\nfrom order.service import OrderService\n")
        self.assertIn("DEP-UNDECLARED", self.codes())

    def test_declared_cycle_is_detected(self):
        self.replace("trace/components.toml", "depends_on = []", 'depends_on = ["COMP-ORDER"]')
        self.assertIn("COMP-CYCLE", self.codes())

    def test_code_outside_any_component_is_detected(self):
        self.write("src/billing/invoice.py", "TOTAL = 0\n")
        self.assertIn("CODE-ORPHAN", self.codes())

    def test_overlapping_component_paths_are_detected(self):
        self.replace("trace/components.toml", 'paths = ["src/order/**"]', 'paths = ["src/**"]')
        self.assertIn("COMP-OVERLAP", self.codes())

    def test_acceptance_criterion_without_test_is_detected(self):
        self.replace("tests/integration/test_order_flow.py", "REQ-002/AC-2", "二重キャンセル")
        self.assertEqual(self.codes(matrix=False), ["REQ-AC-UNTESTED"])

    def test_missing_required_test_level_is_detected(self):
        (self.root / "tests/unit/test_stock.py").unlink()
        self.assertIn("REQ-LEVEL-MISSING", self.codes())

    def test_reference_to_unknown_requirement_or_criterion_is_detected(self):
        self.write("tests/unit/test_extra.py", '"""REQ-999 と REQ-001/AC-9"""\n')
        self.assertEqual(self.codes(matrix=False), ["TEST-DANGLING", "TEST-DANGLING"])

    def test_test_file_without_requirement_id_is_a_warning(self):
        self.write("tests/unit/test_misc.py", "X = 1\n")
        self.assertEqual(self.codes(), [])
        self.assertIn("TEST-UNLINKED", self.codes("warning"))

    def test_approved_requirement_needs_criteria_but_not_tests(self):
        self.write("docs/requirements/REQ-003.md",
                   "---\nid: REQ-003\ntitle: 新しい要件\nstatus: approved\npriority: should\n"
                   "components: [COMP-ORDER]\nverify: [unit]\n---\n\n## 受け入れ条件\n"
                   "- **AC-1**: WHEN x THEN y\n")
        self.assertEqual(self.codes(matrix=False), [])
        self.replace("docs/requirements/REQ-003.md", "- **AC-1**: WHEN x THEN y", "あとで書く")
        self.assertEqual(self.codes(matrix=False), ["REQ-NO-AC"])

    def test_broken_frontmatter_is_reported_not_crashed(self):
        self.write("docs/requirements/REQ-004.md", "id: REQ-004\n")
        self.assertIn("REQ-FORMAT", self.codes(matrix=False))

    def test_requirement_referring_to_unknown_component_is_detected(self):
        self.replace("docs/requirements/REQ-002.md", "COMP-INVENTORY", "COMP-STOCK")
        self.assertIn("REQ-COMP-UNKNOWN", self.codes(matrix=False))

    def test_component_without_requirement_is_a_warning(self):
        self.append("trace/components.toml",
                    '\n[[component]]\nid = "COMP-REPORT"\nname = "帳票"\nresponsibility = "帳票を出す"\n'
                    'paths = ["src/report/**"]\nstatus = "planned"\n')
        self.assertIn("COMP-NOREQ", self.codes("warning", matrix=False))

    def test_stale_matrix_is_detected_and_regenerated(self):
        self.replace("docs/requirements/REQ-001.md", "title: 在庫がある商品を注文できる", "title: 注文できる")
        self.assertEqual(self.codes(), ["MATRIX-STALE"])
        self.assertEqual(self.codes(matrix=False), [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tracekit.main(["--root", str(self.root), "matrix", "--write"]), 0)
        self.assertEqual(self.codes(), [])

    def test_broken_ledger_raises_config_error(self):
        self.write("trace/components.toml", "[[component]\n")
        with self.assertRaises(tracekit.ConfigError):
            self.project()

    def test_coarse_and_scattered_granularity_are_warned(self):
        for number in range(3, 8):
            self.write(f"docs/requirements/REQ-00{number}.md",
                       f"---\nid: REQ-00{number}\ntitle: 要件{number}\nstatus: approved\npriority: should\n"
                       "components: [COMP-ORDER]\nverify: [unit]\n---\n\n- **AC-1**: WHEN x THEN y\n")
        warnings = [f for f in tracekit.check(self.project(), matrix=False) if f.code == "GRAN-COARSE"]
        self.assertEqual(len(warnings), 2)  # どちらを変えても全要件に影響する


    def test_acceptance_criterion_in_unrecognized_format_is_an_error(self):
        for line in ("- **AC-3:** WHEN x THEN y", "- AC-3: WHEN x THEN y", "1. **AC-3**: WHEN x THEN y",
                     "**AC-3**: WHEN x THEN y", "- AC-3： WHEN x THEN y"):
            with self.subTest(line=line):
                path = self.root / "docs/requirements/REQ-002.md"
                original = path.read_text(encoding="utf-8")
                self.append("docs/requirements/REQ-002.md", line + "\n")
                self.assertEqual(self.codes(matrix=False), ["REQ-AC-FORMAT"])
                path.write_text(original, encoding="utf-8")

    def test_mentioning_a_criterion_in_prose_is_not_a_format_error(self):
        self.append("docs/requirements/REQ-002.md",
                    "\n## 検証の方針\nAC-1 は結合テストで確かめる。\n- AC-2 も同じ前提で確かめる\n")
        self.assertEqual(self.codes(matrix=False), [])

    def test_undeclared_dependency_reports_the_line_where_it_appears(self):
        self.append("src/inventory/stock.py", "\n\nfrom order.service import OrderService\n")
        lines = (self.root / "src/inventory/stock.py").read_text(encoding="utf-8").splitlines()
        found = [f for f in tracekit.check(self.project(), matrix=False) if f.code == "DEP-UNDECLARED"]
        self.assertEqual([f.where for f in found], [f"src/inventory/stock.py:{len(lines)}"])

    def test_dependency_is_found_whatever_way_the_reference_pattern_is_written(self):
        patterns = {
            "まとめて走査できる書き方": r"^\s*(from|import)\s+order\b",
            "文字列の端を指す書き方": r"\A\s*(from|import)\s+order\b",
            "式全体にフラグをかける書き方": r"(?i)^\s*(FROM|IMPORT)\s+order\b",
            "後方参照を使う書き方": r"^\s*(from|import)\s+(order)\b(?!\2x)",
        }
        self.append("src/inventory/stock.py", "\nX = 1\r\nfrom order.service import OrderService\r\n")
        for name, pattern in patterns.items():
            with self.subTest(name):
                ledger = self.root / "trace/components.toml"
                original = ledger.read_text(encoding="utf-8")
                self.replace("trace/components.toml", "'^\\s*(from|import)\\s+order\\b'", f"'{pattern}'")
                found = [f.code for f in tracekit.check(self.project(), matrix=False)]
                ledger.write_text(original, encoding="utf-8")
                self.assertEqual(found, ["DEP-UNDECLARED"])

    def test_declared_dependency_that_is_used_raises_nothing_and_unused_one_is_a_warning(self):
        self.assertEqual(tracekit.check(self.project(), matrix=False), [])
        self.replace("src/order/service.py", "from inventory", "from stockroom")
        self.assertIn("DEP-UNUSED", self.codes("warning", matrix=False))

    def test_level_required_by_implemented_requirement_must_be_run_by_a_gate(self):
        self.replace("trace/gates.toml",
                     'cmd = "PYTHONPATH=src python3 -m unittest discover -s tests/integration -q"',
                     'skip_reason = "遅いので"')
        found = [f for f in tracekit.check(self.project(), matrix=False) if f.code == "LEVEL-NOT-RUN"]
        self.assertEqual([f.level for f in found], ["error"])
        self.assertIn("integration", found[0].message)
        self.assertIn("REQ-001", found[0].message)

    def test_one_gate_may_declare_that_it_runs_several_levels(self):
        self.replace("trace/gates.toml",
                     'cmd = "PYTHONPATH=src python3 -m unittest discover -s tests/integration -q"',
                     'skip_reason = "unit のゲートがまとめて実行する"')
        self.replace("trace/gates.toml", 'id = "unit"', 'id = "unit"\nlevels = ["unit", "integration"]')
        self.assertEqual(self.codes(matrix=False), [])
        self.replace("trace/gates.toml", 'levels = ["unit", "integration"]', 'levels = ["unit", "smoke"]')
        self.assertIn("GATE-FORMAT", self.codes(matrix=False))

    def test_tests_at_a_level_no_gate_runs_are_a_warning(self):
        self.write("tests/unit/test_keep.py", '"""REQ-001/AC-1"""\n')
        self.replace("trace/config.toml", '[tests]', '[tests]\ne2e = ["tests/e2e/**"]')
        self.write("tests/e2e/test_flow.py", '"""REQ-001/AC-1"""\n')
        self.assertEqual(self.codes(matrix=False), [])
        self.assertIn("LEVEL-NOT-RUN", self.codes("warning", matrix=False))


    def test_change_record_that_lists_quality_base_without_an_approver_is_an_error(self):
        self.write("docs/changes/CHG-002.md",
                   "---\nid: CHG-002\ntitle: ゲートの見直し\nstatus: active\nkind: chore\n"
                   "requirements: []\ncomponents: []\nquality_base: [trace/gates.toml]\n---\n")
        self.assertEqual(self.codes(matrix=False), ["CHG-NO-APPROVER"])
        self.replace("docs/changes/CHG-002.md", "quality_base: [trace/gates.toml]\n",
                     "quality_base: [trace/gates.toml]\napproved_by: 山田\n")
        self.assertEqual(self.codes(matrix=False), [])


class GlobTest(unittest.TestCase):
    def test_double_star_spans_directories_and_single_star_does_not(self):
        self.assertTrue(tracekit.glob_to_re("src/order/**").match("src/order/a/b.py"))
        self.assertFalse(tracekit.glob_to_re("src/order/**").match("src/orders/a.py"))
        self.assertTrue(tracekit.glob_to_re("src/**/*_test.go").match("src/a_test.go"))
        self.assertTrue(tracekit.glob_to_re("src/**/*_test.go").match("src/x/y/a_test.go"))
        self.assertFalse(tracekit.glob_to_re("src/*.py").match("src/x/a.py"))
        self.assertTrue(tracekit.glob_to_re("src/order/").match("src/order/a.py"))


class ImpactTest(ShopCase):
    def test_change_in_depended_component_reaches_its_dependents(self):
        impact = tracekit.compute_impact(self.project(), files=["src/inventory/stock.py"])
        self.assertEqual(impact["direct_components"], ["COMP-INVENTORY"])
        self.assertEqual(impact["dependent_components"], {"COMP-ORDER": 1})
        self.assertEqual(impact["direct_requirements"], ["REQ-001", "REQ-002"])
        self.assertEqual(impact["tests_direct"],
                         ["tests/integration/test_order_flow.py", "tests/unit/test_stock.py"])

    def test_change_in_leaf_component_does_not_reach_what_it_depends_on(self):
        impact = tracekit.compute_impact(self.project(), files=["src/order/service.py"])
        self.assertEqual(impact["direct_components"], ["COMP-ORDER"])
        self.assertEqual(impact["dependent_components"], {})

    def test_requirements_of_dependents_only_are_indirect(self):
        self.replace("docs/requirements/REQ-002.md",
                     "components: [COMP-ORDER, COMP-INVENTORY]", "components: [COMP-ORDER]")
        impact = tracekit.compute_impact(self.project(), files=["src/inventory/stock.py"])
        self.assertEqual(impact["direct_requirements"], ["REQ-001"])
        self.assertEqual(impact["indirect_requirements"], ["REQ-002"])

    def test_unowned_and_requirement_and_test_files_are_classified(self):
        impact = tracekit.compute_impact(self.project(), files=[
            "src/billing/invoice.py", "docs/requirements/REQ-002.md", "tests/unit/test_stock.py"])
        self.assertEqual(impact["unowned_files"], ["src/billing/invoice.py"])
        # 要件を変えると、その要件を担うコンポーネントが起点になる。同じコンポーネントが担う REQ-001 も関係する
        self.assertEqual(impact["direct_components"], ["COMP-INVENTORY", "COMP-ORDER"])
        self.assertEqual(impact["direct_requirements"], ["REQ-001", "REQ-002"])
        self.assertIn("tests/unit/test_stock.py", impact["tests_direct"])

    def test_requirement_as_the_starting_point_reaches_the_components_that_carry_it(self):
        impact = tracekit.compute_impact(self.project(), requirements=["REQ-001"])
        self.assertEqual(impact["direct_components"], ["COMP-INVENTORY", "COMP-ORDER"])
        self.assertEqual(impact["via_requirements"],
                         {"COMP-INVENTORY": ["REQ-001"], "COMP-ORDER": ["REQ-001"]})
        self.assertEqual(impact["tests_direct"],
                         ["tests/integration/test_order_flow.py", "tests/unit/test_stock.py"])
        text = tracekit.render_impact(self.project(), impact)
        self.assertIn("COMP-ORDER 注文管理", text)
        self.assertIn("REQ-001 を担う", text)

    def test_requirement_as_the_starting_point_reaches_dependents_and_their_requirements(self):
        self.replace("docs/requirements/REQ-001.md",
                     "components: [COMP-ORDER, COMP-INVENTORY]", "components: [COMP-INVENTORY]")
        self.replace("docs/requirements/REQ-002.md",
                     "components: [COMP-ORDER, COMP-INVENTORY]", "components: [COMP-ORDER]")
        impact = tracekit.compute_impact(self.project(), requirements=["REQ-001"])
        self.assertEqual(impact["direct_components"], ["COMP-INVENTORY"])
        self.assertEqual(impact["dependent_components"], {"COMP-ORDER": 1})
        self.assertEqual(impact["direct_requirements"], ["REQ-001"])
        self.assertEqual(impact["indirect_requirements"], ["REQ-002"])

    def test_quality_base_files_in_the_change_are_called_out(self):
        impact = tracekit.compute_impact(self.project(), files=["trace/gates.toml", "README.md"])
        self.assertEqual(impact["quality_base_files"], ["trace/gates.toml"])
        self.assertIn("品質基盤", tracekit.render_impact(self.project(), impact))


class GuardTest(ShopCase):
    def test_production_edit_needs_an_active_change_covering_its_component(self):
        files = ["src/order/service.py", "src/inventory/stock.py"]
        self.assertEqual([f for f, _ in tracekit.guard(self.project(), files)], files)
        self.new_change(components=["COMP-ORDER"])
        self.assertEqual([f for f, _ in tracekit.guard(self.project(), files)],
                         ["src/inventory/stock.py"])

    def test_new_file_outside_any_component_is_blocked_even_with_active_change(self):
        self.new_change(components=["COMP-ORDER", "COMP-INVENTORY"])
        self.assertEqual(len(tracekit.guard(self.project(), ["src/billing/invoice.py"])), 1)

    def test_production_edit_is_blocked_while_requirement_is_draft(self):
        self.new_change(components=["COMP-ORDER"], requirements=["REQ-002"])
        self.replace("docs/requirements/REQ-002.md", "status: implemented", "status: draft")
        problems = tracekit.guard(self.project(), ["src/order/service.py"])
        self.assertEqual(len(problems), 1)
        self.assertIn("draft", problems[0][1])
        self.replace("docs/requirements/REQ-002.md", "status: draft", "status: approved")
        self.assertEqual(tracekit.guard(self.project(), ["src/order/service.py"]), [])

    def test_tests_and_docs_are_never_blocked(self):
        files = ["tests/unit/test_stock.py", "docs/requirements/REQ-001.md", "README.md"]
        self.assertEqual(tracekit.guard(self.project(), files), [])


    def test_changed_requirement_must_be_listed_in_a_change_record_of_the_diff(self):
        changed = ["docs/requirements/REQ-001.md"]
        self.assertEqual(tracekit.guard(self.project(), changed), [])  # 編集の時点（hook）では止めない
        problems = tracekit.guard(self.project(), changed, diff_files=changed)
        self.assertEqual([f for f, _ in problems], changed)
        self.new_change(components=["COMP-ORDER"], requirements=["REQ-001"])
        self.assertEqual(tracekit.guard(self.project(), changed, diff_files=changed), [])

    def test_draft_requirement_may_change_without_a_change_record(self):
        self.replace("docs/requirements/REQ-001.md", "status: implemented", "status: draft")
        changed = ["docs/requirements/REQ-001.md"]
        self.assertEqual(tracekit.guard(self.project(), changed, diff_files=changed), [])

    def test_strict_guard_ignores_active_change_that_the_diff_does_not_touch(self):
        path, _ = self.new_change(components=["COMP-ORDER"])
        rel = path.relative_to(self.root).as_posix()
        files = ["src/order/service.py"]
        self.assertEqual(tracekit.guard(self.project(), files, diff_files=files), [])
        problems = tracekit.guard(self.project(), files, diff_files=files, strict=True)
        self.assertEqual(len(problems), 1)
        self.assertIn("CHG-002", problems[0][1])
        self.assertEqual(tracekit.guard(self.project(), files, diff_files=files + [rel], strict=True), [])


    def test_quality_base_change_needs_a_change_record_that_names_who_approved_it(self):
        base = ["trace/gates.toml", "trace/config.toml", "trace/components.toml", "tools/verify.py", "tools/tests/test_tracekit.py",
                "tools/git-hooks/pre-commit", ".github/workflows/quality.yml", ".claude/hooks/pre_edit.py",
                ".claude/settings.json", ".claude/skills/develop/SKILL.md", "AGENTS.md", "CLAUDE.md"]
        self.assertEqual([f for f, _ in tracekit.guard(self.project(), base)], base)
        self.assertIn("承認", tracekit.guard(self.project(), base)[0][1])
        self.new_change(components=["COMP-ORDER"])  # 承認の記録がない変更記録では通らない
        self.assertEqual([f for f, _ in tracekit.guard(self.project(), base)], base)
        self.approved_change("trace/gates.toml", "tools/**")
        self.assertEqual([f for f, _ in tracekit.guard(self.project(), base)],
                         ["trace/config.toml", "trace/components.toml",
                          ".github/workflows/quality.yml", ".claude/hooks/pre_edit.py",
                          ".claude/settings.json", ".claude/skills/develop/SKILL.md", "AGENTS.md", "CLAUDE.md"])

    def test_quality_base_cannot_be_approved_by_nobody(self):
        with self.assertRaises(tracekit.ConfigError):
            self.approved_change("trace/gates.toml", approved_by=" ")

    def test_projects_may_add_their_own_quality_base_but_cannot_remove_the_built_in_one(self):
        self.assertEqual(tracekit.guard(self.project(), ["ruff.toml"]), [])
        self.replace("trace/config.toml", 'source_roots = ["src"]',
                     'source_roots = ["src"]\nquality_base = ["ruff.toml"]')
        self.assertEqual([f for f, _ in tracekit.guard(self.project(), ["ruff.toml", "trace/gates.toml"])],
                         ["ruff.toml", "trace/gates.toml"])

    def test_outside_git_quality_base_is_not_guarded_until_the_first_component_is_declared(self):
        # 導入の途中（ゲートや置き場所を設定している段階）まで承認の記録を求めると、導入そのものが進まない。
        # git の管理下では、手元の台帳ではなく、コミット済みの台帳で判定する（GitDiffTest）
        self.write("trace/components.toml", "")
        self.assertEqual(tracekit.guard(self.project(), ["trace/gates.toml", "tools/verify.py"]), [])


class ChangeLifecycleTest(ShopCase):
    def test_close_requires_filled_sections_then_passes_gates(self):
        path, change_id = self.new_change()
        self.assertEqual(change_id, "CHG-002")
        log = []
        self.assertFalse(tracekit.change_close(self.project(), change_id, echo=log.append))
        self.assertTrue(any("目的" in line for line in log))
        self.fill(path)
        self.assertTrue(tracekit.change_close(self.project(), change_id, echo=log.append))
        text = path.read_text(encoding="utf-8")
        self.assertIn("status: done", text)
        self.assertIn("## 検証結果", text)
        self.assertEqual(self.project().changes[change_id].status, "done")

    def test_close_is_refused_while_a_gate_fails(self):
        path, change_id = self.new_change()
        self.fill(path)
        self.replace("src/inventory/stock.py", "return current - quantity", "return current")
        self.assertFalse(tracekit.change_close(self.project(), change_id, echo=lambda _: None))
        self.assertEqual(self.project().changes[change_id].status, "active")

    def test_close_is_refused_while_requirement_is_not_implemented(self):
        self.replace("docs/requirements/REQ-002.md", "status: implemented", "status: approved")
        path, change_id = self.new_change()
        self.fill(path)
        log = []
        self.assertFalse(tracekit.change_close(self.project(), change_id, echo=log.append))
        self.assertTrue(any("REQ-002" in line for line in log))

    def test_feature_change_without_requirement_cannot_be_created(self):
        with self.assertRaises(tracekit.ConfigError):
            tracekit.change_new(self.project(), "要件なし", "feature", ["COMP-ORDER"], [])


class VerifyTest(ShopCase):
    def test_example_passes(self):
        _, ok = verify.run_gates(self.root, echo=lambda _: None)
        self.assertTrue(ok)

    def test_gate_without_command_or_reason_fails(self):
        self.append("trace/gates.toml", '\n[[gate]]\nid = "contract"\nname = "契約テスト"\n')
        results, ok = verify.run_gates(self.root, echo=lambda _: None)
        self.assertFalse(ok)
        self.assertEqual(results[-1]["status"], "UNSET")

    def test_failing_command_fails(self):
        self.append("trace/gates.toml", '\n[[gate]]\nid = "x"\nname = "失敗"\ncmd = "exit 3"\n')
        results, ok = verify.run_gates(self.root, echo=lambda _: None)
        self.assertFalse(ok)
        self.assertEqual(results[-1]["status"], "FAIL")


    def test_skip_needs_the_name_of_who_approved_it(self):
        self.append("trace/gates.toml", '\n[[gate]]\nid = "x"\nname = "外したい"\nskip_reason = "時間がない"\n')
        results, ok = verify.run_gates(self.root, echo=lambda _: None)
        self.assertFalse(ok)
        self.assertEqual(results[-1]["status"], "FAIL")
        self.assertIn("skip_approved_by", results[-1]["detail"])

    def test_skip_expires_at_its_deadline(self):
        self.append("trace/gates.toml", '\n[[gate]]\nid = "x"\nname = "一時的に外す"\nskip_reason = "道具の移行中"\n'
                    'skip_kind = "waiver"\nskip_approved_by = "山田"\nskip_until = "2999-12-31"\n')
        results, ok = verify.run_gates(self.root, echo=lambda _: None)
        self.assertTrue(ok)
        self.assertEqual(results[-1]["status"], "SKIP")
        self.assertIn("山田", results[-1]["detail"])
        self.assertIn("2999-12-31", results[-1]["detail"])
        self.replace("trace/gates.toml", 'skip_until = "2999-12-31"', "skip_until = 2020-01-31")  # TOML の日付でも書ける
        results, ok = verify.run_gates(self.root, echo=lambda _: None)
        self.assertFalse(ok)
        self.assertEqual(results[-1]["status"], "FAIL")
        self.assertIn("期限", results[-1]["detail"])

    def test_deadline_that_is_not_a_date_is_a_configuration_error(self):
        self.append("trace/gates.toml", '\n[[gate]]\nid = "x"\nname = "x"\nskip_reason = "r"\n'
                    'skip_kind = "waiver"\nskip_approved_by = "山田"\nskip_until = "来月"\n')
        results, ok = verify.run_gates(self.root, echo=lambda _: None)
        self.assertFalse(ok)
        self.assertEqual(results[-1]["id"], "gates")

    def gate_x(self, body):
        self.append("trace/gates.toml", f'\n[[gate]]\nid = "x"\nname = "x"\n{body}')
        results, ok = verify.run_gates(self.root, echo=lambda _: None)
        return results[-1]["status"], results[-1]["detail"], ok

    def test_required_gate_cannot_be_skipped_whoever_approves(self):
        status, detail, ok = self.gate_x('required = true\nskip_reason = "時間がない"\nskip_kind = "waiver"\n'
                                         'skip_approved_by = "山田"\nskip_until = "2999-12-31"\n')
        self.assertEqual((status, ok), ("FAIL", False))
        self.assertIn("required", detail)

    def test_skip_must_say_whether_there_is_nothing_to_verify_or_it_is_a_temporary_waiver(self):
        status, detail, _ = self.gate_x('skip_reason = "r"\nskip_approved_by = "山田"\n')
        self.assertEqual(status, "FAIL")
        self.assertIn("skip_kind", detail)

    def test_temporary_waiver_needs_a_deadline(self):
        status, detail, _ = self.gate_x('skip_reason = "道具の移行中"\nskip_kind = "waiver"\nskip_approved_by = "山田"\n')
        self.assertEqual(status, "FAIL")
        self.assertIn("skip_until", detail)

    def test_gate_with_nothing_to_verify_needs_no_deadline(self):
        status, detail, ok = self.gate_x('skip_reason = "画面がない"\nskip_kind = "not_applicable"\n'
                                         'skip_approved_by = "山田"\n')
        self.assertEqual((status, ok), ("SKIP", True))
        self.assertIn("対象なし", detail)

    def test_skipped_gates_are_reported_as_not_verified(self):
        report = self.root / "out" / "gates.json"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(verify.main(["--root", str(self.root), "--report", str(report)]), 0)
        self.assertIn("検証していません", out.getvalue().splitlines()[-1])
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["unverified"], ["types", "architecture", "e2e"])

    def test_report_file_records_every_gate(self):
        report = self.root / "out" / "gates.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(["--root", str(self.root), "--report", str(report)]), 0)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual([g["id"] for g in data["gates"]][:2], ["trace", "static"])
        self.assertEqual({g["status"] for g in data["gates"]}, {"PASS", "SKIP"})


class CliTest(ShopCase):
    def run_tool(self, *args, **env):
        return subprocess.run([sys.executable, str(self.root / "tools" / args[0]), *args[1:]],
                              capture_output=True, env=dict(os.environ, **env))

    def setUp(self):
        super().setUp()
        shutil.copytree(REPO / "tools", self.root / "tools",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))

    def test_output_does_not_crash_on_a_console_that_cannot_show_every_character(self):
        for args in (("tracekit.py", "matrix"), ("tracekit.py", "impact", "--components", "COMP-ORDER"),
                     ("tracekit.py", "check"), ("tracekit.py", "status"), ("verify.py",)):
            with self.subTest(args=args):
                done = self.run_tool(*args, PYTHONIOENCODING="cp932")
                self.assertEqual(done.returncode, 0, done.stderr.decode("cp932", "replace"))

    def test_many_warnings_of_one_kind_are_summarized_unless_asked(self):
        for number in range(30):
            self.write(f"tests/unit/test_legacy_{number}.py", "X = 1\n")
        short = self.run_tool("tracekit.py", "check").stdout.decode("utf-8")
        self.assertLess(short.count("TEST-UNLINKED"), 15)
        self.assertIn("警告 30 件", short)
        full = self.run_tool("tracekit.py", "check", "--all").stdout.decode("utf-8")
        self.assertEqual(full.count("WARN  TEST-UNLINKED"), 30)

    def test_doctor_fails_and_names_what_is_not_enabled(self):
        done = self.run_tool("tracekit.py", "doctor")
        text = done.stdout.decode("utf-8")
        self.assertEqual(done.returncode, 1)
        self.assertIn("[WARN] Claude Code の hook", text)
        self.assertIn("[NG  ] git", text)  # 一時フォルダは git の管理下にない


class HookTest(ShopCase):
    def run_hook(self, name, payload):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root))
        return subprocess.run([sys.executable, str(HOOKS / name)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)

    def edit(self, rel):
        return {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": str(self.root),
                "tool_input": {"file_path": str(self.root / rel)}}

    def test_pre_edit_blocks_production_code_without_change_record(self):
        done = self.run_hook("pre_edit.py", self.edit("src/order/service.py"))
        self.assertEqual(done.returncode, 2)
        self.assertIn("COMP-ORDER", done.stderr)

    def test_pre_edit_allows_production_code_inside_active_change(self):
        self.new_change(components=["COMP-ORDER"])
        self.assertEqual(self.run_hook("pre_edit.py", self.edit("src/order/service.py")).returncode, 0)
        self.assertEqual(self.run_hook("pre_edit.py", self.edit("src/inventory/stock.py")).returncode, 2)

    def test_pre_edit_allows_tests_docs_and_files_outside_project(self):
        for rel in ("tests/unit/test_stock.py", "docs/requirements/REQ-009.md", "docs/architecture.md"):
            self.assertEqual(self.run_hook("pre_edit.py", self.edit(rel)).returncode, 0, rel)
        outside = {"tool_name": "Write", "cwd": str(self.root), "tool_input": {"file_path": "/tmp/x.py"}}
        self.assertEqual(self.run_hook("pre_edit.py", outside).returncode, 0)

    def test_pre_edit_blocks_production_code_while_ledger_is_broken_but_allows_fixing_it(self):
        self.write("trace/components.toml", "[[component]\n")
        self.assertEqual(self.run_hook("pre_edit.py", self.edit("src/order/service.py")).returncode, 2)
        self.assertEqual(self.run_hook("pre_edit.py", self.edit("trace/components.toml")).returncode, 0)
        # 台帳を壊しておけば、検査ツールやゲートを自由に書き換えられる、という抜け道にしない
        self.assertEqual(self.run_hook("pre_edit.py", self.edit("trace/gates.toml")).returncode, 2)
        self.assertEqual(self.run_hook("pre_edit.py", self.edit("tools/verify.py")).returncode, 2)

    def test_hooks_answer_a_probe_so_that_doctor_can_tell_they_are_really_called(self):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root), TRACEKIT_PROBE="1")
        for name, code in (("pre_edit.py", 2), ("on_stop.py", 0), ("session_start.py", 0)):
            done = subprocess.run([sys.executable, str(HOOKS / name)], input="{}", capture_output=True,
                                  text=True, env=env)
            self.assertEqual(done.returncode, code, name)
            self.assertIn(tracekit.PROBE_MARK, done.stdout + done.stderr, name)

    def test_pre_edit_blocks_quality_base_until_an_approved_change_record_covers_it(self):
        done = self.run_hook("pre_edit.py", self.edit("trace/gates.toml"))
        self.assertEqual(done.returncode, 2)
        self.assertIn("承認", done.stderr)
        self.approved_change("trace/gates.toml")
        self.assertEqual(self.run_hook("pre_edit.py", self.edit("trace/gates.toml")).returncode, 0)
        self.assertEqual(self.run_hook("pre_edit.py", self.edit("tools/verify.py")).returncode, 2)

    def test_stop_blocks_once_when_ledger_is_inconsistent(self):
        self.write("tests/unit/test_extra.py", '"""REQ-999"""\n')
        done = self.run_hook("on_stop.py", {"hook_event_name": "Stop", "stop_hook_active": False})
        self.assertEqual(done.returncode, 2)
        self.assertIn("TEST-DANGLING", done.stderr)
        again = self.run_hook("on_stop.py", {"hook_event_name": "Stop", "stop_hook_active": True})
        self.assertEqual(again.returncode, 0)

    def test_stop_passes_when_consistent(self):
        done = self.run_hook("on_stop.py", {"hook_event_name": "Stop", "stop_hook_active": False})
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_session_start_reports_active_changes(self):
        self.new_change(components=["COMP-ORDER"])
        done = self.run_hook("session_start.py", {"hook_event_name": "SessionStart"})
        self.assertEqual(done.returncode, 0)
        self.assertIn("CHG-002", done.stdout)


@unittest.skipUnless(shutil.which("git"), "git がありません")
class GitDiffTest(ShopCase):
    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t",
                        "-c", "user.email=t@example.invalid", *args],
                       check=True, capture_output=True)

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return tracekit.main(["--root", str(self.root), *args])

    def test_guard_on_diff_requires_change_record_in_the_same_diff(self):
        self.append("src/order/service.py", "\n# 変更\n")
        self.assertEqual(self.cli("guard", "--base", "HEAD"), 1)
        path, change_id = self.new_change(components=["COMP-ORDER"])
        self.assertEqual(self.cli("guard", "--base", "HEAD"), 0)
        self.fill(path)
        self.assertTrue(tracekit.change_close(self.project(), change_id, echo=lambda _: None))
        self.assertEqual(self.cli("guard", "--base", "HEAD"), 0)  # 完了済みでも、同じ差分に含まれていれば有効

    def test_old_done_change_does_not_cover_new_edits(self):
        self.append("src/order/service.py", "\n# 変更\n")
        self.git("add", "-A")
        self.assertEqual(self.cli("guard", "--staged"), 1)  # CHG-001 は完了済みで、今回の差分に含まれない

    def test_base_that_moved_ahead_does_not_blame_this_branch_for_its_changes(self):
        self.git("checkout", "-q", "-b", "feature")
        self.append("README.md", "\n文書だけの変更\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "feature")
        self.git("checkout", "-q", "-")
        self.append("src/order/service.py", "\n# 基点の側の変更\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base moved")
        base = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.git("checkout", "-q", "feature")
        self.assertEqual(tracekit.git_changed(self.root, base=base), ["README.md"])
        self.assertEqual(self.cli("guard", "--base", base), 0)

    def test_file_with_non_ascii_name_is_not_missed(self):
        self.write("src/order/注文補助.py", "X = 1\n")
        self.assertIn("src/order/注文補助.py", tracekit.git_changed(self.root, base="HEAD"))
        self.git("add", "-A")
        self.assertEqual(tracekit.git_changed(self.root, staged=True), ["src/order/注文補助.py"])
        self.assertEqual(self.cli("guard", "--staged"), 1)

    def test_strict_guard_requires_the_change_record_to_be_part_of_the_diff(self):
        self.new_change(components=["COMP-ORDER"])
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "変更記録を残したまま取り込んだ")
        self.append("src/order/service.py", "\n# 別の変更\n")
        self.assertEqual(self.cli("guard", "--base", "HEAD"), 0)
        self.assertEqual(self.cli("guard", "--base", "HEAD", "--strict"), 1)
        self.append("docs/changes/CHG-002.md", "\n追記\n")
        self.assertEqual(self.cli("guard", "--base", "HEAD", "--strict"), 0)

    def test_requirement_change_in_diff_needs_a_change_record(self):
        self.replace("docs/requirements/REQ-001.md", "在庫が注文数だけ減る", "在庫が減る")
        self.assertEqual(self.cli("guard", "--base", "HEAD"), 1)
        self.new_change(components=["COMP-ORDER"], requirements=["REQ-001"])
        self.assertEqual(self.cli("guard", "--base", "HEAD"), 0)

    def test_gitignore_is_honoured_when_project_is_a_subdirectory_of_the_repository(self):
        sub = self.root / "services" / "app"
        shutil.copytree(EXAMPLE, sub, ignore=shutil.ignore_patterns("__pycache__"))
        (sub / ".gitignore").write_text("generated/\n", encoding="utf-8")
        (sub / "src" / "generated").mkdir()
        (sub / "src" / "generated" / "out.py").write_text("X = 1\n", encoding="utf-8")
        self.assertEqual(tracekit.check(tracekit.Project(sub)), [])

    def test_close_records_the_commit_that_was_verified(self):
        path, change_id = self.new_change()
        self.fill(path)
        self.assertTrue(tracekit.change_close(self.project(), change_id, echo=lambda _: None))
        head = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        text = path.read_text(encoding="utf-8")
        self.assertIn(head, text)
        self.assertIn("未コミットの変更あり", text)

    def test_stop_hook_reports_production_change_made_outside_the_edit_tools(self):
        self.append("src/order/service.py", "\n# シェルから書き換えた\n")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root))
        payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})
        done = subprocess.run([sys.executable, str(HOOKS / "on_stop.py")], input=payload,
                              capture_output=True, text=True, env=env, encoding="utf-8")
        self.assertEqual(done.returncode, 2)
        self.assertIn("src/order/service.py", done.stderr)
        path, change_id = self.new_change(components=["COMP-ORDER"])
        self.fill(path)
        self.assertTrue(tracekit.change_close(self.project(), change_id, echo=lambda _: None))
        done = subprocess.run([sys.executable, str(HOOKS / "on_stop.py")], input=payload,
                              capture_output=True, text=True, env=env, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr)  # 完了済みでも、同じ未コミットの差分にあれば有効

    def test_weakening_a_gate_is_stopped_until_a_person_approves_it(self):
        # レビューで指摘された抜け道: テストのゲートを、何も確かめないコマンドに差し替える
        self.replace("trace/gates.toml",
                     'cmd = "PYTHONPATH=src python3 -m unittest discover -s tests/unit -q"', 'cmd = "true"')
        self.assertEqual(self.cli("guard", "--base", "HEAD"), 1)
        self.assertEqual(self.cli("guard", "--base", "HEAD", "--strict"), 1)
        self.git("add", "-A")
        self.assertEqual(self.cli("guard", "--staged"), 1)
        self.approved_change("trace/gates.toml")
        self.git("add", "-A")
        self.assertEqual(self.cli("guard", "--staged"), 0)
        self.assertEqual(self.cli("guard", "--base", "HEAD", "--strict"), 0)

    def test_approval_left_open_does_not_cover_a_later_pull_request(self):
        self.approved_change("trace/gates.toml")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "承認つきの変更記録を閉じ忘れた")
        self.append("trace/gates.toml", "\n# 別の変更\n")
        self.assertEqual(self.cli("guard", "--base", "HEAD", "--strict"), 1)

    def test_stop_hook_reports_quality_base_change_made_outside_the_edit_tools(self):
        self.append("trace/gates.toml", "\n# シェルから書き換えた\n")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root))
        payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})
        done = subprocess.run([sys.executable, str(HOOKS / "on_stop.py")], input=payload,
                              capture_output=True, text=True, env=env, encoding="utf-8")
        self.assertEqual(done.returncode, 2)
        self.assertIn("trace/gates.toml", done.stderr)

    def test_emptying_the_component_ledger_does_not_switch_the_guard_off(self):
        # 再レビューで指摘された抜け道: 保護が働く条件（コンポーネントがある）を、変更する側の台帳で満たさなくする
        self.write("trace/components.toml", "")
        self.replace("trace/gates.toml",
                     'cmd = "PYTHONPATH=src python3 -m unittest discover -s tests/unit -q"', 'cmd = "true"')
        self.assertEqual(self.project().components, {})
        blocked = [f for f, _ in tracekit.guard(self.project(), ["trace/gates.toml"])]  # 編集の時点（hook）
        self.assertEqual(blocked, ["trace/gates.toml"])
        self.assertEqual(self.cli("guard", "--base", "HEAD", "--strict"), 1)
        self.git("add", "-A")
        self.assertEqual(self.cli("guard", "--staged"), 1)

    def test_guard_starts_once_a_ledger_with_components_is_committed(self):
        original = (self.root / "trace/components.toml").read_text(encoding="utf-8")
        self.write("trace/components.toml", "")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "導入の途中。コンポーネントはまだない")
        self.write("trace/components.toml", original)  # 最初の宣言と、ゲートの設定。まだ承認の記録は求めない
        self.append("trace/gates.toml", "\n# 設定の調整\n")
        self.assertEqual(self.cli("guard", "--base", "HEAD", "--strict"), 0)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "コンポーネントを宣言した")
        self.append("trace/gates.toml", "\n# 導入のあとの変更\n")
        self.assertEqual(self.cli("guard", "--base", "HEAD", "--strict"), 1)

    def test_changing_the_component_ledger_needs_approval_like_any_other_quality_base(self):
        self.replace("trace/components.toml", "depends_on = []", 'depends_on = ["COMP-ORDER"]')
        self.assertEqual(self.cli("guard", "--base", "HEAD"), 1)
        self.approved_change("trace/components.toml")
        self.assertEqual(self.cli("guard", "--base", "HEAD"), 0)

    def doctor(self, **kwargs):
        return {item: state for state, item, _ in tracekit.doctor(self.root, **kwargs)}

    def install_tools(self):
        # hook は、プロジェクトの tools/ にある検査ツールを読み込む
        for name in ("tracekit.py", "verify.py"):
            (self.root / "tools").mkdir(exist_ok=True)
            shutil.copy(REPO / "tools" / name, self.root / "tools" / name)

    def install_hooks(self):
        self.install_tools()
        shutil.copytree(REPO / ".claude" / "hooks", self.root / ".claude" / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)

    def settings(self, text):
        self.install_hooks()
        self.write(".claude/settings.json", text)
        return self.doctor()["Claude Code の hook"]

    @unittest.skipIf(os.name == "nt", "hook を実際に呼ぶ確認は、Windows では行わない")
    def test_doctor_calls_each_hook_and_is_not_fooled_by_a_command_that_only_names_it(self):
        good = (REPO / ".claude" / "settings.json").read_text(encoding="utf-8")
        self.assertEqual(self.settings(good), "OK")
        fake = good.replace('python3 \\"$CLAUDE_PROJECT_DIR/.claude/hooks/pre_edit.py\\"', "echo pre_edit.py")
        self.assertNotEqual(fake, good)
        self.assertEqual(self.settings(fake), "WARN")
        shutil.rmtree(self.root / ".claude" / "hooks")  # 設定は正しいが、hook の本体がない
        self.write(".claude/settings.json", good)
        self.assertEqual(self.doctor()["Claude Code の hook"], "WARN")

    @unittest.skipIf(os.name == "nt", "実行権限は Windows では見ない")
    def test_doctor_flags_a_commit_hook_that_lost_its_executable_bit(self):
        shutil.copytree(REPO / "tools" / "git-hooks", self.root / "tools" / "git-hooks")
        self.git("config", "core.hooksPath", "tools/git-hooks")
        hook = self.root / "tools" / "git-hooks" / "pre-commit"
        hook.chmod(0o644)  # ZIP の展開やコピーで落ちた状態
        self.assertEqual(self.doctor()["コミット前の検査の実行権限"], "NG")
        hook.chmod(0o755)
        self.assertEqual(self.doctor()["コミット前の検査の実行権限"], "OK")

    def test_doctor_for_ci_checks_what_can_be_checked_there(self):
        local = self.doctor()
        ci = self.doctor(ci=True)
        self.assertEqual(local["コミット前の検査"], "NG")  # clone ごとの設定。CI の環境にはない
        self.assertNotIn("コミット前の検査", ci)
        self.assertNotIn("NG", ci.values())
        self.replace("trace/config.toml", 'source_roots = ["src"]', "source_roots = []")
        self.assertEqual(self.doctor(ci=True)["本番コードの置き場所"], "NG")
        self.assertEqual(self.cli("doctor", "--ci"), 1)

    def test_doctor_rejects_source_roots_that_cover_nothing(self):
        self.replace("trace/config.toml", 'source_roots = ["src"]', "source_roots = []")
        self.assertEqual(self.doctor()["本番コードの置き場所"], "NG")
        self.replace("trace/config.toml", "source_roots = []", 'source_roots = ["lib"]')
        (self.root / "lib").mkdir()
        self.assertEqual(self.doctor()["本番コードの置き場所"], "WARN")  # 場所はあるが、本番コードが1件もない

    def test_doctor_reads_hook_settings_instead_of_searching_for_file_names(self):
        good = (REPO / ".claude" / "settings.json").read_text(encoding="utf-8")
        self.assertEqual(self.settings(good), "OK")
        self.assertEqual(self.settings(good[:-3]), "WARN")  # 壊れた JSON。名前は全部書いてあるが、働かない
        self.assertEqual(self.settings(good.replace('"PreToolUse"', '"PostToolUse"')), "WARN")  # 編集の後では止まらない
        self.assertEqual(self.settings(good.replace("Edit|Write|NotebookEdit", "Bash")), "WARN")  # 編集に掛かっていない
        self.assertEqual(self.settings(good.replace("Edit|Write|NotebookEdit", "Edit")), "WARN")  # Write が抜けている
        self.assertEqual(self.settings(good.replace('"matcher": "Edit|Write|NotebookEdit",', "")), "OK")  # すべての道具に掛かる

    def test_doctor_wants_ci_to_run_both_the_guard_and_the_gates(self):
        workflow = (REPO / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
        self.write(".github/workflows/quality.yml", workflow)
        self.assertEqual(self.doctor()["CI"], "OK")
        self.write(".github/workflows/quality.yml", "# python3 tools/verify.py と tracekit.py guard を実行する予定\n")
        self.assertEqual(self.doctor()["CI"], "INFO")
        self.write(".github/workflows/quality.yml", "steps:\n  - run: python3 tools/verify.py\n")
        self.assertEqual(self.doctor()["CI"], "INFO")  # guard がない
        self.write(".github/workflows/quality.yml", 'steps:\n  - run: echo "python3 tools/verify.py"\n'
                   '  - run: echo "python3 tools/tracekit.py guard --base HEAD --strict"\n')
        self.assertEqual(self.doctor()["CI"], "INFO")  # 表示しているだけで、実行していない

    def test_doctor_tells_whether_each_safeguard_is_actually_enabled(self):
        before = self.doctor()
        self.assertEqual(before["コミット前の検査"], "NG")
        self.assertEqual(before["Claude Code の hook"], "WARN")
        self.assertEqual(before["品質ゲートの設定"], "OK")
        shutil.copytree(REPO / "tools" / "git-hooks", self.root / "tools" / "git-hooks")
        # 実行権限は配り方しだいで落ちる（ZIP の展開など）。ここでは付いている状態を作り、落ちた状態は別のテストで見る
        (self.root / "tools" / "git-hooks" / "pre-commit").chmod(0o755)
        self.install_tools()
        self.git("config", "core.hooksPath", "tools/git-hooks")
        shutil.copytree(REPO / ".claude", self.root / ".claude",
                        ignore=shutil.ignore_patterns("__pycache__", "settings.json"))
        self.assertEqual(self.doctor()["Claude Code の hook"], "WARN")  # hook の本体だけでは有効にならない
        shutil.copy(REPO / ".claude" / "settings.json", self.root / ".claude" / "settings.json")
        after = self.doctor()
        self.assertEqual(after["コミット前の検査"], "OK")
        self.assertEqual(after["Claude Code の hook"], "OK")
        self.assertEqual(set(after.values()) - {"OK", "INFO"}, set())

    def test_doctor_flags_unset_gate_and_missing_source_root(self):
        self.append("trace/gates.toml", '\n[[gate]]\nid = "contract"\nname = "契約テスト"\n')
        self.replace("trace/config.toml", 'source_roots = ["src"]', 'source_roots = ["app"]')
        state = self.doctor()
        self.assertEqual(state["品質ゲートの設定"], "NG")
        self.assertEqual(state["本番コードの置き場所"], "NG")

    def test_impact_from_diff(self):
        self.append("src/inventory/stock.py", "\n# 変更\n")
        impact = tracekit.compute_impact(self.project(),
                                         files=tracekit.git_changed(self.root, base="HEAD"))
        self.assertEqual(impact["direct_components"], ["COMP-INVENTORY"])


class AdapterExamplesTest(unittest.TestCase):
    def test_every_toml_example_can_be_pasted_as_it_is(self):
        text = (REPO / "adapters" / "README.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```toml\n(.*?)```", text, re.S)
        self.assertGreaterEqual(len(blocks), 8)
        gate_blocks = 0
        for block in blocks:
            with self.subTest(block=block.splitlines()[0]):
                data = tomllib.loads(block)
                if "gate" not in data:
                    continue
                gate_blocks += 1
                with tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp) / "trace").mkdir()
                    (Path(tmp) / "trace" / "gates.toml").write_text(block, encoding="utf-8")
                    for gate in tracekit.load_gates(Path(tmp)):
                        # 例の承認者は空にしてある（貼ったまま、誰も承認していないのに通ることがないように）
                        if gate.get("skip_reason") and not gate.get("skip_approved_by"):
                            gate["skip_approved_by"] = "承認した人"
                        self.assertIn(tracekit.gate_state(gate)[0], ("RUN", "SKIP"), gate["id"])
        self.assertGreaterEqual(gate_blocks, 4)


if __name__ == "__main__":
    unittest.main()
