from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from openpyxl import Workbook

from translation_platform import cli


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args(arguments: list[str]):
    return cli.parse_args(arguments)


class CliTestCase(unittest.TestCase):
    def assert_parse_error(self, arguments: list[str], message: str) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parse_args(arguments)

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(message, stderr.getvalue())


class HelpTests(unittest.TestCase):
    def test_root_help_lists_translate_and_batch_commands(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "main.py"), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("translate", completed.stdout)
        self.assertIn("batch", completed.stdout)


class TranslateCommandTests(CliTestCase):
    def test_valid_translate_arguments_are_normalized(self) -> None:
        arguments = parse_args(
            [
                "translate",
                "--text",
                "  synthetic hello  ",
                "--from-lang",
                "en",
                "--to-lang",
                "zh-CHS",
            ]
        )

        self.assertEqual(arguments.command, "translate")
        self.assertEqual(arguments.text, "synthetic hello")
        self.assertEqual(arguments.from_lang, "en")
        self.assertEqual(arguments.to_lang, "zh-CHS")

    def test_whitespace_only_text_is_rejected(self) -> None:
        self.assert_parse_error(
            [
                "translate",
                "--text",
                "   ",
                "--from-lang",
                "en",
                "--to-lang",
                "zh-CHS",
            ],
            "--text must not be empty",
        )

    def test_unsupported_language_is_rejected(self) -> None:
        self.assert_parse_error(
            [
                "translate",
                "--text",
                "synthetic hello",
                "--from-lang",
                "fr",
                "--to-lang",
                "en",
            ],
            "invalid choice",
        )

    def test_equal_languages_are_rejected(self) -> None:
        self.assert_parse_error(
            [
                "translate",
                "--text",
                "synthetic hello",
                "--from-lang",
                "en",
                "--to-lang",
                "en",
            ],
            "source and target languages must differ",
        )

    def test_omitted_languages_use_default_text_inference(self) -> None:
        arguments = parse_args(
            [
                "translate",
                "--text",
                "Synthetic text 123",
            ]
        )

        self.assertEqual(arguments.from_lang, "en")
        self.assertEqual(arguments.to_lang, "zh-CHS")

    def test_signal_free_text_requires_explicit_source_language(self) -> None:
        self.assert_parse_error(
            ["translate", "--text", "12345?!"],
            "no supported language",
        )


