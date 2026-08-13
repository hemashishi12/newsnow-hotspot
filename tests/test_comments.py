import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.comments as comments_module
from app.comments import (
    CommentCrawlerService,
    build_media_command,
    build_media_environment,
    normalize_media_output,
)
from app.database import Database


class CommentCrawlerTests(unittest.TestCase):
    def test_crawler_failure_message_keeps_the_real_login_error(self):
        formatter = getattr(comments_module, "crawler_failure_message", None)
        self.assertTrue(callable(formatter))
        message = formatter(
            1,
            [
                "ordinary log line",
                "MediaCrawler ERROR - cookie invalid; manual login timed out",
            ],
        )
        self.assertIn("manual login timed out", message)

    def test_shutdown_terminates_the_active_process_tree(self):
        terminator = getattr(comments_module, "terminate_process_tree", None)
        self.assertTrue(callable(terminator))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = CommentCrawlerService(root / "newsnow-hotspot", Database(root / "shutdown.db"))
            fake_process = SimpleNamespace(pid=43210, poll=lambda: None)
            second_process = SimpleNamespace(pid=43211, poll=lambda: None)
            service._active_processes = {1: fake_process, 2: second_process}
            with patch.object(comments_module, "terminate_process_tree") as terminate:
                service.shutdown()
            self.assertEqual(terminate.call_count, 2)
            terminate.assert_any_call(fake_process)
            terminate.assert_any_call(second_process)

    def test_command_uses_manual_mvp_limits(self):
        command = build_media_command(Path("C:/MediaCrawler"), "dy", "台风白海豚", Path("C:/output"))
        self.assertEqual(command[command.index("--crawler_max_notes_count") + 1], "3")
        self.assertEqual(command[command.index("--max_comments_count_singlenotes") + 1], "30")
        self.assertEqual(command[command.index("--get_sub_comment") + 1], "false")
        self.assertEqual(command[command.index("--max_concurrency_num") + 1], "1")
        self.assertEqual(command[command.index("--type") + 1], "search")
        self.assertEqual(command[command.index("--enable_ip_proxy") + 1], "false")

    def test_comment_crawler_environment_disables_all_proxy_sources(self):
        env = build_media_environment(
            {
                "HTTP_PROXY": "http://127.0.0.1:7890",
                "https_proxy": "http://127.0.0.1:7890",
                "ALL_PROXY": "socks5://127.0.0.1:7891",
                "KEEP_ME": "yes",
            }
        )
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("https_proxy", env)
        self.assertNotIn("ALL_PROXY", env)
        self.assertEqual(env["NO_PROXY"], "*")
        self.assertEqual(env["no_proxy"], "*")
        self.assertEqual(env["MEDIACRAWLER_DIRECT_CONNECTION"], "1")
        self.assertEqual(env["KEEP_ME"], "yes")

    def test_comment_crawler_environment_assigns_a_fixed_port_per_platform(self):
        expected_ports = {"dy": "9222", "wb": "9223", "zhihu": "9224", "bili": "9225"}
        for platform, port in expected_ports.items():
            try:
                env = build_media_environment({}, platform=platform)
            except TypeError:
                self.fail("build_media_environment 尚不支持平台固定端口")
            self.assertEqual(env["MEDIACRAWLER_CDP_DEBUG_PORT"], port)

    def test_only_transient_browser_failures_are_retryable(self):
        retryable = getattr(comments_module, "is_retryable_browser_failure", None)
        self.assertTrue(callable(retryable))
        for message in (
            "Browser failed to start within 60 seconds",
            "playwright TargetClosedError: browser has been closed",
            "TimeoutError: Page.goto: Timeout 30000ms exceeded",
        ):
            self.assertTrue(retryable(message), message)
        self.assertFalse(retryable("未找到可导入的相关帖子，请检查搜索词"))
        self.assertFalse(retryable("d_c0 not found in cookies"))

    def test_transient_browser_failure_is_retried_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "retry.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("重试话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            service = CommentCrawlerService(root / "newsnow-hotspot", database)
            job_id = database.create_comment_jobs(topic_id, "重试话题", ["dy"])[0]
            failed_process = SimpleNamespace(
                pid=1001,
                stdout=iter(["playwright TargetClosedError: browser has been closed\n"]),
                wait=lambda: 1,
            )
            successful_process = SimpleNamespace(pid=1002, stdout=iter([]), wait=lambda: 0)

            with (
                patch.object(comments_module.subprocess, "Popen", side_effect=[failed_process, successful_process]) as popen,
                patch.object(comments_module, "normalize_media_output", return_value=([{"post_id": "1"}], [])),
                patch.object(database, "save_social_data"),
            ):
                service._run_job(job_id)

            self.assertEqual(popen.call_count, 2)
            self.assertEqual(database.comment_job(job_id)["status"], "success")
            messages = [row["message"] for row in database.comment_job_logs(topic_id)]
            self.assertTrue(any("自动重试" in message for message in messages))

    def test_normalizer_caps_posts_and_first_level_comments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "douyin" / "jsonl"
            data_dir.mkdir(parents=True)
            posts = [
                {"aweme_id": str(index), "desc": f"post {index}", "aweme_url": f"https://dy/{index}"}
                for index in range(1, 6)
            ]
            comments = []
            for index in range(35):
                comments.append(
                    {"comment_id": f"root-{index}", "aweme_id": "1", "content": str(index), "parent_comment_id": "0"}
                )
            comments.append(
                {"comment_id": "reply", "aweme_id": "1", "content": "reply", "parent_comment_id": "root-1"}
            )
            comments.append(
                {"comment_id": "outside", "aweme_id": "5", "content": "outside", "parent_comment_id": "0"}
            )
            (data_dir / "search_contents_2026-08-09.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in posts), encoding="utf-8"
            )
            (data_dir / "search_comments_2026-08-09.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in comments), encoding="utf-8"
            )

            normalized_posts, normalized_comments = normalize_media_output(root, "dy")
            self.assertEqual([post["post_id"] for post in normalized_posts], ["1", "2", "3"])
            self.assertEqual(len(normalized_comments), 30)
            self.assertNotIn("reply", {comment["comment_id"] for comment in normalized_comments})
            self.assertNotIn("outside", {comment["comment_id"] for comment in normalized_comments})

    def test_enqueue_order_is_douyin_then_weibo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "jobs.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            service = CommentCrawlerService(root / "newsnow-hotspot", database)
            service._ensure_worker = lambda: None
            job_ids = service.enqueue_topic(topic_id, "话题", ["dy", "wb"])
            self.assertEqual(service._queues["dy"].get_nowait(), job_ids[0])
            self.assertEqual(service._queues["wb"].get_nowait(), job_ids[1])
            self.assertEqual([job["platform"] for job in reversed(database.comment_jobs(topic_id))], ["dy", "wb"])

    def test_enqueue_topic_does_not_duplicate_active_platform_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "deduplicate-jobs.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("去重话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            service = CommentCrawlerService(root / "newsnow-hotspot", database)
            service._ensure_worker = lambda: None

            first_ids = service.enqueue_topic(topic_id, "去重话题", ["dy", "wb"])
            duplicate_ids = service.enqueue_topic(topic_id, "去重话题", ["dy", "wb"])

            self.assertEqual(len(first_ids), 2)
            self.assertEqual(duplicate_ids, [])
            self.assertEqual(len(database.comment_jobs(topic_id)), 2)
            self.assertEqual(sum(job_queue.qsize() for job_queue in service._queues.values()), 2)

    def test_same_platform_jobs_are_serial_while_other_platforms_run_in_parallel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "platform-queues.db")
            topic_ids = []
            with database.connect() as connection:
                for title in ("话题一", "话题二"):
                    cursor = connection.execute(
                        "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                        (title, "", "2026-08-09", "2026-08-09"),
                    )
                    topic_ids.append(int(cursor.lastrowid))

            service = CommentCrawlerService(root / "newsnow-hotspot", database)
            first_dy_started = threading.Event()
            second_dy_started = threading.Event()
            wb_started = threading.Event()
            release_first_dy = threading.Event()
            release_all = threading.Event()
            dy_count = 0
            count_lock = threading.Lock()

            def fake_run_job(job_id):
                nonlocal dy_count
                platform = database.comment_job(job_id)["platform"]
                if platform == "wb":
                    wb_started.set()
                    release_all.wait(timeout=2)
                    return
                with count_lock:
                    dy_count += 1
                    current = dy_count
                if current == 1:
                    first_dy_started.set()
                    release_first_dy.wait(timeout=2)
                    return
                else:
                    second_dy_started.set()
                release_all.wait(timeout=2)

            service._run_job = fake_run_job
            service.enqueue_topic(topic_ids[0], "话题一", ["dy", "wb"])
            self.assertTrue(first_dy_started.wait(timeout=1))
            self.assertTrue(wb_started.wait(timeout=1), "不同平台任务应并行运行")
            service.enqueue_topic(topic_ids[1], "话题二", ["dy"])
            self.assertFalse(second_dy_started.wait(timeout=0.2), "同平台任务发生了重叠")
            release_first_dy.set()
            self.assertTrue(second_dy_started.wait(timeout=1), "前序任务结束后后续任务未启动")
            release_all.set()

    def test_selected_platform_jobs_start_in_parallel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "parallel.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("并行话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            service = CommentCrawlerService(root / "newsnow-hotspot", database)
            started_threads: list[str] = []
            started_lock = threading.Lock()
            two_started = threading.Event()
            release = threading.Event()

            def fake_run_job(job_id):
                with started_lock:
                    started_threads.append(threading.current_thread().name)
                    if len(started_threads) >= 2:
                        two_started.set()
                release.wait(timeout=2)

            service._run_job = fake_run_job
            service.enqueue_topic(topic_id, "并行话题", ["dy", "wb", "bili", "zhihu"])
            self.assertTrue(two_started.wait(timeout=1), "多平台任务未并行启动")
            release.set()
            self.assertGreaterEqual(len(set(started_threads)), 2)

    def test_weibo_normalizer_keeps_self_rooted_first_level_comments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "weibo" / "jsonl"
            data_dir.mkdir(parents=True)
            (data_dir / "search_contents_2026-08-09.jsonl").write_text(
                json.dumps({"note_id": "post-1", "content": "微博帖子"}, ensure_ascii=False),
                encoding="utf-8",
            )
            rows = [
                {"comment_id": "root-1", "note_id": "post-1", "content": "一级", "parent_comment_id": "root-1"},
                {"comment_id": "reply-1", "note_id": "post-1", "content": "二级", "parent_comment_id": "root-1"},
            ]
            (data_dir / "search_comments_2026-08-09.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
            )
            _, comments = normalize_media_output(root, "wb")
            self.assertEqual([comment["comment_id"] for comment in comments], ["root-1"])

    def test_bilibili_and_zhihu_outputs_are_normalized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bili_dir = root / "bili" / "jsonl"
            bili_dir.mkdir(parents=True)
            (bili_dir / "search_contents_2026-08-09.jsonl").write_text(
                json.dumps({"video_id": "av1", "title": "B站视频", "video_url": "https://bilibili/av1"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (bili_dir / "search_comments_2026-08-09.jsonl").write_text(
                json.dumps({"comment_id": "bc1", "video_id": "av1", "content": "B站热评", "parent_comment_id": "0"}, ensure_ascii=False),
                encoding="utf-8",
            )
            bili_posts, bili_comments = normalize_media_output(root, "bili")
            self.assertEqual(bili_posts[0]["post_id"], "av1")
            self.assertEqual(bili_comments[0]["content"], "B站热评")

            zhihu_dir = root / "zhihu" / "jsonl"
            zhihu_dir.mkdir(parents=True)
            (zhihu_dir / "search_contents_2026-08-09.jsonl").write_text(
                json.dumps({"content_id": "z1", "title": "知乎回答", "content_url": "https://zhihu/z1"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (zhihu_dir / "search_comments_2026-08-09.jsonl").write_text(
                json.dumps({"comment_id": "zc1", "content_id": "z1", "content": "知乎热评", "parent_comment_id": "0"}, ensure_ascii=False),
                encoding="utf-8",
            )
            zhihu_posts, zhihu_comments = normalize_media_output(root, "zhihu")
            self.assertEqual(zhihu_posts[0]["post_id"], "z1")
            self.assertEqual(zhihu_comments[0]["content"], "知乎热评")

    def test_restart_fails_running_job_and_recovers_queued_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "recovery.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("话题", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            running_id, queued_id = database.create_comment_jobs(topic_id, "话题", ["dy", "wb"])
            database.set_comment_job_status(running_id, "running")
            self.assertEqual(database.recover_comment_jobs(), [queued_id])
            self.assertEqual(database.comment_job(running_id)["status"], "failed")

    def test_topic_summary_stays_active_while_any_parallel_job_is_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "parallel-status.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    ("并行状态", "", "2026-08-09", "2026-08-09"),
                )
                topic_id = int(cursor.lastrowid)
            first_id, second_id = database.create_comment_jobs(topic_id, "并行状态", ["dy", "wb"])
            database.set_comment_job_status(first_id, "running")
            database.set_comment_job_status(second_id, "success")
            self.assertEqual(database.topic_comment_summary(topic_id)["status"], "running")


if __name__ == "__main__":
    unittest.main()
