from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx


def prepare_ai_submission(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """不做预筛也不分批，保留所选平台的全部条目。"""
    return list(items)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("AI 返回值不是 JSON 对象")
    return result


class AIClusterer:
    def __init__(self, settings: Any, api_key: str | None = None, base_url: str | None = None):
        self.api_key = settings.api_key if api_key is None else api_key
        self.base_url = settings.ai_base_url if base_url is None else base_url
        self.model = settings.ai_model
        self.config = settings.raw.get("ai", {})

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)

    def cluster(
        self,
        items: list[dict[str, Any]],
        recent_topics: list[dict[str, Any]],
        min_platforms: int,
        batch_index: int = 1,
        on_request: Callable[[int, dict[str, Any]], Any] | None = None,
        on_response: Callable[[Any, int | None, str, str], None] | None = None,
    ) -> list[dict[str, Any]]:
        compact_items = [
            {
                "id": item["id"],
                "platform": item["source_id"],
                "platform_name": item["source_name"],
                "rank": item["rank"],
                "title": item["title"],
            }
            for item in items
        ]
        compact_topics = [
            {"topic_id": topic["id"], "title": topic["canonical_title"], "summary": topic["summary"]}
            for topic in recent_topics
        ]
        instructions = str(self.config.get("selection_instructions", ""))
        system = f"""你是严谨的中文新闻事件聚类器。你只根据输入标题建立事件之间的对应关系，不评价平台立场，也不虚构事实。

任务：从本次榜单中找出至少出现在 {min_platforms} 个不同平台的同一具体事件。不同措辞但事实主体、动作和时间背景相同可合并；仅共享关键词、人物、国家或行业不可合并。
附加要求：{instructions}

recent_topics 是近几次已确认的话题。只有本次事件确实是同一事件的延续时才填写 existing_topic_id，否则必须为 null。
每个 item id 最多出现一次；同一 cluster 每个平台最多保留一条最相关标题；不要返回单平台 cluster。

只返回 JSON：
{{"clusters":[{{"title":"简洁中性的话题名","summary":"一句话说明共同事件","existing_topic_id":整数或null,"item_ids":[整数]}}]}}
如果没有跨平台同一事件，返回 {{"clusters":[]}}。"""
        user = json.dumps(
            {"current_items": compact_items, "recent_topics": compact_topics},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": True,
            "temperature": float(self.config.get("temperature", 0.1)),
            "max_tokens": int(self.config.get("max_output_tokens", 8000)),
            "reasoning_effort": str(self.config.get("reasoning_effort", "high")),
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/chat/completions"
        timeout = float(self.config.get("timeout_seconds", 300))
        def post(attempt_payload: dict[str, Any]) -> tuple[httpx.Response, str]:
            token = on_request(batch_index, attempt_payload) if on_request else None
            try:
                with client.stream("POST", url, headers=headers, json=attempt_payload) as response:
                    if response.status_code >= 400:
                        response.read()
                        if on_response:
                            on_response(token, response.status_code, response.text, "")
                        return response, ""

                    fragments: list[str] = []
                    non_sse_lines: list[str] = []
                    for line in response.iter_lines():
                        line = line.strip()
                        if not line or line.startswith(":") or line.startswith("event:"):
                            continue
                        if not line.startswith("data:"):
                            non_sse_lines.append(line)
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        event = json.loads(data)
                        if event.get("error"):
                            raise RuntimeError(f"AI 流式响应错误：{event['error']}")
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if isinstance(content, str):
                            fragments.append(content)
                        elif isinstance(content, list):
                            fragments.extend(
                                str(part.get("text", ""))
                                for part in content
                                if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
                            )

                    content = "".join(fragments)
                    if not content and non_sse_lines:
                        body = json.loads("\n".join(non_sse_lines))
                        content = str(body["choices"][0]["message"]["content"])
                    if not content:
                        raise ValueError("AI 流式响应中没有 content")
                    if on_response:
                        on_response(token, response.status_code, content, "")
                    return response, content
            except Exception as exc:
                if on_response:
                    on_response(token, None, "", str(exc))
                raise

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response, content = post(payload)
            if response.status_code == 400:
                payload = dict(payload)
                payload.pop("response_format", None)
                response, content = post(payload)
            response.raise_for_status()
        result = _extract_json(content)
        clusters = result.get("clusters", [])
        if not isinstance(clusters, list):
            raise ValueError("AI JSON 中 clusters 不是数组")
        return clusters
