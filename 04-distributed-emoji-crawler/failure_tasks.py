import argparse
import json

from redis import Redis

from scrapyWithRedis.failure_store import RedisFailureStore
from scrapyWithRedis import settings


def _decode_payload(payload):
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    return payload


def _positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _new_replay_stats():
    return {
        "scanned": 0,
        "queued": 0,
        "already_claimed": 0,
        "invalid": 0,
    }


def _snapshot(store, queue_key, limit=None):
    if limit is not None and limit <= 0:
        raise ValueError("limit must be greater than zero")
    end = -1 if limit is None else limit - 1
    return list(store.list_raw(queue_key, 0, end))


def find_failure(store, queue_key, failure_id):
    for raw_payload in store.list_raw(queue_key):
        record = json.loads(_decode_payload(raw_payload))
        if record.get("failure_id") == failure_id:
            return raw_payload, record
    return None, None


def replay_page_failure(
    store,
    queue_key,
    dedupe_key,
    retry_queue_key,
    failure_id,
):
    raw_payload, record = find_failure(store, queue_key, failure_id)
    if record is None:
        return False
    retry_payload = json.dumps(
        {"url": record["url"]},
        ensure_ascii=False,
        sort_keys=True,
    )
    return store.replay_page(
        queue_key=queue_key,
        dedupe_key=dedupe_key,
        failure_id=failure_id,
        failure_payload=raw_payload,
        retry_queue_key=retry_queue_key,
        retry_payload=retry_payload,
    )


