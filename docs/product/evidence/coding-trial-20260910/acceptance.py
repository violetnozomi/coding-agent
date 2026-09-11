"""Frozen organizer checks, outside Agent workspaces and without model calls."""
from __future__ import annotations

import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

task, project_arg = sys.argv[1:3]
project = Path(project_arg).resolve()
sys.path.insert(0, str(project))


class FeatureChecks(unittest.TestCase):
    def invoke(self, rows, expected, grouped=True):
        with tempfile.TemporaryDirectory(prefix="csv-check-") as temporary:
            path = Path(temporary) / "expenses with spaces.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["category", "amount"])
                writer.writerows(rows)
            args = [sys.executable, "-B", "-m", "expensebook", str(path)]
            if grouped:
                args.append("--by-category")
            result = subprocess.run(args, cwd=project, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            if grouped:
                self.assertEqual(list(csv.reader(io.StringIO(result.stdout))), [["category", "total"], *expected])
            else:
                self.assertEqual(result.stdout.strip(), expected)

    def test_exact_decimal(self):
        self.invoke([("food", "0.10"), ("food", "0.20")], [["food", "0.30"]])

    def test_trim_blank_and_sort(self):
        self.invoke([(" z ", "2"), ("a", "1"), ("z", "3"), (" ", "4"), ("", "5")],
                    [["a", "1.00"], ["z", "5.00"], ["未分类", "9.00"]])

    def test_refunds(self):
        self.invoke([("food", "4.25"), ("food", "-5.50")], [["food", "-1.25"]])

    def test_csv_quoting_and_unicode(self):
        self.invoke([("餐饮,外卖", "12.30"), ("交通", "2.00")], [["交通", "2.00"], ["餐饮,外卖", "12.30"]])

    def test_header_only(self):
        self.invoke([], [])

    def test_old_default(self):
        self.invoke([("a", "4.20"), ("b", "-1.10")], "3.10", grouped=False)


class BugChecks(unittest.TestCase):
    def merge(self, base, override):
        from layered_settings import merge_settings
        return merge_settings(base, override)

    def test_nested_sibling(self):
        self.assertEqual(self.merge({"db": {"host": "localhost", "port": 5432}}, {"db": {"port": 5433}}),
                         {"db": {"host": "localhost", "port": 5433}})

    def test_deep_and_new_keys(self):
        self.assertEqual(self.merge({"a": {"b": {"x": 1, "y": 2}}, "left": 7}, {"a": {"b": {"y": 3}}, "right": 8}),
                         {"a": {"b": {"x": 1, "y": 3}}, "left": 7, "right": 8})

    def test_replace_and_falsy_values(self):
        self.assertEqual(self.merge({"list": [1, 2], "nil": {}, "flag": True, "n": 4, "s": "x", "obj": 3},
                                    {"list": [], "nil": None, "flag": False, "n": 0, "s": "", "obj": {"k": 1}}),
                         {"list": [], "nil": None, "flag": False, "n": 0, "s": "", "obj": {"k": 1}})

    def test_no_mutation_during_merge(self):
        base, override = {"x": {"y": [1]}}, {"x": {"z": [2]}}
        before = copy.deepcopy((base, override))
        self.merge(base, override)
        self.assertEqual((base, override), before)

    def test_result_detached_from_base(self):
        base = {"x": [{"y": [1]}]}
        result = self.merge(base, {})
        result["x"][0]["y"].append(2)
        self.assertEqual(base, {"x": [{"y": [1]}]})

    def test_result_detached_from_override(self):
        override = {"x": {"y": [1]}}
        result = self.merge({}, override)
        result["x"]["y"].append(2)
        self.assertEqual(override, {"x": {"y": [1]}})

    def test_real_cli(self):
        with tempfile.TemporaryDirectory(prefix="json-check-") as temporary:
            base, override = Path(temporary) / "base config.json", Path(temporary) / "override.json"
            base.write_text(json.dumps({"app": {"name": "项目", "port": 80}}), encoding="utf-8")
            override.write_text(json.dumps({"app": {"port": 81}}), encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", "-m", "layered_settings", str(base), str(override)],
                                    cwd=project, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"app": {"name": "项目", "port": 81}})


suite = unittest.defaultTestLoader.loadTestsFromTestCase(FeatureChecks if task == "feature" else BugChecks)
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
