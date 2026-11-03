"""Pruebas sin navegador. El HTML imita el estilo prosilver de phpBB; no es una
copia de forums.nesdev.org, así que no garantiza que los selectores coincidan."""

import json
import tempfile
import unittest
from pathlib import Path

import page_status
import scraper

BASE = "https://forums.nesdev.org/viewforum.php?f=12"
FORUM_HTML = """
<ul class="topiclist topics">
  <li class="row bg1 sticky"><dl class="row-item"><dt><div class="list-inner">
    <a href="./viewtopic.php?t=7&amp;sid=abc" class="topictitle">Fijado</a>
    <div class="topic-poster"><a class="username-coloured">ana</a>
      <time datetime="2024-01-02T03:04:05+00:00">ene</time></div>
  </div></dt>
  <dd class="posts">1,234 <dfn>Replies</dfn></dd>
  <dd class="views">5,678 <dfn>Views</dfn></dd>
  <dd class="lastpost"><span>by <a class="username">beto</a>
    <time datetime="2025-06-07T08:09:10+00:00">jun</time></span></dd>
  </dl></li>
  <li class="row bg2"><dl class="row-item"><dt><div class="list-inner">
    <a href="./viewtopic.php?t=8" class="topictitle">Normal</a>
    <div class="topic-poster"><span class="username">invitado</span></div>
  </div></dt><dd class="posts">0</dd><dd class="views">3</dd></dl></li>
</ul>
<div class="pagination"><ul>
  <li class="arrow next"><a rel="next" href="./viewforum.php?f=12&amp;start=25&amp;sid=abc">Next</a></li>
</ul></div>
"""


class RobotsTests(unittest.TestCase):
    # Reglas de forums.nesdev.org: `Allow: /` aparece antes que los `Disallow`.
    TEXT = """User-agent: Amazonbot
Disallow: /

User-agent: *
Allow: /
Disallow: /posting.php
Disallow: /search.php
"""

    def test_most_specific_rule_wins_over_rule_order(self):
        rules, _ = scraper.parse_robots(self.TEXT)
        self.assertTrue(scraper.robots_allows(rules, BASE))
        self.assertFalse(
            scraper.robots_allows(rules, "https://forums.nesdev.org/search.php?q=x")
        )

    def test_other_agents_do_not_apply(self):
        rules, _ = scraper.parse_robots("User-agent: Otro\nDisallow: /\n")
        self.assertTrue(scraper.robots_allows(rules, BASE))

    def test_html_error_page_is_not_robots(self):
        self.assertEqual(
            scraper.parse_robots("404 Not Found\nNo existe esa página"), (None, None)
        )

    def test_reads_crawl_delay_for_all_agents_only(self):
        text = "User-agent: Otro\nCrawl-delay: 99\n\nUser-agent: *\nCrawl-delay: 7\n"
        self.assertEqual(scraper.parse_robots(text), ([], 7.0))

    def test_wildcard_and_end_anchor(self):
        rules, _ = scraper.parse_robots("User-agent: *\nDisallow: /*.pdf$\n")
        self.assertFalse(scraper.robots_allows(rules, "https://a.com/x/b.pdf"))
        self.assertTrue(scraper.robots_allows(rules, "https://a.com/x/b.pdf?v=1"))


class PhpbbTests(unittest.TestCase):
    def setUp(self):
        self.topics, self.next_url = scraper.parse_phpbb_forum(FORUM_HTML, BASE)

    def test_extracts_full_topic(self):
        self.assertEqual(
            self.topics[0],
            {
                "id": "7",
                "title": "Fijado",
                "url": "https://forums.nesdev.org/viewtopic.php?t=7",
                "kind": "sticky",
                "author": "ana",
                "created_at": "2024-01-02T03:04:05+00:00",
                "replies": 1234,
                "views": 5678,
                "last_post_author": "beto",
                "last_post_at": "2025-06-07T08:09:10+00:00",
            },
        )

    def test_tolerates_missing_fields(self):
        self.assertEqual(self.topics[1]["author"], "invitado")
        self.assertEqual(self.topics[1]["replies"], 0)
        self.assertIsNone(self.topics[1]["last_post_at"])

    def test_next_page_drops_sid(self):
        self.assertEqual(
            self.next_url, "https://forums.nesdev.org/viewforum.php?f=12&start=25"
        )

    def test_does_not_follow_other_forum(self):
        html = '<div class="pagination"><a rel="next" href="./viewforum.php?f=99"></a></div>'
        self.assertEqual(scraper.parse_phpbb_forum(html, BASE), ([], None))


class HelperTests(unittest.TestCase):
    def test_status_from_log_uses_main_document(self):
        def entry(url, status, kind="Document"):
            params = {"type": kind, "response": {"url": url, "status": status}}
            message = {"method": "Network.responseReceived", "params": params}
            return {"message": json.dumps({"message": message})}

        entries = [
            entry(BASE, 403),  # el desafío, antes de superarlo
            entry(BASE, 200),
            entry(BASE, 500, kind="Image"),
            entry("https://challenges.cloudflare.com/x", 404),
        ]
        self.assertEqual(page_status.status_from_log(entries, BASE + "#p1"), 200)
        self.assertIsNone(page_status.status_from_log([], BASE))

    def test_status_handling(self):
        scraper.check_status(200, BASE)
        scraper.check_status(None, BASE)
        with self.assertRaises(scraper.PageError):
            scraper.check_status(404, BASE)
        for status in (403, 429, 503):
            with self.assertRaises(scraper.BlockedError):
                scraper.check_status(status, BASE)

    def test_load_done_skips_corrupt_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "salida.jsonl"
            record = {"url": BASE + "&sid=x", "next_url": BASE + "&start=25"}
            path.write_text(json.dumps(record) + "\n{roto\n", encoding="utf-8")
            self.assertEqual(scraper.load_done(path), {BASE: BASE + "&start=25"})


if __name__ == "__main__":
    unittest.main()
