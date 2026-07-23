import argparse
import json
import os

from scrapy import cmdline

from scrapyWithRedis.start_urls import normalize_start_urls


AVAILABLE_SPIDERS = (
    "dmoz",
    "myspider_redis",
    "mycrawler_redis",
    "image_retry",
)
DEFAULT_SPIDER = "dmoz"
SPIDER_ENV_VAR = "SPIDER_NAME"


def build_command(argv=None, environ=None):
    parser = argparse.ArgumentParser(
        description="Start one of the project's Scrapy spiders.",
    )
    parser.add_argument(
        "--spider",
        choices=AVAILABLE_SPIDERS,
        help=(
            f"Spider to run. Overrides {SPIDER_ENV_VAR}; "
            f"defaults to {DEFAULT_SPIDER}."
        ),
    )
    parser.add_argument(
        "--start-url",
        action="append",
        dest="start_urls",
        metavar="URL",
        help="Start URL for dmoz; may be supplied more than once.",
    )
    args, scrapy_args = parser.parse_known_args(argv)
    environment = os.environ if environ is None else environ
    spider_name = (
        args.spider
        or environment.get(SPIDER_ENV_VAR)
        or DEFAULT_SPIDER
    )

    if spider_name not in AVAILABLE_SPIDERS:
        parser.error(
            f"invalid {SPIDER_ENV_VAR} value {spider_name!r}; "
            f"choose from {', '.join(AVAILABLE_SPIDERS)}"
        )

    if args.start_urls:
        if spider_name != "dmoz":
            parser.error("--start-url is only supported by the dmoz spider")
        if _spider_argument_is_true(scrapy_args, "redis_start"):
            parser.error("--start-url cannot be combined with Redis start mode")
        try:
            start_urls = normalize_start_urls(args.start_urls)
        except ValueError as exc:
            parser.error(str(exc))
        payload = json.dumps(
            start_urls,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        scrapy_args = ["-a", f"start_urls_json={payload}", *scrapy_args]

    return ["scrapy", "crawl", spider_name, *scrapy_args]


def _spider_argument_is_true(arguments, name):
    values = []
    for index, argument in enumerate(arguments):
        if argument == "-a" and index + 1 < len(arguments):
            values.append(arguments[index + 1])
        elif argument.startswith("-a") and argument != "-a":
            values.append(argument[2:])
    prefix = f"{name}="
    truthy = {"1", "true", "yes", "on"}
    return any(
        value.startswith(prefix)
        and value[len(prefix):].strip().lower() in truthy
        for value in values
    )


def main(argv=None):
    cmdline.execute(build_command(argv))


if __name__ == "__main__":
    main()
