from __future__ import annotations

import math
import threading
import time
from statistics import median
from typing import Any

from .ai import AIClusterer, prepare_ai_submission
from .config import Settings
from .database import Database
from .newsnow import NewsNowClient


RUN_LOCK = threading.Lock()


def normalized_rank_score(rank: int, list_size: int, exponent: float) -> float:
    if list_size <= 0 or rank <= 0:
        return 0.0
    percentile = max(0.0, min(1.0, (list_size - rank + 1) / list_size))
    return percentile**exponent


def collect_once(settings: Settings, database: Database, run_ai: bool = True) -> int:
    if not RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("已有一轮采集正在进行")
    try:
        run_id = database.begin_run()
        database.append_log(run_id, "collection", "开始新一轮采集")
        client = NewsNowClient(settings.raw.get("newsnow", {}))
        errors: list[str] = []
        source_count = 0
        item_count = 0
        available_sources = [source for source in settings.sources if source.collect]
        valid_source_ids = {source.id for source in available_sources}
        default_source_ids = {source.id for source in available_sources if source.analyze}
        selected_source_ids = database.get_analysis_source_ids(default_source_ids, valid_source_ids)
        enabled_sources = [source for source in available_sources if source.id in selected_source_ids]
        for index, source in enumerate(enabled_sources):
            database.append_log(
                run_id,
                "source",
                f"正在采集 {source.name}（{index + 1}/{len(enabled_sources)}）",
                details={"source_id": source.id, "position": index + 1, "total": len(enabled_sources)},
            )
            try:
                response = client.fetch(source.id)
                saved_count = database.save_source(run_id, source, response)
                item_count += saved_count
                source_count += 1
                database.append_log(
                    run_id,
                    "source",
                    f"{source.name} 已保存 {saved_count} 条（NewsNow: {response.get('status', 'success')}）",
                    details={"source_id": source.id, "item_count": saved_count, "status": response.get("status")},
                )
            except Exception as exc:
                message = f"{source.name}: {exc}"
                errors.append(message)
                database.save_source_error(run_id, source, str(exc))
                database.append_log(run_id, "source", f"{source.name} 采集失败：{exc}", "error")
            if index < len(enabled_sources) - 1:
                time.sleep(client.interval)
        database.finish_run(run_id, source_count, item_count, errors)
        database.append_log(
            run_id,
            "collection",
            f"采集完成：{source_count}/{len(enabled_sources)} 个平台，共 {item_count} 条",
            "warning" if errors else "success",
        )
        if run_ai:
            analyze_run(settings, database, run_id)
        else:
            database.set_ai_status(run_id, "skipped")
            database.append_log(run_id, "ai", "本轮跳过 AI 分析", "warning")
        return run_id
    finally:
        RUN_LOCK.release()


