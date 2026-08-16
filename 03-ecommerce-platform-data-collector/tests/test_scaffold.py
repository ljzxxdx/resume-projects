"""Tests for the stage 1 project skeleton."""

from __future__ import annotations

import importlib
import unittest
from pathlib import Path


PLANNED_MODULES = (
    "taobao_collector",
    "taobao_collector.cli",
    "taobao_collector.config",
    "taobao_collector.models",
    "taobao_collector.selectors",
    "taobao_collector.exporters",
    "taobao_collector.observability",
    "taobao_collector.collectors",
    "taobao_collector.collectors.base",
    "taobao_collector.collectors.drission_collector",
    "taobao_collector.collectors.selenium_collector",
)


class ProjectScaffoldTests(unittest.TestCase):
    def test_planned_python_modules_are_importable(self) -> None:
        for module_name in PLANNED_MODULES:
            with self.subTest(module_name=module_name):
                self.assertIsNotNone(importlib.import_module(module_name))

    def test_gitignore_excludes_local_credentials_and_browser_profiles(
        self,
    ) -> None:
        rules = {
            line.strip()
            for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertIn(".env", rules)
        self.assertIn("artifacts/manual-chrome-profile/", rules)
        self.assertIn("artifacts/chrome-profile/", rules)
        self.assertIn("artifacts/runs/", rules)


if __name__ == "__main__":
    unittest.main()
