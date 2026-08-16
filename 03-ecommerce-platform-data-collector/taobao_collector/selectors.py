"""DrissionPage 与 Selenium 共用的页面选择器注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence, Tuple

from selenium.webdriver.common.by import By


class SelectorStrategy(str, Enum):
    """支持的元素定位方式。"""

    XPATH = "xpath"
    CSS = "css"


class SelectorStructureError(LookupError):
    """页面结构不满足某个业务选择器的数量约束。"""


@dataclass(frozen=True)
class Selector:
    """带业务含义且可转换为双引擎格式的选择器。"""

    name: str
    strategy: SelectorStrategy
    query: str
    purpose: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("选择器名称不能为空")
        if not self.query.strip():
            raise ValueError("选择器表达式不能为空")
        if not self.purpose.strip():
            raise ValueError("选择器业务含义不能为空")

    @property
    def drission_locator(self) -> str:
        prefix = (
            "xpath"
            if self.strategy is SelectorStrategy.XPATH
            else "css"
        )
        return f"{prefix}:{self.query}"

    @property
    def selenium_locator(self) -> Tuple[str, str]:
        by = (
            By.XPATH
            if self.strategy is SelectorStrategy.XPATH
            else By.CSS_SELECTOR
        )
        return by, self.query


def require_minimum_elements(
    elements: Sequence[Any],
    selector: Selector,
    minimum: int,
) -> Sequence[Any]:
    """校验复合字段所需的最少元素数，并给出可定位的结构错误。"""
    actual = len(elements)
    if actual < minimum:
        raise SelectorStructureError(
            f"选择器 {selector.name}（{selector.purpose}；"
            f"表达式={selector.query}）至少需要{minimum}个元素，"
            f"实际{actual}个"
        )
    return elements


SEARCH_INPUT = Selector(
    name="search_input",
    strategy=SelectorStrategy.CSS,
    query=".search-suggest-combobox-imageSearch-input",
    purpose="输入商品搜索关键词",
)
PAGE_BODY = Selector(
    name="page_body",
    strategy=SelectorStrategy.CSS,
    query="body",
    purpose="读取当前页面可见文本以识别登录或风控阻塞",
)
PRODUCT_LIST_ITEMS = Selector(
    name="product_list_items",
    strategy=SelectorStrategy.XPATH,
    query='//div[@id="content_items_wrapper"]/div/a',
    purpose="定位商品搜索结果卡片及详情链接",
)
PRODUCT_TITLE = Selector(
    name="product_title",
    strategy=SelectorStrategy.XPATH,
    query='//div[starts-with(@class,"title")]',
    purpose="读取商品标题",
)
PRODUCT_PRICE = Selector(
    name="product_price",
    strategy=SelectorStrategy.XPATH,
    query='//div[starts-with(@class,"innerPriceWrapper")]',
    purpose="读取商品价格文本",
)
PRODUCT_SALES = Selector(
    name="product_sales",
    strategy=SelectorStrategy.XPATH,
    query='//span[starts-with(@class,"realSales")]',
    purpose="读取商品销量文本",
)
NEXT_PAGE_BUTTON = Selector(
    name="next_page_button",
    strategy=SelectorStrategy.XPATH,
    query='//button[starts-with(@aria-label,"下一页")]',
    purpose="定位商品结果下一页按钮",
)
DETAIL_TITLE = Selector(
    name="detail_title",
    strategy=SelectorStrategy.XPATH,
    query='//div[starts-with(@class,"tabDetailItemTitle")]',
    purpose="读取详情页标题与评论数量提示",
)
PRODUCT_PARAMETERS_TAB = Selector(
    name="product_parameters_tab",
    strategy=SelectorStrategy.XPATH,
    query='//span[text()="参数信息"]',
    purpose="打开商品参数信息区域",
)
PRODUCT_MEDIA_TAB = Selector(
    name="product_media_tab",
    strategy=SelectorStrategy.XPATH,
    query='//span[text()="图文详情"]',
    purpose="打开商品图文详情区域",
)
STORE_RECOMMENDATION_TAB = Selector(
    name="store_recommendation_tab",
    strategy=SelectorStrategy.XPATH,
    query='//span[text()="本店推荐"]',
    purpose="查看本店推荐商品区域",
)
RELATED_PRODUCTS_TAB = Selector(
    name="related_products_tab",
    strategy=SelectorStrategy.XPATH,
    query='//span[text()="看了又看"]',
    purpose="查看相关商品区域",
)
COMMENT_OPEN_BUTTON = Selector(
    name="comment_open_button",
    strategy=SelectorStrategy.CSS,
    query='[class^="ShowButton"]',
    purpose="打开商品评论抽屉",
)
COMMENT_DRAWER = Selector(
    name="comment_drawer",
    strategy=SelectorStrategy.CSS,
    query='[class^="Drawer"]',
    purpose="定位评论抽屉容器",
)
COMMENT_ITEMS = Selector(
    name="comment_items",
    strategy=SelectorStrategy.XPATH,
    query=(
        '//div[starts-with(@class,"Comments")]'
        '//div[starts-with(@class,"Comment")]'
    ),
    purpose="定位评论记录列表",
)
COMMENT_USER_NAME = Selector(
    name="comment_user_name",
    strategy=SelectorStrategy.XPATH,
    query='.//div[starts-with(@class,"userName")]/span',
    purpose="读取评论用户名称",
)
COMMENT_SKU = Selector(
    name="comment_sku",
    strategy=SelectorStrategy.XPATH,
    query='.//div[starts-with(@class,"meta")]',
    purpose="读取评论对应购买规格",
)
COMMENT_CONTENT = Selector(
    name="comment_content",
    strategy=SelectorStrategy.XPATH,
    query=(
        './/div[starts-with(@class,"contentWrapper")]'
        '/div[starts-with(@class,"content")]'
    ),
    purpose="读取评论正文",
)
LIVE_NAV_TAB = Selector(
    name="live_nav_tab",
    strategy=SelectorStrategy.XPATH,
    query='//a[contains(normalize-space(.),"淘宝直播")]',
    purpose="打开淘宝直播导航入口",
)
LIVE_SEARCH_INPUT = Selector(
    name="live_search_input",
    strategy=SelectorStrategy.XPATH,
    query='//input[starts-with(@class,"input")]',
    purpose="输入直播搜索关键词",
)
LIVE_LIST_ITEMS = Selector(
    name="live_list_items",
    strategy=SelectorStrategy.XPATH,
    query='//div[starts-with(@class,"listItem")]',
    purpose="定位直播搜索结果卡片",
)
LIVE_LINK_BUTTON = Selector(
    name="live_link_button",
    strategy=SelectorStrategy.XPATH,
    query='.//div[starts-with(@class,"linkBtn")]',
    purpose="打开直播间详情页",
)
LIVE_ACCOUNT_NAME = Selector(
    name="live_account_name",
    strategy=SelectorStrategy.XPATH,
    query=(
        './/div[starts-with(@class,"infoBox")]'
        '/p[starts-with(@class,"accountName")]'
    ),
    purpose="读取直播账号名称",
)
LIVE_INTRODUCTION = Selector(
    name="live_introduction",
    strategy=SelectorStrategy.XPATH,
    query=(
        './/div[starts-with(@class,"infoBox")]'
        '/p[starts-with(@class,"infoText")]'
    ),
    purpose="读取直播间介绍",
)
LIVE_INFO_COUNTS = Selector(
    name="live_info_counts",
    strategy=SelectorStrategy.XPATH,
    query=(
        './/div[starts-with(@class,"infoLine")]'
        '/p[starts-with(@class,"infoText")]'
    ),
    purpose="读取直播观看数和粉丝数",
)
LIVE_GOODS_TITLE = Selector(
    name="live_goods_title",
    strategy=SelectorStrategy.XPATH,
    query=(
        '//div[starts-with(@class,"allGoodsWrapper")]'
        '//div[starts-with(@class,"title")]'
    ),
    purpose="读取直播间商品数量标题",
)


ALL_SELECTORS = (
    SEARCH_INPUT,
    PAGE_BODY,
    PRODUCT_LIST_ITEMS,
    PRODUCT_TITLE,
    PRODUCT_PRICE,
    PRODUCT_SALES,
    NEXT_PAGE_BUTTON,
    DETAIL_TITLE,
    PRODUCT_PARAMETERS_TAB,
    PRODUCT_MEDIA_TAB,
    STORE_RECOMMENDATION_TAB,
    RELATED_PRODUCTS_TAB,
    COMMENT_OPEN_BUTTON,
    COMMENT_DRAWER,
    COMMENT_ITEMS,
    COMMENT_USER_NAME,
    COMMENT_SKU,
    COMMENT_CONTENT,
    LIVE_NAV_TAB,
    LIVE_SEARCH_INPUT,
    LIVE_LIST_ITEMS,
    LIVE_LINK_BUTTON,
    LIVE_ACCOUNT_NAME,
    LIVE_INTRODUCTION,
    LIVE_INFO_COUNTS,
    LIVE_GOODS_TITLE,
)

_registry = {selector.name: selector for selector in ALL_SELECTORS}
if len(_registry) != len(ALL_SELECTORS):
    raise RuntimeError("选择器名称不能重复")

SELECTORS_BY_NAME: Mapping[str, Selector] = MappingProxyType(
    _registry
)
