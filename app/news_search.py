from __future__ import annotations

import html
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus, urlparse
from xml.etree import ElementTree

import httpx


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewsNowHotspot/1.0"


def _text(element: ElementTree.Element, name: str) -> str:
    child = element.find(name)
    return "" if child is None or child.text is None else html.unescape(child.text).strip()


def _clean_title(title: str, source: str) -> str:
    title = re.sub(r"\s+", " ", html.unescape(title)).strip()
    suffix = f" - {source}" if source else ""
    return title[: -len(suffix)].strip() if suffix and title.endswith(suffix) else title


def _format_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
    return parsed.strftime("%y-%m-%d")


class NewsSearchService:
    """Aggregates public news search feeds without requiring another local service."""

    def __init__(self, timeout_seconds: float = 15):
        self.timeout_seconds = timeout_seconds
        self.searxng_url = os.getenv("SEARXNG_URL", "").strip().rstrip("/")

    def search(self, query: str, limit: int = 30) -> dict[str, Any]:
        query = query.strip()
        if not 2 <= len(query) <= 100:
            raise ValueError("话题需要 2-100 个字")
        providers = [self._google_news, self._bing_news]
        if self.searxng_url:
            providers.append(self._searxng)
        results: list[dict[str, str]] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(providers)) as executor:
            futures = {executor.submit(provider, query): provider.__name__ for provider in providers}
            for future in as_completed(futures):
                try:
                    results.extend(future.result())
                except Exception as exc:
                    errors.append(f"{futures[future].removeprefix('_')}: {exc}")

        unique: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in sorted(results, key=lambda row: row.get("published_at", ""), reverse=True):
            key = re.sub(r"[^\w\u4e00-\u9fff]+", "", item["title"].lower())
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= limit:
                break
        return {"query": query, "results": unique, "errors": errors}

    def _feed(self, url: str, provider: str) -> list[dict[str, str]]:
        response = httpx.get(
            url, timeout=self.timeout_seconds, follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        rows = []
        for item in root.findall(".//item"):
            source = _text(item, "source")
            if not source:
                for child in item:
                    if child.tag.lower().endswith("source"):
                        source = (child.text or "").strip()
                        break
            title = _clean_title(_text(item, "title"), source)
            link = _text(item, "link")
            if not title or not link:
                continue
            rows.append(
                {
                    "title": title,
                    "url": link,
                    "source": source or urlparse(link).netloc,
                    "provider": provider,
                    "published_at": _text(item, "pubDate"),
                    "published_date": _format_date(_text(item, "pubDate")),
                    "summary": re.sub(r"<[^>]+>", " ", _text(item, "description")).strip(),
                }
            )
        return rows

    def _google_news(self, query: str) -> list[dict[str, str]]:
        url = (
            "https://news.google.com/rss/search?q=" + quote_plus(query)
            + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        )
        return self._feed(url, "Google News")

    def _bing_news(self, query: str) -> list[dict[str, str]]:
        # Bing no longer serves the historical News RSS endpoint. Its public
        # search result HTML remains available as a graceful fallback.
        url = "https://www.bing.com/search?q=" + quote_plus(query) + "&setlang=zh-cn"
        response = httpx.get(url, timeout=self.timeout_seconds, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        rows: list[dict[str, str]] = []
        pattern = re.compile(r'<li[^>]+class=["\']b_algo["\'][^>]*>.*?<h2>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>.*?</h2>(.*?)</li>', re.IGNORECASE | re.DOTALL)
        for link, raw_title, body in pattern.findall(response.text):
            title = re.sub(r"<[^>]+>", " ", html.unescape(raw_title))
            summary = re.sub(r"<[^>]+>", " ", html.unescape(body))
            title, summary = re.sub(r"\s+", " ", title).strip(), re.sub(r"\s+", " ", summary).strip()
            if title and link.startswith(("http://", "https://")):
                rows.append({"title": title, "url": link, "source": urlparse(link).netloc, "provider": "Bing", "published_at": "", "published_date": "", "summary": summary[:500]})
        return rows

    def _searxng(self, query: str) -> list[dict[str, str]]:
        response = httpx.get(
            f"{self.searxng_url}/search",
            params={"q": query, "categories": "news", "format": "json", "language": "zh-CN"},
            headers={"User-Agent": USER_AGENT}, timeout=self.timeout_seconds, follow_redirects=True,
        )
        response.raise_for_status()
        rows = []
        for item in response.json().get("results", []):
            title, url = str(item.get("title", "")).strip(), str(item.get("url", "")).strip()
            if title and url:
                rows.append({
                    "title": title, "url": url,
                    "source": str(item.get("engine") or urlparse(url).netloc),
                    "provider": "SearXNG", "published_at": str(item.get("publishedDate") or ""),
                    "published_date": _format_date(str(item.get("publishedDate") or "")),
                    "summary": str(item.get("content") or ""),
                })
        return rows