def analyze_run(settings: Settings, database: Database, run_id: int) -> int:
    ai_connection = database.get_ai_connection(settings.api_key, settings.ai_base_url)
    clusterer = AIClusterer(settings, ai_connection["api_key"], ai_connection["base_url"])
    if not clusterer.enabled:
        database.set_ai_status(run_id, "missing_key")
        database.append_log(run_id, "ai", "未配置 AI，已跳过分析", "warning")
        return 0

    scoring = settings.raw.get("scoring", {})
    min_platforms = int(scoring.get("min_platforms", 2))
    exponent = float(scoring.get("rank_exponent", 1.2))
    valid_sources = {source.id for source in settings.sources if source.collect}
    default_sources = {source.id for source in settings.sources if source.collect and source.analyze}
    analyzed_sources = database.get_analysis_source_ids(default_sources, valid_sources)
    items = database.run_items(run_id, analyzed_sources)
    database.append_log(
        run_id,
        "ai",
        f"准备将 {len(items)} 条完整榜单一次性提交给 AI（无预筛、无分批）",
        details={"item_count": len(items), "source_count": len(analyzed_sources)},
    )
    submission_items = prepare_ai_submission(items)
    recent_count = int(settings.raw.get("app", {}).get("recent_runs", 10))
    previous_run_ids = [run["id"] for run in database.recent_runs(recent_count + 1) if run["id"] != run_id]
    recent_topics = database.recent_topic_catalog(
        previous_run_ids,
        int(settings.raw.get("ai", {}).get("max_recent_topics", 120)),
    )

    def on_request(batch_index: int, payload: dict[str, Any]) -> int:
        exchange_id = database.begin_ai_exchange(run_id, batch_index, payload)
        database.append_log(
            run_id,
            "ai_request",
            f"已向 AI 发送完整请求（往返 #{exchange_id}）",
            details={"exchange_id": exchange_id, "item_count": len(items)},
        )
        return exchange_id

    def on_response(exchange_id: int | None, http_status: int | None, response_text: str, error: str) -> None:
        if exchange_id is None:
            return
        successful = not error and http_status is not None and 200 <= http_status < 300
        database.finish_ai_exchange(
            exchange_id,
            "success" if successful else "failed",
            response_text=response_text,
            http_status=http_status,
            error=error,
        )
        if successful:
            database.append_log(run_id, "ai_response", f"AI 已返回回复（往返 #{exchange_id}）", "success")
        else:
            reason = error or f"HTTP {http_status}"
            database.append_log(run_id, "ai_response", f"AI 请求失败（往返 #{exchange_id}）：{reason}", "error")

    try:
        raw_clusters = clusterer.cluster(
            submission_items,
            recent_topics,
            min_platforms,
            batch_index=1,
            on_request=on_request,
            on_response=on_response,
        )
        item_map = {int(item["id"]): item for item in submission_items}
        source_map = settings.source_map
        clean_clusters: list[dict[str, Any]] = []
        globally_used: set[int] = set()
        valid_topic_ids = {int(topic["id"]) for topic in recent_topics}

        for raw in raw_clusters:
            if not isinstance(raw, dict):
                continue
            best_by_source: dict[str, dict[str, Any]] = {}
            for raw_id in raw.get("item_ids", []):
                try:
                    item_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                item = item_map.get(item_id)
                if not item or item_id in globally_used:
                    continue
                current = best_by_source.get(item["source_id"])
                if current is None or item["rank"] < current["rank"]:
                    best_by_source[item["source_id"]] = item
            if len(best_by_source) < min_platforms:
                continue

            members: list[dict[str, Any]] = []
            for item in best_by_source.values():
                source = source_map[item["source_id"]]
                rank_score = normalized_rank_score(item["rank"], item["list_size"], exponent)
                member = dict(item)
                member.update(
                    platform_weight=source.weight,
                    rank_score=rank_score,
                    contribution=source.weight * rank_score,
                )
                members.append(member)
                globally_used.add(int(item["id"]))
            existing_id = raw.get("existing_topic_id")
            try:
                existing_id = int(existing_id) if existing_id is not None else None
            except (TypeError, ValueError):
                existing_id = None
            if existing_id not in valid_topic_ids:
                existing_id = None
            clean_clusters.append(
                {
                    "title": str(raw.get("title") or members[0]["title"])[:180],
                    "summary": str(raw.get("summary") or "")[:500],
                    "existing_topic_id": existing_id,
                    "members": sorted(members, key=lambda member: member["contribution"], reverse=True),
                    "platform_count": len(members),
                    "current_score": sum(member["contribution"] for member in members),
                }
            )
        database.save_clusters(run_id, clean_clusters)
        database.set_ai_status(run_id, "success")
        database.append_log(run_id, "complete", f"AI 分析完成，确认 {len(clean_clusters)} 个跨平台话题", "success")
        return len(clean_clusters)
    except Exception as exc:
        database.set_ai_status(run_id, "failed", str(exc))
        database.append_log(run_id, "complete", f"AI 分析失败：{exc}", "error")
        raise


def _slope(values: list[float]) -> float:
    size = len(values)
    if size < 2:
        return 0.0
    mean_x = (size - 1) / 2
    mean_y = sum(values) / size
    denominator = sum((index - mean_x) ** 2 for index in range(size))
    if denominator == 0:
        return 0.0
    return sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values)) / denominator


