from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    source_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    ai_status TEXT NOT NULL DEFAULT 'pending',
    error_summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    item_key TEXT NOT NULL,
    rank INTEGER NOT NULL,
    list_size INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    mobile_url TEXT NOT NULL DEFAULT '',
    hot_value TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
    UNIQUE(run_id, source_id, item_key)
);

CREATE INDEX IF NOT EXISTS idx_news_run_source ON news_items(run_id, source_id);

CREATE TABLE IF NOT EXISTS source_results (
    run_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_time TEXT NOT NULL DEFAULT '',
    item_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    response_json TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(run_id, source_id),
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    display_title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    current_score REAL NOT NULL,
    platform_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
    UNIQUE(topic_id, run_id)
);

CREATE TABLE IF NOT EXISTS topic_members (
    observation_id INTEGER NOT NULL,
    news_item_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    platform_weight REAL NOT NULL,
    rank INTEGER NOT NULL,
    list_size INTEGER NOT NULL,
    rank_score REAL NOT NULL,
    contribution REAL NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(observation_id, news_item_id),
    FOREIGN KEY(observation_id) REFERENCES topic_observations(id) ON DELETE CASCADE,
    FOREIGN KEY(news_item_id) REFERENCES news_items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_observation_run ON topic_observations(run_id);
CREATE INDEX IF NOT EXISTS idx_observation_topic ON topic_observations(topic_id);

CREATE TABLE IF NOT EXISTS custom_topic_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE,
    UNIQUE(topic_id, url)
);

CREATE INDEX IF NOT EXISTS idx_custom_topic_news_topic ON custom_topic_news(topic_id, id);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_prompt_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_type TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_article_prompt_history_type
ON article_prompt_history(article_type, id DESC);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_run_logs_run ON run_logs(run_id, id);

CREATE TABLE IF NOT EXISTS ai_exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    batch_index INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'sending',
    request_json TEXT NOT NULL,
    response_text TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ai_exchanges_run ON ai_exchanges(run_id, id);

CREATE TABLE IF NOT EXISTS comment_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    keyword TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    output_path TEXT NOT NULL DEFAULT '',
    post_count INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_comment_jobs_topic ON comment_jobs(topic_id, id);
CREATE INDEX IF NOT EXISTS idx_comment_jobs_status ON comment_jobs(status, id);

CREATE TABLE IF NOT EXISTS comment_job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES comment_jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_comment_job_logs_job ON comment_job_logs(job_id, id);

CREATE TABLE IF NOT EXISTS social_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    platform_post_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    like_count INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(job_id) REFERENCES comment_jobs(id) ON DELETE CASCADE,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE,
    UNIQUE(job_id, platform, platform_post_id)
);

CREATE INDEX IF NOT EXISTS idx_social_posts_topic ON social_posts(topic_id, platform);

CREATE TABLE IF NOT EXISTS social_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    platform_comment_id TEXT NOT NULL,
    platform_post_id TEXT NOT NULL,
    parent_comment_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    like_count INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(job_id) REFERENCES comment_jobs(id) ON DELETE CASCADE,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE,
    UNIQUE(job_id, platform, platform_comment_id)
);

CREATE INDEX IF NOT EXISTS idx_social_comments_topic ON social_comments(topic_id, platform, like_count DESC);

CREATE TABLE IF NOT EXISTS generated_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    prompt TEXT NOT NULL,
    model TEXT NOT NULL,
    content TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    article_type TEXT NOT NULL DEFAULT 'standard',
    updated_at TEXT,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_generated_articles_topic ON generated_articles(topic_id, id DESC);

CREATE TABLE IF NOT EXISTS article_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(article_id) REFERENCES generated_articles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_article_revisions_article
ON article_revisions(article_id, id DESC);

CREATE TABLE IF NOT EXISTS article_prompt_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    article_type TEXT NOT NULL DEFAULT 'standard',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name, article_type)
);

