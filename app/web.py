from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, redirect, render_template, request, url_for

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
from .pipeline import build_dashboard, collect_once
from .toutiao import ToutiaoDraftService, split_article_content


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
) -> Flask:
    app = Flask(__name__, template_folder=str(settings.root / "templates"), static_folder=str(settings.root / "static"))
    state: dict[str, Any] = {"collecting": False, "last_error": ""}
    app.config["comment_service"] = comment_service or CommentCrawlerService(settings.root, database)
    app.config["article_service"] = article_service or ArticleGenerator(settings, database)
    app.config["article_job_service"] = ArticleJobService(
        database, app.config["article_service"], poll_seconds=0.05 if app.testing else 1.0
    )
    app.config["toutiao_service"] = toutiao_service or ToutiaoDraftService(settings.root)

    def selected_comment_platforms() -> list[str]:
        selected = database.get_comment_platform_ids({"dy", "wb"}, set(PLATFORM_NAMES))
        return [platform for platform in PLATFORM_NAMES if platform in selected]

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
            recent_runs=int(settings.raw.get("app", {}).get("recent_runs", 10)),
        )

    @app.get("/logs")
    def logs_page():
        run_id = request.args.get("run_id", type=int) or database.newest_run_id()
        return render_template("logs.html", runs=database.all_runs(), run_id=run_id)

    @app.get("/articles")
    def article_history():
        articles = database.generated_articles()
        for article in articles:
            article_title, article_body = split_article_content(
                str(article["content"]), str(article["topic_title"])
            )
            article["article_title"] = article_title
            article["article_body"] = article_body
        return render_template("articles.html", articles=articles)

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
            article_job_id = database.create_article_job(topic_id, article_type, custom_prompt)
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
