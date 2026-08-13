import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import Database
from app.toutiao import ToutiaoDraftService, split_article_content
from app import toutiao_fill
from app.web import create_app


class ToutiaoDraftTests(unittest.TestCase):
    def test_editor_wait_retries_when_login_navigation_destroys_context(self):
        class NavigatingFirefox:
            window_handles = ["main"]

        title_element = object()
        editor_element = object()
        navigation_error = RuntimeError(
            "Page.evaluate_handle: Execution context was destroyed, most likely because of a navigation"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "status.json"
            with patch.object(
                toutiao_fill,
                "best_candidate",
                side_effect=[navigation_error, title_element, editor_element],
            ):
                result = toutiao_fill.wait_for_editor(
                    NavigatingFirefox(), status_path, timeout_seconds=2
                )

        self.assertEqual(result, (title_element, editor_element))

    def test_article_title_and_body_are_split_for_toutiao(self):
        title, body = split_article_content(
            "# 台风“白海豚”来袭，网友：别慌！\n\n第一段\n\n第二段",
            "备用标题",
        )
        self.assertEqual(title, "台风“白海豚”来袭，网友：别慌！")
        self.assertEqual(body, "第一段\n\n第二段")

    def test_service_starts_helper_without_putting_article_in_command_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "newsnow-hotspot"
            root.mkdir()
            process = SimpleNamespace(poll=lambda: None)
            service = ToutiaoDraftService(root)
            with patch("app.toutiao.subprocess.Popen", return_value=process) as popen:
                job = service.start("测试标题\n\n私密的草稿正文")
            command = popen.call_args.args[0]
            self.assertNotIn("私密的草稿正文", command)
            self.assertIn("toutiao-firefox-profile", command[-1])
            self.assertEqual(len(job["job_id"]), 32)
            self.assertEqual(service.status(job["job_id"])["status"], "starting")

    def test_web_routes_start_and_report_fill_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(__file__).resolve().parents[1]
            database = Database(Path(temp_dir) / "toutiao-route.db")
            settings = SimpleNamespace(
                root=root,
                sources=(),
                raw={"app": {}, "scoring": {}},
                api_key="",
                ai_base_url="https://api.example.com/v1",
                ai_model="test-model",
            )

            class FakeToutiaoService:
                def start(self, content, fallback_title):
                    self.received = (content, fallback_title)
                    return {"job_id": "a" * 32, "status": "starting", "title": "标题"}

                def status(self, job_id):
                    return {"status": "filled", "message": "已填入"}

            service = FakeToutiaoService()
            client = create_app(
                settings,
                database,
                comment_service=object(),
                toutiao_service=service,
            ).test_client()
            started = client.post(
                "/api/toutiao/drafts",
                json={"content": "标题\n\n正文", "topic_title": "话题"},
            )
            self.assertEqual(started.status_code, 202)
            self.assertEqual(service.received, ("标题\n\n正文", "话题"))
            status = client.get(f"/api/toutiao/drafts/{'a' * 32}")
            self.assertEqual(status.get_json()["status"], "filled")


if __name__ == "__main__":
    unittest.main()
