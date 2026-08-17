from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright


async def close_browser(port: int) -> None:
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=5000
            )
        except Exception:
            return
        try:
            session = await browser.new_browser_cdp_session()
            await session.send("Browser.close")
        except Exception:
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(close_browser(int(sys.argv[1])))
