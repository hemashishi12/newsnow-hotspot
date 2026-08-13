from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any


def split_article_content(content: str, fallback_title: str = "") -> tuple[str, str]:
    normalized = str(content or "").replace("\r\n", "\n").strip()
    if not normalized:
        raise ValueError("文章内容为空")
    lines = normalized.split("\n")
    first_index = next((index for index, line in enumerate(lines) if line.strip()), 0)
    first_line = lines[first_index].strip()
    title = re.sub(r"^#{1,6}\s*", "", first_line)
    title = re.sub(r"^标题\s*[:：]\s*", "", title).strip()
    body = "\n".join(lines[first_index + 1 :]).strip()
    if not body:
        title = fallback_title.strip() or title
        body = normalized
    if not title:
        title = fallback_title.strip()
    if not title:
        raise ValueError("无法识别文章标题")
    return title, body


class ToutiaoDraftService:
    def __init__(self, root: Path):
        self.root = root
        self.jobs_root = root / "data" / "toutiao-drafts"
        self.profile_root = root / "data" / "toutiao-firefox-profile"
        self.python_path = root / ".venv" / "Scripts" / "python.exe"
        self.helper_path = root / "app" / "toutiao_fill.py"
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._active_job_id = ""

    def start(self, content: str, fallback_title: str = "") -> dict[str, Any]:
        title, body = split_article_content(content, fallback_title)
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("头条号写作窗口已打开，请先完成或关闭上一个窗口。")
            job_id = uuid.uuid4().hex
            job_dir = self.jobs_root / job_id
            job_dir.mkdir(parents=True, exist_ok=False)
            self.profile_root.mkdir(parents=True, exist_ok=True)
            input_path = job_dir / "input.json"
            status_path = job_dir / "status.json"
            log_path = job_dir / "automation.log"
            input_path.write_text(
                json.dumps({"title": title, "body": body}, ensure_ascii=False),
                encoding="utf-8",
            )
            status_path.write_text(
                json.dumps({"status": "starting", "message": "正在启动头条号写作窗口…"}, ensure_ascii=False),
                encoding="utf-8",
            )
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            log_handle = log_path.open("a", encoding="utf-8")
            try:
                self._process = subprocess.Popen(
                    [
                        str(self.python_path),
                        str(self.helper_path),
                        str(input_path),
                        str(status_path),
                        str(self.profile_root),
                    ],
                    cwd=str(self.root),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            finally:
                log_handle.close()
            self._active_job_id = job_id
        return {"job_id": job_id, "status": "starting", "title": title}

    def status(self, job_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            return None
        status_path = self.jobs_root / job_id / "status.json"
        if not status_path.exists():
            return None
        try:
            result = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "starting", "message": "正在读取自动填稿状态…"}
        if job_id == self._active_job_id and self._process is not None:
            result["process_running"] = self._process.poll() is None
        return result
