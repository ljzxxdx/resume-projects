import logging
from collections.abc import Iterator

from scrapy.http import Response

from .items import EmojiItem


IMAGE_SELECTOR = '//img[@class="ui image lazy"]'


def extract_emoji_items(
    response: Response,
    logger: logging.Logger,
) -> Iterator[EmojiItem]:
    """Extract normalized emoji items from one listing page."""
    image_nodes = response.xpath(IMAGE_SELECTOR)

    for image_node in image_nodes:
        raw_img_url = (
            image_node.xpath("./@data-original").get(default="").strip()
        )
        title = image_node.xpath("./@alt").get(default="").strip()

        if not raw_img_url:
            logger.warning(
                "skip item: missing img_url source_url=%s title=%r",
                response.url,
                title,
            )
            continue

        if not title:
            logger.warning(
                "keep item: missing title source_url=%s img_url=%s",
                response.url,
                raw_img_url,
            )

        yield EmojiItem(
            img_url=response.urljoin(raw_img_url),
            title=title,
            source_url=response.url,
        )
