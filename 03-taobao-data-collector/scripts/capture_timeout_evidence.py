"""在离线页面触发 Selenium 元素超时并保存截图证据。"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from taobao_collector.models import Engine  # noqa: E402
from taobao_collector.observability import ScreenshotStore  # noqa: E402


RUN_ID = "phase4-controlled-timeout"
STEP = "element_timeout"


def main() -> int:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-first-run")
    driver_path = shutil.which("chromedriver")
    driver = webdriver.Chrome(
        service=(Service(driver_path) if driver_path else Service()),
        options=options,
    )
    store = ScreenshotStore(PROJECT_ROOT / "artifacts" / "screenshots")
    html = """
    <html><head><meta charset="utf-8"><title>受控超时证据</title></head>
    <body>
<h1>受控元素超时测试</h1>
      <p>页面故意不包含 id=never-appears 的元素。</p>
    </body></html>
    """

    try:
        driver.get(f"data:text/html;charset=utf-8,{quote(html)}")
        try:
            WebDriverWait(driver, 0.5).until(
                EC.visibility_of_element_located(
                    (By.ID, "never-appears")
                )
            )
        except TimeoutException as exc:
            screenshot_path = store.capture(
                driver,
                run_id=RUN_ID,
                engine=Engine.SELENIUM,
                step=STEP,
            )
            run_directory = screenshot_path.parents[2]
            evidence = {
                "run_id": RUN_ID,
                "engine": Engine.SELENIUM.value,
                "step": STEP,
                "scenario": "离线页面等待不存在元素",
                "exception_type": type(exc).__name__,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "screenshot": str(
                    screenshot_path.relative_to(PROJECT_ROOT)
                ),
            }
            (run_directory / "controlled_evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(screenshot_path)
            return 0
        raise RuntimeError("受控场景未触发预期的 TimeoutException")
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
