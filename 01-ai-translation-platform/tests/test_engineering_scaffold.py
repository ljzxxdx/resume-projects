from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProjectPathTests(unittest.TestCase):
    def test_relative_path_is_resolved_from_project_root_not_cwd(self) -> None:
        from translation_platform.paths import resolve_project_path

        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            try:
                os.chdir(temporary_directory)
                resolved = resolve_project_path("artifacts", "validation")
            finally:
                os.chdir(original_cwd)

        self.assertEqual(
            resolved,
            PROJECT_ROOT / "artifacts" / "validation",
        )

    def test_absolute_path_is_preserved(self) -> None:
        from translation_platform.paths import resolve_project_path

        absolute_path = PROJECT_ROOT / "tests"

        self.assertEqual(resolve_project_path(absolute_path), absolute_path)


class EntrypointTests(unittest.TestCase):
    def test_help_runs_from_outside_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            completed = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "main.py"), "--help"],
                cwd=temporary_directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

        combined_output = f"{completed.stdout}\n{completed.stderr}".lower()
        self.assertEqual(completed.returncode, 0, combined_output)
        self.assertIn("ai translation platform", combined_output)


if __name__ == "__main__":
    unittest.main()
