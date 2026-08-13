from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from .database import Database


PLATFORM_NAMES = {"dy": "抖音", "wb": "微博", "bili": "B站", "zhihu": "知乎"}
PLATFORM_OUTPUT_DIRS = {"dy": "douyin", "wb": "weibo", "bili": "bili", "zhihu": "zhihu"}
PLATFORM_CDP_PORTS = {"dy": 9222, "wb": 9223, "zhihu": 9224, "bili": 9225}
PROXY_ENV_NAMES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}


def crawler_failure_message(exit_code: int, error_lines: list[str]) -> str:
    detail = error_lines[-1].strip() if error_lines else "未提供详细错误"
    return f"MediaCrawler 退出码 {exit_code}：{detail[-1200:]}"


def is_retryable_browser_failure(message: str) -> bool:
    lower = message.lower()
    return any(
        marker in lower
        for marker in (
            "browser failed to start",
            "targetclosederror",
            "browser has been closed",
            "page.goto: timeout",
        )
    )


def terminate_process_tree(process: Any) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_media_command(media_root: Path, platform: str, keyword: str, output_path: Path) -> list[str]:
    python_path = media_root / ".venv" / "Scripts" / "python.exe"
    return [
        str(python_path),
        "main.py",
        "--platform", platform,
        "--lt", "qrcode",
        "--type", "search",
        "--keywords", keyword,
        "--get_comment", "true",
        "--get_sub_comment", "false",
        "--enable_ip_proxy", "false",
        "--crawler_max_notes_count", "3",
        "--max_comments_count_singlenotes", "30",
        "--max_concurrency_num", "1",
        "--save_data_option", "jsonl",
        "--save_data_path", str(output_path),
        "--headless", "false",
    ]


def build_media_environment(
    inherited: dict[str, str] | None = None,
    *,
    platform: str | None = None,
) -> dict[str, str]:
    env = dict(os.environ if inherited is None else inherited)
    for key in list(env):
        if key.upper() in PROXY_ENV_NAMES:
            env.pop(key, None)
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "MEDIACRAWLER_DIRECT_CONNECTION": "1",
        }
    )
    if platform in PLATFORM_CDP_PORTS:
        env["MEDIACRAWLER_CDP_DEBUG_PORT"] = str(PLATFORM_CDP_PORTS[platform])
    return env


