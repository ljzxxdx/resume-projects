"""用 lxml 将脱敏 HTML 样本适配为两种采集器所需的最小元素接口。"""

from __future__ import annotations

from pathlib import Path
from typing import List

from lxml import html
from selenium.webdriver.common.by import By

from taobao_collector.selectors import ALL_SELECTORS, Selector


class FixtureSelectorMissingError(LookupError):
    """HTML 样本与已登记选择器不匹配。"""


def _selector_description(by: str, query: str) -> str:
    for selector in ALL_SELECTORS:
        if selector.selenium_locator == (by, query):
            return (
                f"缺失选择器 {selector.name}：{selector.purpose}；"
                f"表达式={query}"
            )
    return f"缺失未登记选择器：定位方式={by}；表达式={query}"


class FixtureElement:
    """同时模拟 Selenium WebElement 与 DrissionPage ChromiumElement。"""

    def __init__(self, element) -> None:
        self._element = element

    @property
    def text(self) -> str:
        return " ".join("".join(self._element.itertext()).split())

    def get_attribute(self, name: str):
        return self._element.get(name)

    def attr(self, name: str):
        return self.get_attribute(name)

    def find_elements(self, by: str, query: str) -> List["FixtureElement"]:
        if by == By.XPATH:
            matched = self._element.xpath(query)
        elif by == By.CSS_SELECTOR:
            matched = self._element.cssselect(query)
        else:
            raise ValueError(f"HTML样本不支持定位方式：{by}")
        return [FixtureElement(element) for element in matched]

    def find_element(self, by: str, query: str) -> "FixtureElement":
        matched = self.find_elements(by, query)
        if not matched:
            raise FixtureSelectorMissingError(
                _selector_description(by, query)
            )
        return matched[0]

    @staticmethod
    def _drission_locator(locator: str):
        strategy, separator, query = locator.partition(":")
        if not separator or strategy not in {"xpath", "css"}:
            raise ValueError(f"HTML样本无法识别Drission定位器：{locator}")
        by = By.XPATH if strategy == "xpath" else By.CSS_SELECTOR
        return by, query

    def ele(self, locator: str) -> "FixtureElement":
        return self.find_element(*self._drission_locator(locator))

    def eles(self, locator: str) -> List["FixtureElement"]:
        return self.find_elements(*self._drission_locator(locator))


class FixtureDocument(FixtureElement):
    """保留原始文本的离线 HTML 文档。"""

    def __init__(self, source: str) -> None:
        self.source = source
        super().__init__(html.fromstring(source))

    @classmethod
    def from_file(cls, path: Path) -> "FixtureDocument":
        return cls(path.read_text(encoding="utf-8"))

    @classmethod
    def from_html(cls, source: str) -> "FixtureDocument":
        return cls(source)

    def required(self, selector: Selector) -> FixtureElement:
        return self.find_element(*selector.selenium_locator)


class FixtureBrowser:
    """只实现 DrissionCollector 构造阶段所需的浏览器接口。"""

    def __init__(self, document: FixtureDocument) -> None:
        self.latest_tab = document
