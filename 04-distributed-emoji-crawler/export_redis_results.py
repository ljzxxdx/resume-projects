import argparse
import json
from pathlib import Path

from redis import Redis

from scrapyWithRedis.settings import OUTPUT_DIR, REDIS_ITEMS_KEY, REDIS_URL
from start import AVAILABLE_SPIDERS


def _prefer_candidate(candidate, current):
    candidate_succeeded = candidate.get("download_status") != "failed"
    current_succeeded = current.get("download_status") != "failed"
    if candidate_succeeded != current_succeeded:
        return candidate_succeeded
    return candidate.get("crawled", "") > current.get("crawled", "")


def export_redis_results(
    server,
    key_template,
    spider_names,
    output_file,
    batch_size=500,
):
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    sources = [
        (
            key_template % {"spider": spider_name},
            server.llen(key_template % {"spider": spider_name}),
        )
        for spider_name in spider_names
    ]
    selected_by_url = {}
    read_count = 0
    duplicate_count = 0

    for key, initial_length in sources:
        for start in range(0, initial_length, batch_size):
            end = min(start + batch_size, initial_length) - 1
            for raw_item in server.lrange(key, start, end):
                item = json.loads(raw_item)
                read_count += 1
                img_url = item.get("img_url") or f"__missing__:{read_count}"
                current = selected_by_url.get(img_url)
                if current is None:
                    selected_by_url[img_url] = item
                    continue
                duplicate_count += 1
                if _prefer_candidate(item, current):
                    selected_by_url[img_url] = item

    try:
        with temporary_path.open("w", encoding="utf-8") as output:
            for img_url in sorted(selected_by_url):
                output.write(
                    json.dumps(
                        selected_by_url[img_url],
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return {
        "read": read_count,
        "exported": len(selected_by_url),
        "duplicates": duplicate_count,
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Export complete distributed results from Redis.",
    )
    parser.add_argument(
        "--output",
        default=str(Path(OUTPUT_DIR) / "all-items-from-redis.jl"),
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--spider",
        action="append",
        choices=AVAILABLE_SPIDERS,
        dest="spiders",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    server = Redis.from_url(REDIS_URL)
    spider_names = tuple(args.spiders or AVAILABLE_SPIDERS)
    stats = export_redis_results(
        server=server,
        key_template=REDIS_ITEMS_KEY,
        spider_names=spider_names,
        output_file=args.output,
        batch_size=args.batch_size,
    )
    print(
        "exported {exported} unique items from {read} records "
        "({duplicates} duplicates) into {output}".format(
            **stats,
            output=args.output,
        )
    )


if __name__ == "__main__":
    main()
