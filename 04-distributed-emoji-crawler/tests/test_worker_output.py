import importlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scrapyWithRedis.pipelines import JsonLinesPipeline


def load_aggregate_module():
    try:
        return importlib.import_module("aggregate_outputs")
    except ModuleNotFoundError:
        return None


class WorkerOutputTests(unittest.TestCase):
    def test_same_worker_id_is_isolated_by_process_id(self):
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            first = JsonLinesPipeline(
                output_dir=output_dir,
                worker_id="worker-a",
                process_id=1001,
            )
            second = JsonLinesPipeline(
                output_dir=output_dir,
                worker_id="worker-a",
                process_id=1002,
            )

            first.open_spider(spider=None)
            second.open_spider(spider=None)
            first.process_item({"value": "first"}, spider=None)
            second.process_item({"value": "second"}, spider=None)
            first.close_spider(spider=None)
            second.close_spider(spider=None)

            self.assertEqual(
                first.output_path.name,
                "items-worker-a-1001.jl",
            )
            self.assertEqual(
                second.output_path.name,
                "items-worker-a-1002.jl",
            )
            self.assertNotEqual(first.output_path, second.output_path)
            self.assertEqual(
                json.loads(first.output_path.read_text(encoding="utf-8")),
                {"value": "first"},
            )
            self.assertEqual(
                json.loads(second.output_path.read_text(encoding="utf-8")),
                {"value": "second"},
            )

    def test_worker_id_is_safe_for_use_in_a_filename(self):
        pipeline = JsonLinesPipeline(
            output_dir="output",
            worker_id="node/one:primary",
            process_id=42,
        )

        self.assertEqual(
            pipeline.output_path.name,
            "items-node_one_primary-42.jl",
        )

    def test_aggregation_overwrites_result_and_ignores_non_worker_files(self):
        aggregate_module = load_aggregate_module()
        self.assertIsNotNone(
            aggregate_module,
            "aggregate_outputs module must exist",
        )

        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            worker_files = {
                "items-worker-a-1001.jl": [
                    {"img_url": "https://example.com/a.gif"},
                ],
                "items-worker-b-1002.jl": [
                    {"img_url": "https://example.com/b.gif"},
                ],
            }
            for name, items in worker_files.items():
                (output_dir / name).write_text(
                    "".join(
                        json.dumps(item) + "\n"
                        for item in items
                    ),
                    encoding="utf-8",
                )

            (output_dir / "output.jl").write_text(
                json.dumps({"legacy": True}) + "\n",
                encoding="utf-8",
            )
            aggregate_path = output_dir / "all-items.jl"

            first_count = aggregate_module.aggregate_json_lines(
                output_dir,
                aggregate_path,
            )
            first_content = aggregate_path.read_text(encoding="utf-8")
            second_count = aggregate_module.aggregate_json_lines(
                output_dir,
                aggregate_path,
            )
            second_content = aggregate_path.read_text(encoding="utf-8")

            self.assertEqual(first_count, 2)
            self.assertEqual(second_count, 2)
            self.assertEqual(second_content, first_content)
            self.assertEqual(
                [json.loads(line) for line in second_content.splitlines()],
                [
                    {"img_url": "https://example.com/a.gif"},
                    {"img_url": "https://example.com/b.gif"},
                ],
            )


if __name__ == "__main__":
    unittest.main()
