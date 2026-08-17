from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .tts import TTS_PROVIDERS, synthesize


ENGINE_STATES = {-1: "failed", 1: "success", 4: "processing"}
VOICE_OPTIONS = {
    "zh-CN-XiaoxiaoNeural-Female": "晓晓（女声）",
    "zh-CN-XiaoyiNeural-Female": "晓伊（女声）",
    "zh-CN-YunxiNeural-Male": "云希（男声）",
    "zh-CN-YunjianNeural-Male": "云健（男声）",
}
VIDEO_ASPECTS = {"9:16", "16:9", "1:1"}
VIDEO_SOURCES = {"pexels": "Pexels", "pixabay": "Pixabay", "coverr": "Coverr"}
VIDEO_FORM_DEFAULTS = {
    "aspect": "9:16",
    "voice": "zh-CN-XiaoxiaoNeural-Female",
    "voice_rate": 1.0,
    "source": "pexels",
    "subtitle_enabled": True,
    "tts_provider": "moneyprinter",
    "tts_voice": "alloy",
}
NARRATION_QUOTE_CHARS = str.maketrans(
    "",
    "",
    "\"'“”‘’＂＇„‟‚‛«»‹›「」『』﹁﹂﹃﹄",
)


def strip_narration_quotes(text: str) -> str:
    return str(text or "").translate(NARRATION_QUOTE_CHARS)


def article_to_narration(content: str) -> str:
    text = str(content or "").replace("\r\n", "\n")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:>|[-+*]|\d+\.)\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"(?:\*\*|__|~~|`)(.*?)(?:\*\*|__|~~|`)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    return strip_narration_quotes("\n\n".join(paragraphs))


