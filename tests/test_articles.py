import json
import io
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.article import ArticleGenerator, DEFAULT_ARTICLE_PROMPT, DEFAULT_LONG_ARTICLE_PROMPT
from app.database import Database
from app.web import create_app


def make_settings(root: Path, api_key: str = "test-key") -> SimpleNamespace:
    return SimpleNamespace(
        root=root,
        sources=(),
        raw={"app": {}, "scoring": {}, "ai": {"timeout_seconds": 10}},
        api_key=api_key,
        ai_base_url="https://api.example.com/v1",
        ai_model="test-model",
    )


class ArticleFeatureTests(unittest.TestCase):
    def test_article_edit_is_saved_with_revision_and_conflict_protection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "articles.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("编辑话题", "", "2026-08-15", "2026-08-15"),
                )
                topic_id = int(cursor.lastrowid)
            article_id = database.save_generated_article(
                topic_id, "提示词", "模型", "原标题\n\n原正文", {}
            )
            original = database.generated_article(article_id)
            client = create_app(make_settings(root), database, comment_service=object()).test_client()

            saved = client.put(
                f"/api/articles/{article_id}",
                json={"content": "新标题\n\n新正文", "updated_at": original["updated_at"]},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertTrue(saved.get_json()["saved"])
            self.assertEqual(database.generated_article(article_id)["content"], "新标题\n\n新正文")
            self.assertEqual(database.article_revisions(article_id)[0]["content"], "原标题\n\n原正文")

            conflict = client.put(
                f"/api/articles/{article_id}",
                json={"content": "覆盖内容", "updated_at": original["updated_at"]},
            )
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(database.generated_article(article_id)["content"], "新标题\n\n新正文")

    def test_article_editor_rejects_empty_content_and_uploads_local_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "articles.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("图片话题", "", "2026-08-15", "2026-08-15"),
                )
                topic_id = int(cursor.lastrowid)
            article_id = database.save_generated_article(topic_id, "提示词", "模型", "内容", {})
            article = database.generated_article(article_id)
            client = create_app(make_settings(root), database, comment_service=object()).test_client()
            empty = client.put(
                f"/api/articles/{article_id}",
                json={"content": "   ", "updated_at": article["updated_at"]},
            )
            self.assertEqual(empty.status_code, 400)

            uploaded = client.post(
                "/api/article-images",
                data={"file[]": (io.BytesIO(b"\x89PNG\r\n\x1a\nvalid"), "sample.png", "image/png")},
                content_type="multipart/form-data",
            )
            self.assertEqual(uploaded.status_code, 200)
            image_url = next(iter(uploaded.get_json()["data"]["succMap"].values()))
            self.assertTrue(image_url.startswith("/article-images/"))
            image_response = client.get(image_url)
            self.assertEqual(image_response.data, b"\x89PNG\r\n\x1a\nvalid")
            image_response.close()

    def test_article_history_loads_inline_vditor_and_realtime_save_controls(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "articles.html").read_text(
            encoding="utf-8"
        )
        script = (Path(__file__).resolve().parents[1] / "static" / "article-history-editor.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("history-edit-button", template)
        self.assertIn("vendor/vditor/dist/index.min.js", template)
        self.assertIn("SAVE_DELAY_MS = 800", script)
        self.assertIn("mode: 'ir'", script)
        self.assertIn("/api/article-images", script)

    def test_prompt_presets_are_saved_by_article_type_and_available_from_api(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "presets.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            client = create_app(settings, database, comment_service=object()).test_client()

            saved = client.post(
                "/api/article-prompt-presets",
                json={"name": "克制评论", "prompt": "请保持克制并清楚区分事实、观点和推测，完成一篇可发布的文章。", "article_type": "standard"},
            )
            self.assertEqual(saved.status_code, 201)
            response = client.get("/api/article-prompts?article_type=standard")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["presets"][0]["name"], "克制评论")
            self.assertEqual(response.get_json()["presets"][0]["prompt"], saved.get_json()["prompt"])

            deleted = client.delete(
                f"/api/article-prompt-presets/{saved.get_json()['id']}"
            )
            self.assertEqual(deleted.status_code, 204)
            self.assertEqual(
                client.get("/api/article-prompts?article_type=standard").get_json()["presets"],
                [],
            )

    def test_home_prompt_tags_include_a_separate_delete_control(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "article-actions.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("article-prompt-tag-delete", script)
        self.assertIn("删除提示词标签", script)

    def test_all_prompt_view_starts_with_two_fixed_templates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "all-prompts.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            database.save_article_prompt_preset("我的标签", "这是一段足够长的自定义写作提示词，用于验证全部标签显示。", "long")
            data = create_app(settings, database, comment_service=object()).test_client().get(
                "/api/article-prompts?article_type=all"
            ).get_json()
            self.assertEqual([item["name"] for item in data["defaults"]], ["爆款文章", "深度长文"])
            self.assertTrue(all(item["fixed"] for item in data["defaults"]))
            self.assertEqual(data["presets"][0]["name"], "我的标签")

    def test_background_mode_returns_immediately_even_when_comments_exist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "always-background.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("已有评论话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            comment_job = database.create_comment_jobs(topic_id, "已有评论话题", ["wb"])[0]
            database.save_social_data(
                comment_job, topic_id, "wb", [],
                [{"comment_id": "c1", "post_id": "p1", "content": "已有评论", "like_count": 1}],
            )
            database.set_comment_job_status(comment_job, "success", comment_count=1)

            class FakeArticleService:
                def generate(self, topic_id, article_type="standard", prompt_override=None):
                    article_id = database.save_generated_article(
                        topic_id, prompt_override or "默认", "test", "后台文章", {}, article_type
                    )
                    return {"id": article_id, "topic_id": topic_id, "content": "后台文章", "article_type": article_type}

            response = create_app(
                settings, database, comment_service=object(), article_service=FakeArticleService()
            ).test_client().post(
                f"/api/topics/{topic_id}/article", json={"background": True}
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.get_json()["job_id"]
            deadline = time.time() + 2
            while time.time() < deadline and database.article_job(job_id)["status"] not in {"success", "failed"}:
                time.sleep(0.01)
            self.assertEqual(database.article_job(job_id)["status"], "success")

    def test_background_article_can_enqueue_video_after_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "article-video-chain.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("文章视频链路", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            video_calls = []

            class FakeCommentService:
                def enqueue_topic(self, *_args):
                    return []

            class FakeArticleService:
                def generate(self, topic_id, article_type="standard", prompt_override=None):
                    article_id = database.save_generated_article(
                        topic_id,
                        prompt_override or "默认",
                        "test",
                        "文章视频链路标题\n\n这是足够长的文章正文，用于验证生成文章后自动排队视频。",
                        {},
                        article_type,
                    )
                    return {"id": article_id, "topic_id": topic_id, "content": "文章正文", "article_type": article_type}

            class FakeVideoService:
                def enqueue_article_video(self, article_id):
                    video_calls.append(article_id)
                    return 101

            client = create_app(
                settings,
                database,
                comment_service=FakeCommentService(),
                article_service=FakeArticleService(),
                video_service=FakeVideoService(),
            ).test_client()
            response = client.post(
                f"/api/topics/{topic_id}/article",
                json={"background": True, "follow_up_video": True},
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.get_json()["job_id"]
            deadline = time.time() + 2
            while time.time() < deadline and database.article_job(job_id)["status"] not in {"success", "failed"}:
                time.sleep(0.01)
            job = database.article_job(job_id)
            self.assertEqual(job["status"], "success")
            self.assertEqual(job["follow_up_video"], 1)
            self.assertIn("视频已排队", job["message"])
            self.assertEqual(len(video_calls), 1)

    def test_custom_prompt_is_only_used_for_that_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "custom-prompt.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("定制话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            custom_prompt = "只针对本次文章采用现场叙事结构，同时严格区分事实、评论与分析。"
            captured = []

            class FakeArticleService:
                def generate(self, topic_id, article_type="standard", prompt_override=None):
                    captured.append(prompt_override)
                    article_id = database.save_generated_article(
                        topic_id, prompt_override or "默认提示词", "test-model", "标题\n\n正文", {}, article_type
                    )
                    return {"id": article_id, "topic_id": topic_id, "topic_title": "定制话题", "content": "标题\n\n正文", "article_type": article_type}

            client = create_app(settings, database, comment_service=object(), article_service=FakeArticleService()).test_client()
            response = client.post(
                f"/api/topics/{topic_id}/article",
                json={"comments_attempted": True, "prompt": custom_prompt},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(captured, [custom_prompt])
            self.assertEqual(database.latest_generated_article(topic_id)["prompt"], custom_prompt)
            self.assertEqual(database.get_article_prompt(DEFAULT_ARTICLE_PROMPT), DEFAULT_ARTICLE_PROMPT)

    def test_background_article_job_finishes_after_page_request_has_returned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "background-article.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("后台话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)

            class FakeCommentService:
                def enqueue_topic(self, topic_id, keyword, platforms):
                    return []

            class FakeArticleService:
                def generate(self, topic_id, article_type="standard", prompt_override=None):
                    article_id = database.save_generated_article(
                        topic_id, prompt_override or "默认", "test", "后台标题\n\n后台正文", {}, article_type
                    )
                    return {"id": article_id, "topic_id": topic_id, "topic_title": "后台话题", "content": "后台标题\n\n后台正文", "article_type": article_type}

            app = create_app(settings, database, FakeCommentService(), FakeArticleService())
            response = app.test_client().post(f"/api/topics/{topic_id}/article", json={})
            self.assertEqual(response.status_code, 202)
            job_id = response.get_json()["job_id"]
            deadline = time.time() + 2
            while time.time() < deadline and database.article_job(job_id)["status"] not in {"success", "failed"}:
                time.sleep(0.01)
            self.assertEqual(database.article_job(job_id)["status"], "success")
            self.assertEqual(database.latest_generated_article(topic_id)["content"], "后台标题\n\n后台正文")

    def test_existing_article_table_is_upgraded_with_article_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """CREATE TABLE generated_articles (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       topic_id INTEGER NOT NULL,
                       created_at TEXT NOT NULL,
                       prompt TEXT NOT NULL,
                       model TEXT NOT NULL,
                       content TEXT NOT NULL,
                       input_json TEXT NOT NULL DEFAULT '{}')"""
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)
            with database.connect() as connection:
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(generated_articles)")
                }
            self.assertIn("article_type", columns)

    def test_existing_article_is_reused_and_history_lists_every_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "history.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("历史话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            database.save_generated_article(topic_id, "提示词", "模型甲", "旧标题\n\n旧正文", {})
            latest_id = database.save_generated_article(
                topic_id, "提示词", "模型乙", "新标题\n\n新正文", {}
            )

            class NeverGenerate:
                def generate(self, _topic_id, _article_type="standard"):
                    raise AssertionError("读取旧文章时不应调用 AI")

            client = create_app(
                settings,
                database,
                comment_service=object(),
                article_service=NeverGenerate(),
            ).test_client()
            reused = client.get(f"/api/topics/{topic_id}/article")
            self.assertEqual(reused.status_code, 200)
            self.assertEqual(reused.get_json()["id"], latest_id)
            self.assertEqual(reused.get_json()["content"], "新标题\n\n新正文")
            self.assertTrue(reused.get_json()["reused"])

            history = client.get("/articles")
            html = history.data.decode("utf-8")
            self.assertEqual(history.status_code, 200)
            self.assertLess(html.index("新标题"), html.index("旧标题"))
            self.assertIn("新正文", html)
            self.assertIn("旧正文", html)
            self.assertIn("头条文章", html)

    def test_prompt_can_be_saved_from_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "prompt.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            client = create_app(settings, database, comment_service=object()).test_client()
            prompt = "请用克制、准确而有故事感的方式写一篇今日头条文章。"
            response = client.post(
                "/settings",
                data={"form_action": "article_prompt", "article_prompt": prompt},
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(prompt.encode("utf-8"), response.data)
            self.assertEqual(database.get_article_prompt(DEFAULT_ARTICLE_PROMPT), prompt)

    def test_each_settings_prompt_save_is_appended_to_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "prompt-history.db")
            first_standard = "第一版头条文章提示词，需要准确叙述事实并清楚区分事实和观点。"
            second_standard = "第二版头条文章提示词，需要增强故事性但不能夸大事实或编造细节。"
            long_prompt = "第一版深度长文提示词，需要梳理时间线、争议成因和长期影响。"

            database.set_article_prompt(first_standard)
            database.set_article_prompt(second_standard)
            database.set_long_article_prompt(long_prompt)

            with database.connect() as connection:
                rows = connection.execute(
                    "SELECT article_type,prompt FROM article_prompt_history ORDER BY id"
                ).fetchall()
            self.assertEqual(
                [(row["article_type"], row["prompt"]) for row in rows],
                [
                    ("standard", first_standard),
                    ("standard", second_standard),
                    ("long", long_prompt),
                ],
            )
            self.assertEqual(database.get_article_prompt(DEFAULT_ARTICLE_PROMPT), second_standard)
            self.assertEqual(database.get_long_article_prompt(DEFAULT_LONG_ARTICLE_PROMPT), long_prompt)

    def test_existing_active_prompt_is_archived_during_database_upgrade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy-prompt.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO app_settings(key,value_json,updated_at) VALUES ('article_prompt',?,?)",
                    (json.dumps("升级前最后保存的头条提示词", ensure_ascii=False), "2026-08-10T08:00:00+08:00"),
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(database_path)
            with database.connect() as connection:
                rows = connection.execute(
                    "SELECT article_type,prompt,created_at FROM article_prompt_history ORDER BY id"
                ).fetchall()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["article_type"], "standard")
            self.assertEqual(rows[0]["prompt"], "升级前最后保存的头条提示词")
            self.assertEqual(rows[0]["created_at"], "2026-08-10T08:00:00+08:00")

    def test_collection_refresh_waits_for_prompt_dialog_to_close(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (articleDialog?.open)", template)
        self.assertIn("articleDialog.addEventListener('close', () => location.reload(), {once: true})", template)

    def test_long_article_prompt_can_be_saved_independently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "long-prompt.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            client = create_app(settings, database, comment_service=object()).test_client()
            prompt = "请写一篇重视事实脉络、争议成因和长期影响的中文深度长文。"
            response = client.post(
                "/settings",
                data={"form_action": "long_article_prompt", "long_article_prompt": prompt},
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(prompt.encode("utf-8"), response.data)
            self.assertEqual(database.get_long_article_prompt(DEFAULT_LONG_ARTICLE_PROMPT), prompt)
            self.assertEqual(database.get_article_prompt(DEFAULT_ARTICLE_PROMPT), DEFAULT_ARTICLE_PROMPT)

    def test_article_api_uses_injected_service(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "route.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("路由测试", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)

            class FakeArticleService:
                def generate(self, topic_id, article_type="standard"):
                    return {
                        "id": 9,
                        "topic_id": topic_id,
                        "content": "生成的文章",
                        "article_type": article_type,
                    }

            client = create_app(
                settings,
                database,
                comment_service=object(),
                article_service=FakeArticleService(),
            ).test_client()
            response = client.post(
                f"/api/topics/{topic_id}/article", json={"comments_attempted": True}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["content"], "生成的文章")

            long_response = client.post(
                f"/api/topics/{topic_id}/article?article_type=long",
                json={"comments_attempted": True},
            )
            self.assertEqual(long_response.status_code, 200)
            self.assertEqual(long_response.get_json()["article_type"], "long")

    def test_standard_and_long_articles_keep_separate_latest_results_in_one_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "article-types.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("双类型话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            standard_id = database.save_generated_article(
                topic_id, "普通提示词", "模型", "普通标题\n\n普通正文", {}, "standard"
            )
            long_id = database.save_generated_article(
                topic_id, "长文提示词", "模型", "长文标题\n\n长文正文", {}, "long"
            )

            client = create_app(settings, database, comment_service=object()).test_client()
            standard = client.get(f"/api/topics/{topic_id}/article").get_json()
            long_article = client.get(
                f"/api/topics/{topic_id}/article?article_type=long"
            ).get_json()
            self.assertEqual(standard["id"], standard_id)
            self.assertEqual(long_article["id"], long_id)

            history = client.get("/articles").data.decode("utf-8")
            self.assertIn("头条文章", history)
            self.assertIn("深度长文", history)
            self.assertIn("普通正文", history)
            self.assertIn("长文正文", history)

    def test_article_api_auto_enqueues_selected_comment_platforms(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "auto-comments.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("无热评话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            database.set_comment_platform_ids({"dy", "wb", "bili", "zhihu"})

            class FakeCommentService:
                def __init__(self):
                    self.calls = []

                def enqueue_topic(self, topic_id, keyword, platforms):
                    self.calls.append((topic_id, keyword, platforms))
                    return [1, 2, 3, 4]

            comment_service = FakeCommentService()
            client = create_app(
                settings,
                database,
                comment_service=comment_service,
                article_service=object(),
            ).test_client()
            response = client.post(f"/api/topics/{topic_id}/article", json={})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()["platforms"], ["抖音", "微博", "B站", "知乎"])
            self.assertEqual(comment_service.calls[0][2], ["dy", "wb", "bili", "zhihu"])

    def test_generator_sends_news_and_hot_comments_then_persists_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "generate.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("消费热点", "一起备受关注的消费事件", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            job_id = database.create_comment_jobs(topic_id, "消费热点", ["wb"])[0]
            database.save_social_data(
                job_id,
                topic_id,
                "wb",
                [{"post_id": "p1", "title": "样本帖子", "url": "https://example.com/p1", "like_count": 8}],
                [{"comment_id": "c1", "post_id": "p1", "content": "这条热评很有代表性", "like_count": 99}],
            )

            captured = {}

            class FakeResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"choices": [{"message": {"content": "标题\n\n这是生成的正文。"}}]}

            class FakeClient:
                def __init__(self, **kwargs):
                    captured["client_kwargs"] = kwargs

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

                def post(self, url, **kwargs):
                    captured["url"] = url
                    captured.update(kwargs)
                    return FakeResponse()

            with patch("app.article.httpx.Client", FakeClient):
                result = ArticleGenerator(settings, database).generate(topic_id)

            user_content = captured["json"]["messages"][1]["content"]
            self.assertIn("这条热评很有代表性", user_content)
            self.assertIn("消费热点", user_content)
            self.assertEqual(captured["json"].get("tools"), [{"type": "web_search_preview"}])
            self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")
            self.assertEqual(result["content"], "标题\n\n这是生成的正文。")
            saved = database.latest_generated_article(topic_id)
            self.assertEqual(saved["model"], "test-model")
            self.assertEqual(json.loads(saved["input_json"])["hot_comments"][0]["likes"], 99)

    def test_long_generator_uses_long_prompt_and_long_output_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "generate-long.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("深度话题", "背景摘要", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            long_prompt = "请从事实时间线、争议成因和长期影响三个层面写一篇严谨的深度长文。"
            database.set_long_article_prompt(long_prompt)
            captured = {}

            class FakeResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"choices": [{"message": {"content": "深度标题\n\n深度正文"}}]}

            class FakeClient:
                def __init__(self, **_kwargs):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def post(self, _url, **kwargs):
                    captured.update(kwargs)
                    return FakeResponse()

            with patch("app.article.httpx.Client", FakeClient):
                result = ArticleGenerator(settings, database).generate(topic_id, "long")

            request_json = captured["json"]
            self.assertTrue(request_json["messages"][0]["content"].startswith(long_prompt))
            self.assertEqual(request_json["max_tokens"], 9000)
            self.assertEqual(result["article_type"], "long")
            self.assertIsNone(database.latest_generated_article(topic_id, "standard"))
            self.assertEqual(database.latest_generated_article(topic_id, "long")["content"], "深度标题\n\n深度正文")

    def test_generator_omits_web_search_tool_when_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "generate-offline.db")
            settings = make_settings(Path(__file__).resolve().parents[1])
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("离线话题", "背景摘要", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            database.set_article_web_search_enabled(False)
            captured = {}

            class FakeResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"choices": [{"message": {"content": "标题\n\n正文"}}]}

            class FakeClient:
                def __init__(self, **_kwargs):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def post(self, _url, **kwargs):
                    captured.update(kwargs)
                    return FakeResponse()

            with patch("app.article.httpx.Client", FakeClient):
                ArticleGenerator(settings, database).generate(topic_id)
            self.assertNotIn("tools", captured["json"])


if __name__ == "__main__":
    unittest.main()
