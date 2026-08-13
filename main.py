from __future__ import annotations

import argparse

from app.config import load_settings
from app.database import Database
from app.pipeline import analyze_run, collect_once
from app.web import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="NewsNow 跨平台热点分析器")
    parser.add_argument("command", choices=("serve", "collect", "analyze", "init"), nargs="?", default="serve")
    parser.add_argument("--no-ai", action="store_true", help="本轮只采集，不调用 AI")
    args = parser.parse_args()

    settings = load_settings()
    database = Database(settings.database_path)
    if args.command == "serve":
        serve(settings, database)
    elif args.command == "collect":
        run_id = collect_once(settings, database, run_ai=not args.no_ai)
        print(f"采集完成：run_id={run_id}")
    elif args.command == "analyze":
        run_id = database.latest_run_id()
        if run_id is None:
            raise SystemExit("还没有可分析的采集记录")
        count = analyze_run(settings, database, run_id)
        print(f"分析完成：run_id={run_id}, topics={count}")
    else:
        print(f"数据库已初始化：{settings.database_path}")


if __name__ == "__main__":
    main()