def _read_jsonl_files(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def normalize_media_output(output_path: Path, platform: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    jsonl_dir = output_path / PLATFORM_OUTPUT_DIRS.get(platform, platform) / "jsonl"
    raw_posts = _read_jsonl_files(sorted(jsonl_dir.glob("search_contents_*.jsonl")))
    raw_comments = _read_jsonl_files(sorted(jsonl_dir.glob("search_comments_*.jsonl")))

    posts: list[dict[str, Any]] = []
    seen_posts: set[str] = set()
    for raw in raw_posts:
        post_id = str(
            raw.get("aweme_id")
            if platform == "dy"
            else raw.get("note_id")
            or raw.get("video_id")
            or raw.get("content_id")
            or ""
        )
        if not post_id or post_id in seen_posts:
            continue
        seen_posts.add(post_id)
        posts.append(
            {
                "post_id": post_id,
                "title": str(
                    raw.get("title")
                    or raw.get("desc")
                    or raw.get("content")
                    or raw.get("content_text")
                    or ""
                ),
                "url": str(
                    raw.get("aweme_url")
                    or raw.get("note_url")
                    or raw.get("video_url")
                    or raw.get("content_url")
                    or ""
                ),
                "author": str(raw.get("nickname") or raw.get("user_nickname") or ""),
                "published_at": str(
                    raw.get("create_date_time")
                    or raw.get("create_time")
                    or raw.get("created_time")
                    or ""
                ),
                "like_count": _as_int(raw.get("liked_count") or raw.get("voteup_count")),
                "comment_count": _as_int(
                    raw.get("comment_count") or raw.get("comments_count") or raw.get("video_comment")
                ),
                "raw": raw,
            }
        )
        if len(posts) == 3:
            break

    allowed_posts = {post["post_id"] for post in posts}
    per_post_count: defaultdict[str, int] = defaultdict(int)
    comments: list[dict[str, Any]] = []
    seen_comments: set[str] = set()
    for raw in raw_comments:
        comment_id = str(raw.get("comment_id") or "")
        post_id = str(
            raw.get("aweme_id")
            if platform == "dy"
            else raw.get("note_id")
            or raw.get("video_id")
            or raw.get("content_id")
            or ""
        )
        parent_id = str(raw.get("parent_comment_id") or "")
        # 微博接口会把一级评论的 rootid 写成评论自身 id；二级评论则指向
        # 另一条根评论。抖音一级评论通常使用 0。
        is_first_level = parent_id in {"", "0", "None"} or (platform == "wb" and parent_id == comment_id)
        if (
            not comment_id
            or comment_id in seen_comments
            or post_id not in allowed_posts
            or not is_first_level
            or per_post_count[post_id] >= 30
        ):
            continue
        seen_comments.add(comment_id)
        per_post_count[post_id] += 1
        comments.append(
            {
                "comment_id": comment_id,
                "post_id": post_id,
                "parent_comment_id": parent_id,
                "content": str(raw.get("content") or ""),
                "author": str(raw.get("nickname") or raw.get("user_nickname") or ""),
                "published_at": str(
                    raw.get("create_date_time")
                    or raw.get("create_time")
                    or raw.get("publish_time")
                    or ""
                ),
                "like_count": _as_int(raw.get("like_count") or raw.get("comment_like_count")),
                "raw": raw,
            }
        )
    return posts, comments


class CommentCrawlerService:
    def __init__(self, root: Path, database: Database):
        self.database = database
        self.media_root = root.parent / "mediacrawler"
        self.output_root = root / "data" / "mediacrawler"
        self._queues: dict[str, queue.Queue[int]] = {
            platform: queue.Queue() for platform in PLATFORM_NAMES
        }
        self._lock = threading.Lock()
        self._workers: dict[str, threading.Thread] = {}
        self._active_processes: dict[int, subprocess.Popen[str]] = {}
        recovered_job_ids = self.database.recover_comment_jobs()
        for job_id in recovered_job_ids:
            job = self.database.comment_job(job_id)
            if job and str(job["platform"]) in self._queues:
                self._queues[str(job["platform"])].put(job_id)
        if recovered_job_ids:
            self._ensure_worker()

    def enqueue_topic(self, topic_id: int, keyword: str, platforms: list[str]) -> list[int]:
        allowed = [platform for platform in platforms if platform in PLATFORM_NAMES]
        job_ids = self.database.create_comment_jobs(topic_id, keyword.strip(), allowed)
        for job_id in job_ids:
            job = self.database.comment_job(job_id)
            platform = str(job["platform"]) if job else ""
            self.database.append_comment_job_log(job_id, "任务已加入平台队列")
            if platform in self._queues:
                self._queues[platform].put(job_id)
        self._ensure_worker()
        return job_ids

    def _ensure_worker(self) -> None:
        with self._lock:
            self._workers = {
                platform: worker for platform, worker in self._workers.items() if worker.is_alive()
            }
            for platform in PLATFORM_NAMES:
                if platform in self._workers:
                    continue
                worker = threading.Thread(
                    target=self._work_loop,
                    args=(platform,),
                    daemon=True,
                    name=f"mediacrawler-{platform}-worker",
                )
                worker.start()
                self._workers[platform] = worker

    def _work_loop(self, platform: str) -> None:
        job_queue = self._queues[platform]
        while True:
            job_id = job_queue.get()
            try:
                self._run_job(job_id)
            finally:
                job_queue.task_done()

    def shutdown(self) -> None:
        with self._lock:
            processes = list(self._active_processes.values())
        for process in processes:
            terminate_process_tree(process)

    def _run_job(self, job_id: int) -> None:
        job = self.database.comment_job(job_id)
        if not job:
            return
        platform = str(job["platform"])
        platform_name = PLATFORM_NAMES.get(platform, platform)
        output_path = (self.output_root / f"job-{job_id}").resolve()
        self.database.set_comment_job_status(job_id, "running")
        self.database.append_comment_job_log(
            job_id,
            f"开始采集{platform_name}：3篇帖子，每篇最多30条一级评论",
            "success",
        )
        self.database.append_comment_job_log(
            job_id,
            "程序会自动打开专用 Chrome；如需登录、扫码或验证码，请在浏览器中手动完成",
            "warning",
        )
        command = build_media_command(self.media_root, platform, str(job["keyword"]), output_path)
        env = build_media_environment(platform=platform)
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        process = None
        try:
            for attempt in (1, 2):
                error_lines: list[str] = []
                process = subprocess.Popen(
                    command,
                    cwd=str(self.media_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                    creationflags=creationflags,
                    start_new_session=os.name != "nt",
                )
                with self._lock:
                    self._active_processes[job_id] = process
                if process.stdout:
                    for line in process.stdout:
                        message = line.strip()
                        if message:
                            lower = message.lower()
                            is_error = any(
                                marker in lower
                                for marker in ("error", "failed", "timeout", "invalid", "exception")
                            )
                            level = "error" if is_error else "info"
                            if is_error:
                                error_lines.append(message)
                            self.database.append_comment_job_log(job_id, message, level)
                exit_code = process.wait()
                with self._lock:
                    if self._active_processes.get(job_id) is process:
                        self._active_processes.pop(job_id, None)
                if exit_code == 0:
                    break
                failure = crawler_failure_message(exit_code, error_lines)
                if attempt == 1 and is_retryable_browser_failure(failure):
                    self.database.append_comment_job_log(
                        job_id,
                        "检测到临时浏览器故障，将自动重试一次",
                        "warning",
                    )
                    continue
                raise RuntimeError(failure)
            posts, comments = normalize_media_output(output_path, platform)
            if not posts:
                detail = f"；最近错误：{error_lines[-1][-800:]}" if error_lines else ""
                raise RuntimeError(f"未找到可导入的相关帖子，请检查登录状态或搜索词{detail}")
            self.database.save_social_data(job_id, int(job["topic_id"]), platform, posts, comments)
            self.database.set_comment_job_status(
                job_id,
                "success",
                output_path=str(output_path),
                post_count=len(posts),
                comment_count=len(comments),
            )
            self.database.append_comment_job_log(
                job_id, f"导入完成：{len(posts)} 篇帖子，{len(comments)} 条一级评论", "success"
            )
        except Exception as exc:
            self.database.set_comment_job_status(job_id, "failed", output_path=str(output_path), error=str(exc))
            self.database.append_comment_job_log(job_id, f"任务失败：{exc}", "error")
        finally:
            with self._lock:
                if self._active_processes.get(job_id) is locals().get("process"):
                    self._active_processes.pop(job_id, None)
