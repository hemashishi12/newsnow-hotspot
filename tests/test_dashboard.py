import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import SourceConfig
from app.database import Database
from app.pipeline import _build_rank_chart, build_dashboard, normalized_rank_score


class DashboardHistoryTests(unittest.TestCase):
    def test_new_topic_label_precedes_current_label_only_on_first_observation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            source = SourceConfig("a", "平台A", 1.0, True, True)
            settings = SimpleNamespace(
                raw={
                    "app": {"recent_runs": 3},
                    "scoring": {
                        "max_results_per_section": 20,
                        "rising_min_score": 999.0,
                        "sustained_min_presence_ratio": 1.0,
                    },
                },
                sources=(source,),
                api_key="test",
                ai_base_url="https://api.example.com/v1",
            )

            established_topic_id = None
            for run_number in (1, 2):
                run_id = database.begin_run()
                database.save_source(
                    run_id,
                    source,
                    {
                        "status": "success",
                        "items": [
                            {"id": f"old-{run_number}", "title": "既有话题", "url": f"https://example.com/old/{run_number}"},
                            *(
                                [{"id": "new-2", "title": "新话题", "url": "https://example.com/new/2"}]
                                if run_number == 2
                                else []
                            ),
                        ],
                    },
                )
                database.finish_run(run_id, 1, 1, [])
                items = database.run_items(run_id)
                clusters = []
                for item in items:
                    item.update(rank=1, list_size=20, platform_weight=1.0, rank_score=1.0, contribution=1.0)
                    clusters.append(
                        {
                            "title": item["title"],
                            "summary": "测试",
                            "existing_topic_id": established_topic_id if item["title"] == "既有话题" else None,
                            "members": [item],
                            "platform_count": 1,
                            "current_score": 1.0,
                        }
                    )
                database.save_clusters(run_id, clusters)
                database.set_ai_status(run_id, "success")
                if established_topic_id is None:
                    established_topic_id = next(iter(database.topic_history([run_id])))

            topics = {topic["title"]: topic for topic in build_dashboard(settings, database)["topics"]}
            self.assertEqual(
                [label["name"] for label in topics["新话题"]["labels"][:2]],
                ["新上榜", "多平台共振"],
            )
            self.assertNotIn("新上榜", [label["name"] for label in topics["既有话题"]["labels"]])

    def test_rising_and_sustained_topics_are_computed_from_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            sources = (
                SourceConfig("a", "平台A", 1.0, True, True),
                SourceConfig("b", "平台B", 1.0, True, True),
            )
            settings = SimpleNamespace(
                raw={
                    "app": {"recent_runs": 3},
                    "scoring": {
                        "max_results_per_section": 20,
                        "rising_min_score": 0.0,
                        "sustained_min_presence_ratio": 0.5,
                    },
                },
                sources=sources,
                api_key="test",
                ai_base_url="https://api.example.com/v1",
            )
            topic_id = None
            for rank in (10, 6, 2):
                run_id = database.begin_run()
                item_ids = []
                for source in sources:
                    database.save_source(
                        run_id,
                        source,
                        {
                            "status": "success",
                            "items": [{"id": f"{source.id}-{run_id}", "title": "同一事件", "url": f"https://example.com/{source.id}/{run_id}"}],
                        },
                    )
                database.finish_run(run_id, 2, 2, [])
                items = database.run_items(run_id)
                members = []
                for item in items:
                    item["rank"] = rank
                    item["list_size"] = 20
                    score = normalized_rank_score(rank, 20, 1.2)
                    item.update(platform_weight=1.0, rank_score=score, contribution=score)
                    members.append(item)
                database.save_clusters(
                    run_id,
                    [{
                        "title": "同一事件",
                        "summary": "测试",
                        "existing_topic_id": topic_id,
                        "members": members,
                        "platform_count": 2,
                        "current_score": sum(member["contribution"] for member in members),
                    }],
                )
                database.set_ai_status(run_id, "success")
                if topic_id is None:
                    topic_id = next(iter(database.topic_history([run_id])))

            dashboard = build_dashboard(settings, database)
            self.assertEqual(len(dashboard["current"]), 1)
            self.assertEqual(len(dashboard["rising"]), 1)
            self.assertEqual(len(dashboard["sustained"]), 1)
            self.assertEqual(len(dashboard["topics"]), 1)
            self.assertAlmostEqual(dashboard["topics"][0]["score"], 1.0)
            self.assertEqual(
                [label["name"] for label in dashboard["topics"][0]["labels"]],
                ["多平台共振", "快速升温", "持续高热"],
            )
            self.assertGreater(dashboard["rising"][0]["score"], 0)
            self.assertEqual(
                [member["rank_change"] for member in dashboard["rising"][0]["members"]],
                [4, 4],
            )
            self.assertEqual(
                [member["previous_rank"] for member in dashboard["rising"][0]["members"]],
                [6, 6],
            )
            self.assertEqual(dashboard["sustained"][0]["presence_count"], 3)
            self.assertEqual(
                [member["rank_change"] for member in dashboard["topics"][0]["members"]],
                [4, 4],
            )
            self.assertEqual(
                [label.split()[0] for label in dashboard["topics"][0]["rank_chart"]["labels"]],
                ["#1", "#2", "#3"],
            )
            self.assertEqual(dashboard["topics"][0]["rank_chart"]["series"][0]["values"], [10, 6, 2])
            self.assertFalse(dashboard["topics"][0]["rank_chart"]["separate_axes"])

            skipped_run = database.begin_run()
            database.finish_run(skipped_run, 0, 0, [])
            database.set_ai_status(skipped_run, "failed", "provider timeout")
            fallback = build_dashboard(settings, database)
            self.assertTrue(fallback["analysis_stale"])
            self.assertEqual(len(fallback["current"]), 1)

    def test_rank_chart_uses_two_axes_only_for_materially_different_rank_ranges(self):
        runs = [{"id": 1}, {"id": 2}, {"id": 3}]
        observations = {
            run_id: {
                "members": [
                    {"source_id": "top", "source_name": "高位平台", "rank": rank},
                    {"source_id": "deep", "source_name": "深位平台", "rank": deep_rank},
                ]
            }
            for run_id, rank, deep_rank in ((1, 2, 36), (2, 1, 42), (3, 3, 39))
        }
        chart = _build_rank_chart(observations, [1, 2, 3], runs)
        self.assertTrue(chart["separate_axes"])
        self.assertEqual([series["axis"] for series in chart["series"]], ["rank-low", "rank-high"])


if __name__ == "__main__":
    unittest.main()
