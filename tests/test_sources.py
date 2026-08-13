import unittest

from app.config import load_settings


EXPECTED_NEWSNOW_HOTTEST_IDS = {
    "baidu", "bilibili-hot-search", "chongbuluo-hot", "cls-hot", "coolapk",
    "douban", "douyin", "freebuf", "github-trending-today", "hackernews",
    "hupu", "ifeng", "iqiyi-hot-ranklist", "juejin", "nowcoder",
    "producthunt", "qqvideo-tv-hotsearch", "sspai", "steam", "tencent-hot",
    "thepaper", "tieba", "toutiao", "wallstreetcn-hot", "weibo",
    "xueqiu-hotstock", "zhihu",
}


class SourceConfigurationTests(unittest.TestCase):
    def test_all_27_public_hottest_sources_are_configured(self):
        settings = load_settings()
        configured = {source.id for source in settings.sources if source.collect}
        self.assertEqual(configured, EXPECTED_NEWSNOW_HOTTEST_IDS)
        self.assertEqual(len(configured), 27)


if __name__ == "__main__":
    unittest.main()