def validate_local_engine_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip().rstrip("/"))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("视频引擎地址必须是本机 http://127.0.0.1 或 localhost 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("视频引擎地址不能包含凭据、查询参数或片段")
    if parsed.path not in {"", "/"}:
        raise ValueError("视频引擎地址只需填写主机和端口")
    if not parsed.port:
        raise ValueError("视频引擎地址必须包含端口")
    return f"http://{parsed.hostname}:{parsed.port}"


class MoneyPrinterTurboClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = validate_local_engine_url(base_url)
        self.timeout = timeout

    def _data(self, response: httpx.Response) -> Any:
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and int(payload.get("status", 200)) >= 400:
            raise RuntimeError(str(payload.get("message") or "视频引擎请求失败"))
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def health(self) -> bool:
        try:
            with httpx.Client(timeout=3.0) as client:
                response = client.get(f"{self.base_url}/openapi.json")
            if response.status_code != 200:
                return False
            payload = response.json()
            return str(payload.get("info", {}).get("title", "")) == "MoneyPrinterTurbo"
        except (httpx.HTTPError, ValueError):
            return False

    def submit(self, params: dict[str, Any]) -> str:
        with httpx.Client(timeout=self.timeout) as client:
            data = self._data(client.post(f"{self.base_url}/api/v1/videos", json=params))
        task_id = str((data or {}).get("task_id", ""))
        if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", task_id):
            raise RuntimeError("视频引擎没有返回有效任务编号")
        return task_id

    def task(self, task_id: str) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            data = self._data(client.get(f"{self.base_url}/api/v1/tasks/{task_id}"))
        if not isinstance(data, dict):
            raise RuntimeError("视频引擎返回了无效任务状态")
        return data

    def artifact(self, task_id: str) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(f"{self.base_url}/tasks/{task_id}/script.json")
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    def public_result_url(self, result_path: str) -> str:
        if not str(result_path).startswith("/tasks/"):
            raise ValueError("视频引擎返回了不安全的结果路径")
        return urljoin(f"{self.base_url}/", str(result_path).lstrip("/"))


class VideoJobService:
    def __init__(self, root: Path, database: Any, poll_seconds: float = 2.0):
        self.root = root
        self.database = database
        self.poll_seconds = poll_seconds
        self.engine_root = root.parent / "MoneyPrinterTurbo"
        self._lock = threading.Lock()
        self._queue: queue.Queue[int] = queue.Queue()
        self._queued_job_ids: set[int] = set()
        self._active_job_id: int | None = None
        self._process: subprocess.Popen[str] | None = None
        self._worker = threading.Thread(
            target=self._consume, daemon=True, name="video-job-queue"
        )
        self._worker.start()
        for job in self.database.pending_article_video_jobs():
            self.start(int(job["id"]))

    def start(self, job_id: int) -> None:
        job_id = int(job_id)
        with self._lock:
            if self._active_job_id == job_id or job_id in self._queued_job_ids:
                return
            self._queued_job_ids.add(job_id)
            self._queue.put(job_id)

    def _consume(self) -> None:
        while True:
            job_id = self._queue.get()
            with self._lock:
                self._queued_job_ids.discard(job_id)
                self._active_job_id = job_id
            try:
                self._run(job_id)
            finally:
                with self._lock:
                    self._active_job_id = None
                self._queue.task_done()

    def enqueue_article_video(self, article_id: int) -> int:
        article = self.database.generated_article(int(article_id))
        if not article:
            raise RuntimeError("文章不存在，无法创建视频任务")
        topic = self.database.topic(int(article["topic_id"])) or {}
        title = str(topic.get("canonical_title") or "自定视频").strip()[:120]
        script = article_to_narration(str(article.get("content") or ""))
        if len(script) < 20:
            raise RuntimeError("文章正文不足 20 个字，无法生成视频")
        params = self.database.get_video_preferences(VIDEO_FORM_DEFAULTS)
        params["title"] = title
        params["search_terms"] = title
        job_id = self.database.create_article_video_job(int(article_id), script, params)
        self.start(job_id)
        return job_id

    def engine_status(self) -> dict[str, Any]:
        settings = self.database.get_video_engine_settings()
        client = MoneyPrinterTurboClient(settings["engine_url"])
        return {
            "installed": (self.engine_root / ".venv" / "Scripts" / "python.exe").exists(),
            "online": client.health(),
            "engine_url": client.base_url,
        }

    def reconfigure(self) -> None:
        lock = getattr(self, "_lock", None)
        if lock is None:
            if getattr(self, "_active_job_id", None) is not None:
                return
        else:
            with lock:
                if self._active_job_id is not None:
                    return
        settings = self.database.get_video_engine_settings()
        if (self.engine_root / "config.example.toml").exists():
            self._configure_local_engine(settings["engine_url"], settings)
        client = MoneyPrinterTurboClient(settings["engine_url"])
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        if client.health():
            self._terminate_external_engine(client)

    def _terminate_external_engine(self, client: MoneyPrinterTurboClient) -> None:
        if os.name != "nt":
            return
        port = urlparse(client.base_url).port
        if port is None:
            return
        command = (
            "$listener = Get-NetTCPConnection -State Listen -LocalPort "
            f"{port} -ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if ($listener) { $listener.OwningProcess }"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            pid = int(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and client.health():
            time.sleep(0.2)

    def _replace_config_value(self, text: str, key: str, value: str) -> str:
        pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
        if not pattern.search(text):
            raise RuntimeError(f"MoneyPrinterTurbo 配置缺少 {key}")
        return pattern.sub(f"{key} = {value}", text, count=1)

    def _configure_local_engine(
        self,
        engine_url: str,
        settings: dict[str, str],
        subtitle_provider: str | None = None,
    ) -> Path:
        config_path = self.engine_root / "config.toml"
        example_path = self.engine_root / "config.example.toml"
        if not config_path.exists():
            if not example_path.exists():
                raise RuntimeError("视频引擎尚未安装，请先运行 setup-video-engine.ps1")
            shutil.copyfile(example_path, config_path)
        parsed = urlparse(engine_url)
        text = config_path.read_text(encoding="utf-8")
        text = self._replace_config_value(text, "listen_host", json.dumps("127.0.0.1"))
        text = self._replace_config_value(text, "listen_port", str(parsed.port))
        text = self._replace_config_value(
            text,
            "pexels_api_keys",
            f"[{json.dumps(settings['pexels_api_key'], ensure_ascii=False)}]"
            if settings["pexels_api_key"] else "[]",
        )
        text = self._replace_config_value(
            text,
            "pixabay_api_keys",
            f"[{json.dumps(settings['pixabay_api_key'], ensure_ascii=False)}]"
            if settings["pixabay_api_key"] else "[]",
        )
        text = self._replace_config_value(
            text,
            "coverr_api_keys",
            f"[{json.dumps(settings['coverr_api_key'], ensure_ascii=False)}]"
            if settings["coverr_api_key"] else "[]",
        )
        if subtitle_provider:
            subtitle_pattern = re.compile(r"^subtitle_provider\s*=.*$", re.MULTILINE)
            subtitle_value = f"subtitle_provider = {json.dumps(subtitle_provider)}"
            if subtitle_pattern.search(text):
                text = subtitle_pattern.sub(subtitle_value, text, count=1)
            else:
                text = text.rstrip() + f"\n\n{subtitle_value}\n"
        config_path.write_text(text, encoding="utf-8")
        return config_path

    def _ensure_engine(
        self,
        client: MoneyPrinterTurboClient,
        settings: dict[str, str],
        subtitle_provider: str | None = None,
    ) -> None:
        healthy = client.health()
        if healthy and subtitle_provider is None:
            return
        config_path = self.engine_root / "config.toml"
        previous_config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        self._configure_local_engine(client.base_url, settings, subtitle_provider)
        config_changed = previous_config != config_path.read_text(encoding="utf-8")
        if healthy and not config_changed:
            return
        if healthy and config_changed:
            self._terminate_external_engine(client)
        python_path = self.engine_root / ".venv" / "Scripts" / "python.exe"
        if not python_path.exists():
            raise RuntimeError("视频引擎运行环境不存在，请运行 setup-video-engine.ps1")
        log_dir = self.root / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_handle = (log_dir / "video-engine.log").open("a", encoding="utf-8")
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            self._process = subprocess.Popen(
                [str(python_path), "main.py"],
                cwd=str(self.engine_root),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        finally:
            log_handle.close()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("视频引擎启动失败，请检查 data/logs/video-engine.log")
            if client.health():
                return
            time.sleep(1)
        raise RuntimeError("视频引擎启动超时，请检查 data/logs/video-engine.log")

    def _engine_params(
        self, job: dict[str, Any], custom_audio_file: str | None = None
    ) -> dict[str, Any]:
        params = json.loads(str(job["params_json"]))
        terms = [item.strip() for item in str(params.get("search_terms", "")).split(",") if item.strip()]
        engine_params = {
            "video_subject": str(params["title"]),
            "video_script": str(job["script"]),
            "video_terms": terms or None,
            "video_aspect": str(params["aspect"]),
            "video_concat_mode": "sequential",
            "video_transition_mode": "Shuffle",
            "video_clip_duration": 5,
            "match_materials_to_script": True,
            "video_count": 1,
            "video_source": str(params["source"]),
            "voice_name": str(params["voice"]),
            "voice_rate": float(params.get("voice_rate", 1.0)),
            "bgm_type": "none",
            "bgm_volume": 0,
            "subtitle_enabled": bool(params.get("subtitle_enabled", True)),
            "subtitle_position": "bottom",
            "font_size": 60,
        }
        if custom_audio_file:
            engine_params["custom_audio_file"] = custom_audio_file
        return engine_params

    def _external_audio_file(
        self,
        job: dict[str, Any],
        params: dict[str, Any],
        settings: dict[str, str],
    ) -> str:
        provider = str(params.get("tts_provider") or "")
        if provider not in TTS_PROVIDERS or provider == "moneyprinter":
            return ""
        extension = ".mp3" if provider == "openai" else ".wav"
        output_path = (
            self.engine_root / "storage" / "external-tts" / f"newsnow-job-{int(job['id'])}{extension}"
        )
        synthesize(provider, str(job["script"]), output_path, params, settings)
        return output_path.relative_to(self.engine_root).as_posix()

    def _run(self, job_id: int) -> None:
        try:
            job = self.database.article_video_job(job_id)
            if not job:
                return
            settings = self.database.get_video_engine_settings()
            params = json.loads(str(job["params_json"]))
            source = str(params.get("source", "pexels"))
            if source not in VIDEO_SOURCES:
                raise RuntimeError("不支持的视频素材来源")
            if not settings[f"{source}_api_key"]:
                raise RuntimeError(f"尚未配置 {VIDEO_SOURCES[source]} API Key，请先到分析设置保存")
            client = MoneyPrinterTurboClient(settings["engine_url"])
            tts_provider = str(params.get("tts_provider") or "moneyprinter")
            if tts_provider not in TTS_PROVIDERS:
                raise RuntimeError("不支持的配音引擎")
            engine_task_id = str(job.get("engine_task_id") or "")
            if not engine_task_id:
                custom_audio_file = ""
                if tts_provider != "moneyprinter":
                    self.database.update_article_video_job(
                        job_id, status="starting", message="正在生成外部配音"
                    )
                    custom_audio_file = self._external_audio_file(job, params, self.database.get_video_tts_settings())
                subtitle_provider = None
                if bool(params.get("subtitle_enabled", True)):
                    subtitle_provider = "whisper" if custom_audio_file else "edge"
                self.database.update_article_video_job(
                    job_id, status="starting", message="正在启动本地视频引擎"
                )
                self._ensure_engine(client, settings, subtitle_provider=subtitle_provider)
                engine_task_id = client.submit(self._engine_params(job, custom_audio_file))
                self.database.update_article_video_job(
                    job_id,
                    engine_task_id=engine_task_id,
                    status="processing",
                    progress=1,
                    message="视频任务已提交",
                )
            while True:
                task = client.task(engine_task_id)
                state = int(task.get("state", 4))
                progress = max(0, min(100, int(float(task.get("progress", 0) or 0))))
                if state == -1:
                    raise RuntimeError(str(task.get("error") or "视频引擎生成失败"))
                if state == 1:
                    videos = task.get("videos") or []
                    if not videos:
                        raise RuntimeError("视频引擎已完成，但没有返回成片")
                    artifact = client.artifact(engine_task_id)
                    result = {
                        "material_sources": artifact.get("material_sources", []),
                        "terms": task.get("terms") or artifact.get("video_terms") or [],
                        "duration": task.get("audio_duration"),
                        "warnings": task.get("warnings") or [],
                    }
                    self.database.update_article_video_job(
                        job_id,
                        status="success",
                        progress=100,
                        message="口播视频已生成",
                        result_path=str(videos[0]),
                        result_json=json.dumps(result, ensure_ascii=False),
                    )
                    return
                self.database.update_article_video_job(
                    job_id,
                    status=ENGINE_STATES.get(state, "processing"),
                    progress=progress,
                    message="正在生成口播、字幕与素材画面",
                )
                time.sleep(self.poll_seconds)
        except Exception as exc:
            self.database.update_article_video_job(
                job_id, status="failed", message="口播视频生成失败", error=str(exc)
            )

    def result_url(self, job: dict[str, Any]) -> str:
        settings = self.database.get_video_engine_settings()
        return MoneyPrinterTurboClient(settings["engine_url"]).public_result_url(
            str(job["result_path"])
        )

    def result_file(self, job: dict[str, Any]) -> Path | None:
        task_id = str(job.get("engine_task_id") or "")
        result_path = str(job.get("result_path") or "")
        prefix = f"/tasks/{task_id}/"
        if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", task_id) or not result_path.startswith(prefix):
            return None
        relative = Path(result_path.removeprefix("/tasks/"))
        target = (self.engine_root / "storage" / "tasks" / relative).resolve()
        allowed_root = (self.engine_root / "storage" / "tasks").resolve()
        try:
            target.relative_to(allowed_root)
        except ValueError:
            return None
        return target if target.is_file() else None
