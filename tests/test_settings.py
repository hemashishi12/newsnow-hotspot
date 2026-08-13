import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import SourceConfig
from app.database import Database
from app.web import create_app


class SettingsPageTests(unittest.TestCase):
    def test_home_comment_collection_opens_new_tab_and_uses_geometric_trend_symbols(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "home-ui.db")
            settings = SimpleNamespace(
                root=Path(__file__).resolve().parents[1], sources=(), raw={"app": {}, "scoring": {}},
                api_key="", ai_base_url="https://api.example.com/v1", ai_model="test-model",
            )
            topic = {
                "topic_id": 7,
                "title": "测试热点",
                "summary": "测试趋势符号",
                "score": 3.2,
                "labels": [
                    {"key": "current", "name": "多平台共振"},
                    {"key": "rising", "name": "快速升温"},
                ],
                "weighted_scores": {"current": 0.5, "rising": 0.3, "sustained": 0.0},
                "comment_summary": {"status": "idle", "post_count": 0, "comment_count": 0},
                "members": [
                    {"source_name": "平台升", "url": "#up", "title": "升", "rank": 2, "previous_rank": 6, "rank_change": 4},
                    {"source_name": "平台降", "url": "#down", "title": "降", "rank": 8, "previous_rank": 3, "rank_change": -5},
                    {"source_name": "平台平", "url": "#flat", "title": "平", "rank": 5, "previous_rank": 5, "rank_change": 0},
                ],
                "rank_chart": {
                    "labels": ["#1 08-10 08:00", "#2 08-10 08:30"],
                    "series": [{"source_id": "up", "source_name": "平台升", "values": [6, 2], "axis": "rank-low"}],
                    "separate_axes": False,
                    "run_count": 2,
                },
            }
            dashboard = {
                "topics": [topic], "current": [], "rising": [topic], "sustained": [], "recent_runs_limit": 10,
                "latest_run": None, "analysis_run": None, "analysis_stale": False,
                "ai_configured": True, "source_results": [], "runs": [],
                "section_weights": {"current": 0.5, "rising": 0.3, "sustained": 0.2},
                "collection_interval_minutes": 30,
            }
            app = create_app(settings, database, comment_service=object())
            with patch("app.web.build_dashboard", return_value=dashboard):
                html = app.test_client().get("/").data.decode("utf-8")

            self.assertIn('action="/topics/7/comments/collect" target="_blank"', html)
            self.assertIn('class="article-generate-button" data-topic-id="7"', html)
            self.assertIn('data-article-type="long"', html)
            self.assertIn('✦ AI 写深度长文', html)
            self.assertIn('class="article-customize-button"', html)
            self.assertIn('id="article-generate"', html)
            self.assertIn('id="article-history"', html)
            self.assertIn('<span class="trend-symbol" aria-hidden="true">▲</span>4', html)
            self.assertIn('<span class="trend-symbol" aria-hidden="true">▼</span>5', html)
            self.assertIn('<span class="trend-symbol" aria-hidden="true">●</span>0', html)
            self.assertIn('<i class="rank-current">#2</i>', html)
            self.assertIn('多平台共振', html)
            self.assertIn('快速升温', html)
            self.assertNotIn('平台榜位轨迹', html)
            self.assertNotIn('近 2 次分析', html)
            self.assertIn('class="rank-chart"', html)
            self.assertIn('class="rank-chart-legend"', html)
            self.assertNotIn('测试趋势符号', html)
            self.assertEqual(html.count('class="report-section'), 1)

    def test_run_logs_are_returned_newest_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "logs-order.db")
            run_id = database.begin_run()
            database.append_log(run_id, "test", "第一条")
            database.append_log(run_id, "test", "第二条")
            self.assertEqual(
                [entry["message"] for entry in database.run_logs(run_id)],
                ["第二条", "第一条"],
            )

    def test_manual_comment_trigger_queues_douyin_then_weibo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "comments-route.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("台风白海豚", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)

            class FakeCommentService:
                def __init__(self):
                    self.calls = []

                def enqueue_topic(self, topic_id, keyword, platforms):
                    self.calls.append((topic_id, keyword, platforms))
                    return [11, 12]

            service = FakeCommentService()
            settings = SimpleNamespace(
                root=Path(__file__).resolve().parents[1],
                sources=(),
                raw={"app": {}, "scoring": {}},
                api_key="",
                ai_base_url="https://api.example.com/v1",
                ai_model="test-model",
            )
            app = create_app(settings, database)
            app.config["comment_service"] = service
            client = app.test_client()
            response = client.post(
                f"/topics/{topic_id}/comments/collect",
                data={"keyword": "台风白海豚"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(service.calls, [(topic_id, "台风白海豚", ["dy", "wb"])])
            self.assertIn(f"/topics/{topic_id}/comments", response.headers["Location"])

    def test_comment_page_displays_saved_posts_and_comments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "comments-display.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("消费热点", "普通人的消费体验", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            job_id = database.create_comment_jobs(topic_id, "消费热点", ["wb"])[0]
            database.save_social_data(
                job_id,
                topic_id,
                "wb",
                [{"post_id": "p1", "title": "样本帖子", "url": "https://example.com/p1", "like_count": 8}],
                [{"comment_id": "c1", "post_id": "p1", "content": "样本评论", "like_count": 12}],
            )
            settings = SimpleNamespace(
                root=Path(__file__).resolve().parents[1], sources=(), raw={"app": {}, "scoring": {}},
                api_key="", ai_base_url="https://api.example.com/v1", ai_model="test-model",
            )
            client = create_app(settings, database, comment_service=object()).test_client()
            response = client.get(f"/topics/{topic_id}/comments")
            self.assertEqual(response.status_code, 200)
            self.assertIn("样本帖子".encode("utf-8"), response.data)
            self.assertIn("样本评论".encode("utf-8"), response.data)
            self.assertEqual(database.topic_comment_summary(topic_id)["post_count"], 1)
            self.assertEqual(database.topic_comment_summary(topic_id)["comment_count"], 1)

    def test_platform_selection_is_persisted_and_requires_two_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "settings.db")
            sources = (
                SourceConfig("a", "平台A", 1.0, True, True),
                SourceConfig("b", "平台B", 0.9, True, True),
                SourceConfig("c", "平台C", 0.8, True, False),
            )
            settings = SimpleNamespace(
                root=Path(__file__).resolve().parents[1],
                sources=sources,
                raw={"app": {"recent_runs": 10}, "scoring": {}},
                api_key="env-key",
                ai_base_url="https://env.example/v1",
                ai_model="test-model",
            )
            client = create_app(settings, database).test_client()
            interval_updates = []
            client.application.config["set_collection_interval"] = interval_updates.append
            self.assertEqual(client.get("/settings").status_code, 200)
            invalid = client.post("/settings", data={"analysis_sources": "a"})
            self.assertEqual(invalid.status_code, 200)
            self.assertIn("至少选择两个平台".encode("utf-8"), invalid.data)
            saved = client.post(
                "/settings",
                data={"analysis_sources": ["a", "c"]},
                follow_redirects=True,
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(database.get_analysis_source_ids({"a", "b"}, {"a", "b", "c"}), {"a", "c"})
            ai_saved = client.post(
                "/settings",
                data={
                    "form_action": "ai_connection",
                    "ai_api_key": "new-key",
                    "ai_base_url": "https://api.example.com/v1/",
                },
                follow_redirects=True,
            )
            self.assertEqual(ai_saved.status_code, 200)
            self.assertIn(b"new-key", ai_saved.data)
            self.assertEqual(
                database.get_ai_connection("env-key", "https://env.example/v1"),
                {"api_key": "new-key", "base_url": "https://api.example.com/v1"},
            )
            invalid_url = client.post(
                "/settings",
                data={"form_action": "ai_connection", "ai_api_key": "", "ai_base_url": "not-a-url"},
            )
            self.assertIn("Base URL".encode(), invalid_url.data)
            comment_saved = client.post(
                "/settings",
                data={"form_action": "comment_platforms", "comment_platforms": ["dy", "bili", "zhihu"]},
                follow_redirects=True,
            )
            self.assertEqual(comment_saved.status_code, 200)
            self.assertEqual(
                database.get_comment_platform_ids({"dy", "wb"}, {"dy", "wb", "bili", "zhihu"}),
                {"dy", "bili", "zhihu"},
            )
            weights_saved = client.post(
                "/settings",
                data={
                    "form_action": "ranking_weights",
                    "weight_current": "20",
                    "weight_rising": "50",
                    "weight_sustained": "30",
                },
                follow_redirects=True,
            )
            self.assertEqual(weights_saved.status_code, 200)
            self.assertIn("总榜权重已归一化并立即生效".encode("utf-8"), weights_saved.data)
            self.assertEqual(
                database.get_section_weights({"current": 0.5, "rising": 0.3, "sustained": 0.2}),
                {"current": 0.2, "rising": 0.5, "sustained": 0.3},
            )
            invalid_weights = client.post(
                "/settings",
                data={
                    "form_action": "ranking_weights",
                    "weight_current": "0",
                    "weight_rising": "0",
                    "weight_sustained": "0",
                },
            )
            self.assertIn("不能同时为 0".encode("utf-8"), invalid_weights.data)
            interval_saved = client.post(
                "/settings",
                data={"form_action": "collection_interval", "interval_minutes": "45"},
                follow_redirects=True,
            )
            self.assertEqual(interval_saved.status_code, 200)
            self.assertIn("后台定时任务已重新排期".encode("utf-8"), interval_saved.data)
            self.assertEqual(database.get_collection_interval_minutes(30), 45)
            self.assertEqual(interval_updates, [45])
            invalid_interval = client.post(
                "/settings",
                data={"form_action": "collection_interval", "interval_minutes": "0"},
            )
            self.assertIn("1–1440".encode("utf-8"), invalid_interval.data)

    def test_article_web_search_setting_is_persisted_and_rendered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "web-search.db")
            settings = SimpleNamespace(
                root=Path(__file__).resolve().parents[1],
                sources=(),
                raw={"app": {}, "scoring": {}},
                api_key="",
                ai_base_url="https://api.example.com/v1",
                ai_model="test-model",
            )
            client = create_app(settings, database, comment_service=object()).test_client()
            page = client.get("/settings")
            self.assertEqual(page.status_code, 200)
            self.assertIn("文章生成时允许 AI 联网".encode("utf-8"), page.data)
            self.assertIn(b'name="article_web_search_enabled"', page.data)
            self.assertTrue(database.get_article_web_search_enabled())

            saved = client.post(
                "/settings",
                data={"form_action": "article_web_search"},
                follow_redirects=True,
            )
            self.assertEqual(saved.status_code, 200)
            self.assertFalse(database.get_article_web_search_enabled())
            self.assertNotIn(b'checked', saved.data.split(b'name="article_web_search_enabled"', 1)[1].split(b'>', 1)[0])

            client.post(
                "/settings",
                data={"form_action": "article_web_search", "article_web_search_enabled": "1"},
            )
            self.assertTrue(database.get_article_web_search_enabled())

    def test_log_page_and_ai_exchange_details_are_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "logs.db")
            run_id = database.begin_run()
            database.append_log(run_id, "collection", "开始采集")
            exchange_id = database.begin_ai_exchange(run_id, 1, {"model": "test", "messages": []})
            database.finish_ai_exchange(exchange_id, "success", '{"answer":"ok"}', 200)
            settings = SimpleNamespace(
                root=Path(__file__).resolve().parents[1],
                sources=(),
                raw={"app": {}, "scoring": {}},
                api_key="",
                ai_base_url="https://api.example.com/v1",
                ai_model="test-model",
            )
            client = create_app(settings, database).test_client()
            self.assertEqual(client.get(f"/logs?run_id={run_id}").status_code, 200)
            logs = client.get(f"/api/logs?run_id={run_id}").get_json()
            self.assertEqual(logs["logs"][0]["message"], "开始采集")
            exchange = client.get(f"/api/ai-exchanges/{exchange_id}").get_json()
            self.assertIn('"model": "test"', exchange["request_json"])
            self.assertEqual(exchange["response_text"], '{"answer":"ok"}')


if __name__ == "__main__":
    unittest.main()