def _build_rank_chart(
    observations: dict[int, dict[str, Any]],
    run_ids: list[int],
    analyzed_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    source_order: list[str] = []
    for run_id in run_ids:
        observation = observations.get(run_id)
        if not observation:
            continue
        for member in observation["members"]:
            source_id = str(member["source_id"])
            if source_id not in sources:
                sources[source_id] = {
                    "source_id": source_id,
                    "source_name": str(member["source_name"]),
                    "values": [None] * len(run_ids),
                }
                source_order.append(source_id)
            index = run_ids.index(run_id)
            rank = int(member["rank"])
            existing = sources[source_id]["values"][index]
            sources[source_id]["values"][index] = rank if existing is None else min(existing, rank)

    medians = {
        source_id: float(median(value for value in sources[source_id]["values"] if value is not None))
        for source_id in source_order
    }
    separate_axes = False
    split_at = 0.0
    if len(medians) >= 2:
        lowest = min(medians.values())
        highest = max(medians.values())
        separate_axes = highest - lowest >= 10 and highest / max(lowest, 1.0) >= 3
        split_at = math.sqrt(max(lowest, 1.0) * highest)

    labels: list[str] = []
    for run in analyzed_runs:
        timestamp = str(run.get("completed_at") or run.get("started_at") or "")
        short_time = timestamp[5:16].replace("T", " ") if len(timestamp) >= 16 else ""
        labels.append(f"#{run['id']} {short_time}".strip())

    series = []
    for source_id in source_order:
        item = sources[source_id]
        axis = "rank-high" if separate_axes and medians[source_id] > split_at else "rank-low"
        series.append({**item, "axis": axis})
    return {
        "labels": labels,
        "series": series,
        "separate_axes": separate_axes,
        "run_count": len(run_ids),
    }


def build_dashboard(settings: Settings, database: Database) -> dict[str, Any]:
    run_limit = int(settings.raw.get("app", {}).get("recent_runs", 10))
    collection_runs = database.recent_runs(run_limit)
    analyzed_runs = database.recent_analyzed_runs(run_limit)
    run_ids = [int(run["id"]) for run in analyzed_runs]
    latest_id = run_ids[-1] if run_ids else None
    history = database.topic_history(run_ids)
    scoring = settings.raw.get("scoring", {})
    max_results = int(scoring.get("max_results_total", scoring.get("max_results_per_section", 20)))
    rise_floor = float(scoring.get("rising_min_score", 0.02))
    sustained_ratio = float(scoring.get("sustained_min_presence_ratio", 0.5))
    source_weights = {source.id: source.weight for source in settings.sources}

    configured_weights = scoring.get("section_weights", {})
    default_weights = {
        "current": max(0.0, float(configured_weights.get("current", 0.5))),
        "rising": max(0.0, float(configured_weights.get("rising", 0.3))),
        "sustained": max(0.0, float(configured_weights.get("sustained", 0.2))),
    }
    section_weights = database.get_section_weights(default_weights)

    topics: list[dict[str, Any]] = []
    for topic in history.values():
        observations = topic["observations"]
        series = [float(observations.get(run_id, {}).get("current_score", 0.0)) for run_id in run_ids]
        present = [value > 0 for value in series]
        latest = observations.get(latest_id) if latest_id is not None else None
        if not latest:
            continue

        previous_by_source: dict[str, dict[str, Any]] = {}
        for run_id in reversed(run_ids[:-1]):
            observation = observations.get(run_id)
            if not observation:
                continue
            for member in observation["members"]:
                previous_by_source.setdefault(str(member["source_id"]), member)

        trend_members: list[dict[str, Any]] = []
        for member in latest["members"]:
            previous = previous_by_source.get(str(member["source_id"]))
            previous_rank = int(previous["rank"]) if previous else None
            rank_change = previous_rank - int(member["rank"]) if previous_rank is not None else 0
            trend_members.append(
                {**member, "previous_rank": previous_rank, "rank_change": rank_change}
            )

        base = {
            "topic_id": topic["topic_id"],
            "title": latest["display_title"],
            "summary": latest["summary"],
            "members": trend_members,
            "platform_count": latest["platform_count"],
            "series": series,
            "presence_count": sum(present),
            "comment_summary": database.topic_comment_summary(topic["topic_id"]),
            "rank_chart": _build_rank_chart(observations, run_ids, analyzed_runs),
        }

        slope = _slope(series)
        rank_gain = 0.0
        gain_steps = 0
        previous_members: dict[str, dict[str, Any]] = {}
        for run_id in run_ids:
            observation = observations.get(run_id)
            if not observation:
                continue
            members = {member["source_id"]: member for member in observation["members"]}
            for source_id, member in members.items():
                previous = previous_members.get(source_id)
                if previous:
                    gain = max(0.0, float(member["rank_score"]) - float(previous["rank_score"]))
                    rank_gain += source_weights.get(source_id, 1.0) * gain
                    gain_steps += 1
            previous_members = members
        rank_gain = rank_gain / gain_steps if gain_steps else 0.0
        latest_platforms = int(latest["platform_count"])
        rising_score = latest_platforms * max(0.0, slope) * max(1, len(run_ids)) * (1 + rank_gain)
        sustained_score = sum(series)
        required_presence = max(2, math.ceil(len(run_ids) * sustained_ratio)) if run_ids else 2
        is_rising = rising_score >= rise_floor
        is_sustained = sum(present) >= required_presence
        is_new = not any(run_id in observations for run_id in run_ids[:-1])
        module_scores: dict[str, float | None] = {
            "current": float(latest["current_score"]),
            "rising": rising_score if is_rising else None,
            "sustained": sustained_score if is_sustained else None,
        }
        labels = []
        if is_new:
            labels.append({"key": "new", "name": "新上榜"})
        labels.append({"key": "current", "name": "多平台共振"})
        if is_rising:
            labels.append({"key": "rising", "name": "快速升温"})
        if is_sustained:
            labels.append({"key": "sustained", "name": "持续高热"})
        topics.append(
            {
                **base,
                "module_scores": module_scores,
                "labels": labels,
                "slope": slope,
                "rank_gain": rank_gain,
                "coverage": sum(present) / len(run_ids) if run_ids else 0.0,
            }
        )

    maxima = {
        key: max(
            (float(topic["module_scores"][key]) for topic in topics if topic["module_scores"][key] is not None),
            default=0.0,
        )
        for key in section_weights
    }
    for topic in topics:
        normalized_scores: dict[str, float] = {}
        weighted_scores: dict[str, float] = {}
        for key, weight in section_weights.items():
            raw_score = topic["module_scores"][key]
            maximum = maxima[key]
            normalized = float(raw_score) / maximum if raw_score is not None and maximum > 0 else 0.0
            normalized_scores[key] = normalized
            weighted_scores[key] = normalized * weight
        topic["normalized_scores"] = normalized_scores
        topic["weighted_scores"] = weighted_scores
        topic["score"] = sum(weighted_scores.values())

    topics.sort(
        key=lambda item: (
            -item["score"],
            -item["normalized_scores"]["current"],
            -item["platform_count"],
        )
    )
    current = sorted(
        ({**topic, "score": float(topic["module_scores"]["current"])} for topic in topics),
        key=lambda item: (-item["score"], -item["platform_count"]),
    )
    rising = sorted(
        (
            {**topic, "score": float(topic["module_scores"]["rising"])}
            for topic in topics
            if topic["module_scores"]["rising"] is not None
        ),
        key=lambda item: (-item["score"], -item["slope"]),
    )
    sustained = sorted(
        (
            {**topic, "score": float(topic["module_scores"]["sustained"])}
            for topic in topics
            if topic["module_scores"]["sustained"] is not None
        ),
        key=lambda item: (-item["score"], -item["presence_count"]),
    )
    latest_run = collection_runs[-1] if collection_runs else None
    analysis_run = analyzed_runs[-1] if analyzed_runs else None
    return {
        "topics": topics[:max_results],
        "current": current[:max_results],
        "rising": rising[:max_results],
        "sustained": sustained[:max_results],
        "runs": collection_runs,
        "latest_run": latest_run,
        "analysis_run": analysis_run,
        "analysis_stale": bool(latest_run and (not analysis_run or latest_run["id"] != analysis_run["id"])),
        "source_results": database.source_results(latest_run["id"]) if latest_run else [],
        "ai_configured": bool(database.get_ai_connection(settings.api_key, settings.ai_base_url)["api_key"]),
        "recent_runs_limit": run_limit,
        "section_weights": section_weights,
        "collection_interval_minutes": database.get_collection_interval_minutes(
            int(settings.raw.get("app", {}).get("interval_minutes", 30))
        ),
    }
