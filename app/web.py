from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for
from werkzeug.utils import secure_filename

from .article import (
    ARTICLE_TYPES,
    ArticleGenerator,
    DEFAULT_ARTICLE_PROMPT,
    DEFAULT_LONG_ARTICLE_PROMPT,
)
from .article_jobs import ArticleJobService
from .config import Settings
from .comments import CommentCrawlerService, PLATFORM_NAMES
from .database import Database
from .news_search import NewsSearchService
from .pipeline import build_dashboard, collect_once
from .toutiao import ToutiaoDraftService, split_article_content
from .video import (
    VIDEO_FORM_DEFAULTS,
    VIDEO_ASPECTS,
    VIDEO_SOURCES,
    VOICE_OPTIONS,
    VideoJobService,
    article_to_narration,
    strip_narration_quotes,
    validate_local_engine_url,
)
from .tts import TTS_PROVIDERS, tts_configuration_error


def seconds_until_collection(
    latest_completed_at: str | None,
    interval_minutes: int,
    now: datetime | None = None,
) -> float:
    interval_seconds = max(1, interval_minutes) * 60
    if not latest_completed_at:
        return 0.0
    try:
        latest = datetime.fromisoformat(latest_completed_at)
    except ValueError:
        return 0.0
    current = now or datetime.now().astimezone()
    if latest.tzinfo is None:
        latest = latest.astimezone()
    elapsed = (current - latest.astimezone(current.tzinfo)).total_seconds()
    return min(float(interval_seconds), max(0.0, interval_seconds - elapsed))


