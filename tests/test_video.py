import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.database import Database
from app.tts import synthesize
from app.video import VideoJobService, article_to_narration, validate_local_engine_url
from app.web import create_app


def make_settings(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        root=root,
        sources=(),
        raw={"app": {}, "scoring": {}, "ai": {"timeout_seconds": 10}},
        api_key="test-key",
        ai_base_url="https://api.example.com/v1",
        ai_model="test-model",
    )


def insert_article(database: Database) -> int:
    with database.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
            ("新能源发布会", "", "2026-08-15", "2026-08-15"),
        )
        topic_id = int(cursor.lastrowid)
    return database.save_generated_article(
        topic_id,
        "提示词",
        "模型",
        "# 新能源发布会\n\n**现场消息**显示，[新车型](https://example.com)已经发布。",
        {},
    )


class VideoFeatureTests(unittest.TestCase):
    def test_video_jobs_are_consumed_serially(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "video-queue.db")
            service = VideoJobService(Path(temp_dir), database, poll_seconds=0.01)
            started = threading.Event()
            finished = threading.Event()
            release_first = threading.Event()
            lock = threading.Lock()
            running = 0
            maximum_running = 0
            order = []

            def fake_run(job_id):
                nonlocal running, maximum_running
                with lock:
                    running += 1
                    maximum_running = max(maximum_running, running)
                    order.append(job_id)
                started.set()
                if job_id == 1:
                    release_first.wait(timeout=2)
                with lock:
                    running -= 1
                if job_id == 2:
                    finished.set()

            service._run = fake_run
            service.start(1)
            service.start(1)
            service.start(2)

            self.assertTrue(started.wait(1))
            time.sleep(0.05)
            with lock:
                self.assertEqual(maximum_running, 1)
                self.assertEqual(order, [1])
            release_first.set()
            self.assertTrue(finished.wait(1))
            service._queue.join()
            self.assertEqual(order, [1, 2])

    def test_article_video_enqueue_uses_saved_preferences(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "video-defaults.db")
            article_id = insert_article(database)
            database.set_video_preferences({"source": "pixabay", "voice_rate": 1.2})
            service = VideoJobService(root, database, poll_seconds=0.01)
            service._run = lambda _job_id: None

            job_id = service.enqueue_article_video(article_id)
            service._queue.join()
            job = database.article_video_job(job_id)
            params = json.loads(job["params_json"])
            self.assertEqual(params["source"], "pixabay")
            self.assertEqual(params["voice_rate"], 1.2)
            self.assertEqual(params["title"], "新能源发布会")
            self.assertEqual(params["search_terms"], "新能源发布会")

    def test_openai_compatible_tts_writes_audio_and_uses_selected_voice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "speech.mp3"
            captured = {}

            class FakeResponse:
                headers = {"content-type": "audio/mpeg"}
                content = b"audio-bytes" * 40

                def raise_for_status(self):
                    return None

            class FakeClient:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def post(self, url, **kwargs):
                    captured["url"] = url
                    captured.update(kwargs)
                    return FakeResponse()

            with patch("app.tts.httpx.Client", return_value=FakeClient()):
                result = synthesize(
                    "openai",
                    "一段外部 API 配音测试文本。",
                    output,
                    {"tts_voice": "nova", "voice_rate": 1.2},
                    {
                        "tts_api_url": "https://tts.example/v1",
                        "tts_api_key": "secret",
                        "tts_model": "custom-tts",
                        "tts_voice": "alloy",
                    },
                )

            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), b"audio-bytes" * 40)
            self.assertEqual(captured["url"], "https://tts.example/v1/audio/speech")
            self.assertEqual(captured["headers"]["Authorization"], "Bearer secret")
            self.assertEqual(captured["json"]["voice"], "nova")
            self.assertEqual(captured["json"]["model"], "custom-tts")

    def test_markdown_article_is_converted_to_plain_narration(self):
        narration = article_to_narration(
            '# “标题”\n\n> **重点内容** "引用"\n\n[“来源”](https://example.com)\n\n![图](image.png)'
        )
        self.assertEqual(narration, "标题\n\n重点内容 引用\n\n来源")
        self.assertNotRegex(narration, r'["\'“”‘’＂＇„‟‚‛«»‹›「」『』﹁﹂﹃﹄]')

    def test_engine_url_is_restricted_to_localhost(self):
        self.assertEqual(
            validate_local_engine_url("http://127.0.0.1:8080/"),
            "http://127.0.0.1:8080",
        )
        with self.assertRaises(ValueError):
            validate_local_engine_url("https://video.example.com:8080")
        with self.assertRaises(ValueError):
            validate_local_engine_url("http://user:pass@127.0.0.1:8080")

    def test_video_jobs_are_persisted_and_keep_provider_params(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "video.db")
            article_id = insert_article(database)
            job_id = database.create_article_video_job(
                article_id,
                "这是一段用于视频生成的口播稿。",
                {"source": "pixabay", "aspect": "9:16"},
            )
            database.update_article_video_job(
                job_id,
                engine_task_id="a" * 32,
                status="processing",
                progress=40,
                message="正在生成",
            )
            job = database.article_video_job(job_id)
            self.assertEqual(job["progress"], 40)
            self.assertEqual(json.loads(job["params_json"])["source"], "pixabay")
            self.assertEqual(database.active_article_video_job(article_id)["id"], job_id)

    def test_article_history_shows_all_videos_above_regeneration_form(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "article-video-history.db")
            article_id = insert_article(database)
            older_id = database.create_article_video_job(
                article_id,
                "这是第一条文章视频的口播稿，用于验证历史记录不会被覆盖。",
                {"title": "第一条视频", "source": "pexels"},
            )
            newer_id = database.create_article_video_job(
                article_id,
                "这是第二条文章视频的口播稿，用于验证最新记录显示在最上方。",
                {"title": "第二条视频", "source": "pexels"},
            )
            database.update_article_video_job(
                older_id, status="success", progress=100, message="口播视频已生成"
            )
            database.update_article_video_job(
                newer_id, status="success", progress=100, message="口播视频已生成"
            )

            self.assertEqual(
                [job["id"] for job in database.article_video_jobs(article_id)],
                [newer_id, older_id],
            )
            client = create_app(
                make_settings(Path(__file__).resolve().parents[1]),
                database,
                comment_service=object(),
            ).test_client()
            html = client.get("/articles").data.decode("utf-8")
            details_start = html.index("<details>")
            details_end = html.index("</details>", details_start)
            panel_index = html.index('data-video-history="true"')
            self.assertGreater(panel_index, details_end)
            self.assertIn(">查看视频</button>", html)
            self.assertIn('history-video-inline-status is-success', html)
            self.assertIn("口播视频已生成", html)
            self.assertNotIn("ARTICLE TO VIDEO", html)
            self.assertLess(html.index(f'"id": {newer_id}'), html.index(f'"id": {older_id}'))

    def test_legacy_video_jobs_are_migrated_to_allow_custom_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy-video.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE topics (
                    id INTEGER PRIMARY KEY,
                    canonical_title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE generated_articles (
                    id INTEGER PRIMARY KEY,
                    topic_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    model TEXT NOT NULL,
                    content TEXT NOT NULL,
                    input_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE article_video_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    engine_task_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    script TEXT NOT NULL,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    result_path TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO topics(id,canonical_title,first_seen_at,last_seen_at)
                VALUES (3,'旧话题','2026-08-16','2026-08-16');
                INSERT INTO generated_articles(id,topic_id,created_at,prompt,model,content)
                VALUES (7,3,'2026-08-16','提示词','模型','文章内容');
                INSERT INTO article_video_jobs(article_id,script,created_at,updated_at)
                VALUES (7,'已有文章视频任务','2026-08-16','2026-08-16');
                """
            )
            connection.commit()
            connection.close()

            database = Database(path)
            with database.connect() as connection:
                columns = {
                    str(row["name"]): int(row["notnull"] or 0)
                    for row in connection.execute("PRAGMA table_info(article_video_jobs)")
                }
            self.assertEqual(columns["article_id"], 0)
            self.assertIn("read_at", columns)
            self.assertEqual(database.article_video_job(1)["script"], "已有文章视频任务")
            self.assertIsNotNone(database.create_article_video_job(None, "自定任务", {"title": "测试"}))

    def test_video_routes_create_poll_and_serve_a_finished_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "video-route.db")
            article_id = insert_article(database)
            database.set_video_engine_settings(
                "http://127.0.0.1:8080", "pexels-test-key", "", ""
            )
            result_path = root / "finished.mp4"
            result_path.write_bytes(b"video-bytes")

            class FakeVideoService:
                def __init__(self):
                    self.started = []

                def start(self, job_id):
                    self.started.append(job_id)

                def engine_status(self):
                    return {"installed": True, "online": True}

                def result_file(self, _job):
                    return result_path

            service = FakeVideoService()
            client = create_app(
                make_settings(root), database, comment_service=object(), video_service=service
            ).test_client()
            response = client.post(
                f"/api/articles/{article_id}/videos",
                json={
                    "script": "这是一段长度足够并且适合直接朗读的视频口播稿。",
                    "aspect": "9:16",
                    "voice": "zh-CN-XiaoxiaoNeural-Female",
                    "voice_rate": 1.0,
                    "source": "pexels",
                    "search_terms": "technology, electric car",
                    "subtitle_enabled": True,
                },
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.get_json()["id"]
            self.assertEqual(service.started, [job_id])

            database.update_article_video_job(
                job_id,
                status="success",
                progress=100,
                message="完成",
                result_path=f"/tasks/{'a' * 32}/final-1.mp4",
                result_json=json.dumps(
                    {
                        "material_sources": [
                            {
                                "provider": "pexels",
                                "source_page": "https://www.pexels.com/video/1/",
                                "creator": {"name": "Author", "profile_page": "https://www.pexels.com/@author"},
                            }
                        ]
                    }
                ),
            )
            status = client.get(f"/api/article-videos/{job_id}")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.get_json()["credits"][0]["creator_name"], "Author")
            video = client.get(f"/api/article-videos/{job_id}/result")
            self.assertEqual(video.status_code, 200)
            self.assertEqual(video.data, b"video-bytes")
            video.close()

    def test_custom_video_is_saved_in_history_and_video_notifications(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "custom-video.db")
            database.set_video_engine_settings(
                "http://127.0.0.1:8080", "pexels-test-key", "", ""
            )

            class FakeVideoService:
                def __init__(self):
                    self.started = []

                def start(self, job_id):
                    self.started.append(job_id)

                def engine_status(self):
                    return {"installed": True, "online": True}

            service = FakeVideoService()
            client = create_app(
                make_settings(Path(__file__).resolve().parents[1]),
                database,
                comment_service=object(),
                video_service=service,
            ).test_client()
            script = '“这是一段用户自己输入的口播文案”，用来验证自定视频任务。'
            sanitized_script = "这是一段用户自己输入的口播文案，用来验证自定视频任务。"
            response = client.post(
                "/api/custom-videos",
                json={
                    "title": "自定视频测试",
                    "script": script,
                    "aspect": "16:9",
                    "voice": "zh-CN-YunxiNeural-Male",
                    "voice_rate": 1.2,
                    "source": "pexels",
                    "search_terms": "测试素材",
                    "subtitle_enabled": False,
                },
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.get_json()["id"]
            self.assertEqual(service.started, [job_id])
            self.assertIsNone(database.article_video_job(job_id)["article_id"])
            self.assertEqual(database.article_video_job(job_id)["script"], sanitized_script)
            self.assertEqual(
                database.get_video_preferences(
                    {
                        "aspect": "9:16",
                        "voice": "zh-CN-XiaoxiaoNeural-Female",
                        "voice_rate": 1.0,
                        "source": "pexels",
                        "subtitle_enabled": True,
                    }
                ),
                {
                    "aspect": "16:9",
                    "voice": "zh-CN-YunxiNeural-Male",
                    "voice_rate": 1.2,
                    "source": "pexels",
                    "subtitle_enabled": False,
                },
            )
            self.assertIn("自定视频测试", client.get("/articles").data.decode("utf-8"))
            custom_page = client.get("/custom-video")
            self.assertEqual(custom_page.status_code, 200)
            custom_html = custom_page.data.decode("utf-8")
            self.assertIn('history-video-form', custom_html)
            self.assertIn("ARTICLE TO VIDEO", custom_html)
            self.assertIn("/api/custom-videos", custom_html)
            self.assertIn('value="16:9" checked', custom_html)
            self.assertIn('value="zh-CN-YunxiNeural-Male" selected', custom_html)
            self.assertIn('name="voice_rate" min="0.7" max="1.5" step="0.1" value="1.2"', custom_html)
            self.assertIn('value="pexels" selected', custom_html)
            self.assertIn('name="subtitle_enabled"><span>烧录字幕</span>', custom_html)

            notifications = client.get("/api/article-jobs").get_json()
            video_notification = next(item for item in notifications["jobs"] if item["id"] == job_id)
            self.assertEqual(video_notification["notification_type"], "video")
            self.assertEqual(video_notification["topic_title"], "自定视频测试")

            database.update_article_video_job(job_id, status="success", progress=100, message="完成")
            self.assertEqual(client.get("/api/article-jobs").get_json()["unread"], 1)
            read = client.post(
                "/api/article-jobs/read", json={"jobs": [{"type": "video", "id": job_id}]}
            )
            self.assertEqual(read.status_code, 204)
            self.assertEqual(client.get("/api/article-jobs").get_json()["unread"], 0)

    def test_gpt_sovits_can_be_selected_after_its_settings_are_saved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "gpt-sovits.db")
            database.set_video_engine_settings(
                "http://127.0.0.1:8080", "pexels-test-key", "", ""
            )

            class FakeVideoService:
                def start(self, _job_id):
                    return None

            client = create_app(
                make_settings(Path(__file__).resolve().parents[1]),
                database,
                comment_service=object(),
                video_service=FakeVideoService(),
            ).test_client()
            script = "这是一段用于 GPT-SoVITS 选择测试的自定视频口播文案。"
            missing_reference = client.post(
                "/api/custom-videos",
                json={
                    "title": "GPT-SoVITS 测试",
                    "script": script,
                    "source": "pexels",
                    "tts_provider": "gpt_sovits",
                },
            )
            self.assertEqual(missing_reference.status_code, 400)
            self.assertIn("参考音频", missing_reference.get_json()["error"])

            database.set_video_tts_settings(
                {
                    "gpt_sovits_url": "http://127.0.0.1:9880/tts",
                    "gpt_sovits_ref_audio": "E:/voices/reference.wav",
                    "gpt_sovits_prompt_lang": "zh",
                    "gpt_sovits_text_lang": "zh",
                }
            )
            response = client.post(
                "/api/custom-videos",
                json={
                    "title": "GPT-SoVITS 测试",
                    "script": script,
                    "source": "pexels",
                    "tts_provider": "gpt_sovits",
                    "voice_rate": 1.1,
                },
            )
            self.assertEqual(response.status_code, 202)
            job = database.article_video_job(response.get_json()["id"])
            params = json.loads(job["params_json"])
            self.assertEqual(params["tts_provider"], "gpt_sovits")
            self.assertEqual(params["voice_rate"], 1.1)

    def test_articles_and_custom_videos_share_one_time_sorted_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "mixed-history.db")
            article_id = insert_article(database)
            video_id = database.create_article_video_job(
                None,
                "这是一段用于混合历史排序测试的自定视频口播稿。",
                {"title": "较新的视频", "source": "pexels"},
            )
            with database.connect() as connection:
                connection.execute(
                    "UPDATE generated_articles SET created_at=?,updated_at=? WHERE id=?",
                    ("2026-08-15T08:00:00+08:00", "2026-08-15T08:00:00+08:00", article_id),
                )
                connection.execute(
                    "UPDATE article_video_jobs SET created_at=?,updated_at=? WHERE id=?",
                    ("2026-08-15T09:00:00+08:00", "2026-08-15T09:00:00+08:00", video_id),
                )

            client = create_app(
                make_settings(Path(__file__).resolve().parents[1]),
                database,
                comment_service=object(),
            ).test_client()
            html = client.get("/articles").data.decode("utf-8")
            self.assertLess(html.index("较新的视频"), html.index("新能源发布会"))
            self.assertIn('class="history-video-collapse"', html)
            self.assertNotIn('class="history-video-collapse" open', html)

    def test_video_ui_is_inline_and_setup_path_is_adjacent_to_project(self):
        project_root = Path(__file__).resolve().parents[1]
        template = (project_root / "templates" / "articles.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "article-video.js").read_text(encoding="utf-8")
        setup = (project_root / "setup-video-engine.ps1").read_text(encoding="utf-8")
        service = VideoJobService.__new__(VideoJobService)
        service.root = project_root
        service.engine_root = project_root.parent / "MoneyPrinterTurbo"
        video_partial = (project_root / "templates" / "_video_form.html").read_text(encoding="utf-8")
        self.assertIn("history-video-panel", template + video_partial)
        self.assertIn("/api/articles/${articleId}/videos", script)
        self.assertIn("history-video-job-toggle", script)
        self.assertIn("history-video-card-toggle", script)
        self.assertIn("refreshHistoryCardStates", script)
        self.assertIn("is-history-collapsed", script)
        self.assertIn('name="tts_provider"', video_partial)
        self.assertIn("tts_provider: data.get('tts_provider')", script)
        self.assertIn('"..\\MoneyPrinterTurbo"', setup)
        self.assertEqual(service.engine_root, project_root.parent / "MoneyPrinterTurbo")

    def test_reconfigure_stops_an_already_running_external_engine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine_root = Path(temp_dir)
            (engine_root / "config.example.toml").write_text(
                'listen_host = "0.0.0.0"\n'
                'listen_port = 8080\n'
                'pexels_api_keys = []\n'
                'pixabay_api_keys = []\n'
                'coverr_api_keys = []\n',
                encoding="utf-8",
            )
            settings = {
                "engine_url": "http://127.0.0.1:8080",
                "pexels_api_key": "saved-key",
                "pixabay_api_key": "",
                "coverr_api_key": "",
            }
            service = VideoJobService.__new__(VideoJobService)
            service.engine_root = engine_root
            service.database = SimpleNamespace(get_video_engine_settings=lambda: settings)
            service._process = None
            service._terminate_external_engine = Mock()
            client = Mock()
            client.health.return_value = True
            with patch("app.video.MoneyPrinterTurboClient", return_value=client):
                service.reconfigure()

            service._terminate_external_engine.assert_called_once_with(client)
            config = (engine_root / "config.toml").read_text(encoding="utf-8")
            self.assertIn('listen_host = "127.0.0.1"', config)
            self.assertIn('pexels_api_keys = ["saved-key"]', config)

    def test_reconfigure_checks_for_a_surviving_child_after_owned_process_stops(self):
        service = VideoJobService.__new__(VideoJobService)
        service.engine_root = Path("missing-engine")
        service.database = SimpleNamespace(
            get_video_engine_settings=lambda: {
                "engine_url": "http://127.0.0.1:8080",
                "pexels_api_key": "saved-key",
                "pixabay_api_key": "",
                "coverr_api_key": "",
            }
        )
        service._process = Mock()
        service._process.poll.return_value = None
        service._terminate_external_engine = Mock()
        client = Mock()
        client.health.return_value = True
        with patch("app.video.MoneyPrinterTurboClient", return_value=client):
            service.reconfigure()

        service._terminate_external_engine.assert_called_once_with(client)


if __name__ == "__main__":
    unittest.main()
