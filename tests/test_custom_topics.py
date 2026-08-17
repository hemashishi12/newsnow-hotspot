import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import Database
from app.news_search import NewsSearchService, _format_date
from app.web import create_app


class CustomTopicTests(unittest.TestCase):
    def test_news_time_is_short_date(self):
        self.assertEqual(_format_date("Wed, 17 Nov 2021 08:00:00 GMT"), "21-11-17")

    def test_bing_html_fallback_parses_public_results(self):
        class Response:
            text = '<ol><li class="b_algo"><h2><a href="https://example.com/news">Bing 结果</a></h2><p>摘要</p></li></ol>'
            def raise_for_status(self): pass
        with patch("app.news_search.httpx.get", return_value=Response()):
            result = NewsSearchService()._bing_news("测试")
        self.assertEqual(result[0]["title"], "Bing 结果")

    def test_news_search_parses_and_deduplicates_rss_results(self):
        xml = """<rss><channel>
          <item><title>事件标题 - 新闻源</title><link>https://example.com/a</link><source>新闻源</source></item>
          <item><title>事件标题</title><link>https://example.com/b</link><source>另一源</source></item>
        </channel></rss>""".encode("utf-8")
        class Response:
            content = xml
            def raise_for_status(self): pass
        with patch("app.news_search.httpx.get", return_value=Response()):
            result = NewsSearchService()._google_news("测试")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "事件标题")

    def test_custom_topic_is_saved_and_used_as_article_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "topics.db")
            topic_id = database.create_custom_topic(
                "自定事件", "背景", [{"title": "相关新闻", "url": "https://example.com", "source": "媒体"}]
            )
            context = database.topic_article_context(topic_id)
            self.assertEqual(context["members"][0]["title"], "相关新闻")
            self.assertEqual(context["members"][0]["source_name"], "媒体")
            same_id = database.create_custom_topic(
                " 自定 事件 ", "", [{"title": "追加新闻", "url": "https://example.com/2", "source": "媒体"}]
            )
            self.assertEqual(same_id, topic_id)
            self.assertEqual(len(database.custom_topics()), 1)
            self.assertEqual(database.custom_topics()[0]["news_count"], 2)

    def test_custom_topics_page_search_and_save(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "topics.db")
            settings = SimpleNamespace(root=Path(__file__).resolve().parents[1], sources=(), raw={"app": {}, "scoring": {}}, api_key="", ai_base_url="https://example.com/v1", ai_model="test")
            app = create_app(settings, database, comment_service=object())
            app.config["news_search_service"].search = lambda query: {"query": query, "results": [{"title": "搜索结果", "url": "https://example.com", "source": "媒体", "provider": "Google News", "summary": ""}], "errors": []}
            client = app.test_client()
            self.assertEqual(client.post("/custom-topics", data={"action": "search", "query": "测试话题"}).status_code, 200)
            response = client.post("/custom-topics", data={"action": "save", "title": "测试话题", "news_json": '{"title":"搜索结果","url":"https://example.com","source":"媒体"}'})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(len(database.custom_topics()), 1)

    def test_saving_multiple_news_items_creates_independent_topics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "multiple-topics.db")
            settings = SimpleNamespace(root=Path(__file__).resolve().parents[1], sources=(), raw={"app": {}, "scoring": {}}, api_key="", ai_base_url="https://example.com/v1", ai_model="test")
            client = create_app(settings, database, comment_service=object()).test_client()
            response = client.post(
                "/custom-topics",
                data={
                    "action": "save",
                    "title": "共同搜索词",
                    "news_json": [
                        '{"title":"第一条独立新闻","url":"https://example.com/1","source":"媒体一","summary":"第一条摘要"}',
                        '{"title":"第二条独立新闻","url":"https://example.com/2","source":"媒体二","summary":"第二条摘要"}',
                    ],
                },
            )
            self.assertEqual(response.status_code, 302)
            topics = database.custom_topics()
            self.assertEqual(len(topics), 2)
            self.assertEqual({topic["canonical_title"] for topic in topics}, {"第一条独立新闻", "第二条独立新闻"})
            self.assertEqual([topic["news_count"] for topic in topics], [1, 1])

    def test_batch_article_controls_are_available_on_both_topic_pages(self):
        root = Path(__file__).resolve().parents[1]
        batch_script = (root / "static" / "article-batch-actions.js").read_text(encoding="utf-8")
        index_template = (root / "templates" / "index.html").read_text(encoding="utf-8")
        styles = (root / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("for (const input of items)", batch_script)
        self.assertIn("background: true", batch_script)
        self.assertIn("follow_up_video: true", batch_script)
        self.assertIn("articleType", batch_script)
        self.assertIn("getBoundingClientRect().top", batch_script)
        self.assertIn("window.scrollBy(0, layoutShift)", batch_script)
        self.assertIn("window.requestAnimationFrame(() =>", batch_script)
        self.assertIn("setTimeout(restoreAnchor, 0)", batch_script)
        self.assertNotIn(">选择话题</span>", index_template)
        self.assertIn(".article-batch-toolbar[hidden]", styles)
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "batch-pages.db")
            topic_id = database.create_custom_topic(
                "批量页面话题", "", [{"title": "批量页面新闻", "url": "https://example.com/batch"}]
            )
            settings = SimpleNamespace(root=root, sources=(), raw={"app": {}, "scoring": {}}, api_key="", ai_base_url="https://example.com/v1", ai_model="test")
            app = create_app(settings, database, comment_service=object())
            client = app.test_client()
            home_html = client.get("/").data.decode("utf-8")
            custom_html = client.get("/custom-topics").data.decode("utf-8")
            self.assertIn("article-batch-toolbar", home_html)
            self.assertIn("article-batch-actions.js", home_html)
            self.assertGreaterEqual(home_html.count('data-article-batch'), 2)
            for html in (custom_html,):
                self.assertIn("article-topic-select", html)
                self.assertIn("data-batch-article-type=\"standard\"", html)
                self.assertIn("data-batch-article-type=\"long\"", html)
                self.assertIn("data-batch-video", html)
                self.assertIn("article-batch-actions.js", html)
                self.assertGreaterEqual(html.count('data-article-batch'), 2)
                self.assertNotIn(">选择话题</span>", html)
            self.assertIn(f'data-topic-id="{topic_id}"', custom_html)


if __name__ == "__main__":
    unittest.main()