def replay_image_failure(
    store,
    queue_key,
    dedupe_key,
    item_dupe_key,
    retry_queue_key,
    failure_id,
):
    raw_payload, record = find_failure(store, queue_key, failure_id)
    if record is None:
        return False
    retry_item = {
        "img_url": record["img_url"],
        "title": record.get("title", ""),
        "source_url": record["source_url"],
    }
    retry_payload = json.dumps(
        {
            "url": record["source_url"],
            "meta": {"retry_item": retry_item},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return store.replay_image(
        queue_key=queue_key,
        dedupe_key=dedupe_key,
        failure_id=failure_id,
        failure_payload=raw_payload,
        item_dupe_key=item_dupe_key,
        img_url=record["img_url"],
        retry_queue_key=retry_queue_key,
        retry_payload=retry_payload,
    )


def replay_all_page_failures(
    store,
    queue_key,
    dedupe_key,
    retry_queue_key,
    limit=None,
):
    stats = _new_replay_stats()
    for raw_payload in _snapshot(store, queue_key, limit):
        stats["scanned"] += 1
        try:
            record = json.loads(_decode_payload(raw_payload))
            failure_id = record["failure_id"]
            retry_payload = json.dumps(
                {"url": record["url"]},
                ensure_ascii=False,
                sort_keys=True,
            )
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            stats["invalid"] += 1
            continue

        replayed = store.replay_page(
            queue_key=queue_key,
            dedupe_key=dedupe_key,
            failure_id=failure_id,
            failure_payload=raw_payload,
            retry_queue_key=retry_queue_key,
            retry_payload=retry_payload,
        )
        result_key = "queued" if replayed else "already_claimed"
        stats[result_key] += 1
    return stats


def replay_all_image_failures(
    store,
    queue_key,
    dedupe_key,
    item_dupe_key,
    retry_queue_key,
    limit=None,
):
    stats = _new_replay_stats()
    for raw_payload in _snapshot(store, queue_key, limit):
        stats["scanned"] += 1
        try:
            record = json.loads(_decode_payload(raw_payload))
            failure_id = record["failure_id"]
            img_url = record["img_url"]
            retry_item = {
                "img_url": img_url,
                "title": record.get("title", ""),
                "source_url": record["source_url"],
            }
            retry_payload = json.dumps(
                {
                    "url": record["source_url"],
                    "meta": {"retry_item": retry_item},
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            stats["invalid"] += 1
            continue

        replayed = store.replay_image(
            queue_key=queue_key,
            dedupe_key=dedupe_key,
            failure_id=failure_id,
            failure_payload=raw_payload,
            item_dupe_key=item_dupe_key,
            img_url=img_url,
            retry_queue_key=retry_queue_key,
            retry_payload=retry_payload,
        )
        result_key = "queued" if replayed else "already_claimed"
        stats[result_key] += 1
    return stats


def _queue_key(template, spider_name):
    return template % {"spider": spider_name}


def build_parser():
    parser = argparse.ArgumentParser(
        description="View and replay Redis failure records.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show", help="Show queued failures.")
    show.add_argument("kind", choices=("page", "image"))
    show.add_argument("--spider", required=True)

    replay = subparsers.add_parser(
        "replay",
        help="Replay one failure or a queue snapshot.",
    )
    replay.add_argument("kind", choices=("page", "image"))
    replay.add_argument("--spider", required=True)
    selection = replay.add_mutually_exclusive_group(required=True)
    selection.add_argument("--failure-id")
    selection.add_argument(
        "--all",
        action="store_true",
        help="Replay the current failure queue snapshot.",
    )
    replay.add_argument(
        "--limit",
        type=_positive_int,
        help="Process at most this many snapshot records; requires --all.",
    )
    replay.add_argument(
        "--target-spider",
        default="mycrawler_redis",
        help="Redis spider receiving replayed page URLs.",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if (
        args.command == "replay"
        and args.limit is not None
        and not args.all
    ):
        parser.error("--limit requires --all")
    server = Redis.from_url(settings.REDIS_URL)
    store = RedisFailureStore(server)
    queue_template = (
        settings.PAGE_FAILURE_QUEUE_KEY
        if args.kind == "page"
        else settings.IMAGE_FAILURE_QUEUE_KEY
    )
    queue_key = _queue_key(queue_template, args.spider)

    if args.command == "show":
        records = store.list_records(queue_key)
        for record in records:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        print(f"total={len(records)} queue={queue_key}")
        return 0

    if args.all:
        if args.kind == "page":
            stats = replay_all_page_failures(
                store=store,
                queue_key=queue_key,
                dedupe_key=_queue_key(
                    settings.PAGE_FAILURE_DUPE_KEY,
                    args.spider,
                ),
                retry_queue_key=(
                    settings.REDIS_START_URLS_KEY
                    % {"name": args.target_spider}
                ),
                limit=args.limit,
            )
        else:
            stats = replay_all_image_failures(
                store=store,
                queue_key=queue_key,
                dedupe_key=settings.IMAGE_FAILURE_DUPE_KEY,
                item_dupe_key=settings.ITEM_URL_DUPE_KEY,
                retry_queue_key=settings.IMAGE_RETRY_QUEUE_KEY,
                limit=args.limit,
            )
        print(
            "scanned={scanned} queued={queued} "
            "already_claimed={already_claimed} invalid={invalid}".format(
                **stats
            )
        )
        return 0

    if args.kind == "page":
        replayed = replay_page_failure(
            store=store,
            queue_key=queue_key,
            dedupe_key=_queue_key(
                settings.PAGE_FAILURE_DUPE_KEY,
                args.spider,
            ),
            retry_queue_key=(
                settings.REDIS_START_URLS_KEY
                % {"name": args.target_spider}
            ),
            failure_id=args.failure_id,
        )
    else:
        replayed = replay_image_failure(
            store=store,
            queue_key=queue_key,
            dedupe_key=settings.IMAGE_FAILURE_DUPE_KEY,
            item_dupe_key=settings.ITEM_URL_DUPE_KEY,
            retry_queue_key=settings.IMAGE_RETRY_QUEUE_KEY,
            failure_id=args.failure_id,
        )

    status = "queued" if replayed else "not-found-or-already-replayed"
    print(f"status={status} failure_id={args.failure_id}")
    return 0 if replayed else 1


if __name__ == "__main__":
    raise SystemExit(main())
