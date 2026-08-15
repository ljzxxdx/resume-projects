"""DrissionPage 商品、评论与直播采集流程。"""

from __future__ import annotations

import re
import random
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlparse

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
    LiveRoomRecord,
    ProductRecord,
)


HOME_URL = "https://www.taobao.com/"
_DATE_ONLY = re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$")
_SAFE_DETAIL_TABS = (
    selectors.PRODUCT_PARAMETERS_TAB,
    selectors.PRODUCT_MEDIA_TAB,
    selectors.STORE_RECOMMENDATION_TAB,
    selectors.RELATED_PRODUCTS_TAB,
)


@dataclass(frozen=True)
class _ProductCardData:
    product_id: str
    source_url: str
    name: str
    price: Optional[Decimal]
    sales_count: Optional[int]


@dataclass(frozen=True)
class _LiveCardData:
    live_room_id: str
    source_url: str
    account_name: str
    introduction: str
    viewer_count: Optional[int]
    follower_count: Optional[int]


def _extract_source_id(source_url: str) -> str:
    parsed = urlparse(source_url)
    query = parse_qs(parsed.query)
    for name in ("id", "item_id", "roomId", "room_id", "liveId"):
        values = query.get(name)
        if values and values[0]:
            return values[0]

    path_tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return path_tail or source_url


def _validate_limit(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} 必须是正整数")