class BatchCommandTests(CliTestCase):
    def create_csv(self, directory: Path, name: str = "input.csv") -> Path:
        path = directory / name
        path.write_text("title,location\nSynthetic event,Example city\n", encoding="utf-8")
        return path

    def create_xlsx(self, directory: Path) -> Path:
        path = directory / "input.xlsx"
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["title", "location"])
        worksheet.append(["Synthetic event", "Example city"])
        workbook.save(path)
        workbook.close()
        return path

    def batch_arguments(
        self,
        input_path: Path,
        output_path: Path,
        *additional: str,
    ) -> list[str]:
        return [
            "batch",
            "--input",
            str(input_path),
            "--columns",
            "title, location",
            "--output",
            str(output_path),
            *additional,
        ]

    def test_valid_csv_arguments_are_normalized_with_low_load_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = self.create_csv(directory)
            output_path = directory / "output.xlsx"

            arguments = parse_args(self.batch_arguments(input_path, output_path))

        self.assertEqual(arguments.command, "batch")
        self.assertEqual(arguments.input_path, input_path)
        self.assertEqual(arguments.columns, ("title", "location"))
        self.assertEqual(arguments.output_path, output_path)
        self.assertEqual(arguments.from_lang, "zh-CHS")
        self.assertEqual(arguments.to_lang, "en")
        self.assertEqual(arguments.workers, 1)
        self.assertEqual(arguments.min_interval, 1.0)
        self.assertEqual(arguments.max_retries, 3)
        self.assertIsNone(arguments.checkpoint_path)
        self.assertFalse(arguments.use_proxy)

    def test_valid_xlsx_columns_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = self.create_xlsx(directory)

            arguments = parse_args(
                self.batch_arguments(input_path, directory / "output.csv")
            )

        self.assertEqual(arguments.columns, ("title", "location"))

    def test_relative_paths_are_resolved_from_project_root_not_cwd(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as project_directory:
            project_path = Path(project_directory)
            input_path = self.create_csv(project_path)
            relative_input = input_path.relative_to(PROJECT_ROOT)
            relative_output = relative_input.parent / "output.csv"
            relative_checkpoint = relative_input.parent / "checkpoint.jsonl"

            original_cwd = Path.cwd()
            with tempfile.TemporaryDirectory() as other_directory:
                try:
                    os.chdir(other_directory)
                    arguments = parse_args(
                        self.batch_arguments(
                            relative_input,
                            relative_output,
                            "--checkpoint",
                            str(relative_checkpoint),
                        )
                    )
                finally:
                    os.chdir(original_cwd)

        self.assertEqual(arguments.input_path, input_path)
        self.assertEqual(arguments.output_path, PROJECT_ROOT / relative_output)
        self.assertEqual(
            arguments.checkpoint_path,
            PROJECT_ROOT / relative_checkpoint,
        )

    def test_missing_input_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            self.assert_parse_error(
                self.batch_arguments(directory / "missing.csv", directory / "output.csv"),
                "input file does not exist",
            )

    def test_missing_source_column_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = self.create_csv(directory)
            arguments = self.batch_arguments(input_path, directory / "output.csv")
            arguments[arguments.index("title, location")] = "title,missing"

            self.assert_parse_error(arguments, "input is missing columns: missing")

    def test_empty_and_duplicate_columns_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = self.create_csv(directory)
            output_path = directory / "output.csv"

            empty = self.batch_arguments(input_path, output_path)
            empty[empty.index("title, location")] = "title, "
            self.assert_parse_error(empty, "--columns contains an empty name")

            duplicate = self.batch_arguments(input_path, output_path)
            duplicate[duplicate.index("title, location")] = "title,title"
            self.assert_parse_error(duplicate, "--columns contains duplicate names")

    def test_unsupported_extensions_and_source_overwrite_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            text_input = directory / "input.txt"
            text_input.write_text("title\nSynthetic event\n", encoding="utf-8")
            csv_input = self.create_csv(directory)

            self.assert_parse_error(
                self.batch_arguments(text_input, directory / "output.csv"),
                "--input must be a .csv or .xlsx file",
            )
            self.assert_parse_error(
                self.batch_arguments(csv_input, directory / "output.txt"),
                "--output must be a .csv or .xlsx file",
            )
            self.assert_parse_error(
                self.batch_arguments(csv_input, csv_input),
                "--output must differ from --input",
            )

    def test_numeric_boundaries_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = self.create_csv(directory)
            output_path = directory / "output.csv"

            invalid_values = (
                ("--workers", "0", "--workers must be between 1 and 2"),
                ("--workers", "3", "--workers must be between 1 and 2"),
                ("--min-interval", "0.99", "--min-interval must be between 1 and 60"),
                ("--min-interval", "61", "--min-interval must be between 1 and 60"),
                ("--max-retries", "-1", "--max-retries must be between 0 and 5"),
                ("--max-retries", "6", "--max-retries must be between 0 and 5"),
            )
            for option, value, message in invalid_values:
                with self.subTest(option=option, value=value):
                    self.assert_parse_error(
                        self.batch_arguments(input_path, output_path, option, value),
                        message,
                    )

            accepted = parse_args(
                self.batch_arguments(
                    input_path,
                    output_path,
                    "--workers",
                    "2",
                    "--min-interval",
                    "60",
                    "--max-retries",
                    "0",
                )
            )
            self.assertEqual(
                (accepted.workers, accepted.min_interval, accepted.max_retries),
                (2, 60.0, 0),
            )

    def test_proxy_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = self.create_csv(directory)
            output_path = directory / "output.csv"

            default_arguments = parse_args(
                self.batch_arguments(input_path, output_path)
            )
            enabled_arguments = parse_args(
                self.batch_arguments(input_path, output_path, "--use-proxy")
            )

        self.assertFalse(default_arguments.use_proxy)
        self.assertTrue(enabled_arguments.use_proxy)

    def test_equal_batch_languages_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = self.create_csv(directory)

            self.assert_parse_error(
                self.batch_arguments(
                    input_path,
                    directory / "output.csv",
                    "--from-lang",
                    "en",
                    "--to-lang",
                    "en",
                ),
                "source and target languages must differ",
            )


if __name__ == "__main__":
    unittest.main()
