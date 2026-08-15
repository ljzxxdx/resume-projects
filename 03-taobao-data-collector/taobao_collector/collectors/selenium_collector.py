"""使用 Selenium 独立实现淘宝商品、评论与直播采集。"""

from __future__ import annotations

import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from inspect import signature
from typing import (
    Any,
    Callable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import parse_qs, urlencode, urlparse

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.keys import Keys

from taobao_collector import parsers, selectors
from taobao_collector.collectors.base import (
    CollectionBlockedError,
    CollectionResult,
    LiveBehaviorPolicy,
    ProductFilterPolicy,
    ReadOnlyBehaviorPolicy,
    RetryPolicy,
    WaitPolicy,
)
from taobao_collector.models import (
    CommentRecord,
    Engine,
    ProductRecord,
    LiveRoomRecord,
)
from taobao_collector.selectors import Selector

HOME_URL = "https://www.taobao.com/"
PRODUCT_SEARCH_URL = "https://s.taobao.com/search"
_DATE_ONLY = re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$")


@dataclass(frozen=True)
class _ProductCardData:
    """商品搜索结果卡片解析后的内部数据。"""

    product_id: str
    source_url: str
    name: str
    price: Optional[Decimal]
    sales_count: Optional[int]

@dataclass(frozen=True)
class _LiveCardData:
    """直播搜索结果卡片解析后的内部数据。"""

    live_room_id: str
    source_url: str
    account_name: str
    introduction: str
    viewer_count: Optional[int]
    follower_count: Optional[int]

_SAFE_DETAIL_TABS = (
    selectors.PRODUCT_PARAMETERS_TAB,
    selectors.PRODUCT_MEDIA_TAB,
    selectors.STORE_RECOMMENDATION_TAB,
    selectors.RELATED_PRODUCTS_TAB,
)


def _extract_source_id(source_url: str) -> str:
    """从来源 URL 的常见参数或路径末尾提取业务 ID。"""
    parsed = urlparse(source_url)
    query = parse_qs(parsed.query)

    for name in ("id", "item_id", "roomId", "room_id", "liveId"):
        values = query.get(name)
        if values and values[0]:
            return values[0]

    path_tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return path_tail or source_url


def _validate_limit(name: str, value: int) -> None:
    """校验采集上限必须为正整数。"""
    if value <= 0:
        raise ValueError(f"{name} 必须是正整数")


class SeleniumCollector:
    """封装 Selenium 页面等待与采集流程。"""

    def __init__(
        self,
        *,
        driver: Any,
        run_id: str,
        wait_policy: Optional[WaitPolicy] = None,
        behavior_policy: Optional[ReadOnlyBehaviorPolicy] = None,
        live_behavior_policy: Optional[LiveBehaviorPolicy] = None,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: Optional[random.Random] = None,
        filter_policy: Optional[ProductFilterPolicy] = None,
        retry_policy: Optional[RetryPolicy] = None,
        block_guard: Optional[Any] = None,
        login_waiter: Optional[Any] = None
    ) -> None:
        normalized_run_id = run_id.strip()
        if not normalized_run_id:
            raise ValueError("run_id 不能为空")

        self.driver = driver
        self.run_id = normalized_run_id
        self.wait_policy = wait_policy or WaitPolicy()
        self.behavior_policy = (
            behavior_policy or ReadOnlyBehaviorPolicy()
        )
        self.live_behavior_policy = (
            live_behavior_policy or LiveBehaviorPolicy()
        )
        self._sleep = sleeper
        self._random = random_source or random.Random()
        self.filter_policy = filter_policy or ProductFilterPolicy()
        self.retry_policy = retry_policy or RetryPolicy()
        self.block_guard = block_guard
        self.login_waiter = login_waiter
        self.last_failure_screenshot = None
        self.last_failure_context = None

    def collect_products(
        self,
        *,
        keyword: str,
        product_limit: int,
        comment_limit: int,
    ) -> CollectionResult:
        """翻页采集达到上限的合格商品及其评论。"""
        _validate_limit("product_limit", product_limit)
        _validate_limit("comment_limit", comment_limit)
        normalized_keyword = keyword.strip()
        if not normalized_keyword:
            raise ValueError("keyword 不能为空")

        self.last_failure_screenshot = None
        self.last_failure_context = None

        try:
            self._open_home()
            self._check_block("home")
            search_input = self._wait_product_search_input()
            self._submit_product_search(search_input, normalized_keyword)
        except (CollectionBlockedError, KeyboardInterrupt):
            raise
        except Exception as error:
            self._capture_failure_evidence("product_entry", error)
            raise

        product_records: List[ProductRecord] = []
        comment_records: List[CommentRecord] = []
        seen_page_signatures: set[tuple[str, ...]] = set()

        try:
            while len(product_records) < product_limit:
                self._check_block("product_results")
                cards = self._wait_product_cards()
                signature = self._product_page_signature(cards)
                if signature in seen_page_signatures:
                    break
                seen_page_signatures.add(signature)
                for card in cards:
                    if len(product_records) >= product_limit:
                        break

                    product_card = self._parse_product_card(card)
                    if not self.filter_policy.accepts_sales(
                        product_card.sales_count
                    ):
                        continue

                    result = self._collect_product_with_retries(
                        card=card,
                        product=product_card,
                        keyword=normalized_keyword,
                        comment_limit=comment_limit,
                    )
                    self._pace_product_transition()
                    if result is None:
                        continue

                    product_record, product_comments = result
                    product_records.append(product_record)
                    comment_records.extend(product_comments)

                if len(product_records) >= product_limit:
                    break

                if not self._open_next_product_page():
                    break
                if not self._wait_product_page_change(signature):
                    break
                self._pace_page_transition()
        except (CollectionBlockedError, KeyboardInterrupt):
            raise
        except Exception as error:
            self._capture_failure_evidence("product_results", error)
            raise

        return CollectionResult(
            products=tuple(product_records),
            comments=tuple(comment_records),
        )

    def collect_live(
        self,
        *,
        keyword: str,
        live_limit: int,
    ) -> CollectionResult:
        """采集直播搜索结果及直播间商品数量。"""
        _validate_limit("live_limit", live_limit)
        normalized_keyword = keyword.strip()
        if not normalized_keyword:
            raise ValueError("keyword 不能为空")

        self.last_failure_screenshot = None
        self.last_failure_context = None

        try:
            self._open_home()
            self._check_block("home")
            button = self._wait_live_navigation()
        except (CollectionBlockedError, KeyboardInterrupt):
            raise
        except Exception as error:
            self._capture_failure_evidence("live_entry", error)
            raise

        live_rooms: List[LiveRoomRecord] = []

        try:
            with self._temporary_new_tab(
                lambda: self._move_and_click(button)
            ):
                try:
                    self._check_block("live_results")
                    search_input = self._wait_live_search_input()
                    self._submit_live_search(search_input, normalized_keyword)
                    cards = self._load_live_cards(live_limit=live_limit)

                    for card in cards:
                        card_data = self._parse_live_card(card)
                        link = card.find_element(
                            *self._to_locator(selectors.LIVE_LINK_BUTTON)
                        )

                        try:
                            with self._temporary_new_tab(
                                lambda: self._move_and_click(link)
                            ):
                                self._check_block("live_detail")
                                self._wait_live_detail()
                                self._perform_live_read_only_behavior()
                                product_count = (
                                    self._parse_live_product_count()
                                )
                                detail_url = self.driver.current_url
                                record = self._build_live_record(
                                    card_data,
                                    normalized_keyword,
                                    product_count,
                                    detail_url,
                                )
                                live_rooms.append(record)
                        except (CollectionBlockedError, KeyboardInterrupt):
                            raise
                        except Exception as error:
                            self._capture_failure_evidence(
                                "live_detail",
                                error,
                            )
                            raise
                        self._pace_live_room_transition()
                except (CollectionBlockedError, KeyboardInterrupt):
                    raise
                except Exception as error:
                    self._capture_failure_evidence(
                        "live_results",
                        error,
                    )
                    raise
        except (CollectionBlockedError, KeyboardInterrupt):
            raise
        except Exception as error:
            self._capture_failure_evidence("live_entry", error)
            raise

        return CollectionResult(live_rooms=tuple(live_rooms))

    @staticmethod
    def _classify_exception(error: BaseException) -> str:
        """将 Selenium 及普通异常归入稳定的错误类别。"""
        if isinstance(error, TimeoutException):
            return "element_timeout"
        if isinstance(error, NoSuchElementException):
            return "selector_invalid"
        if isinstance(error, NoSuchWindowException):
            return "window_lost"
        if isinstance(error, WebDriverException):
            return "webdriver_error"
        return "unexpected_error"

    def _record_failure_context(
        self,
        *,
        step: str,
        error: BaseException,
        error_type: str,
        screenshot_path: Optional[Any],
    ) -> None:
        """保存一次失败的结构化上下文。"""
        try:
            current_url = self.driver.current_url
        except Exception:
            current_url = ""

        self.last_failure_context = {
            "url": current_url,
            "engine": "selenium",
            "step": step,
            "error_type": error_type,
            "exception_type": type(error).__name__,
            "message": str(error),
            "screenshot_path": (
                str(screenshot_path)
                if screenshot_path is not None
                else None
            ),
        }

    def _capture_failure_evidence(
        self,
        step: str,
        error: BaseException,
    ) -> None:
        """保存首次普通失败的截图与结构化上下文。"""
        if self.last_failure_context is not None:
            return

        screenshot_path = None
        screenshot_store = getattr(
            self.block_guard,
            "screenshot_store",
            None,
        )
        if screenshot_store is not None:
            try:
                screenshot_path = screenshot_store.capture(
                    self.driver,
                    run_id=self.run_id,
                    engine=Engine.SELENIUM,
                    step=step,
                )
            except Exception:
                screenshot_path = None

        self.last_failure_screenshot = screenshot_path
        self._record_failure_context(
            step=step,
            error=error,
            error_type=self._classify_exception(error),
            screenshot_path=screenshot_path,
        )

    def _check_block(self, step: str) -> None:
        """先等待人工登录，再检测验证码及风控阻塞。"""
        try:
            if self.login_waiter is not None:
                self.login_waiter.wait(
                    self.driver,
                    run_id=self.run_id,
                    engine=Engine.SELENIUM,
                    step=step,
                )
            if self.block_guard is not None:
                self.block_guard.inspect(
                    self.driver,
                    run_id=self.run_id,
                    engine=Engine.SELENIUM,
                    step=step,
                )
        except CollectionBlockedError as error:
            if self.last_failure_context is None:
                self.last_failure_screenshot = error.screenshot_path
                self._record_failure_context(
                    step=step,
                    error=error,
                    error_type="blocked",
                    screenshot_path=error.screenshot_path,
                )
            raise

    @staticmethod
    def _to_locator(selector: Selector) -> Tuple[str, str]:
        """将公共选择器转换为 Selenium 定位器元组。"""
        return selector.selenium_locator

    def _new_wait(self, root = None) -> WebDriverWait:
        """根据统一等待策略创建 Selenium 显式等待对象。"""
        root = self.driver if root is None else root
        return WebDriverWait(
            root,
            self.wait_policy.timeout,
            poll_frequency=self.wait_policy.poll_interval,
        )

    def _wait_visible(self, selector: Selector) -> Any:
        """等待指定元素可见并返回该元素。"""
        locator = self._to_locator(selector)
        return self._new_wait().until(
            EC.visibility_of_element_located(locator)
        )

    def _wait_clickable(self, selector: Selector) -> Any:
        """等待指定元素可见且可点击并返回该元素。"""
        locator = self._to_locator(selector)
        return self._new_wait().until(
            EC.element_to_be_clickable(locator)
        )

    def _wait_all_present(
        self,
        selector: Selector,
    ) -> Sequence[Any]:
        """等待列表至少出现一个元素并返回当前全部元素。"""
        locator = self._to_locator(selector)
        return self._new_wait().until(
            EC.presence_of_all_elements_located(locator)
        )

    def _wait_for_more_elements(
        self,
        selector: Selector,
        *,
        previous_count: int,
        root: Optional[Any] = None,
    ) -> Sequence[Any]:
        """等待异步列表元素数量超过滚动前数量。"""
        locator = self._to_locator(selector)
        search_root = root if root is not None else self.driver

        def element_count_grew(_driver: Any):
            elements = search_root.find_elements(*locator)
            if len(elements) > previous_count:
                return elements
            return False

        return self._new_wait().until(element_count_grew)

    def _wait_until_stale(self, element: Any) -> bool:
        """等待翻页前的旧元素脱离当前 DOM。"""
        return self._new_wait().until(
            EC.staleness_of(element)
        )

    def _wait_product_search_input(self) -> Any:
        return self._wait_clickable(
            selectors.SEARCH_INPUT
        )

    def _wait_product_cards(self) -> Sequence[Any]:
        """等待商品搜索结果卡片及详情入口出现。"""
        return self._wait_all_present(
            selectors.PRODUCT_LIST_ITEMS
        )

    def _wait_product_detail(self) -> Any:
        """等待商品详情标题可见并返回标题元素。"""
        return self._wait_visible(
            selectors.DETAIL_TITLE
        )

    def _wait_next_page_button(self) -> Any:
        return self._wait_clickable(
            selectors.NEXT_PAGE_BUTTON
        )

    def _wait_comment_open_button(self) -> Any:
        """等待评论抽屉入口按钮达到可点击状态。"""
        return self._wait_clickable(
            selectors.COMMENT_OPEN_BUTTON
        )

    def _wait_comment_drawer(self) -> Any:
        """等待评论抽屉完成可见渲染。"""
        return self._wait_visible(
            selectors.COMMENT_DRAWER
        )

    def _wait_comment_items(self) -> Sequence[Any]:
        """等待首批评论记录进入 DOM。"""
        return self._wait_all_present(
            selectors.COMMENT_ITEMS
        )

    def _wait_live_navigation(self) -> Any:
        """等待淘宝直播导航入口达到可点击状态。"""
        return self._wait_clickable(
            selectors.LIVE_NAV_TAB
        )

    def _wait_live_search_input(self) -> Any:
        """等待直播搜索输入框达到可交互状态。"""
        return self._wait_clickable(
            selectors.LIVE_SEARCH_INPUT
        )

    def _wait_live_cards(self) -> Sequence[Any]:
        """等待直播搜索结果列表至少出现一个房间。"""
        return self._wait_all_present(
            selectors.LIVE_LIST_ITEMS
        )

    def _wait_live_detail(self) -> Any:
        """等待直播详情页商品数量标题可见。"""
        return self._wait_visible(
            selectors.LIVE_GOODS_TITLE
        )

    def _open_home(self) -> None:
        """打开淘宝首页。"""
        self.driver.get(HOME_URL)

    def _submit_product_search(self, search_input: Any, keyword: str) -> None:
        """输入商品关键词并导航到对应搜索结果页。"""
        search_input.clear()
        search_input.send_keys(keyword)
        query = urlencode({"q": keyword})
        self.driver.get(f"{PRODUCT_SEARCH_URL}?{query}")

    def _submit_live_search(
        self,
        search_input: Any,
        keyword: str,
    ) -> None:
        """清空直播搜索框，输入关键词并提交搜索。"""
        search_input.clear()
        search_input.send_keys(keyword)
        search_input.send_keys(Keys.ENTER)

    @staticmethod
    def _product_page_signature(cards: Sequence[Any]) -> tuple:
        """根据商品链接生成可比较的当前页面签名。"""
        return tuple(
            (card.get_attribute("href") or "").strip()
            for card in cards
        )

    def _open_next_product_page(self) -> bool:
        """存在可用的下一页按钮时点击并返回真。"""
        locator = self._to_locator(selectors.NEXT_PAGE_BUTTON)
        buttons = self.driver.find_elements(*locator)
        if not buttons:
            return False

        next_page_button = buttons[-1]
        if (
                not next_page_button.is_enabled()
                or next_page_button.get_attribute("disabled") is not None
                or next_page_button.get_attribute("aria-disabled") == "true"
        ):
            return False
        self._move_and_click(next_page_button)
        return True

    def _wait_product_page_change(self, previous_signature: tuple) -> bool:
        """等待商品列表签名改变，超时则返回假。"""
        selector = self._to_locator(selectors.PRODUCT_LIST_ITEMS)
        def check_change(_driver):
            cards = _driver.find_elements(*selector)
            signature = self._product_page_signature(cards)
            return bool(signature and signature != previous_signature)
        try:
            return self._new_wait().until(check_change)
        except TimeoutException:
            return False

    def _parse_product_card(self, card: Any) -> _ProductCardData:
        """将 Selenium 商品卡片解析为内部规范数据。"""
        source_url = card.get_attribute("href") or ""

        name = card.find_element(
            *self._to_locator(selectors.PRODUCT_TITLE)
        ).text.strip()

        price_text = card.find_element(
            *self._to_locator(selectors.PRODUCT_PRICE)
        ).text

        sales_text = card.find_element(
            *self._to_locator(selectors.PRODUCT_SALES)
        ).text

        return _ProductCardData(
            product_id=_extract_source_id(source_url),
            source_url=source_url,
            name=name,
            price=parsers.parse_price(price_text),
            sales_count=parsers.parse_sales_count(sales_text),
        )

    def _load_live_cards(
        self,
        *,
        live_limit: int,
    ) -> Sequence[Any]:
        """在滚动次数上限内加载所需数量的直播卡片。"""
        cards = list(self._wait_live_cards())
        if len(cards) >= live_limit:
            cards = cards[:live_limit]
        else:
            for _ in range(self.live_behavior_policy.max_scrolls):
                previous_count = len(cards)
                self._scroll_page()
                if self.live_behavior_policy.enabled:
                    self._pace_between(
                        self.live_behavior_policy.min_pause,
                        self.live_behavior_policy.max_pause,
                    )
                try:
                    new_cards = self._wait_for_more_elements(
                        selectors.LIVE_LIST_ITEMS,
                        previous_count=previous_count,
                    )
                except TimeoutException:
                    break
                cards = new_cards
                if len(cards) >= live_limit:
                    cards = cards[:live_limit]
                    break
        return cards


    def _parse_live_card(self, card: Any) -> _LiveCardData:
        """解析直播列表卡片中的身份、介绍和人数信息。"""
        link = card.find_element(
            *self._to_locator(selectors.LIVE_LINK_BUTTON)
        )
        source_url = link.get_attribute("href") or ""
        info_counts = selectors.require_minimum_elements(
            card.find_elements(
                *self._to_locator(selectors.LIVE_INFO_COUNTS)
            ),
            selectors.LIVE_INFO_COUNTS,
            2,
        )
        return _LiveCardData(
            live_room_id=_extract_source_id(source_url),
            source_url=source_url,
            account_name=card.find_element(
                *self._to_locator(
                    selectors.LIVE_ACCOUNT_NAME
                )
            ).text.strip(),
            introduction=card.find_element(
                *self._to_locator(
                    selectors.LIVE_INTRODUCTION
                )
            ).text.strip(),
            viewer_count=parsers.parse_viewer_count(
                info_counts[0].text
            ),
            follower_count=parsers.parse_follower_count(
                info_counts[1].text
            ),
        )

    def _parse_live_product_count(self) -> Optional[int]:
        """解析当前直播详情页展示的商品数量。"""
        title = self.driver.find_element(
            *self._to_locator(
                selectors.LIVE_GOODS_TITLE
            )
        ).text
        return parsers.parse_live_product_count(title)

    def _build_live_record(
            self,
            live_room: _LiveCardData,
            keyword: str,
            product_count: Optional[int],
            detail_url: str,
    ) -> LiveRoomRecord:
        """将直播卡片和详情字段转换为统一直播记录。"""
        source_url = live_room.source_url or detail_url
        return LiveRoomRecord(
            run_id=self.run_id,
            engine=Engine.SELENIUM,
            keyword=keyword,
            live_room_id=(
                live_room.live_room_id
                or _extract_source_id(source_url)
            ),
            source_url=source_url,
            account_name=live_room.account_name,
            introduction=live_room.introduction,
            viewer_count=live_room.viewer_count,
            follower_count=live_room.follower_count,
            product_count=product_count,
        )



    def _parse_comment_item(
        self,
        item: Any,
        product: _ProductCardData,
        keyword: str,
    ) -> CommentRecord:
        """解析单条评论并建立与商品的关联。"""
        user_name = item.find_element(
            *self._to_locator(selectors.COMMENT_USER_NAME)
        ).text.strip()

        sku_text = item.find_element(
            *self._to_locator(selectors.COMMENT_SKU)
        ).text.strip()

        sku_info = sku_text.rsplit("：", 1)[-1]
        if _DATE_ONLY.fullmatch(sku_info):
            sku_info = "无"

        content = item.find_element(
            *self._to_locator(selectors.COMMENT_CONTENT)
        ).text.strip()

        return CommentRecord(
            run_id=self.run_id,
            engine=Engine.SELENIUM,
            keyword=keyword,
            product_id=product.product_id,
            source_url=product.source_url,
            user_name=user_name,
            sku_info=sku_info,
            content=content,
        )

    def _parse_total_comments(self) -> Optional[int]:
        detail_title = self.driver.find_element(
            *self._to_locator(selectors.DETAIL_TITLE)
        ).text.strip()

        return parsers.parse_comment_count(detail_title)

    def _wait_comment_growth(
        self,
        drawer: Any,
        previous_count: int,
    ) -> bool:
        try:
            self._wait_for_more_elements(
                selectors.COMMENT_ITEMS,
                previous_count=previous_count,
                root=drawer,
            )
            return True
        except TimeoutException:
            return False

    def _collect_comments(
        self,
        *,
        drawer: Any,
        product: _ProductCardData,
        keyword: str,
        comment_limit: int,
    ) -> List[CommentRecord]:
        records: List[CommentRecord] = []
        locator = self._to_locator(selectors.COMMENT_ITEMS)

        while len(records) < comment_limit:
            items = drawer.find_elements(*locator)
            while (
                len(records) < len(items)
                and len(records) < comment_limit
            ):
                records.append(
                    self._parse_comment_item(
                        items[len(records)],
                        product,
                        keyword,
                    )
                )
            if len(records) >= comment_limit:
                break

            previous_count = len(items)
            self._scroll_element(drawer)
            self._pace_comment_load()
            if not self._wait_comment_growth(
                drawer,
                previous_count,
            ):
                break
        return records

    def _collect_product_with_retries(
        self,
        *,
        card: Any,
        product: _ProductCardData,
        keyword: str,
        comment_limit: int,
    ) -> Optional[Tuple[ProductRecord, List[CommentRecord]]]:
        for attempt in range(self.retry_policy.max_attempts):
            try:
                with self._temporary_new_tab(lambda: self._move_and_click(card)):
                    self._check_block("product_detail")
                    self._wait_product_detail()
                    if self.behavior_policy.enabled:
                        self._pace()
                    self._perform_read_only_behavior()
                    total_comments = self._parse_total_comments()
                    if not self.filter_policy.accepts_comments(
                        total_comments
                    ):
                        return None
                    open_button = self._wait_comment_open_button()
                    self._move_and_click(open_button)
                    drawer = self._wait_comment_drawer()
                    if self.behavior_policy.enabled:
                        self._pace()
                    comments = self._collect_comments(
                        drawer=drawer,
                        product=product,
                        keyword=keyword,
                        comment_limit=comment_limit,
                    )
                    return (
                        self._build_product_record(product, keyword),
                        comments,
                    )
            except (CollectionBlockedError, KeyboardInterrupt):
                raise
            except Exception as error:
                if attempt >= self.retry_policy.max_retries:
                    self._capture_failure_evidence(
                        "product_detail",
                        error,
                    )
                    raise
                self._sleep(self.retry_policy.delays[attempt])

        raise RuntimeError("不可达的商品详情重试状态")


    def _build_product_record(
        self,
        product: _ProductCardData,
        keyword: str,
    ) -> ProductRecord:
        """将内部商品数据转换为统一导出模型。"""
        return ProductRecord(
            run_id=self.run_id,
            engine=Engine.SELENIUM,
            keyword=keyword,
            product_id=product.product_id,
            source_url=product.source_url,
            name=product.name,
            price=product.price,
            sales_count=product.sales_count,
        )

    def _pace_between(
        self,
        minimum: float,
        maximum: float,
    ) -> None:
        """在给定区间内随机选择本次只读交互的停顿时长。"""
        delay = self._random.uniform(minimum, maximum)
        self._sleep(delay)

    def _pace(self) -> None:
        """按照商品详情页行为策略执行一次随机停顿。"""
        self._pace_between(
            self.behavior_policy.min_pause,
            self.behavior_policy.max_pause,
        )

    def _pace_comment_load(self) -> None:
        policy = self.behavior_policy
        if policy.enabled:
            self._pace_between(
                policy.comment_min_pause,
                policy.comment_max_pause,
            )

    def _pace_product_transition(self) -> None:
        policy = self.behavior_policy
        if policy.enabled:
            self._pace_between(
                policy.product_min_pause,
                policy.product_max_pause,
            )

    def _pace_page_transition(self) -> None:
        policy = self.behavior_policy
        if policy.enabled:
            self._pace_between(
                policy.page_min_pause,
                policy.page_max_pause,
            )

    def _pace_live_room_transition(self) -> None:
        policy = self.live_behavior_policy
        if policy.enabled:
            self._pace_between(
                policy.room_min_pause,
                policy.room_max_pause,
            )

    def _scroll_page(self, distance: int = 400) -> None:
        """通过 Selenium 动作链按指定距离滚动当前页面。"""
        ActionChains(self.driver).scroll_by_amount(
            0,
            distance,
        ).perform()

    def _scroll_element(
        self,
        element: Any,
        distance: int = 10000,
    ) -> None:
        """通过受控脚本滚动指定的内部容器。"""
        self.driver.execute_script(
            "arguments[0].scrollTop += arguments[1];",
            element,
            distance,
        )

    def _move_and_click(self, element: Any) -> None:
        """将鼠标移动到元素后通过动作链执行单击。"""
        ActionChains(self.driver).move_to_element(
            element
        ).click().perform()

    def _perform_read_only_behavior(self) -> None:
        """执行次数受限的商品详情标签查看和页面滚动。"""
        policy = self.behavior_policy
        if not policy.enabled:
            return

        available_tabs = []
        for selector in _SAFE_DETAIL_TABS:
            elements = self.driver.find_elements(
                *self._to_locator(selector)
            )
            if elements:
                available_tabs.append(elements[0])

        view_count = min(
            policy.max_tab_views,
            len(available_tabs),
        )
        selected_tabs = self._random.sample(
            available_tabs,
            view_count,
        )

        for element in selected_tabs:
            self._move_and_click(element)
            self._pace()

        for _ in range(policy.max_scrolls):
            self._scroll_page()
            self._pace()

    def _perform_live_read_only_behavior(self) -> None:
        """执行次数受限的直播间停留和页面滚动。"""
        policy = self.live_behavior_policy
        if not policy.enabled:
            return

        self._pace_between(
            policy.min_pause,
            policy.max_pause,
        )

        for _ in range(policy.max_scrolls):
            self._scroll_page()
            self._pace_between(
                policy.min_pause,
                policy.max_pause,
            )

    def _new_window_handles(
        self,
        before_handles: Sequence[str],
    ) -> Sequence[str]:
        """返回相较操作前新增的全部窗口句柄。"""
        before_set = set(before_handles)
        return tuple(
            handle
            for handle in self.driver.window_handles
            if handle not in before_set
        )

    def _cleanup_new_windows(
        self,
        before_handles: Sequence[str],
        original_handle: str,
        *,
        suppress_errors: bool,
    ) -> None:
        """关闭本次操作新增的窗口，并尝试切回原窗口。"""
        cleanup_error = None

        for handle in self._new_window_handles(before_handles):
            try:
                self.driver.switch_to.window(handle)
                self.driver.close()
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc

        try:
            if original_handle in self.driver.window_handles:
                self.driver.switch_to.window(original_handle)
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc

        if cleanup_error is not None and not suppress_errors:
            raise cleanup_error

    @contextmanager
    def _temporary_new_tab(
        self,
        opener: Callable[[], Any],
    ) -> Iterator[str]:
        """在临时新标签页内执行采集，并在退出时恢复现场。"""
        original_handle = self.driver.current_window_handle
        before_handles = tuple(self.driver.window_handles)
        body_failed = False

        try:
            opener()
            self._new_wait().until(
                EC.new_window_is_opened(before_handles)
            )

            new_handles = self._new_window_handles(before_handles)
            if not new_handles:
                raise RuntimeError("未检测到新标签页")

            target_handle = new_handles[0]
            self.driver.switch_to.window(target_handle)
            yield target_handle
        except BaseException:
            body_failed = True
            raise
        finally:
            self._cleanup_new_windows(
                before_handles,
                original_handle,
                suppress_errors=body_failed,
            )



__all__ = ["SeleniumCollector"]
