from __future__ import annotations

import threading
import time
from typing import Any


class ArticleJobService:
    """Owns comment-waiting and article generation independently of a browser page."""

    def __init__(
        self,
        database: Any,
        article_service: Any,
        poll_seconds: float = 1.0,
        video_service: Any | None = None,
    ):
        self.database = database
        self.article_service = article_service
        self.poll_seconds = poll_seconds
        self.video_service = video_service
        self._lock = threading.Lock()
        self._workers: dict[int, threading.Thread] = {}
        for job in self.database.pending_article_jobs():
            self.start(int(job["id"]))

    def start(self, job_id: int) -> None:
        with self._lock:
            worker = self._workers.get(job_id)
            if worker and worker.is_alive():
                return
            worker = threading.Thread(
                target=self._run, args=(job_id,), daemon=True, name=f"article-job-{job_id}"
            )
            self._workers[job_id] = worker
            worker.start()

    def _run(self, job_id: int) -> None:
        try:
            job = self.database.article_job(job_id)
            if not job:
                return
            topic_id = int(job["topic_id"])
            self.database.update_article_job(
                job_id, "waiting_comments", "正在等待热评采集完成", progress=15
            )
            while True:
                jobs = self.database.comment_jobs(topic_id)
                active = [item for item in jobs if item["status"] in {"queued", "running"}]
                if not active:
                    break
                finished = sum(item["status"] in {"success", "failed"} for item in jobs)
                progress = 15 + int(45 * finished / max(1, len(jobs)))
                self.database.update_article_job(
                    job_id, "waiting_comments", "正在等待热评采集完成", progress=progress
                )
                time.sleep(self.poll_seconds)
            self.database.update_article_job(
                job_id, "generating", "热评采集已结束，AI 正在写作", progress=70
            )
            prompt = str(job.get("prompt") or "").strip() or None
            if prompt is None:
                article = self.article_service.generate(topic_id, str(job["article_type"]))
            else:
                article = self.article_service.generate(topic_id, str(job["article_type"]), prompt)
            message = "文章生成完成"
            if bool(job.get("follow_up_video")):
                try:
                    enqueue_video = getattr(self.video_service, "enqueue_article_video", None)
                    if not callable(enqueue_video):
                        raise RuntimeError("视频服务不可用")
                    enqueue_video(int(article["id"]))
                    message = "文章生成完成，视频已排队"
                except Exception as exc:
                    message = f"文章生成完成，但视频提交失败：{exc}"
                    self.database.update_article_job(
                        job_id,
                        "success",
                        message,
                        article_id=int(article["id"]),
                        error=str(exc),
                        progress=100,
                    )
                    return
            self.database.update_article_job(
                job_id, "success", message, article_id=int(article["id"]), progress=100
            )
        except Exception as exc:
            self.database.update_article_job(
                job_id, "failed", "文章生成失败", error=str(exc), progress=100
            )