CREATE TABLE IF NOT EXISTS article_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    article_type TEXT NOT NULL DEFAULT 'standard',
    prompt TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    message TEXT NOT NULL DEFAULT '',
    article_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    progress INTEGER NOT NULL DEFAULT 0,
    follow_up_video INTEGER NOT NULL DEFAULT 0,
    read_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE,
    FOREIGN KEY(article_id) REFERENCES generated_articles(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_article_jobs_topic ON article_jobs(topic_id, article_type, id DESC);

CREATE TABLE IF NOT EXISTS article_video_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    engine_task_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    script TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    result_path TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    read_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(article_id) REFERENCES generated_articles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_article_video_jobs_article
ON article_video_jobs(article_id, id DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._connection_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            article_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(generated_articles)")
            }
            if "article_type" not in article_columns:
                connection.execute(
                    "ALTER TABLE generated_articles ADD COLUMN article_type TEXT NOT NULL DEFAULT 'standard'"
                )
            if "updated_at" not in article_columns:
                connection.execute("ALTER TABLE generated_articles ADD COLUMN updated_at TEXT")
                connection.execute(
                    "UPDATE generated_articles SET updated_at=created_at WHERE updated_at IS NULL"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_generated_articles_topic_type "
                "ON generated_articles(topic_id, article_type, id DESC)"
            )
            video_columns = list(connection.execute("PRAGMA table_info(article_video_jobs)"))
            article_id_column = next(
                (row for row in video_columns if str(row["name"]) == "article_id"), None
            )
            if article_id_column is not None and int(article_id_column["notnull"] or 0):
                connection.execute("ALTER TABLE article_video_jobs RENAME TO article_video_jobs_legacy")
                connection.execute("DROP INDEX IF EXISTS idx_article_video_jobs_article")
                connection.execute(
                    """CREATE TABLE article_video_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        article_id INTEGER,
                        engine_task_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'queued',
                        progress INTEGER NOT NULL DEFAULT 0,
                        message TEXT NOT NULL DEFAULT '',
                        script TEXT NOT NULL,
                        params_json TEXT NOT NULL DEFAULT '{}',
                        result_path TEXT NOT NULL DEFAULT '',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        error TEXT NOT NULL DEFAULT '',
                        read_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(article_id) REFERENCES generated_articles(id) ON DELETE CASCADE
                    )"""
                )
                connection.execute(
                    """INSERT INTO article_video_jobs
                       (id,article_id,engine_task_id,status,progress,message,script,params_json,
                        result_path,result_json,error,created_at,updated_at)
                       SELECT id,article_id,engine_task_id,status,progress,message,script,params_json,
                        result_path,result_json,error,created_at,updated_at
                       FROM article_video_jobs_legacy"""
                )
                connection.execute("DROP TABLE article_video_jobs_legacy")
            video_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(article_video_jobs)")
            }
            if "read_at" not in video_columns:
                connection.execute("ALTER TABLE article_video_jobs ADD COLUMN read_at TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_article_video_jobs_article "
                "ON article_video_jobs(article_id, id DESC)"
            )
            job_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(article_jobs)")
            }
            if "progress" not in job_columns:
                connection.execute(
                    "ALTER TABLE article_jobs ADD COLUMN progress INTEGER NOT NULL DEFAULT 0"
                )
            if "read_at" not in job_columns:
                connection.execute("ALTER TABLE article_jobs ADD COLUMN read_at TEXT")
            if "follow_up_video" not in job_columns:
                connection.execute(
                    "ALTER TABLE article_jobs ADD COLUMN follow_up_video INTEGER NOT NULL DEFAULT 0"
                )
            for article_type, setting_key in (
                ("standard", "article_prompt"),
                ("long", "long_article_prompt"),
            ):
                history_exists = connection.execute(
                    "SELECT 1 FROM article_prompt_history WHERE article_type=? LIMIT 1",
                    (article_type,),
                ).fetchone()
                if history_exists:
                    continue
                setting = connection.execute(
                    "SELECT value_json,updated_at FROM app_settings WHERE key=?",
                    (setting_key,),
                ).fetchone()
                if setting is None:
                    continue
                try:
                    saved_prompt = str(json.loads(setting["value_json"])).strip()
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if saved_prompt:
                    connection.execute(
                        "INSERT INTO article_prompt_history(article_type,prompt,created_at) VALUES (?,?,?)",
                        (article_type, saved_prompt, setting["updated_at"]),
                    )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._connection_lock:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def begin_run(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO runs(started_at) VALUES (?)", (datetime.now().astimezone().isoformat(timespec="seconds"),)
            )
            return int(cursor.lastrowid)

    def append_log(
        self,
        run_id: int,
        stage: str,
        message: str,
        level: str = "info",
        details: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO run_logs(run_id, created_at, level, stage, message, details_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    level,
                    stage,
                    message,
                    json.dumps(details or {}, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def run_logs(self, run_id: int, limit: int = 1000) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM run_logs WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json"))
            except (TypeError, ValueError, json.JSONDecodeError):
                item["details"] = {}
                item.pop("details_json", None)
            result.append(item)
        return result

    def begin_ai_exchange(self, run_id: int, batch_index: int, request_payload: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO ai_exchanges(run_id, batch_index, created_at, request_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    run_id,
                    batch_index,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    json.dumps(request_payload, ensure_ascii=False, indent=2),
                ),
            )
            return int(cursor.lastrowid)

    def finish_ai_exchange(
        self,
        exchange_id: int,
        status: str,
        response_text: str = "",
        http_status: int | None = None,
        error: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE ai_exchanges SET completed_at=?, status=?, response_text=?, http_status=?, error=?
                   WHERE id=?""",
                (
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    status,
                    response_text,
                    http_status,
                    error[:4000],
                    exchange_id,
                ),
            )

    def ai_exchanges(self, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_exchanges WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def ai_exchange(self, exchange_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM ai_exchanges WHERE id=?", (exchange_id,)).fetchone()
        return dict(row) if row else None

    def all_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def newest_run_id(self) -> int | None:
        with self.connect() as connection:
            row = connection.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def topic(self, topic_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        return dict(row) if row else None

    def topic_article_context(self, topic_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            topic = connection.execute(
                "SELECT * FROM topics WHERE id=?", (topic_id,)
            ).fetchone()
            if topic is None:
                return None
            observation = connection.execute(
                """SELECT * FROM topic_observations WHERE topic_id=?
                   ORDER BY run_id DESC LIMIT 1""",
                (topic_id,),
            ).fetchone()
            members = []
            if observation:
                members = connection.execute(
                    """SELECT * FROM topic_members WHERE observation_id=?
                       ORDER BY contribution DESC""",
                    (observation["id"],),
                ).fetchall()
            if not members:
                custom_rows = connection.execute(
                    "SELECT * FROM custom_topic_news WHERE topic_id=? ORDER BY id", (topic_id,)
                ).fetchall()
                members = [
                    {
                        "source_name": row["source_name"] or row["provider"],
                        "rank": index,
                        "title": row["title"],
                        "url": row["url"],
                    }
                    for index, row in enumerate(custom_rows, 1)
                ]
        social = self.topic_social_data(topic_id)
        result = dict(topic)
        result["observation_summary"] = str(observation["summary"]) if observation else ""
        result["members"] = [dict(row) for row in members]
        result.update(social)
        return result

    def create_custom_topic(
        self,
        title: str,
        summary: str,
        news_items: list[dict[str, Any]],
        *,
        merge_existing: bool = True,
    ) -> int:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        normalized_title = "".join(str(title).casefold().split())
        with self.connect() as connection:
            topic_id = None
            if merge_existing:
                existing = connection.execute(
                    "SELECT id,canonical_title FROM topics WHERE canonical_title IS NOT NULL"
                ).fetchall()
                topic_id = next(
                    (
                        int(row["id"])
                        for row in existing
                        if "".join(str(row["canonical_title"]).casefold().split())
                        == normalized_title
                    ),
                    None,
                )
            if topic_id is None:
                cursor = connection.execute(
                    "INSERT INTO topics(canonical_title,summary,first_seen_at,last_seen_at) VALUES (?,?,?,?)",
                    (title.strip(), summary.strip(), now, now),
                )
                topic_id = int(cursor.lastrowid)
            else:
                connection.execute(
                    "UPDATE topics SET last_seen_at=?,summary=CASE WHEN ?<>'' THEN ? ELSE summary END WHERE id=?",
                    (now, summary.strip(), summary.strip(), topic_id),
                )
            for item in news_items:
                connection.execute(
                    """INSERT OR IGNORE INTO custom_topic_news
                       (topic_id,source_name,provider,title,url,published_at,summary,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        topic_id, str(item.get("source", "")), str(item.get("provider", "")),
                        str(item.get("title", "")), str(item.get("url", "")),
                        str(item.get("published_at", "")), str(item.get("summary", "")), now,
                    ),
                )
        return topic_id

    def custom_topics(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            topics = connection.execute(
                """SELECT t.*,COUNT(n.id) AS news_count FROM topics t
                   JOIN custom_topic_news n ON n.topic_id=t.id
                   GROUP BY t.id ORDER BY t.id DESC"""
            ).fetchall()
            result = []
            for topic in topics:
                item = dict(topic)
                item["news"] = [dict(row) for row in connection.execute(
                    "SELECT * FROM custom_topic_news WHERE topic_id=? ORDER BY id", (topic["id"],)
                ).fetchall()]
                result.append(item)
        for item in result:
            item["comment_summary"] = self.topic_comment_summary(int(item["id"]))
        return result

    def create_comment_jobs(self, topic_id: int, keyword: str, platforms: list[str]) -> list[int]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        job_ids: list[int] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for platform in platforms:
                active = connection.execute(
                    """SELECT id FROM comment_jobs
                       WHERE topic_id=? AND platform=? AND status IN ('queued','running')
                       LIMIT 1""",
                    (topic_id, platform),
                ).fetchone()
                if active:
                    continue
                cursor = connection.execute(
                    """INSERT INTO comment_jobs(topic_id, platform, keyword, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (topic_id, platform, keyword, now),
                )
                job_ids.append(int(cursor.lastrowid))
        return job_ids

    def recover_comment_jobs(self) -> list[int]:
        """Fail jobs interrupted by a restart and return jobs that never started."""
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            interrupted = connection.execute(
                "SELECT id FROM comment_jobs WHERE status='running' ORDER BY id"
            ).fetchall()
            for row in interrupted:
                job_id = int(row["id"])
                connection.execute(
                    """UPDATE comment_jobs SET status='failed',completed_at=?,
                       error='程序重启，采集任务已中断，请手动重新发起' WHERE id=?""",
                    (now, job_id),
                )
                connection.execute(
                    """INSERT INTO comment_job_logs(job_id,created_at,level,message)
                       VALUES (?,?,'error','程序重启，原采集任务已中断')""",
                    (job_id, now),
                )
            queued = connection.execute(
                "SELECT id FROM comment_jobs WHERE status='queued' ORDER BY id"
            ).fetchall()
        return [int(row["id"]) for row in queued]

    def comment_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM comment_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def set_comment_job_status(
        self,
        job_id: int,
        status: str,
        *,
        output_path: str | None = None,
        post_count: int | None = None,
        comment_count: int | None = None,
        error: str = "",
    ) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            if status == "running":
                connection.execute(
                    "UPDATE comment_jobs SET status=?, started_at=?, error='' WHERE id=?",
                    (status, now, job_id),
                )
            elif status in {"success", "failed"}:
                connection.execute(
                    """UPDATE comment_jobs SET status=?, completed_at=?, output_path=COALESCE(?,output_path),
                       post_count=COALESCE(?,post_count), comment_count=COALESCE(?,comment_count), error=? WHERE id=?""",
                    (status, now, output_path, post_count, comment_count, error[:4000], job_id),
                )
            else:
                connection.execute("UPDATE comment_jobs SET status=? WHERE id=?", (status, job_id))

    def append_comment_job_log(self, job_id: int, message: str, level: str = "info") -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO comment_job_logs(job_id,created_at,level,message) VALUES (?,?,?,?)""",
                (job_id, datetime.now().astimezone().isoformat(timespec="seconds"), level, message),
            )
            return int(cursor.lastrowid)

    def comment_jobs(self, topic_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM comment_jobs WHERE topic_id=? ORDER BY id DESC", (topic_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def comment_job_logs(self, topic_id: int, limit: int = 300) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT l.*,j.platform,j.status AS job_status FROM comment_job_logs l
                   JOIN comment_jobs j ON j.id=l.job_id WHERE j.topic_id=?
                   ORDER BY l.id DESC LIMIT ?""",
                (topic_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_social_data(
        self,
        job_id: int,
        topic_id: int,
        platform: str,
        posts: list[dict[str, Any]],
        comments: list[dict[str, Any]],
    ) -> None:
        with self.connect() as connection:
            for post in posts:
                connection.execute(
                    """INSERT OR REPLACE INTO social_posts
                       (job_id,topic_id,platform,platform_post_id,title,url,author,published_at,
                        like_count,comment_count,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id, topic_id, platform, post["post_id"], post.get("title", ""),
                        post.get("url", ""), post.get("author", ""), post.get("published_at", ""),
                        int(post.get("like_count", 0)), int(post.get("comment_count", 0)),
                        json.dumps(post.get("raw", {}), ensure_ascii=False),
                    ),
                )
            for comment in comments:
                connection.execute(
                    """INSERT OR REPLACE INTO social_comments
                       (job_id,topic_id,platform,platform_comment_id,platform_post_id,parent_comment_id,
                        content,author,published_at,like_count,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id, topic_id, platform, comment["comment_id"], comment["post_id"],
                        comment.get("parent_comment_id", ""), comment.get("content", ""),
                        comment.get("author", ""), comment.get("published_at", ""),
                        int(comment.get("like_count", 0)),
                        json.dumps(comment.get("raw", {}), ensure_ascii=False),
                    ),
                )

    def topic_social_data(self, topic_id: int) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as connection:
            posts = connection.execute(
                """SELECT * FROM social_posts WHERE topic_id=?
                   ORDER BY job_id DESC, like_count DESC""",
                (topic_id,),
            ).fetchall()
            comments = connection.execute(
                """SELECT c.*,p.title AS post_title,p.url AS post_url FROM social_comments c
                   LEFT JOIN social_posts p ON p.job_id=c.job_id AND p.platform=c.platform
                    AND p.platform_post_id=c.platform_post_id
                   WHERE c.topic_id=? ORDER BY c.like_count DESC,c.id DESC""",
                (topic_id,),
            ).fetchall()
        return {"posts": [dict(row) for row in posts], "comments": [dict(row) for row in comments]}

    def topic_comment_summary(self, topic_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM social_posts WHERE topic_id=?) AS post_count,
                     (SELECT COUNT(*) FROM social_comments WHERE topic_id=?) AS comment_count""",
                (topic_id, topic_id),
            ).fetchone()
            job = connection.execute(
                "SELECT status FROM comment_jobs WHERE topic_id=? ORDER BY id DESC LIMIT 1", (topic_id,)
            ).fetchone()
            active_job = connection.execute(
                """SELECT status FROM comment_jobs WHERE topic_id=? AND status IN ('running','queued')
                   ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id LIMIT 1""",
                (topic_id,),
            ).fetchone()
        return {
            "post_count": int(row["post_count"] or 0) if row else 0,
            "comment_count": int(row["comment_count"] or 0) if row else 0,
            "status": str(active_job["status"]) if active_job else (str(job["status"]) if job else "idle"),
        }

    def save_source(self, run_id: int, source: Any, response: dict[str, Any]) -> int:
        items = response.get("items") or []
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO source_results
                   (run_id, source_id, source_name, status, updated_time, item_count, response_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    source.id,
                    source.name,
                    str(response.get("status", "success")),
                    str(response.get("updatedTime", "")),
                    len(items),
                    json.dumps(response, ensure_ascii=False),
                ),
            )
            for index, raw_item in enumerate(items, start=1):
                item_key = str(raw_item.get("id") or raw_item.get("url") or f"{index}:{raw_item.get('title', '')}")
                extra = raw_item.get("extra") if isinstance(raw_item.get("extra"), dict) else {}
                rank = int(raw_item.get("rank") or index)
                connection.execute(
                    """INSERT OR REPLACE INTO news_items
                       (run_id, source_id, source_name, item_key, rank, list_size, title, url,
                        mobile_url, hot_value, extra_json, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        source.id,
                        source.name,
                        item_key,
                        rank,
                        len(items),
                        str(raw_item.get("title") or "").strip(),
                        str(raw_item.get("url") or ""),
                        str(raw_item.get("mobileUrl") or raw_item.get("mobile_url") or ""),
                        str(extra.get("info") or raw_item.get("hot") or ""),
                        json.dumps(extra, ensure_ascii=False),
                        json.dumps(raw_item, ensure_ascii=False),
                    ),
                )
        return len(items)

    def save_source_error(self, run_id: int, source: Any, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO source_results
                   (run_id, source_id, source_name, status, error)
                   VALUES (?, ?, ?, 'failed', ?)""",
                (run_id, source.id, source.name, error[:1000]),
            )

    def finish_run(self, run_id: int, source_count: int, item_count: int, errors: list[str]) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE runs SET completed_at=?, status=?, source_count=?, item_count=?, error_summary=?
                   WHERE id=?""",
                (
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    "partial" if errors else "success",
                    source_count,
                    item_count,
                    "\n".join(errors),
                    run_id,
                ),
            )

    def set_ai_status(self, run_id: int, status: str, error: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET ai_status=?, error_summary=CASE WHEN ?='' THEN error_summary ELSE error_summary || ? END WHERE id=?",
                (status, error, f"\nAI: {error}", run_id),
            )

    def latest_run_id(self) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM runs WHERE status IN ('success','partial') ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return int(row["id"]) if row else None

    def latest_completed_run_at(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT completed_at FROM runs
                   WHERE status IN ('success','partial') AND completed_at IS NOT NULL
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            return str(row["completed_at"]) if row else None

    def set_analysis_source_ids(self, source_ids: set[str]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        value = json.dumps(sorted(source_ids), ensure_ascii=False)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO app_settings(key, value_json, updated_at)
                   VALUES ('analyzed_source_ids', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (value, now),
            )

    def get_analysis_source_ids(self, defaults: set[str], valid_ids: set[str]) -> set[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key='analyzed_source_ids'"
            ).fetchone()
        if not row:
            return defaults & valid_ids
        try:
            selected = {str(value) for value in json.loads(row["value_json"])}
        except (TypeError, ValueError, json.JSONDecodeError):
            return defaults & valid_ids
        return selected & valid_ids

    def set_comment_platform_ids(self, platform_ids: set[str]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        value = json.dumps(sorted(platform_ids), ensure_ascii=False)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO app_settings(key, value_json, updated_at)
                   VALUES ('comment_platform_ids', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (value, now),
            )

    def get_comment_platform_ids(self, defaults: set[str], valid_ids: set[str]) -> set[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key='comment_platform_ids'"
            ).fetchone()
        if not row:
            return defaults & valid_ids
        try:
            selected = {str(value) for value in json.loads(row["value_json"])}
        except (TypeError, ValueError, json.JSONDecodeError):
            return defaults & valid_ids
        return selected & valid_ids

    def set_section_weights(self, weights: dict[str, float]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        value = json.dumps(weights, ensure_ascii=False)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO app_settings(key, value_json, updated_at)
                   VALUES ('section_weights', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (value, now),
            )

    def get_section_weights(self, defaults: dict[str, float]) -> dict[str, float]:
        keys = ("current", "rising", "sustained")
        fallback = {key: max(0.0, float(defaults.get(key, 0.0))) for key in keys}
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key='section_weights'"
            ).fetchone()
        values = fallback
        if row:
            try:
                saved = json.loads(row["value_json"])
                values = {key: max(0.0, float(saved.get(key, fallback[key]))) for key in keys}
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                values = fallback
        total = sum(values.values())
        if total <= 0:
            values = {"current": 0.5, "rising": 0.3, "sustained": 0.2}
            total = 1.0
        return {key: value / total for key, value in values.items()}

    def set_collection_interval_minutes(self, interval_minutes: int) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO app_settings(key, value_json, updated_at)
                   VALUES ('collection_interval_minutes', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (json.dumps(int(interval_minutes)), now),
            )

    def get_collection_interval_minutes(self, default: int) -> int:
        fallback = max(1, min(1440, int(default)))
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key='collection_interval_minutes'"
            ).fetchone()
        if not row:
            return fallback
        try:
            value = int(json.loads(row["value_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback
        return value if 1 <= value <= 1440 else fallback

    def set_article_web_search_enabled(self, enabled: bool) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO app_settings(key, value_json, updated_at)
                   VALUES ('article_web_search_enabled', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (json.dumps(bool(enabled)), now),
            )

    def get_article_web_search_enabled(self, default: bool = True) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key='article_web_search_enabled'"
            ).fetchone()
        if not row:
            return bool(default)
        try:
            value = json.loads(row["value_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return bool(default)
        if isinstance(value, bool):
            return value
        return bool(default)

    def get_ai_connection(self, default_api_key: str, default_base_url: str) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT key, value_json FROM app_settings WHERE key IN ('ai_api_key','ai_base_url')"
            ).fetchall()
        values: dict[str, str] = {}
        for row in rows:
            try:
                values[str(row["key"])] = str(json.loads(row["value_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return {
            "api_key": values.get("ai_api_key", default_api_key).strip(),
            "base_url": values.get("ai_base_url", default_base_url).strip().rstrip("/"),
        }

    def set_ai_connection(self, api_key: str | None, base_url: str) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO app_settings(key, value_json, updated_at) VALUES ('ai_base_url', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (json.dumps(base_url.rstrip("/"), ensure_ascii=False), now),
            )
            if api_key is not None:
                connection.execute(
                    """INSERT INTO app_settings(key, value_json, updated_at) VALUES ('ai_api_key', ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                    (json.dumps(api_key.strip(), ensure_ascii=False), now),
                )

    def get_article_prompt(self, default: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key='article_prompt'"
            ).fetchone()
        if not row:
            return default
        try:
            value = str(json.loads(row["value_json"])).strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        return value or default

    def set_article_prompt(self, prompt: str) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        prompt = prompt.strip()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO article_prompt_history(article_type,prompt,created_at) VALUES ('standard',?,?)",
                (prompt, now),
            )
            connection.execute(
                """INSERT INTO app_settings(key, value_json, updated_at) VALUES ('article_prompt', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (json.dumps(prompt, ensure_ascii=False), now),
            )

    def article_prompt_presets(self, article_type: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM article_prompt_presets WHERE article_type=? ORDER BY updated_at DESC, id DESC",
                (article_type,),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_article_prompt_presets(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM article_prompt_presets ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_article_prompt_preset(self, name: str, prompt: str, article_type: str) -> dict[str, Any]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO article_prompt_presets(name,prompt,article_type,created_at,updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(name,article_type) DO UPDATE SET prompt=excluded.prompt,updated_at=excluded.updated_at""",
                (name.strip(), prompt.strip(), article_type, now, now),
            )
            row = connection.execute(
                "SELECT * FROM article_prompt_presets WHERE name=? AND article_type=?",
                (name.strip(), article_type),
            ).fetchone()
        return dict(row)

    def delete_article_prompt_preset(self, preset_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM article_prompt_presets WHERE id=?", (preset_id,)
            )
        return cursor.rowcount > 0

    def create_article_job(
        self,
        topic_id: int,
        article_type: str,
        prompt: str = "",
        follow_up_video: bool = False,
    ) -> int:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO article_jobs(
                       topic_id,article_type,prompt,status,message,progress,follow_up_video,created_at,updated_at
                   ) VALUES (?,?,?,'queued','等待热评采集完成',5,?,?,?)""",
                (topic_id, article_type, prompt.strip(), int(follow_up_video), now, now),
            )
        return int(cursor.lastrowid)

    def article_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM article_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def latest_article_job(self, topic_id: int, article_type: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM article_jobs WHERE topic_id=? AND article_type=? ORDER BY id DESC LIMIT 1",
                (topic_id, article_type),
            ).fetchone()
        return dict(row) if row else None

    def pending_article_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM article_jobs WHERE status IN ('queued','waiting_comments','generating') ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def article_job_notifications(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT j.*,t.canonical_title AS topic_title FROM article_jobs j
                   JOIN topics t ON t.id=j.topic_id
                   WHERE j.status IN ('queued','waiting_comments','generating')
                      OR (j.status IN ('success','failed') AND j.read_at IS NULL)
                   ORDER BY j.id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_article_jobs_read(self, job_ids: list[int]) -> None:
        if not job_ids:
            return
        marks = ",".join("?" for _ in job_ids)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                f"UPDATE article_jobs SET read_at=? WHERE id IN ({marks}) AND status IN ('success','failed')",
                (now, *job_ids),
            )

    def update_article_job(
        self, job_id: int, status: str, message: str, *, article_id: int | None = None,
        error: str = "", progress: int | None = None
    ) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """UPDATE article_jobs SET status=?,message=?,article_id=COALESCE(?,article_id),error=?,
                   progress=COALESCE(?,progress),updated_at=?
                   WHERE id=?""",
                (status, message, article_id, error, progress, now, job_id),
            )

    def get_long_article_prompt(self, default: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key='long_article_prompt'"
            ).fetchone()
        if not row:
            return default
        try:
            value = str(json.loads(row["value_json"])).strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        return value or default

    def set_long_article_prompt(self, prompt: str) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        prompt = prompt.strip()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO article_prompt_history(article_type,prompt,created_at) VALUES ('long',?,?)",
                (prompt, now),
            )
            connection.execute(
                """INSERT INTO app_settings(key, value_json, updated_at) VALUES ('long_article_prompt', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (json.dumps(prompt, ensure_ascii=False), now),
            )

    def save_generated_article(
        self,
        topic_id: int,
        prompt: str,
        model: str,
        content: str,
        input_payload: dict[str, Any],
        article_type: str = "standard",
    ) -> int:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO generated_articles
                   (topic_id,created_at,prompt,model,content,input_json,article_type,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    topic_id,
                    now,
                    prompt,
                    model,
                    content,
                    json.dumps(input_payload, ensure_ascii=False),
                    article_type,
                    now,
                ),
            )
        return int(cursor.lastrowid)

    def latest_generated_article(
        self, topic_id: int, article_type: str = "standard"
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM generated_articles
                   WHERE topic_id=? AND article_type=? ORDER BY id DESC LIMIT 1""",
                (topic_id, article_type),
            ).fetchone()
        return dict(row) if row else None

    def generated_article(self, article_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM generated_articles WHERE id=?", (article_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_generated_article(
        self, article_id: int, content: str, expected_updated_at: str
    ) -> tuple[str, dict[str, Any] | None]:
        now = datetime.now().astimezone().isoformat(timespec="microseconds")
        with self._connection_lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM generated_articles WHERE id=?", (article_id,)
            ).fetchone()
            if row is None:
                return "missing", None
            article = dict(row)
            current_updated_at = str(article.get("updated_at") or article["created_at"])
            if current_updated_at != expected_updated_at:
                return "conflict", article
            if str(article["content"]) == content:
                return "unchanged", article
            connection.execute(
                "INSERT INTO article_revisions(article_id,content,created_at) VALUES (?,?,?)",
                (article_id, article["content"], now),
            )
            connection.execute(
                "UPDATE generated_articles SET content=?,updated_at=? WHERE id=?",
                (content, now, article_id),
            )
            article["content"] = content
            article["updated_at"] = now
            return "updated", article

    def article_revisions(self, article_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM article_revisions WHERE article_id=? ORDER BY id DESC",
                (article_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def generated_articles(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT generated_articles.*, topics.canonical_title AS topic_title
                   FROM generated_articles
                   JOIN topics ON topics.id=generated_articles.topic_id
                   ORDER BY generated_articles.id DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_video_engine_settings(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT key,value_json FROM app_settings WHERE key IN
                   ('video_engine_url','pexels_api_key','pixabay_api_key','coverr_api_key')"""
            ).fetchall()
        values: dict[str, str] = {}
        for row in rows:
            try:
                values[str(row["key"])] = str(json.loads(row["value_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return {
            "engine_url": values.get("video_engine_url", "http://127.0.0.1:8080").rstrip("/"),
            "pexels_api_key": values.get("pexels_api_key", ""),
            "pixabay_api_key": values.get("pixabay_api_key", ""),
            "coverr_api_key": values.get("coverr_api_key", ""),
        }

    def set_video_engine_settings(
        self,
        engine_url: str,
        pexels_api_key: str,
        pixabay_api_key: str,
        coverr_api_key: str,
    ) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            for key, value in (
                ("video_engine_url", engine_url.rstrip("/")),
                ("pexels_api_key", pexels_api_key),
                ("pixabay_api_key", pixabay_api_key),
                ("coverr_api_key", coverr_api_key),
            ):
                connection.execute(
                    """INSERT INTO app_settings(key,value_json,updated_at) VALUES (?,?,?)
                       ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )

    def get_video_tts_settings(self) -> dict[str, str]:
        defaults = {
            "tts_api_url": "https://api.openai.com/v1/audio/speech",
            "tts_api_key": "",
            "tts_model": "gpt-4o-mini-tts",
            "tts_voice": "alloy",
            "gpt_sovits_url": "http://127.0.0.1:9880/tts",
            "gpt_sovits_ref_audio": "",
            "gpt_sovits_prompt_text": "",
            "gpt_sovits_prompt_lang": "zh",
            "gpt_sovits_text_lang": "zh",
        }
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT key,value_json FROM app_settings WHERE key IN
                   ('tts_api_url','tts_api_key','tts_model','tts_voice',
                    'gpt_sovits_url','gpt_sovits_ref_audio','gpt_sovits_prompt_text',
                    'gpt_sovits_prompt_lang','gpt_sovits_text_lang')"""
            ).fetchall()
        for row in rows:
            try:
                defaults[str(row["key"])] = str(json.loads(row["value_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return defaults

    def set_video_tts_settings(self, settings: dict[str, str]) -> None:
        allowed = {
            "tts_api_url",
            "tts_api_key",
            "tts_model",
            "tts_voice",
            "gpt_sovits_url",
            "gpt_sovits_ref_audio",
            "gpt_sovits_prompt_text",
            "gpt_sovits_prompt_lang",
            "gpt_sovits_text_lang",
        }
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            for key, value in settings.items():
                if key not in allowed:
                    continue
                connection.execute(
                    """INSERT INTO app_settings(key,value_json,updated_at) VALUES (?,?,?)
                       ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                    (key, json.dumps(str(value or "").strip(), ensure_ascii=False), now),
                )

    def get_video_preferences(self, defaults: dict[str, Any]) -> dict[str, Any]:
        preferences = dict(defaults)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key='video_preferences'"
            ).fetchone()
        if not row:
            return preferences
        try:
            saved = json.loads(row["value_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return preferences
        if not isinstance(saved, dict):
            return preferences
        for key in preferences:
            if key in saved:
                preferences[key] = saved[key]
        return preferences

    def set_video_preferences(self, preferences: dict[str, Any]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO app_settings(key,value_json,updated_at) VALUES ('video_preferences',?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (json.dumps(preferences, ensure_ascii=False), now),
            )

    def create_article_video_job(
        self, article_id: int | None, script: str, params: dict[str, Any]
    ) -> int:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO article_video_jobs
                   (article_id,status,progress,message,script,params_json,created_at,updated_at)
                   VALUES (?,'queued',0,'等待视频引擎',?,?,?,?)""",
                (article_id, script, json.dumps(params, ensure_ascii=False), now, now),
            )
        return int(cursor.lastrowid)

    def article_video_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM article_video_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest_article_video_job(self, article_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM article_video_jobs WHERE article_id=? ORDER BY id DESC LIMIT 1",
                (article_id,),
            ).fetchone()
        return dict(row) if row else None

    def article_video_jobs(
        self, article_id: int, limit: int | None = None
    ) -> list[dict[str, Any]]:
        query = """SELECT * FROM article_video_jobs
                   WHERE article_id=? ORDER BY id DESC"""
        parameters: list[Any] = [article_id]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(max(1, int(limit)))
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def latest_custom_video_job(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM article_video_jobs WHERE article_id IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def active_article_video_job(self, article_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM article_video_jobs
                   WHERE article_id=? AND status IN ('queued','starting','processing')
                   ORDER BY id DESC LIMIT 1""",
                (article_id,),
            ).fetchone()
        return dict(row) if row else None

    def active_custom_video_job(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM article_video_jobs
                   WHERE article_id IS NULL AND status IN ('queued','starting','processing')
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def custom_video_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM article_video_jobs
                   WHERE article_id IS NULL ORDER BY id DESC LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_article_video_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM article_video_jobs
                   WHERE status IN ('queued','starting','processing') ORDER BY id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def video_job_notifications(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT v.*,t.canonical_title AS article_topic_title
                   FROM article_video_jobs v
                   LEFT JOIN generated_articles a ON a.id=v.article_id
                   LEFT JOIN topics t ON t.id=a.topic_id
                   WHERE v.status IN ('queued','starting','processing')
                      OR (v.status IN ('success','failed') AND v.read_at IS NULL)
                   ORDER BY v.id DESC LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                params = json.loads(str(item.get("params_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                params = {}
            item["notification_type"] = "video"
            item["topic_title"] = str(
                item.get("article_topic_title") or params.get("title") or "自定视频"
            )
            result.append(item)
        return result

    def mark_article_video_jobs_read(self, job_ids: list[int]) -> None:
        if not job_ids:
            return
        marks = ",".join("?" for _ in job_ids)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                f"UPDATE article_video_jobs SET read_at=? WHERE id IN ({marks}) AND status IN ('success','failed')",
                (now, *job_ids),
            )

    def update_article_video_job(self, job_id: int, **fields: Any) -> None:
        allowed = {
            "engine_task_id", "status", "progress", "message", "result_path",
            "result_json", "error",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE article_video_jobs SET {assignments} WHERE id=?",
                (*updates.values(), job_id),
            )

    def recent_runs(self, limit: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM runs WHERE status IN ('success','partial')
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def recent_analyzed_runs(self, limit: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM runs WHERE status IN ('success','partial') AND ai_status='success'
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def run_items(self, run_id: int, source_ids: set[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM news_items WHERE run_id=?"
        params: list[Any] = [run_id]
        if source_ids:
            marks = ",".join("?" for _ in source_ids)
            sql += f" AND source_id IN ({marks})"
            params.extend(sorted(source_ids))
        sql += " ORDER BY source_id, rank"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def recent_topic_catalog(self, run_ids: list[int], limit: int) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        marks = ",".join("?" for _ in run_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT t.id, t.canonical_title, t.summary, MAX(o.run_id) AS last_run_id
                    FROM topics t JOIN topic_observations o ON o.topic_id=t.id
                    WHERE o.run_id IN ({marks}) GROUP BY t.id ORDER BY last_run_id DESC LIMIT ?""",
                [*run_ids, limit],
            ).fetchall()
            return [dict(row) for row in rows]

    def save_clusters(self, run_id: int, clusters: list[dict[str, Any]]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute("DELETE FROM topic_observations WHERE run_id=?", (run_id,))
            used_topic_ids: set[int] = set()
            for cluster in clusters:
                topic_id = cluster.get("existing_topic_id")
                if topic_id:
                    exists = connection.execute("SELECT 1 FROM topics WHERE id=?", (topic_id,)).fetchone()
                    if not exists or int(topic_id) in used_topic_ids:
                        topic_id = None
                if not topic_id:
                    cursor = connection.execute(
                        "INSERT INTO topics(canonical_title, summary, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                        (cluster["title"], cluster.get("summary", ""), now, now),
                    )
                    topic_id = int(cursor.lastrowid)
                else:
                    topic_id = int(topic_id)
                    connection.execute(
                        "UPDATE topics SET canonical_title=?, summary=?, last_seen_at=? WHERE id=?",
                        (cluster["title"], cluster.get("summary", ""), now, topic_id),
                    )
                used_topic_ids.add(topic_id)
                cursor = connection.execute(
                    """INSERT INTO topic_observations
                       (topic_id, run_id, display_title, summary, current_score, platform_count, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        topic_id,
                        run_id,
                        cluster["title"],
                        cluster.get("summary", ""),
                        cluster["current_score"],
                        cluster["platform_count"],
                        now,
                    ),
                )
                observation_id = int(cursor.lastrowid)
                for member in cluster["members"]:
                    connection.execute(
                        """INSERT INTO topic_members
                           (observation_id, news_item_id, source_id, source_name, platform_weight,
                            rank, list_size, rank_score, contribution, title, url)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            observation_id,
                            member["id"],
                            member["source_id"],
                            member["source_name"],
                            member["platform_weight"],
                            member["rank"],
                            member["list_size"],
                            member["rank_score"],
                            member["contribution"],
                            member["title"],
                            member["url"],
                        ),
                    )

    def topic_history(self, run_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not run_ids:
            return {}
        marks = ",".join("?" for _ in run_ids)
        with self.connect() as connection:
            observations = connection.execute(
                f"""SELECT o.*, t.canonical_title FROM topic_observations o
                    JOIN topics t ON t.id=o.topic_id WHERE o.run_id IN ({marks})
                    ORDER BY o.run_id""",
                run_ids,
            ).fetchall()
            observation_ids = [row["id"] for row in observations]
            members_by_observation: dict[int, list[dict[str, Any]]] = {}
            if observation_ids:
                member_marks = ",".join("?" for _ in observation_ids)
                members = connection.execute(
                    f"SELECT * FROM topic_members WHERE observation_id IN ({member_marks}) ORDER BY contribution DESC",
                    observation_ids,
                ).fetchall()
                for row in members:
                    members_by_observation.setdefault(int(row["observation_id"]), []).append(dict(row))

        history: dict[int, dict[str, Any]] = {}
        for row in observations:
            topic_id = int(row["topic_id"])
            entry = history.setdefault(
                topic_id,
                {"topic_id": topic_id, "title": row["canonical_title"], "observations": {}},
            )
            observation = dict(row)
            observation["members"] = members_by_observation.get(int(row["id"]), [])
            entry["observations"][int(row["run_id"])] = observation
            entry["title"] = row["display_title"]
        return history

    def source_results(self, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_results WHERE run_id=? ORDER BY source_id", (run_id,)
            ).fetchall()
            return [dict(row) for row in rows]