def create_app(
    settings: Settings,
    database: Database,
    comment_service: Any | None = None,
    article_service: Any | None = None,
    toutiao_service: Any | None = None,
    video_service: Any | None = None,
) -> Flask:
    app = Flask(__name__, template_folder=str(settings.root / "templates"), static_folder=str(settings.root / "static"))
    state: dict[str, Any] = {"collecting": False, "last_error": ""}
    app.config["comment_service"] = comment_service or CommentCrawlerService(settings.root, database)
    app.config["article_service"] = article_service or ArticleGenerator(settings, database)
    app.config["toutiao_service"] = toutiao_service or ToutiaoDraftService(settings.root)
    app.config["video_service"] = video_service or VideoJobService(
        settings.root, database, poll_seconds=0.05 if app.testing else 2.0
    )
    app.config["article_job_service"] = ArticleJobService(
        database,
        app.config["article_service"],
        poll_seconds=0.05 if app.testing else 1.0,
        video_service=app.config["video_service"],
    )
    app.config["news_search_service"] = NewsSearchService()

    def public_video_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        try:
            params = json.loads(str(job.get("params_json") or "{}"))
        except json.JSONDecodeError:
            params = {}
        try:
            result = json.loads(str(job.get("result_json") or "{}"))
        except json.JSONDecodeError:
            result = {}
        credits = []
        for source in result.get("material_sources", []):
            if not isinstance(source, dict):
                continue
            creator = source.get("creator") if isinstance(source.get("creator"), dict) else {}
            credits.append(
                {
                    "provider": str(source.get("provider") or params.get("source") or ""),
                    "source_page": str(source.get("source_page") or ""),
                    "creator_name": str(creator.get("name") or ""),
                    "creator_page": str(creator.get("profile_page") or ""),
                }
            )
        return {
            "id": int(job["id"]),
            "article_id": int(job["article_id"]) if job.get("article_id") is not None else None,
            "status": str(job["status"]),
            "progress": int(job.get("progress") or 0),
            "message": str(job.get("message") or ""),
            "error": str(job.get("error") or ""),
            "script": str(job.get("script") or ""),
            "params": params,
            "credits": credits,
            "created_at": str(job.get("created_at") or ""),
            "updated_at": str(job.get("updated_at") or ""),
            "read_at": str(job.get("read_at") or ""),
            **(
                {"video_url": url_for("api_article_video_result", job_id=int(job["id"]))}
                if job["status"] == "success" else {}
            ),
        }

    def parse_video_payload(
        payload: dict[str, Any],
        *,
        default_title: str = "",
        default_search_terms: str = "",
    ) -> tuple[str, dict[str, Any]]:
        script = strip_narration_quotes(str(payload.get("script") or "").strip())
        if not 20 <= len(script) <= 50_000:
            raise ValueError("口播稿需要 20–50000 个字")
        aspect = str(payload.get("aspect", "9:16"))
        voice = str(payload.get("voice", "zh-CN-XiaoxiaoNeural-Female"))
        source = str(payload.get("source", "pexels"))
        if aspect not in VIDEO_ASPECTS:
            raise ValueError("不支持的视频比例")
        if voice not in VOICE_OPTIONS:
            raise ValueError("不支持的口播声音")
        if source not in VIDEO_SOURCES:
            raise ValueError("不支持的素材来源")
        tts_provider = str(payload.get("tts_provider", "moneyprinter"))
        if tts_provider not in TTS_PROVIDERS:
            raise ValueError("不支持的配音引擎")
        try:
            voice_rate = float(payload.get("voice_rate", 1.0))
        except (TypeError, ValueError):
            raise ValueError("语速必须是有效数字") from None
        if not 0.7 <= voice_rate <= 1.5:
            raise ValueError("语速必须在 0.7–1.5 之间")
        requested_title = str(payload.get("title") or "").strip()
        if len(requested_title) > 120:
            raise ValueError("视频主题不能超过 120 个字")
        title = requested_title or str(default_title).strip()
        if not title:
            title = next((line.strip() for line in script.splitlines() if line.strip()), "自定视频")
        title = title[:120]
        search_terms = str(payload.get("search_terms") or "").strip()
        if len(search_terms) > 500:
            raise ValueError("素材关键词不能超过 500 个字")
        tts_voice = str(payload.get("tts_voice") or "alloy").strip()
        if len(tts_voice) > 120:
            raise ValueError("外部配音声音标识不能超过 120 个字")
        return script, {
            "title": title,
            "aspect": aspect,
            "voice": voice,
            "voice_rate": voice_rate,
            "source": source,
            "search_terms": search_terms or str(default_search_terms).strip() or title,
            "subtitle_enabled": bool(payload.get("subtitle_enabled", True)),
            "tts_provider": tts_provider,
            "tts_voice": tts_voice,
        }

    def selected_comment_platforms() -> list[str]:
        selected = database.get_comment_platform_ids({"dy", "wb"}, set(PLATFORM_NAMES))
        return [platform for platform in PLATFORM_NAMES if platform in selected]

    def remember_video_preferences(params: dict[str, Any]) -> None:
        database.set_video_preferences(
            {key: params[key] for key in VIDEO_FORM_DEFAULTS if key in params}
        )

    def background_collect() -> None:
        if state["collecting"]:
            return
        state["collecting"] = True
        state["last_error"] = ""
        try:
            collect_once(settings, database)
        except Exception as exc:
            state["last_error"] = str(exc)
        finally:
            state["collecting"] = False

    @app.get("/")
    def index():
        return render_template("index.html", dashboard=build_dashboard(settings, database), state=state)

    @app.route("/custom-topics", methods=["GET", "POST"])
    def custom_topics_page():
        search_data = None
        error = ""
        if request.method == "POST":
            action = request.form.get("action", "search")
            if action == "search":
                try:
                    search_data = app.config["news_search_service"].search(request.form.get("query", ""))
                except Exception as exc:
                    error = str(exc)
            elif action == "save":
                title = request.form.get("title", "").strip()
                selected = request.form.getlist("news_json")
                try:
                    items = [json.loads(item) for item in selected]
                except (TypeError, json.JSONDecodeError):
                    items = []
                items = [
                    item for item in items
                    if isinstance(item, dict) and str(item.get("title", "")).strip()
                ]
                if not 2 <= len(title) <= 120:
                    error = "自定话题名称需要 2-120 个字。"
                elif not items:
                    error = "请至少选择一条新闻。"
                else:
                    form_summary = request.form.get("summary", "").strip()
                    if len(items) == 1:
                        topic_ids = [
                            database.create_custom_topic(title, form_summary, items[:1])
                        ]
                    else:
                        topic_ids = []
                        for item in items[:30]:
                            item_title = str(item.get("title", "")).strip() or title
                            item_summary = str(item.get("summary", "")).strip() or form_summary
                            topic_ids.append(
                                database.create_custom_topic(
                                    item_title,
                                    item_summary,
                                    [item],
                                    merge_existing=False,
                                )
                            )
                    return redirect(url_for("custom_topics_page", selected=topic_ids[0]))
        return render_template(
            "custom_topics.html", search_data=search_data, topics=database.custom_topics(),
            error=error, selected=request.args.get("selected", type=int),
        )

    @app.get("/custom-video")
    def custom_video_page():
        latest = database.latest_custom_video_job()
        active = latest and latest["status"] in {"queued", "starting", "processing"}
        params = {}
        if active:
            try:
                params = json.loads(str(latest.get("params_json") or "{}"))
            except json.JSONDecodeError:
                params = {}
        return render_template(
            "custom_video.html",
            video_script=str(latest.get("script") or "") if active else "",
            video_title=str(params.get("title") or "") if active else "",
            video_job=public_video_job(latest) if active else None,
            video_defaults=database.get_video_preferences(VIDEO_FORM_DEFAULTS),
        )

    @app.get("/raw")
    def raw_lists():
        run_id = request.args.get("run_id", type=int) or database.latest_run_id()
        items = database.run_items(run_id) if run_id else []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            grouped.setdefault(item["source_name"], []).append(item)
        return render_template("raw.html", grouped=grouped, run_id=run_id)

    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        available_sources = [source for source in settings.sources if source.collect]
        valid_ids = {source.id for source in available_sources}
        default_ids = {source.id for source in available_sources if source.analyze}
        error = ""
        if request.method == "POST":
            action = request.form.get("form_action", "platforms")
            if action == "ai_connection":
                base_url = request.form.get("ai_base_url", "").strip().rstrip("/")
                parsed = urlparse(base_url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    error = "Base URL 必须是完整的 http:// 或 https:// 地址。"
                else:
                    entered_key = request.form.get("ai_api_key", "").strip()
                    database.set_ai_connection(entered_key or None, base_url)
                    return redirect(url_for("settings_page", saved="ai"))
            elif action == "video_engine":
                try:
                    engine_url = validate_local_engine_url(
                        request.form.get("video_engine_url", "")
                    )
                except ValueError as exc:
                    error = str(exc)
                else:
                    database.set_video_engine_settings(
                        engine_url,
                        request.form.get("pexels_api_key", "").strip(),
                        request.form.get("pixabay_api_key", "").strip(),
                        request.form.get("coverr_api_key", "").strip(),
                    )
                    reconfigure = getattr(app.config["video_service"], "reconfigure", None)
                    if callable(reconfigure):
                        reconfigure()
                    return redirect(url_for("settings_page", saved="video"))
            elif action == "video_tts":
                database.set_video_tts_settings(
                    {
                        "tts_api_url": request.form.get("tts_api_url", ""),
                        "tts_api_key": request.form.get("tts_api_key", ""),
                        "tts_model": request.form.get("tts_model", ""),
                        "tts_voice": request.form.get("tts_voice", ""),
                        "gpt_sovits_url": request.form.get("gpt_sovits_url", ""),
                        "gpt_sovits_ref_audio": request.form.get("gpt_sovits_ref_audio", ""),
                        "gpt_sovits_prompt_text": request.form.get("gpt_sovits_prompt_text", ""),
                        "gpt_sovits_prompt_lang": request.form.get("gpt_sovits_prompt_lang", ""),
                        "gpt_sovits_text_lang": request.form.get("gpt_sovits_text_lang", ""),
                    }
                )
                return redirect(url_for("settings_page", saved="video_tts"))
            elif action == "article_prompt":
                article_prompt = request.form.get("article_prompt", "").strip()
                if len(article_prompt) < 20:
                    error = "文章提示词至少需要 20 个字。"
                else:
                    database.set_article_prompt(article_prompt)
                    return redirect(url_for("settings_page", saved="prompt"))
            elif action == "long_article_prompt":
                long_article_prompt = request.form.get("long_article_prompt", "").strip()
                if len(long_article_prompt) < 20:
                    error = "深度长文提示词至少需要 20 个字。"
                else:
                    database.set_long_article_prompt(long_article_prompt)
                    return redirect(url_for("settings_page", saved="long_prompt"))
            elif action == "comment_platforms":
                selected_platforms = set(request.form.getlist("comment_platforms")) & set(PLATFORM_NAMES)
                if not selected_platforms:
                    error = "请至少选择一个热评采集平台。"
                else:
                    database.set_comment_platform_ids(selected_platforms)
                    return redirect(url_for("settings_page", saved="comments"))
            elif action == "ranking_weights":
                try:
                    section_weights = {
                        "current": float(request.form.get("weight_current", "")),
                        "rising": float(request.form.get("weight_rising", "")),
                        "sustained": float(request.form.get("weight_sustained", "")),
                    }
                except (TypeError, ValueError):
                    error = "三个权重都必须填写有效数字。"
                else:
                    if any(value < 0 for value in section_weights.values()):
                        error = "权重不能小于 0。"
                    elif sum(section_weights.values()) <= 0:
                        error = "三个权重不能同时为 0。"
                    else:
                        total = sum(section_weights.values())
                        database.set_section_weights(
                            {key: value / total for key, value in section_weights.items()}
                        )
                        return redirect(url_for("settings_page", saved="weights"))
            elif action == "collection_interval":
                try:
                    interval_minutes = int(request.form.get("interval_minutes", ""))
                except (TypeError, ValueError):
                    error = "采集间隔必须是整数分钟。"
                else:
                    if not 1 <= interval_minutes <= 1440:
                        error = "采集间隔必须在 1–1440 分钟之间。"
                    else:
                        database.set_collection_interval_minutes(interval_minutes)
                        update_interval = app.config.get("set_collection_interval")
                        if callable(update_interval):
                            update_interval(interval_minutes)
                        return redirect(url_for("settings_page", saved="interval"))
            elif action == "article_web_search":
                database.set_article_web_search_enabled(
                    request.form.get("article_web_search_enabled") == "1"
                )
                return redirect(url_for("settings_page", saved="web_search"))
            else:
                selected_ids = set(request.form.getlist("analysis_sources")) & valid_ids
                if len(selected_ids) < 2:
                    error = "请至少选择两个平台，否则无法识别跨平台同一话题。"
                else:
                    database.set_analysis_source_ids(selected_ids)
                    return redirect(url_for("settings_page", saved="platforms"))
        selected_ids = database.get_analysis_source_ids(default_ids, valid_ids)
        ai_connection = database.get_ai_connection(settings.api_key, settings.ai_base_url)
        configured_weights = settings.raw.get("scoring", {}).get("section_weights", {})
        section_weights = database.get_section_weights(
            {
                "current": float(configured_weights.get("current", 0.5)),
                "rising": float(configured_weights.get("rising", 0.3)),
                "sustained": float(configured_weights.get("sustained", 0.2)),
            }
        )
        interval_minutes = database.get_collection_interval_minutes(
            int(settings.raw.get("app", {}).get("interval_minutes", 30))
        )
        article_web_search_enabled = database.get_article_web_search_enabled(True)
        video_engine_settings = database.get_video_engine_settings()
        video_tts_settings = database.get_video_tts_settings()
        return render_template(
            "settings.html",
            sources=available_sources,
            selected_ids=selected_ids,
            saved=request.args.get("saved", ""),
            error=error,
            ai_base_url=ai_connection["base_url"],
            ai_api_key=ai_connection["api_key"],
            ai_key_configured=bool(ai_connection["api_key"]),
            ai_model=settings.ai_model,
            article_prompt=database.get_article_prompt(DEFAULT_ARTICLE_PROMPT),
            long_article_prompt=database.get_long_article_prompt(DEFAULT_LONG_ARTICLE_PROMPT),
            comment_platforms=PLATFORM_NAMES,
            selected_comment_platforms=set(selected_comment_platforms()),
            scoring=settings.raw.get("scoring", {}),
            section_weights=section_weights,
            interval_minutes=interval_minutes,
            article_web_search_enabled=article_web_search_enabled,
            video_engine_settings=video_engine_settings,
            video_tts_settings=video_tts_settings,
            recent_runs=int(settings.raw.get("app", {}).get("recent_runs", 10)),
        )

    @app.get("/logs")
    def logs_page():
        run_id = request.args.get("run_id", type=int) or database.newest_run_id()
        return render_template("logs.html", runs=database.all_runs(), run_id=run_id)

    @app.get("/articles")
    def article_history():
        articles = database.generated_articles()
        history_items = []
        for article in articles:
            article_title, article_body = split_article_content(
                str(article["content"]), str(article["topic_title"])
            )
            article["article_title"] = article_title
            article["article_body"] = article_body
            article["video_script"] = article_to_narration(str(article["content"]))
            article["video_jobs"] = [
                public_video_job(job)
                for job in database.article_video_jobs(int(article["id"]))
            ]
            latest_video = article["video_jobs"][0] if article["video_jobs"] else None
            has_successful_video = any(
                job["status"] == "success" for job in article["video_jobs"]
            )
            article["video_has_success"] = has_successful_video
            if latest_video and latest_video["status"] in {"queued", "starting", "processing"}:
                article["video_status_text"] = (
                    latest_video["error"] or latest_video["message"] or "正在生成口播视频"
                )
                article["video_status_class"] = latest_video["status"]
            elif has_successful_video:
                article["video_status_text"] = "口播视频已生成"
                article["video_status_class"] = "success"
            elif latest_video:
                article["video_status_text"] = latest_video["error"] or latest_video["message"]
                article["video_status_class"] = latest_video["status"]
            else:
                article["video_status_text"] = ""
                article["video_status_class"] = ""
            article["kind"] = "article"
            history_items.append(article)
        custom_video_jobs = [public_video_job(job) for job in database.custom_video_jobs()]
        for video in custom_video_jobs:
            video["kind"] = "video"
            history_items.append(video)

        def history_sort_key(item: dict[str, Any]) -> tuple[datetime, int]:
            try:
                created_at = datetime.fromisoformat(str(item.get("created_at") or ""))
            except ValueError:
                created_at = datetime.min.replace(tzinfo=timezone.utc)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            return created_at.astimezone(timezone.utc), int(item.get("id") or 0)

        history_items.sort(key=history_sort_key, reverse=True)
        return render_template(
            "articles.html",
            history_items=history_items,
            article_count=len(articles),
            video_count=len(custom_video_jobs),
            video_defaults=database.get_video_preferences(VIDEO_FORM_DEFAULTS),
        )

    @app.put("/api/articles/<int:article_id>")
    def api_update_article(article_id: int):
        payload = request.get_json(silent=True) or {}
        content = str(payload.get("content", ""))
        expected_updated_at = str(payload.get("updated_at", ""))
        if not content.strip():
            return jsonify({"error": "文章内容不能为空"}), 400
        if len(content) > 500_000:
            return jsonify({"error": "文章内容不能超过 50 万字"}), 413
        if not expected_updated_at:
            return jsonify({"error": "缺少文章版本信息，请刷新页面后重试"}), 400
        status, article = database.update_generated_article(
            article_id, content, expected_updated_at
        )
        if status == "missing":
            return jsonify({"error": "文章不存在"}), 404
        if status == "conflict":
            return jsonify({"error": "文章已在其他页面被修改，请刷新后重试"}), 409
        return jsonify(
            {
                "id": article_id,
                "content": article["content"],
                "updated_at": article.get("updated_at") or article["created_at"],
                "saved": status == "updated",
            }
        )

    @app.post("/api/article-images")
    def api_upload_article_image():
        upload = request.files.get("file[]") or request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"code": 1, "msg": "请选择图片", "data": None}), 400
        allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
        extension = allowed.get(upload.mimetype)
        if extension is None:
            return jsonify({"code": 1, "msg": "仅支持 JPG、PNG、GIF 和 WebP 图片", "data": None}), 415
        signature = upload.stream.read(16)
        valid_signature = {
            ".jpg": signature.startswith(b"\xff\xd8\xff"),
            ".png": signature.startswith(b"\x89PNG\r\n\x1a\n"),
            ".gif": signature.startswith((b"GIF87a", b"GIF89a")),
            ".webp": signature.startswith(b"RIFF") and signature[8:12] == b"WEBP",
        }[extension]
        if not valid_signature:
            return jsonify({"code": 1, "msg": "图片文件内容与格式不符", "data": None}), 415
        upload.stream.seek(0, 2)
        size = upload.stream.tell()
        upload.stream.seek(0)
        if size > 10 * 1024 * 1024:
            return jsonify({"code": 1, "msg": "图片不能超过 10 MB", "data": None}), 413
        image_dir = settings.root / "data" / "article-images"
        image_dir.mkdir(parents=True, exist_ok=True)
        original_stem = Path(secure_filename(upload.filename)).stem[:40] or "image"
        filename = f"{original_stem}-{uuid.uuid4().hex[:12]}{extension}"
        upload.save(image_dir / filename)
        image_url = url_for("article_image", filename=filename)
        return jsonify(
            {"code": 0, "msg": "", "data": {"errFiles": [], "succMap": {upload.filename: image_url}}}
        )

    @app.get("/article-images/<path:filename>")
    def article_image(filename: str):
        return send_from_directory(settings.root / "data" / "article-images", filename)

    @app.get("/api/video-engine/status")
    def api_video_engine_status():
        status_method = getattr(app.config["video_service"], "engine_status", None)
        if not callable(status_method):
            return jsonify({"installed": True, "online": True})
        try:
            return jsonify(status_method())
        except ValueError as exc:
            return jsonify({"installed": False, "online": False, "error": str(exc)}), 400

    @app.post("/api/articles/<int:article_id>/videos")
    def api_create_article_video(article_id: int):
        article = database.generated_article(article_id)
        if article is None:
            return jsonify({"error": "文章不存在"}), 404
        active = database.active_article_video_job(article_id)
        if active:
            return jsonify(public_video_job(active)), 202
        payload = request.get_json(silent=True) or {}
        topic = database.topic(int(article["topic_id"])) or {}
        title, _ = split_article_content(
            str(article["content"]), str(topic.get("canonical_title") or "")
        )
        try:
            script, params = parse_video_payload(
                payload,
                default_title=title,
                default_search_terms=str(topic.get("canonical_title") or title),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        engine_settings = database.get_video_engine_settings()
        source = params["source"]
        if not engine_settings[f"{source}_api_key"]:
            return jsonify({"error": f"请先在分析设置中填写 {VIDEO_SOURCES[source]} API Key"}), 400
        tts_error = tts_configuration_error(
            params["tts_provider"], database.get_video_tts_settings()
        )
        if tts_error:
            return jsonify({"error": tts_error}), 400
        remember_video_preferences(params)
        job_id = database.create_article_video_job(article_id, script, params)
        app.config["video_service"].start(job_id)
        return jsonify(public_video_job(database.article_video_job(job_id))), 202

    @app.post("/api/custom-videos")
    def api_create_custom_video():
        active = database.active_custom_video_job()
        if active:
            return jsonify(public_video_job(active)), 202
        payload = request.get_json(silent=True) or {}
        try:
            script, params = parse_video_payload(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        source = params["source"]
        engine_settings = database.get_video_engine_settings()
        if not engine_settings[f"{source}_api_key"]:
            return jsonify({"error": f"请先在分析设置中填写 {VIDEO_SOURCES[source]} API Key"}), 400
        tts_error = tts_configuration_error(
            params["tts_provider"], database.get_video_tts_settings()
        )
        if tts_error:
            return jsonify({"error": tts_error}), 400
        remember_video_preferences(params)
        job_id = database.create_article_video_job(None, script, params)
        app.config["video_service"].start(job_id)
        return jsonify(public_video_job(database.article_video_job(job_id))), 202

    @app.get("/api/articles/<int:article_id>/videos/latest")
    def api_latest_article_video(article_id: int):
        if database.generated_article(article_id) is None:
            return jsonify({"error": "文章不存在"}), 404
        job = database.latest_article_video_job(article_id)
        if job is None:
            return jsonify({"error": "这篇文章还没有生成过视频"}), 404
        return jsonify(public_video_job(job))

    @app.get("/api/article-videos/<int:job_id>")
    def api_article_video_status(job_id: int):
        job = database.article_video_job(job_id)
        if job is None:
            return jsonify({"error": "视频任务不存在"}), 404
        return jsonify(public_video_job(job))

    @app.get("/api/article-videos/<int:job_id>/result")
    def api_article_video_result(job_id: int):
        job = database.article_video_job(job_id)
        if job is None:
            return jsonify({"error": "视频任务不存在"}), 404
        if job["status"] != "success":
            return jsonify({"error": "视频尚未生成完成"}), 409
        result_file = getattr(app.config["video_service"], "result_file", lambda _job: None)(job)
        if result_file is not None:
            return send_file(
                result_file,
                mimetype="video/mp4",
                conditional=True,
                as_attachment=request.args.get("download") == "1",
                download_name=(
                    f"article-{job['article_id']}-video.mp4"
                    if job.get("article_id") is not None else f"custom-video-{job['id']}.mp4"
                ),
            )
        return redirect(app.config["video_service"].result_url(job))

    @app.get("/api/article-prompts")
    def api_article_prompts():
        article_type = request.args.get("article_type", "standard")
        if article_type == "all":
            return jsonify(
                {
                    "defaults": [
                        {
                            "name": "爆款文章",
                            "article_type": "standard",
                            "prompt": database.get_article_prompt(DEFAULT_ARTICLE_PROMPT),
                            "fixed": True,
                        },
                        {
                            "name": "深度长文",
                            "article_type": "long",
                            "prompt": database.get_long_article_prompt(DEFAULT_LONG_ARTICLE_PROMPT),
                            "fixed": True,
                        },
                    ],
                    "presets": database.all_article_prompt_presets(),
                }
            )
        if article_type not in ARTICLE_TYPES:
            return jsonify({"error": "不支持的文章类型"}), 400
        default_prompt = (
            database.get_long_article_prompt(DEFAULT_LONG_ARTICLE_PROMPT)
            if article_type == "long"
            else database.get_article_prompt(DEFAULT_ARTICLE_PROMPT)
        )
        return jsonify(
            {"default_prompt": default_prompt, "presets": database.article_prompt_presets(article_type)}
        )

    @app.post("/api/article-prompt-presets")
    def api_save_article_prompt_preset():
        payload = request.get_json(silent=True) or {}
        article_type = str(payload.get("article_type", "standard"))
        name = str(payload.get("name", "")).strip()
        prompt = str(payload.get("prompt", "")).strip()
        if article_type not in ARTICLE_TYPES:
            return jsonify({"error": "不支持的文章类型"}), 400
        if not 1 <= len(name) <= 30:
            return jsonify({"error": "标签名需要 1–30 个字"}), 400
        if len(prompt) < 20:
            return jsonify({"error": "提示词至少需要 20 个字"}), 400
        return jsonify(database.save_article_prompt_preset(name, prompt, article_type)), 201

    @app.delete("/api/article-prompt-presets/<int:preset_id>")
    def api_delete_article_prompt_preset(preset_id: int):
        if not database.delete_article_prompt_preset(preset_id):
            return jsonify({"error": "提示词标签不存在"}), 404
        return "", 204

    @app.get("/api/logs")
    def api_logs():
        run_id = request.args.get("run_id", type=int) or database.newest_run_id()
        if run_id is None:
            return jsonify({"run_id": None, "logs": [], "exchanges": [], "collecting": state["collecting"]})
        exchanges = database.ai_exchanges(run_id)
        summaries = [
            {key: exchange[key] for key in (
                "id", "run_id", "batch_index", "created_at", "completed_at", "status", "http_status", "error"
            )}
            for exchange in exchanges
        ]
        return jsonify(
            {
                "run_id": run_id,
                "logs": database.run_logs(run_id),
                "exchanges": summaries,
                "collecting": state["collecting"],
            }
        )

    @app.get("/api/ai-exchanges/<int:exchange_id>")
    def api_ai_exchange(exchange_id: int):
        exchange = database.ai_exchange(exchange_id)
        if exchange is None:
            return jsonify({"error": "AI 往返记录不存在"}), 404
        return jsonify(exchange)

    @app.get("/topics/<int:topic_id>/comments")
    def topic_comments(topic_id: int):
        topic = database.topic(topic_id)
        if topic is None:
            return "话题不存在", 404
        jobs = database.comment_jobs(topic_id)
        social = database.topic_social_data(topic_id)
        return render_template(
            "comments.html",
            topic=topic,
            jobs=jobs,
            posts=social["posts"],
            comments=social["comments"],
            logs=database.comment_job_logs(topic_id),
            platform_names=PLATFORM_NAMES,
            active=any(job["status"] in {"queued", "running"} for job in jobs),
            selected_platforms=selected_comment_platforms(),
            selected_platform_names=[PLATFORM_NAMES[item] for item in selected_comment_platforms()],
        )

    @app.post("/topics/<int:topic_id>/comments/collect")
    def trigger_topic_comments(topic_id: int):
        topic = database.topic(topic_id)
        if topic is None:
            return "话题不存在", 404
        keyword = request.form.get("keyword", "").strip() or str(topic["canonical_title"])
        service = app.config["comment_service"]
        service.enqueue_topic(topic_id, keyword, selected_comment_platforms())
        return redirect(url_for("topic_comments", topic_id=topic_id))

    @app.get("/api/topics/<int:topic_id>/comments")
    def api_topic_comments(topic_id: int):
        jobs = database.comment_jobs(topic_id)
        summary = database.topic_comment_summary(topic_id)
        return jsonify(
            {
                "jobs": jobs,
                "logs": database.comment_job_logs(topic_id),
                "summary": summary,
                "active": any(job["status"] in {"queued", "running"} for job in jobs),
            }
        )

    @app.get("/api/topics/<int:topic_id>/article")
    def api_latest_article(topic_id: int):
        topic = database.topic(topic_id)
        if topic is None:
            return jsonify({"error": "话题不存在"}), 404
        article_type = request.args.get("article_type", "standard")
        if article_type not in ARTICLE_TYPES:
            return jsonify({"error": "不支持的文章类型"}), 400
        article = database.latest_generated_article(topic_id, article_type)
        if article is None:
            job = database.latest_article_job(topic_id, article_type)
            if job and job["status"] in {"queued", "waiting_comments", "generating"}:
                return jsonify({"job_id": job["id"], "status": job["status"], "message": job["message"]}), 202
            message = "该话题还没有写过深度长文" if article_type == "long" else "该话题还没有写过文章"
            return jsonify({"error": message}), 404
        return jsonify(
            {
                "id": article["id"],
                "topic_id": topic_id,
                "topic_title": topic["canonical_title"],
                "created_at": article["created_at"],
                "model": article["model"],
                "content": article["content"],
                "article_type": article_type,
                "reused": True,
            }
        )

    @app.post("/api/topics/<int:topic_id>/article")
    def api_generate_article(topic_id: int):
        topic = database.topic(topic_id)
        if topic is None:
            return jsonify({"error": "话题不存在"}), 404
        payload = request.get_json(silent=True) or {}
        article_type = request.args.get("article_type", str(payload.get("article_type", "standard")))
        if article_type not in ARTICLE_TYPES:
            return jsonify({"error": "不支持的文章类型"}), 400
        comments_attempted = bool(payload.get("comments_attempted"))
        background = bool(payload.get("background"))
        follow_up_video = bool(payload.get("follow_up_video"))
        if follow_up_video:
            background = True
        custom_prompt = str(payload.get("prompt", "")).strip()
        if custom_prompt and len(custom_prompt) < 20:
            return jsonify({"error": "本次提示词至少需要 20 个字"}), 400
        comment_summary = database.topic_comment_summary(topic_id)
        if background:
            job_ids: list[int] = []
            platforms: list[str] = []
            if comment_summary["comment_count"] == 0:
                jobs = database.comment_jobs(topic_id)
                active = any(job["status"] in {"queued", "running"} for job in jobs)
                platforms = selected_comment_platforms()
                if not active:
                    job_ids = app.config["comment_service"].enqueue_topic(
                        topic_id, str(topic["canonical_title"]), platforms
                    )
            article_job_id = database.create_article_job(
                topic_id,
                article_type,
                custom_prompt,
                follow_up_video=follow_up_video,
            )
            app.config["article_job_service"].start(article_job_id)
            return (
                jsonify(
                    {
                        "status": "queued",
                        "message": "后台写作任务已创建。",
                        "job_id": article_job_id,
                        "job_ids": job_ids,
                        "platforms": [PLATFORM_NAMES[platform] for platform in platforms],
                    }
                ),
                202,
            )
        if comment_summary["comment_count"] == 0 and not comments_attempted:
            jobs = database.comment_jobs(topic_id)
            active = any(job["status"] in {"queued", "running"} for job in jobs)
            job_ids: list[int] = []
            platforms = selected_comment_platforms()
            if not active:
                job_ids = app.config["comment_service"].enqueue_topic(
                    topic_id, str(topic["canonical_title"]), platforms
                )
            article_job_id = None
            if callable(getattr(app.config["article_service"], "generate", None)):
                article_job_id = database.create_article_job(topic_id, article_type, custom_prompt)
                app.config["article_job_service"].start(article_job_id)
            return (
                jsonify(
                    {
                        "status": "collecting_comments",
                        "message": "该话题尚无热评，已先启动自动采集。",
                        "platforms": [PLATFORM_NAMES[platform] for platform in platforms],
                        "job_ids": job_ids,
                        **({"job_id": article_job_id} if article_job_id is not None else {}),
                    }
                ),
                202,
            )
        try:
            if custom_prompt:
                article = app.config["article_service"].generate(topic_id, article_type, custom_prompt)
            else:
                article = app.config["article_service"].generate(topic_id, article_type)
            return jsonify(article)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("话题 %s 文章生成失败", topic_id)
            return jsonify({"error": f"AI 生成失败：{exc}"}), 502

    @app.get("/api/article-jobs/<int:job_id>")
    def api_article_job(job_id: int):
        job = database.article_job(job_id)
        if job is None:
            return jsonify({"error": "写作任务不存在"}), 404
        result = dict(job)
        if job.get("article_id"):
            article = database.generated_article(int(job["article_id"]))
            if article:
                topic = database.topic(int(job["topic_id"])) or {}
                result["article"] = {
                    "id": article["id"], "topic_id": article["topic_id"],
                    "topic_title": topic.get("canonical_title", ""), "content": article["content"],
                    "article_type": article["article_type"],
                }
        return jsonify(result)

    @app.get("/api/article-jobs")
    def api_article_job_notifications():
        jobs = database.article_job_notifications()
        for job in jobs:
            job["notification_type"] = "article"
        jobs.extend(database.video_job_notifications())
        jobs.sort(
            key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""),
            reverse=True,
        )
        jobs = jobs[:30]
        unread = sum(
            job["status"] in {"success", "failed"} and not job.get("read_at")
            for job in jobs
        )
        return jsonify({"jobs": jobs, "unread": unread})

    @app.post("/api/article-jobs/read")
    def api_mark_article_jobs_read():
        payload = request.get_json(silent=True) or {}
        ids = payload.get("ids", []) if isinstance(payload.get("ids", []), list) else []
        video_ids = payload.get("video_ids", []) if isinstance(payload.get("video_ids", []), list) else []
        if isinstance(payload.get("jobs"), list):
            ids = [item.get("id") for item in payload["jobs"] if item.get("type") == "article"]
            video_ids = [item.get("id") for item in payload["jobs"] if item.get("type") == "video"]
        try:
            ids = [int(value) for value in ids]
            video_ids = [int(value) for value in video_ids]
        except (TypeError, ValueError):
            return jsonify({"error": "任务 ID 无效"}), 400
        database.mark_article_jobs_read(ids)
        database.mark_article_video_jobs_read(video_ids)
        return "", 204

    @app.post("/api/toutiao/drafts")
    def api_open_toutiao_draft():
        payload = request.get_json(silent=True) or {}
        try:
            job = app.config["toutiao_service"].start(
                str(payload.get("content", "")),
                str(payload.get("topic_title", "")),
            )
            return jsonify(job), 202
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            app.logger.exception("启动头条号自动填稿失败")
            return jsonify({"error": f"无法打开头条号写作页：{exc}"}), 500

    @app.get("/api/toutiao/drafts/<job_id>")
    def api_toutiao_draft_status(job_id: str):
        status = app.config["toutiao_service"].status(job_id)
        if status is None:
            return jsonify({"error": "头条号填稿任务不存在"}), 404
        return jsonify(status)

    @app.post("/collect")
    def trigger_collect():
        if not state["collecting"]:
            threading.Thread(target=background_collect, daemon=True).start()
        return redirect(url_for("index"))

    @app.get("/api/status")
    def api_status():
        return jsonify({**state, "latest_run_id": database.latest_run_id()})

    app.config["background_collect"] = background_collect
    return app


def serve(settings: Settings, database: Database) -> None:
    app = create_app(settings, database)
    interval = database.get_collection_interval_minutes(
        int(settings.raw.get("app", {}).get("interval_minutes", 30))
    )
    collect_on_startup = bool(settings.raw.get("app", {}).get("collect_on_startup", True))
    now = datetime.now().astimezone()
    startup_delay = seconds_until_collection(database.latest_completed_run_at(), interval, now)
    next_delay = startup_delay if collect_on_startup and startup_delay > 0 else interval * 60
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        app.config["background_collect"],
        "interval",
        id="collection",
        minutes=interval,
        next_run_time=now + timedelta(seconds=next_delay),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=interval * 60,
    )
    scheduler.start()

    def set_collection_interval(interval_minutes: int) -> None:
        scheduler.reschedule_job(
            "collection",
            trigger="interval",
            minutes=interval_minutes,
        )

    app.config["set_collection_interval"] = set_collection_interval

    if collect_on_startup and startup_delay <= 0:
        threading.Thread(target=app.config["background_collect"], daemon=True).start()

    host = str(settings.raw.get("app", {}).get("host", "127.0.0.1"))
    port = int(settings.raw.get("app", {}).get("port", 8765))
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    finally:
        scheduler.shutdown(wait=False)
        shutdown_comments = getattr(app.config.get("comment_service"), "shutdown", None)
        if callable(shutdown_comments):
            shutdown_comments()
