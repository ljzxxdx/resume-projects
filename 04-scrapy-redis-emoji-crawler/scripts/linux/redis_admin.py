#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from redis import Redis
from scrapyWithRedis import settings
from scrapyWithRedis.start_urls import normalize_start_urls


ATOMIC_START_URL_ENQUEUE_SCRIPT = """
local added = 0
for index = 1, #ARGV, 2 do
    if redis.call('SADD', KEYS[2], ARGV[index]) == 1 then
        redis.call('RPUSH', KEYS[1], ARGV[index + 1])
        added = added + 1
    end
end
return added
"""

START_QUEUE_SPIDERS = (
    "dmoz",
    "myspider_redis",
    "mycrawler_redis",
)


normalize_urls = normalize_start_urls


def enqueue_urls(
    server,
    spider,
    urls,
    start_key_template=None,
    dedupe_key_template=None,
):
    input_urls = list(urls)
    normalized = normalize_urls(input_urls)
    start_template = start_key_template or settings.REDIS_START_URLS_KEY
    dedupe_template = dedupe_key_template or settings.START_URL_DUPE_KEY
    queue_key = start_template % {"name": spider}
    dedupe_key = dedupe_template % {"spider": spider}
    before = server.llen(queue_key)
    arguments = []
    for url in normalized:
        arguments.extend(
            (url, json.dumps({"url": url}, ensure_ascii=False, sort_keys=True))
        )
    added = (
        int(
            server.eval(
                ATOMIC_START_URL_ENQUEUE_SCRIPT,
                2,
                queue_key,
                dedupe_key,
                *arguments,
            )
        )
        if arguments
        else 0
    )
    after = server.llen(queue_key)
    return {
        "spider": spider,
        "queue_key": queue_key,
        "dedupe_key": dedupe_key,
        "before": before,
        "after": after,
        "input": len(input_urls),
        "unique_input": len(normalized),
        "added": added,
        "duplicates": len(input_urls) - added,
    }


def redis_key_size(server, key):
    key_type = server.type(key)
    if isinstance(key_type, bytes):
        key_type = key_type.decode("utf-8")
    size_methods = {
        "list": server.llen,
        "set": server.scard,
        "zset": server.zcard,
    }
    if key_type == "none":
        return key_type, 0
    if key_type not in size_methods:
        raise RuntimeError(f"unsupported Redis key type {key_type!r} for {key}")
    return key_type, int(size_methods[key_type](key))


def collect_redis_observation(server, spider, settings_module=settings):
    key_specs = (
        (
            "start_queue",
            settings_module.REDIS_START_URLS_KEY % {"name": spider},
        ),
        (
            "start_url_dupe",
            settings_module.START_URL_DUPE_KEY % {"spider": spider},
        ),
        (
            "scheduler_queue",
            settings_module.SCHEDULER_QUEUE_KEY % {"spider": spider},
        ),
        ("request_dupe", settings_module.SCHEDULER_DUPEFILTER_KEY),
        ("item_url_dupe", settings_module.ITEM_URL_DUPE_KEY),
        (
            "results",
            settings_module.REDIS_ITEMS_KEY % {"spider": spider},
        ),
        (
            "page_failures",
            settings_module.PAGE_FAILURE_QUEUE_KEY % {"spider": spider},
        ),
        (
            "image_failures",
            settings_module.IMAGE_FAILURE_QUEUE_KEY % {"spider": spider},
        ),
    )
    records = []
    for role, key in key_specs:
        key_type, size = redis_key_size(server, key)
        records.append(
            {"role": role, "key": key, "type": key_type, "size": size}
        )
    return records


def build_parser():
    parser = argparse.ArgumentParser(
        description="Linux Redis task enqueue and observation helper.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument(
        "--spider",
        choices=START_QUEUE_SPIDERS,
        default="mycrawler_redis",
    )
    enqueue.add_argument("urls", nargs="+")
    observe = subparsers.add_parser("observe")
    observe.add_argument("--spider", default="mycrawler_redis")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    server = Redis.from_url(settings.REDIS_URL)
    if args.command == "enqueue":
        try:
            result = enqueue_urls(server, args.spider, args.urls)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    records = collect_redis_observation(server, args.spider)
    print(f"{'role':<20} {'type':<8} {'size':>10} key")
    for record in records:
        print(
            f"{record['role']:<20} {record['type']:<8} "
            f"{record['size']:>10} {record['key']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
