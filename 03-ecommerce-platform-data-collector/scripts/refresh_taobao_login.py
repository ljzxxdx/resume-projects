"""使用项目浏览器 Profile 交互式刷新淘宝登录态。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from taobao_collector.collectors.drission_session import (  # noqa: E402
    DrissionBrowserSession,
)
from taobao_collector.config import load_config  # noqa: E402
from taobao_collector.models import Engine  # noqa: E402
from taobao_collector.observability import (  # noqa: E402
    ManualLoginWaiter,
    ScreenshotStore,
)


LOGIN_URL = "https://login.taobao.com/member/login.jhtml"


def main() -> int:
    config = load_config()
    store = ScreenshotStore(config.screenshot_dir)
    waiter = ManualLoginWaiter(
        timeout=config.login_wait_timeout,
        poll_interval=config.poll_interval,
        screenshot_store=store,
    )
    with DrissionBrowserSession(config) as browser:
        tab = browser.latest_tab
        tab.get(LOGIN_URL)
        waiter.wait(
            tab,
            run_id="manual-login-refresh",
            engine=Engine.DRISSION,
            step="login_refresh",
        )
        print("登录态刷新完成，浏览器将正常关闭。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
