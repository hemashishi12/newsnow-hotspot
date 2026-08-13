from __future__ import annotations

# This helper only opens Firefox and fills a draft. It intentionally contains
# no selector or action for publishing, submitting, saving, or confirming a post.

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver import FirefoxOptions
from selenium.webdriver.common.keys import Keys


PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
TRANSIENT_NAVIGATION_ERRORS = (
    "execution context was destroyed",
    "cannot find context with specified id",
    "frame was detached",
)


def write_status(path: Path, status: str, message: str, **extra: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"status": status, "message": message, **extra}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def best_candidate(driver: webdriver.Firefox, kind: str):
    return driver.execute_script(
        """
          const kind = arguments[0];
          const visible = element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              style.opacity !== '0' && rect.width > 0 && rect.height > 0;
          };
          const selectors = kind === 'title'
            ? ['textarea[placeholder*="标题"]', 'input[placeholder*="标题"]', 'textarea', 'input', '[contenteditable="true"]']
            : ['.ProseMirror', '[data-placeholder*="正文"]', '[data-placeholder*="内容"]',
               '.article-editor [contenteditable="true"]', '.rich-editor [contenteditable="true"]',
               '.public-DraftEditor-content', '[contenteditable="true"]'];
          const seen = new Set();
          const candidates = [];
          for (const selector of selectors) {
            for (const raw of document.querySelectorAll(selector)) {
              const element = raw.matches('textarea,input,[contenteditable="true"]')
                ? raw : raw.querySelector('textarea,input,[contenteditable="true"]') || raw;
              if (seen.has(element) || !visible(element)) continue;
              seen.add(element);
              const rect = element.getBoundingClientRect();
              const haystack = [element.tagName, element.className, element.id,
                element.getAttribute('placeholder'), element.getAttribute('data-placeholder'),
                element.getAttribute('aria-label'), element.getAttribute('role')]
                .filter(Boolean).join(' ').toLowerCase();
              let score = 0;
              if (kind === 'title') {
                if (/input|textarea/i.test(element.tagName)) score += 90;
                if (/标题|title|headline/.test(haystack)) score += 170;
                if (/title-input|title-editor|article-title|headline/.test(haystack)) score += 120;
                if (/prosemirror|rich|content|drafteditor/.test(haystack)) score -= 200;
                if (rect.height > 140) score -= 50;
              } else {
                if (/prosemirror/.test(haystack)) score += 220;
                if (/editor-inner|article-editor|publish-content|rich-editor/.test(haystack)) score += 160;
                if (/editor|content|article|rich|write|textbox/.test(haystack)) score += 70;
                if (/title|标题|headline/.test(haystack)) score -= 240;
                if (/input|textarea/i.test(element.tagName)) score -= 260;
                score += Math.min(180, Math.round(rect.width * rect.height / 2500));
              }
              candidates.push({element, score});
            }
          }
          candidates.sort((a, b) => b.score - a.score);
          const best = candidates[0];
          return best && best.score > 0 ? best.element : null;
        """,
        kind,
    )


def is_transient_navigation_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(fragment in message for fragment in TRANSIENT_NAVIGATION_ERRORS)


def browser_is_closed(driver: webdriver.Firefox) -> bool:
    try:
        return not driver.window_handles
    except WebDriverException:
        return True


def wait_for_editor(driver: webdriver.Firefox, status_path: Path, timeout_seconds: int = 600):
    deadline = time.monotonic() + timeout_seconds
    login_reported = False
    while time.monotonic() < deadline:
        if browser_is_closed(driver):
            raise RuntimeError("头条号窗口已关闭")
        try:
            title = best_candidate(driver, "title")
            editor = best_candidate(driver, "editor")
        except Exception as exc:
            if not is_transient_navigation_error(exc):
                raise
            if browser_is_closed(driver):
                raise RuntimeError("头条号窗口已关闭") from exc
            time.sleep(0.2)
            continue
        if title is not None and editor is not None:
            return title, editor
        if not login_reported:
            write_status(
                status_path,
                "waiting_login",
                "请在新打开的 Firefox 中登录头条号，登录后将自动继续填稿。",
            )
            login_reported = True
        time.sleep(1)
    raise TimeoutError("等待头条号登录或编辑器超时")


def fill_element(element, value: str) -> None:
    element.click()
    element.send_keys(Keys.CONTROL, "a")
    element.send_keys(Keys.BACKSPACE)
    element.send_keys(value)


def main() -> int:
    input_path, status_path, profile_path = map(Path, sys.argv[1:4])
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    title = str(payload["title"])
    body = str(payload["body"])
    driver: webdriver.Firefox | None = None
    try:
        write_status(status_path, "opening", "正在用 Firefox 打开头条号写作页…")
        options = FirefoxOptions()
        options.add_argument("-profile")
        options.add_argument(str(profile_path))
        driver = webdriver.Firefox(options=options)
        driver.maximize_window()
        driver.get(PUBLISH_URL)
        title_element, editor_element = wait_for_editor(driver, status_path)
        fill_element(title_element, title)
        fill_element(editor_element, body)
        write_status(
            status_path,
            "filled",
            "标题和正文已填入 Firefox，请在窗口中检查；程序不会自动发布。",
            title=title,
        )
        while not browser_is_closed(driver):
            time.sleep(1)
        return 0
    except Exception as exc:
        write_status(status_path, "error", f"头条号自动填稿失败：{exc}")
        return 1
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
