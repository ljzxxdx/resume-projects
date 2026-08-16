"""验证处理器的离线单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taobao_collector.models import Engine
from taobao_collector.verification import (
    DrissionSliderInteractor,
    LocalSliderDemoHandler,
    ManualVerificationHandler,
    SeleniumSliderInteractor,
    VerificationContext,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, delay: float) -> None:
        self.value += delay


class FakeInteractor:
    def __init__(self) -> None:
        self.pages = []

    def drag(self, page) -> None:
        self.pages.append(page)


class FakeElement:
    def __init__(self, width: float) -> None:
        self.size = {"width": width}
        self.rect = type("Rect", (), {"size": (width, 40)})()


class FakeSeleniumPage:
    current_url = "file:///project/scripts/slider_demo.html"

    def __init__(self) -> None:
        self.elements = {
            ("id", "slider-handle"): FakeElement(40),
            ("id", "slider-track"): FakeElement(300),
        }

    def find_element(self, by: str, value: str):
        return self.elements[(by, value)]


class FakeActionChains:
    last_instance = None

    def __init__(self, page) -> None:
        self.calls = []
        self.__class__.last_instance = self

    def move_to_element(self, element):
        self.calls.append(("move_to_element", element))
        return self

    def click_and_hold(self):
        self.calls.append(("click_and_hold",))
        return self

    def move_by_offset(self, x: float, y: float):
        self.calls.append(("move_by_offset", x, y))
        return self

    def release(self):
        self.calls.append(("release",))
        return self

    def perform(self):
        self.calls.append(("perform",))
        return self


class FakeDrissionActions:
    def __init__(self) -> None:
        self.calls = []

    def move_to(self, element):
        self.calls.append(("move_to", element))
        return self

    def hold(self):
        self.calls.append(("hold",))
        return self

    def move(self, x: float, y: float, duration: float):
        self.calls.append(("move", x, y, duration))
        return self

    def release(self):
        self.calls.append(("release",))
        return self


class FakeDrissionPage:
    url = "file:///project/scripts/slider_demo.html"

    def __init__(self) -> None:
        self.actions = FakeDrissionActions()
        self.elements = {
            "#slider-handle": FakeElement(40),
            "#slider-track": FakeElement(300),
        }

    def ele(self, locator: str):
        return self.elements[locator]


class VerificationTests(unittest.TestCase):
    def make_context(self, engine: Engine) -> VerificationContext:
        return VerificationContext(
            run_id="run-verification",
            engine=engine,
            step="product_results",
            reason="检测到滑块验证",
        )

    def test_manual_handler_waits_until_verification_disappears(self) -> None:
        clock = FakeClock()
        notices = []
        states = iter((True, False))
        handler = ManualVerificationHandler(
            timeout=5,
            poll_interval=0.5,
            clock=clock,
            sleeper=clock.sleep,
            notifier=notices.append,
        )

        handled = handler.handle(
            object(),
            context=self.make_context(Engine.SELENIUM),
            is_blocked=lambda: next(states),
        )

        self.assertTrue(handled)
        self.assertEqual(clock.value, 0.5)
        self.assertEqual(len(notices), 1)
        self.assertIn("手动完成", notices[0])

    def test_manual_handler_returns_false_after_timeout(self) -> None:
        clock = FakeClock()
        handler = ManualVerificationHandler(
            timeout=1,
            poll_interval=0.4,
            clock=clock,
            sleeper=clock.sleep,
            notifier=lambda message: None,
        )

        handled = handler.handle(
            object(),
            context=self.make_context(Engine.DRISSION),
            is_blocked=lambda: True,
        )

        self.assertFalse(handled)
        self.assertEqual(clock.value, 1.0)

    def test_manual_handler_declines_non_verification_block(self) -> None:
        notices = []
        checks = []
        handler = ManualVerificationHandler(
            timeout=1,
            poll_interval=0.1,
            notifier=notices.append,
        )
        context = VerificationContext(
            run_id="run-limited",
            engine=Engine.SELENIUM,
            step="product_results",
            reason="检测到访问受限提示",
        )

        handled = handler.handle(
            object(),
            context=context,
            is_blocked=lambda: checks.append(True) or True,
        )

        self.assertFalse(handled)
        self.assertEqual(notices, [])
        self.assertEqual(checks, [])

    def test_local_demo_handler_refuses_non_local_page(self) -> None:
        interactor = FakeInteractor()
        page = type("Page", (), {"current_url": "https://www.taobao.com/"})()
        handler = LocalSliderDemoHandler(
            interactors={Engine.SELENIUM: interactor},
            allowed_file_root=Path.cwd(),
        )

        handled = handler.handle(
            page,
            context=self.make_context(Engine.SELENIUM),
            is_blocked=lambda: True,
        )

        self.assertFalse(handled)
        self.assertEqual(interactor.pages, [])

    def test_local_demo_handler_drags_authorized_file_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page_path = root / "slider_demo.html"
            page_path.write_text("<html></html>", encoding="utf-8")
            page = type("Page", (), {"url": page_path.as_uri()})()
            interactor = FakeInteractor()
            states = iter((True, False))
            handler = LocalSliderDemoHandler(
                interactors={Engine.DRISSION: interactor},
                allowed_file_root=root,
                timeout=1,
                poll_interval=0.1,
                sleeper=lambda delay: None,
            )

            handled = handler.handle(
                page,
                context=self.make_context(Engine.DRISSION),
                is_blocked=lambda: next(states),
            )

            self.assertTrue(handled)
            self.assertEqual(interactor.pages, [page])

    def test_selenium_interactor_drags_remaining_track_width(self) -> None:
        page = FakeSeleniumPage()
        interactor = SeleniumSliderInteractor(
            handle_locator=("id", "slider-handle"),
            track_locator=("id", "slider-track"),
            action_chain_factory=FakeActionChains,
        )

        interactor.drag(page)

        self.assertIn(
            ("move_by_offset", 260.0, 0),
            FakeActionChains.last_instance.calls,
        )
        self.assertEqual(
            FakeActionChains.last_instance.calls[-1],
            ("perform",),
        )

    def test_drission_interactor_drags_remaining_track_width(self) -> None:
        page = FakeDrissionPage()
        interactor = DrissionSliderInteractor(
            handle_locator="#slider-handle",
            track_locator="#slider-track",
            duration=0.8,
        )

        interactor.drag(page)

        self.assertIn(("move", 260.0, 0, 0.8), page.actions.calls)
        self.assertEqual(page.actions.calls[-1], ("release",))

    def test_local_slider_demo_contains_expected_contract(self) -> None:
        html = (PROJECT_ROOT / "scripts" / "slider_demo.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="slider-track"', html)
        self.assertIn('id="slider-handle"', html)
        self.assertIn("请拖动下方滑块完成验证", html)
        self.assertIn('dataset.verified = "true"', html)


if __name__ == "__main__":
    unittest.main()
