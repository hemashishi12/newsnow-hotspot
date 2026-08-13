from __future__ import annotations

import threading
import time
from typing import Any


class ArticleJobService:
    """Owns comment-waiting and article generation independently of a browser page."""

    def __init__(self, database: Any, article_service: Any, poll_seconds: float = 1.0):
        self.database = database
        self.article_service = article_service
        self.poll_seconds = poll_seconds
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
            self.database.update_article_job(job_id, "waiting_comments", "正在等待热评采集完成")
            while True:
                jobs = self.database.comment_jobs(topic_id)
                if not any(item["status"] in {"queued", "running"} for item in jobs):
                    break
                time.sleep(self.poll_seconds)
            self.database.update_article_job(job_id, "generating", "热评采集已结束，AI 正在写作")
            prompt = str(job.get("prompt") or "").strip() or None
            if prompt is None:
                article = self.article_service.generate(topic_id, str(job["article_type"]))
            else:
                article = self.article_service.generate(topic_id, str(job["article_type"]), prompt)
            self.database.update_article_job(
                job_id, "success", "文章生成完成", article_id=int(article["id"])
            )
        except Exception as exc:
            self.database.update_article_job(job_id, "failed", "文章生成失败", error=str(exc))
