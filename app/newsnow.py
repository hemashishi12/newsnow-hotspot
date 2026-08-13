from __future__ import annotations

import time
from typing import Any

import httpx


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Referer": "https://newsnow.busiyi.world/",
}


class NewsNowClient:
    def __init__(self, config: dict[str, Any]):
        self.api_url = str(config.get("api_url", "https://newsnow.busiyi.world/api/s"))
        self.timeout = float(config.get("timeout_seconds", 20))
        self.retries = int(config.get("retries", 2))
        self.interval = float(config.get("request_interval_seconds", 1.5))

    def fetch(self, source_id: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout, headers=HEADERS, follow_redirects=True) as client:
                    response = client.get(self.api_url, params={"id": source_id, "latest": ""})
                    response.raise_for_status()
                    payload = response.json()
                if payload.get("status") not in {"success", "cache"}:
                    raise RuntimeError(f"NewsNow status={payload.get('status')!r}")
                if not isinstance(payload.get("items"), list):
                    raise RuntimeError("NewsNow 响应缺少 items 列表")
                return payload
            except Exception as exc:  # 网络错误需要统一重试
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"抓取 {source_id} 失败：{last_error}")

