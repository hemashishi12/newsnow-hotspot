import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import SourceConfig
from app.database import Database
from app.pipeline import collect_once


class SelectedSourceCollectionTests(unittest.TestCase):
    def test_collection_fetches_only_sources_selected_in_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "collection.db")
            sources = (
                SourceConfig("a", "平台A", 1.0, True, True),
                SourceConfig("b", "平台B", 1.0, True, True),
                SourceConfig("c", "平台C", 1.0, True, True),
            )
            database.set_analysis_source_ids({"a", "c"})
            settings = SimpleNamespace(raw={"newsnow": {}}, sources=sources)
            fetched: list[str] = []

            class FakeNewsNowClient:
                interval = 0

                def __init__(self, config):
                    pass

                def fetch(self, source_id):
                    fetched.append(source_id)
                    return {
                        "status": "success",
                        "items": [{"id": source_id, "title": f"{source_id} title"}],
                    }

            with patch("app.pipeline.NewsNowClient", FakeNewsNowClient):
                run_id = collect_once(settings, database, run_ai=False)

            self.assertEqual(fetched, ["a", "c"])
            self.assertEqual(len(database.source_results(run_id)), 2)
            self.assertEqual(len(database.run_items(run_id)), 2)


if __name__ == "__main__":
    unittest.main()
