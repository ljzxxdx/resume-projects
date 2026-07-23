# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

import scrapy


class EmojiItem(scrapy.Item):
    # Extracted by the spider: one item represents one image.
    img_url = scrapy.Field()
    title = scrapy.Field()
    source_url = scrapy.Field()

    # Filled by the custom image pipeline.
    image_path = scrapy.Field()
    image_checksum = scrapy.Field()
    download_status = scrapy.Field()
    download_error = scrapy.Field()

    # Filled by the metadata pipeline.
    crawled = scrapy.Field()
    spider = scrapy.Field()
    worker_id = scrapy.Field()