class DrissionCollector:
    """使用已创建的 DrissionPage 浏览器会话完成只读采集。"""

    def __init__(
        self,
        browser: Any,
        *,
        run_id: str,
        filter_policy: Optional[ProductFilterPolicy] = None,
        behavior_policy: Optional[ReadOnlyBehaviorPolicy] = None,
        live_behavior_policy: Optional[LiveBehaviorPolicy] = None,
        retry_policy: Optional[RetryPolicy] = None,
        wait_policy: Optional[WaitPolicy] = None,
        sleeper: Any = time.sleep,
        poll_sleeper: Any = time.sleep,
        random_source: Optional[random.Random] = None,
        block_guard: Optional[Any] = None,
        login_waiter: Optional[Any] = None,
    ) -> None:
        normalized_run_id = run_id.strip()
        if not normalized_run_id:
            raise ValueError("run_id 不能为空")
        self.browser = browser
        self.main_tab = browser.latest_tab
        self.run_id = normalized_run_id
        self.filter_policy = filter_policy or ProductFilterPolicy()
        self.behavior_policy = (
            behavior_policy or ReadOnlyBehaviorPolicy()
        )
        self.live_behavior_policy = (
            live_behavior_policy or LiveBehaviorPolicy()
        )
        self.retry_policy = retry_policy or RetryPolicy()
        self.wait_policy = wait_policy or WaitPolicy(
            timeout=float(getattr(self.main_tab, "timeout", 20.0))
        )
        self._sleep = sleeper
        self._poll_sleep = poll_sleeper
        self._random = random_source or random.Random()
        self.block_guard = block_guard
        self.login_waiter = login_waiter
        self.last_failure_screenshot = None

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

        self._open_home()
        search_input = self._wait_product_search_input()
        results_tab = self._open_product_results(
            search_input,
            normalized_keyword,
        )
        products: List[ProductRecord] = []
        comments: List[CommentRecord] = []
        seen_page_signatures = set()
        try:
            while len(products) < product_limit:
                cards = self._wait_product_cards(results_tab)
                page_signature = self._product_page_signature(cards)
                if page_signature in seen_page_signatures:
                    break
                seen_page_signatures.add(page_signature)

                for card in cards:
                    if len(products) >= product_limit:
                        break

                    card_data = self._parse_product_card(card)
                    if not self.filter_policy.accepts_sales(
                        card_data.sales_count
                    ):
                        continue

                    collected = self._collect_product_with_retries(
                        card=card,
                        product=card_data,
                        keyword=normalized_keyword,
                        comment_limit=comment_limit,
                    )
                    self._pace_product_transition()
                    if collected is None:
                        continue
                    product_record, product_comments = collected
                    products.append(product_record)
                    comments.extend(product_comments)

                if len(products) >= product_limit:
                    break
                if not self._open_next_product_page(results_tab):
                    break
                if not self._wait_product_page_change(
                    results_tab,
                    page_signature,
                ):
                    break
                self._pace_page_transition()
        finally:
            if results_tab is not self.main_tab:
                self._close_tab(
                    results_tab,
                    suppress_errors=sys.exc_info()[0] is not None,
                )

        return CollectionResult(
            products=tuple(products),
            comments=tuple(comments),
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
        try:
            self._open_home()
            live_navigation = self._wait_live_navigation()
            results_tab = self._open_live_portal(live_navigation)
        except (CollectionBlockedError, KeyboardInterrupt):
            raise
        except Exception:
            self._capture_failure_evidence(
                self.main_tab,
                "live_unavailable",
            )
            raise

        live_rooms: List[LiveRoomRecord] = []
        try:
            search_input = self._wait_live_search_input(results_tab)
            self._submit_live_search(
                search_input,
                normalized_keyword,
            )
            cards = self._wait_live_cards(results_tab)
            for card in cards[:live_limit]:
                card_data = self._parse_live_card(card)
                detail_tab = self._open_live_detail(card)
                try:
                    self._wait_live_detail(detail_tab)
                    self._perform_live_read_only_behavior(detail_tab)
                    product_count = self._parse_live_product_count(
                        detail_tab
                    )
                    live_rooms.append(
                        self._build_live_record(
                            card_data,
                            normalized_keyword,
                            product_count,
                            detail_tab.url,
                        )
                    )
                except (CollectionBlockedError, KeyboardInterrupt):
                    raise
                except Exception:
                    self._capture_failure_evidence(
                        detail_tab,
                        "live_unavailable",
                    )
                    raise
                finally:
                    self._close_tab(
                        detail_tab,
                        suppress_errors=sys.exc_info()[0] is not None,
                    )
                self._pace_live_room_transition()
        except (CollectionBlockedError, KeyboardInterrupt):
            raise
        except Exception:
            self._capture_failure_evidence(
                results_tab,
                "live_unavailable",
            )
            raise
        finally:
            self._close_tab(
                results_tab,
                suppress_errors=sys.exc_info()[0] is not None,
            )

        return CollectionResult(live_rooms=tuple(live_rooms))

    def _open_home(self) -> None:
        self.main_tab.get(HOME_URL)

    def _wait_product_search_input(self) -> Any:
        locator = selectors.SEARCH_INPUT.drission_locator
        self._wait_loaded(self.main_tab, locator, "home")
        return self.main_tab.ele(locator)

    def _open_product_results(
        self,
        search_input: Any,
        keyword: str,
    ) -> Any:
        def open_results() -> Any:
            before_ids = self._known_tab_ids()
            previous_url = str(getattr(self.main_tab, "url", "") or "")
            search_input.input(f"{keyword}\n")
            opened_tabs = self._new_tabs_since(before_ids)
            if opened_tabs:
                return opened_tabs[0]
            current_url = str(getattr(self.main_tab, "url", "") or "")
            if current_url and current_url != previous_url:
                return self.main_tab
            tab_id = self.browser.wait.new_tab(
                timeout=self.wait_policy.timeout,
                curr_tab=self.main_tab,
                raise_err=True,
            )
            return self.browser.get_tab(tab_id)

        return self._open_new_tab(
            open_results,
            step="product_results",
        )

    def _wait_product_cards(self, results_tab: Any) -> Sequence[Any]:
        locator = selectors.PRODUCT_LIST_ITEMS.drission_locator
        self._wait_loaded(
            results_tab,
            locator,
            "product_results",
        )
        return results_tab.eles(locator)

    def _wait_loaded(
        self,
        tab: Any,
        locator: str,
        step: str,
    ) -> None:
        self._check_block(tab, step)
        try:
            tab.wait.eles_loaded(
                locator,
                timeout=self.wait_policy.timeout,
                raise_err=True,
            )
        except Exception:
            self._check_block(tab, step)
            raise
        self._check_block(tab, step)

    def _check_block(self, tab: Any, step: str) -> None:
        if self.login_waiter is not None:
            self.login_waiter.wait(
                tab,
                run_id=self.run_id,
                engine=Engine.DRISSION,
                step=step,
            )
        if self.block_guard is None:
            return
        self.block_guard.inspect(
            tab,
            run_id=self.run_id,
            engine=Engine.DRISSION,
            step=step,
        )

    @staticmethod
    def _product_page_signature(cards: Sequence[Any]) -> tuple:
        return tuple(
            (card.attr("href") or "").strip()
            for card in cards
        )

    def _wait_product_page_change(
        self,
        results_tab: Any,
        previous_signature: tuple,
    ) -> bool:
        deadline = time.monotonic() + self.wait_policy.timeout
        while True:
            self._check_block(results_tab, "product_pagination")
            cards = self._wait_product_cards(results_tab)
            if (
                self._product_page_signature(cards)
                != previous_signature
            ):
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._poll_sleep(
                min(self.wait_policy.poll_interval, remaining)
            )

    @staticmethod
    def _open_next_product_page(results_tab: Any) -> bool:
        locator = selectors.NEXT_PAGE_BUTTON.drission_locator
        buttons = results_tab.eles(locator)
        if not buttons:
            return False

        button = buttons[-1]
        if (
            button.attr("disabled") is not None
            or button.attr("aria-disabled") == "true"
        ):
            return False
        button.scroll.to_see()
        button.click()
        return True

    def _parse_product_card(self, card: Any) -> _ProductCardData:
        source_url = card.attr("href") or ""
        name = card.ele(
            selectors.PRODUCT_TITLE.drission_locator
        ).text.strip()
        price_text = card.ele(
            selectors.PRODUCT_PRICE.drission_locator
        ).text
        sales_text = card.ele(
            selectors.PRODUCT_SALES.drission_locator
        ).text
        return _ProductCardData(
            product_id=_extract_source_id(source_url),
            source_url=source_url,
            name=name,
            price=parsers.parse_price(price_text),
            sales_count=parsers.parse_sales_count(sales_text),
        )

    def _open_product_detail(self, card: Any) -> Any:
        card.scroll.to_see()
        return self._open_new_tab(
            card.click.for_new_tab,
            step="product_detail",
        )

    def _collect_product_with_retries(
        self,
        *,
        card: Any,
        product: _ProductCardData,
        keyword: str,
        comment_limit: int,
    ) -> Optional[Tuple[ProductRecord, List[CommentRecord]]]:
        for attempt in range(self.retry_policy.max_attempts):
            detail_tab = None
            try:
                detail_tab = self._open_product_detail(card)
                self._wait_product_detail(detail_tab)
                if self.behavior_policy.enabled:
                    self._pace()
                self._perform_read_only_behavior(detail_tab)
                total_comments = self._parse_total_comments(detail_tab)
                if not self.filter_policy.accepts_comments(
                    total_comments
                ):
                    return None

                open_button = self._wait_comment_open_button(detail_tab)
                self._open_comment_drawer(open_button)
                drawer = self._wait_comment_drawer(detail_tab)
                if self.behavior_policy.enabled:
                    self._pace()
                comments = self._collect_comments(
                    detail_tab=detail_tab,
                    drawer=drawer,
                    product=product,
                    keyword=keyword,
                    comment_limit=comment_limit,
                )
                return (
                    self._build_product_record(product, keyword),
                    comments,
                )
            except CollectionBlockedError:
                raise
            except Exception:
                if attempt >= self.retry_policy.max_retries:
                    raise
                self._sleep(self.retry_policy.delays[attempt])
            finally:
                if detail_tab is not None:
                    self._close_tab(
                        detail_tab,
                        suppress_errors=sys.exc_info()[0] is not None,
                    )

        raise RuntimeError("不可达的商品详情重试状态")

    def _wait_product_detail(self, detail_tab: Any) -> None:
        self._wait_loaded(
            detail_tab,
            selectors.DETAIL_TITLE.drission_locator,
            "product_detail",
        )

    def _parse_total_comments(
        self,
        detail_tab: Any,
    ) -> Optional[int]:
        title = detail_tab.ele(
            selectors.DETAIL_TITLE.drission_locator
        ).text
        return parsers.parse_comment_count(title)

    def _perform_read_only_behavior(self, detail_tab: Any) -> None:
        policy = self.behavior_policy
        if not policy.enabled:
            return

        available_tabs = []
        for selector in _SAFE_DETAIL_TABS:
            element = detail_tab.ele(
                selector.drission_locator,
                timeout=0,
            )
            if element is not None:
                available_tabs.append(element)

        view_count = min(policy.max_tab_views, len(available_tabs))
        for element in self._random.sample(
            available_tabs,
            view_count,
        ):
            element.scroll.to_see()
            detail_tab.actions.move_to(ele_or_loc=element)
            element.click()
            self._pace()

        for _ in range(policy.max_scrolls):
            detail_tab.scroll.down(400)
            self._pace()

    def _pace(self) -> None:
        self._pace_between(
            self.behavior_policy.min_pause,
            self.behavior_policy.max_pause,
        )

    def _pace_between(self, minimum: float, maximum: float) -> None:
        delay = self._random.uniform(minimum, maximum)
        self._sleep(delay)

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

    def _perform_live_read_only_behavior(self, detail_tab: Any) -> None:
        policy = self.live_behavior_policy
        if not policy.enabled:
            return

        self._pace_between(policy.min_pause, policy.max_pause)
        self._check_block(detail_tab, "live_detail")
        for _ in range(policy.max_scrolls):
            detail_tab.scroll.down(400)
            self._pace_between(policy.min_pause, policy.max_pause)
            self._check_block(detail_tab, "live_detail")

    def _wait_comment_open_button(self, detail_tab: Any) -> Any:
        locator = selectors.COMMENT_OPEN_BUTTON.drission_locator
        self._wait_loaded(detail_tab, locator, "comment_open")
        return detail_tab.ele(locator)

    @staticmethod
    def _open_comment_drawer(open_button: Any) -> None:
        open_button.scroll.to_see()
        open_button.click()

    def _wait_comment_drawer(self, detail_tab: Any) -> Any:
        locator = selectors.COMMENT_DRAWER.drission_locator
        self._wait_loaded(detail_tab, locator, "comment_drawer")
        return detail_tab.ele(locator)

    def _collect_comments(
        self,
        *,
        detail_tab: Any,
        drawer: Any,
        product: _ProductCardData,
        keyword: str,
        comment_limit: int,
    ) -> List[CommentRecord]:
        records: List[CommentRecord] = []
        locator = selectors.COMMENT_ITEMS.drission_locator

        while len(records) < comment_limit:
            items = drawer.eles(locator)
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
            self._scroll_comments(drawer)
            self._pace_comment_load()
            if not self._wait_comment_growth(
                detail_tab,
                drawer,
                locator,
                previous_count,
            ):
                break

        return records

    @staticmethod
    def _scroll_comments(drawer: Any) -> None:
        drawer.scroll.down(10000)

    def _wait_comment_growth(
        self,
        detail_tab: Any,
        drawer: Any,
        locator: str,
        previous_count: int,
    ) -> bool:
        deadline = time.monotonic() + self.wait_policy.timeout
        while True:
            self._check_block(detail_tab, "comment_scroll")
            if len(drawer.eles(locator)) > previous_count:
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._poll_sleep(
                min(self.wait_policy.poll_interval, remaining)
            )

    def _parse_comment_item(
        self,
        item: Any,
        product: _ProductCardData,
        keyword: str,
    ) -> CommentRecord:
        user_name = item.ele(
            selectors.COMMENT_USER_NAME.drission_locator
        ).text.strip()
        sku_text = item.ele(
            selectors.COMMENT_SKU.drission_locator
        ).text.strip()
        sku_info = sku_text.rsplit("：", 1)[-1]
        if _DATE_ONLY.fullmatch(sku_info):
            sku_info = "无"
        content = item.ele(
            selectors.COMMENT_CONTENT.drission_locator
        ).text.strip()
        return CommentRecord(
            run_id=self.run_id,
            engine=Engine.DRISSION,
            keyword=keyword,
            product_id=product.product_id,
            source_url=product.source_url,
            user_name=user_name,
            sku_info=sku_info,
            content=content,
        )

    def _build_product_record(
        self,
        product: _ProductCardData,
        keyword: str,
    ) -> ProductRecord:
        return ProductRecord(
            run_id=self.run_id,
            engine=Engine.DRISSION,
            keyword=keyword,
            product_id=product.product_id,
            source_url=product.source_url,
            name=product.name,
            price=product.price,
            sales_count=product.sales_count,
        )

    def _wait_live_navigation(self) -> Any:
        locator = selectors.LIVE_NAV_TAB.drission_locator
        self._wait_loaded(self.main_tab, locator, "home")
        return self.main_tab.ele(locator)

    def _open_live_portal(self, live_navigation: Any) -> Any:
        def open_portal() -> Any:
            live_navigation.click()
            tab_id = self.browser.wait.new_tab(
                curr_tab=self.main_tab,
                raise_err=True,
            )
            return self.browser.get_tab(tab_id)

        return self._open_new_tab(
            open_portal,
            step="live_results",
            capture_failure=True,
        )

    def _wait_live_search_input(self, results_tab: Any) -> Any:
        locator = selectors.LIVE_SEARCH_INPUT.drission_locator
        self._wait_loaded(results_tab, locator, "live_results")
        return results_tab.ele(locator)

    @staticmethod
    def _submit_live_search(search_input: Any, keyword: str) -> None:
        search_input.input(f"{keyword}\n")

    def _wait_live_cards(self, results_tab: Any) -> Sequence[Any]:
        locator = selectors.LIVE_LIST_ITEMS.drission_locator
        self._wait_loaded(results_tab, locator, "live_results")
        return results_tab.eles(locator)

    def _parse_live_card(self, card: Any) -> _LiveCardData:
        link = card.ele(selectors.LIVE_LINK_BUTTON.drission_locator)
        source_url = link.attr("href") or ""
        info_counts = selectors.require_minimum_elements(
            card.eles(selectors.LIVE_INFO_COUNTS.drission_locator),
            selectors.LIVE_INFO_COUNTS,
            2,
        )
        return _LiveCardData(
            live_room_id=_extract_source_id(source_url),
            source_url=source_url,
            account_name=card.ele(
                selectors.LIVE_ACCOUNT_NAME.drission_locator
            ).text.strip(),
            introduction=card.ele(
                selectors.LIVE_INTRODUCTION.drission_locator
            ).text.strip(),
            viewer_count=parsers.parse_viewer_count(
                info_counts[0].text
            ),
            follower_count=parsers.parse_follower_count(
                info_counts[1].text
            ),
        )

    def _open_live_detail(self, card: Any) -> Any:
        link = card.ele(selectors.LIVE_LINK_BUTTON.drission_locator)
        return self._open_new_tab(
            link.click.for_new_tab,
            step="live_detail",
            capture_failure=True,
        )

    def _wait_live_detail(self, detail_tab: Any) -> None:
        self._wait_loaded(
            detail_tab,
            selectors.LIVE_GOODS_TITLE.drission_locator,
            "live_detail",
        )

    @staticmethod
    def _parse_live_product_count(
        detail_tab: Any,
    ) -> Optional[int]:
        title = detail_tab.ele(
            selectors.LIVE_GOODS_TITLE.drission_locator
        ).text
        return parsers.parse_live_product_count(title)

    def _build_live_record(
        self,
        live_room: _LiveCardData,
        keyword: str,
        product_count: Optional[int],
        detail_url: str,
    ) -> LiveRoomRecord:
        source_url = live_room.source_url or detail_url
        return LiveRoomRecord(
            run_id=self.run_id,
            engine=Engine.DRISSION,
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

    @staticmethod
    def _close_tab(
        tab: Any,
        *,
        suppress_errors: bool = False,
    ) -> None:
        try:
            tab.close()
        except Exception:
            if not suppress_errors:
                raise

    def _known_tab_ids(self) -> Optional[Set[str]]:
        try:
            return set(self.browser.tab_ids)
        except Exception:
            return None

    def _new_tabs_since(
        self,
        before_ids: Optional[Set[str]],
    ) -> List[Any]:
        if before_ids is None:
            return []
        current_ids = self._known_tab_ids()
        if current_ids is None:
            return []
        new_ids = current_ids - before_ids
        tabs = []
        for tab_id in new_ids:
            try:
                tabs.append(self.browser.get_tab(tab_id))
            except Exception:
                close_tabs = getattr(self.browser, "close_tabs", None)
                if close_tabs is None:
                    continue
                try:
                    close_tabs(tab_id)
                except Exception:
                    pass
        return tabs

    def _close_new_tabs(
        self,
        before_ids: Optional[Set[str]],
    ) -> None:
        for tab in self._new_tabs_since(before_ids):
            self._close_tab(tab, suppress_errors=True)

    def _recover_open_failure(
        self,
        before_ids: Optional[Set[str]],
        *,
        step: str,
        capture_failure: bool,
    ) -> None:
        blocked_error = None
        for tab in self._new_tabs_since(before_ids):
            try:
                self._check_block(tab, step)
            except CollectionBlockedError as exc:
                blocked_error = exc
            if blocked_error is None and capture_failure:
                self._capture_failure_evidence(
                    tab,
                    "live_unavailable",
                )
            self._close_tab(tab, suppress_errors=True)

        if blocked_error is not None:
            raise blocked_error

        fallback_tab = getattr(
            self.browser,
            "latest_tab",
            self.main_tab,
        )
        self._check_block(fallback_tab, step)
        if capture_failure:
            self._capture_failure_evidence(
                fallback_tab,
                "live_unavailable",
            )

    def _open_new_tab(
        self,
        opener: Callable[[], Any],
        *,
        step: str,
        capture_failure: bool = False,
    ) -> Any:
        before_ids = self._known_tab_ids()
        try:
            return opener()
        except (CollectionBlockedError, KeyboardInterrupt):
            self._close_new_tabs(before_ids)
            raise
        except Exception:
            self._recover_open_failure(
                before_ids,
                step=step,
                capture_failure=capture_failure,
            )
            raise

    def _capture_failure_evidence(
        self,
        tab: Any,
        step: str,
    ) -> None:
        if self.last_failure_screenshot is not None:
            return
        store = getattr(
            self.block_guard,
            "screenshot_store",
            None,
        )
        if store is None:
            return
        try:
            self.last_failure_screenshot = store.capture(
                tab,
                run_id=self.run_id,
                engine=Engine.DRISSION,
                step=step,
            )
        except Exception:
            return


__all__ = ["DrissionCollector"]
