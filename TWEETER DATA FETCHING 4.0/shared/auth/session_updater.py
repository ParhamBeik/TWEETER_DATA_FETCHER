import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from playwright.sync_api import sync_playwright, Request

logger = logging.getLogger(__name__)

class SessionUpdater:
    """
    مدیریت و به‌روزرسانی پارامترهای احراز هویت توییتر.
    از Playwright برای استخراج x-client-transaction-id و کوکی‌های جدید (ct0) استفاده می‌کند.
    """

    def __init__(self):
        # مسیر فایل کانفیگ بر اساس ساختار پروژه
        self.config_path = Path(__file__).resolve().parents[1] / "config" / "config.json"
        self._target_url = "https://twitter.com/home"
        self._graphql_indicator = "/graphql/"

    def _load_config(self) -> Dict[str, Any]:
        """بارگذاری فایل کانفیگ اصلی."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found at: {self.config_path}")
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _save_config(self, config_data: Dict[str, Any]) -> None:
        """ذخیره تغییرات در فایل کانفیگ."""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Config successfully updated at {self.config_path}")

    def update_session(self) -> bool:
        """
        اجرای مرورگر، تزریق کوکی‌های فعلی، شنود درخواست‌ها و استخراج پارامترهای جدید.
        برمی‌گرداند: True در صورت موفقیت، False در صورت شکست.
        """
        config = self._load_config()
        current_cookies = config.get("api_cookies", {})
        
        # تبدیل کوکی‌های فایل کانفیگ به فرمت Playwright
        playwright_cookies = []
        for name, value in current_cookies.items():
            playwright_cookies.append({
                "name": name,
                "value": str(value),
                "domain": ".twitter.com",
                "path": "/"
            })

        extracted_data = {
            "x-client-transaction-id": None,
            "ct0": None,
            "auth_token": current_cookies.get("auth_token")
        }

        logger.info("Launching Playwright to extract fresh auth parameters...")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                context.add_cookies(playwright_cookies)
                page = context.new_page()

                # تابع شنود (Interceptor)
                def handle_request(request: Request):
                    if self._graphql_indicator in request.url:
                        headers = request.headers
                        if "x-client-transaction-id" in headers and not extracted_data["x-client-transaction-id"]:
                            extracted_data["x-client-transaction-id"] = headers["x-client-transaction-id"]
                            logger.debug("Successfully intercepted x-client-transaction-id.")

                page.on("request", handle_request)
                
                # رفتن به صفحه هوم توییتر برای تریگر شدن ریکوئست‌های GraphQL
                page.goto(self._target_url, wait_until="networkidle", timeout=60000)

                # استخراج کوکی‌های جدید به‌روزرسانی شده توسط مرورگر
                new_cookies = context.cookies()
                for cookie in new_cookies:
                    if cookie["name"] == "ct0":
                        extracted_data["ct0"] = cookie["value"]
                    elif cookie["name"] == "auth_token":
                        extracted_data["auth_token"] = cookie["value"]

                browser.close()

            # بررسی اینکه آیا پارامترهای کلیدی با موفقیت استخراج شده‌اند یا خیر
            if extracted_data["x-client-transaction-id"] and extracted_data["ct0"]:
                logger.info("New authentication parameters extracted successfully.")
                
                # اعمال تغییرات در شیء کانفیگ
                config["api_headers"]["x-client-transaction-id"] = extracted_data["x-client-transaction-id"]
                config["api_cookies"]["ct0"] = extracted_data["ct0"]
                config["api_cookies"]["auth_token"] = extracted_data["auth_token"]
                
                self._save_config(config)
                return True
            else:
                logger.error("Failed to extract complete auth parameters. Manual login may be required.")
                return False

        except Exception as e:
            logger.error(f"Error during session update via Playwright: {e}")
            return False

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    updater = SessionUpdater()
    updater.update_session()
