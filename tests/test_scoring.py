import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.ai import AIClusterer, prepare_ai_submission
from app.pipeline import _slope, normalized_rank_score


class ScoringTests(unittest.TestCase):
    def test_ai_clusterer_uses_streaming_and_assembles_sse_content(self):
        settings = SimpleNamespace(
            api_key="test-key",
            ai_base_url="https://api.example.com/v1",
            ai_model="test-model",
            raw={"ai": {"timeout_seconds": 10}},
        )
        requests = []
        responses = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertTrue(payload.get("stream", False))
            body = (
                'data: {"choices":[{"delta":{"content":"{\\"clusters\\":"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"[]}"}}]}\n\n'
                'data: [DONE]\n\n'
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        clusterer = AIClusterer(settings)
        with patch("app.ai.httpx.Client", return_value=client):
            result = clusterer.cluster(
                [],
                [],
                2,
                on_request=lambda batch, payload: requests.append(payload) or 1,
                on_response=lambda token, status, text, error: responses.append((status, text, error)),
            )

        self.assertEqual(result, [])
        self.assertEqual(len(requests), 1)
        self.assertEqual(responses, [(200, '{"clusters":[]}', "")])

    def test_rank_one_is_maximum(self):
        self.assertEqual(normalized_rank_score(1, 20, 1.2), 1.0)

    def test_lower_rank_has_lower_score(self):
        self.assertGreater(normalized_rank_score(3, 20, 1.2), normalized_rank_score(12, 20, 1.2))

    def test_upward_series_has_positive_slope(self):
        self.assertGreater(_slope([0.1, 0.2, 0.5, 0.9]), 0)

    def test_flat_series_has_zero_slope(self):
        self.assertAlmostEqual(_slope([0.5, 0.5, 0.5]), 0.0)

    def test_ai_submission_keeps_every_item_without_prefiltering(self):
        items = [
            {"id": 1, "source_id": "a", "rank": 1, "title": "某地停车场改为按分钟计费"},
            {"id": 2, "source_id": "b", "rank": 8, "title": "不相关标题"},
            {"id": 3, "source_id": "c", "rank": 20, "title": "一款新游戏公布发售日期"},
        ]
        submission = prepare_ai_submission(items)
        self.assertEqual([item["id"] for item in submission], [1, 2, 3])

    def test_ai_submission_is_not_split_even_above_old_batch_size(self):
        items = [
            {"id": index, "source_id": str(index % 5), "rank": index, "title": f"标题 {index}"}
            for index in range(1, 141)
        ]
        submission = prepare_ai_submission(items)
        self.assertEqual(len(submission), 140)


if __name__ == "__main__":
    unittest.main()
