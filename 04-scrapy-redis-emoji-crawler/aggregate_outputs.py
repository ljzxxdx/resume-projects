import argparse
import json
from pathlib import Path

from scrapyWithRedis.settings import OUTPUT_DIR


WORKER_FILE_PATTERN = "items-*.jl"


def aggregate_json_lines(input_dir, output_file):
    input_path = Path(input_dir)
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    item_count = 0

    try:
        with temporary_path.open("w", encoding="utf-8") as destination:
            for worker_path in sorted(input_path.glob(WORKER_FILE_PATTERN)):
                with worker_path.open("r", encoding="utf-8") as source:
                    for line_number, line in enumerate(source, start=1):
                        if not line.strip():
                            continue
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError as error:
                            raise ValueError(
                                f"Invalid JSON in {worker_path} "
                                f"at line {line_number}"
                            ) from error
                        destination.write(
                            json.dumps(item, ensure_ascii=False) + "\n"
                        )
                        item_count += 1
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return item_count


def build_parser():
    parser = argparse.ArgumentParser(
        description="Aggregate isolated worker JSON Lines files.",
    )
    parser.add_argument(
        "--input-dir",
        default=OUTPUT_DIR,
        help=f"Worker output directory (default: {OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--output",
        help="Aggregate output file (default: <input-dir>/all-items.jl).",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    input_dir = Path(args.input_dir)
    output_file = (
        Path(args.output)
        if args.output
        else input_dir / "all-items.jl"
    )
    item_count = aggregate_json_lines(input_dir, output_file)
    print(f"aggregated {item_count} items into {output_file}")


if __name__ == "__main__":
    main()
