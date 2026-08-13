from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SourceConfig:
    id: str
    name: str
    weight: float
    collect: bool
    analyze: bool


@dataclass(frozen=True)
class Settings:
    root: Path
    raw: dict[str, Any]
    sources: tuple[SourceConfig, ...]
    database_path: Path
    api_key: str
    ai_base_url: str
    ai_model: str

    @property
    def source_map(self) -> dict[str, SourceConfig]:
        return {source.id: source for source in self.sources}


def load_settings(config_path: Path | None = None) -> Settings:
    config_path = config_path or ROOT / "config.yaml"
    load_dotenv(ROOT / ".env")
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    sources = tuple(
        SourceConfig(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            weight=float(item.get("weight", 1.0)),
            collect=bool(item.get("collect", True)),
            analyze=bool(item.get("analyze", True)),
        )
        for item in raw.get("newsnow", {}).get("sources", [])
    )
    database = Path(raw.get("app", {}).get("database", "data/hotspots.db"))
    if not database.is_absolute():
        database = ROOT / database

    return Settings(
        root=ROOT,
        raw=raw,
        sources=sources,
        database_path=database,
        api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_base_url=os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        ai_model=os.getenv("AI_MODEL", "gpt-4.1-mini").strip(),
    )

