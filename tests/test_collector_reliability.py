import datetime as dt
import io
import json
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from scripts import collect_cv_papers as collector


ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>http://arxiv.org/abs/2611.12345v2</id>
    <title>Underwater Image Enhancement with Physical Priors</title>
    <summary>We enhance underwater images with a physical prior.</summary>
    <author><name>John Smith</name></author>
    <published>2026-11-20T00:00:00Z</published><updated>2026-11-23T00:00:00Z</updated>
    <link rel="related" type="text/html" href="https://example.org/project" />
  </entry></feed>"""


class Response(io.BytesIO):
    def __init__(self, body=b"success"):
        super().__init__(body)
        self.headers = Message()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.policy = patch.object(collector, "REQUEST_POLICY", collector.RequestPolicy(timeout=1, backoff=0))
        self.policy.start()
        self.addCleanup(self.policy.stop)

    @patch.object(collector.RATE_LIMITER, "wait")
    @patch.object(collector.time, "sleep")
    def test_rate_limit_retries_respect_retry_after(self, sleep, limiter):
        headers = Message()
        headers["Retry-After"] = "5"
        error = urllib.error.HTTPError("https://example.org", 429, "rate limited", headers, None)
        with patch.object(collector.urllib.request, "urlopen", side_effect=[error, Response()]) as request:
            self.assertEqual(collector.fetch_url("https://example.org", min_delay=3.2), "success")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(limiter.call_count, 2)
        sleep.assert_called_once_with(5.0)

    @patch.object(collector.RATE_LIMITER, "wait")
    @patch.object(collector.time, "sleep")
    def test_timeout_is_retried(self, sleep, limiter):
        with patch.object(collector.urllib.request, "urlopen", side_effect=[TimeoutError("timeout"), Response()]):
            self.assertEqual(collector.fetch_url("https://example.org"), "success")
        self.assertEqual(sleep.call_count, 1)

    def test_permanent_error_is_not_retried(self):
        error = urllib.error.HTTPError("https://example.org", 404, "missing", Message(), None)
        with patch.object(collector.urllib.request, "urlopen", side_effect=error) as request:
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                collector.fetch_url("https://example.org")
        self.assertEqual(request.call_count, 1)

    def test_long_retry_after_stops_instead_of_retrying_too_soon(self):
        headers = Message()
        headers["Retry-After"] = "120"
        error = urllib.error.HTTPError("https://example.org", 503, "unavailable", headers, None)
        with patch.object(collector.urllib.request, "urlopen", side_effect=error) as request:
            with self.assertRaises(RuntimeError):
                collector.fetch_url("https://example.org")
        self.assertEqual(request.call_count, 1)

    def test_retry_after_http_date(self):
        with patch.object(collector.time, "time", return_value=0):
            self.assertEqual(collector.retry_after_seconds("Thu, 01 Jan 1970 00:00:10 GMT"), 10)
        self.assertEqual(collector.retry_after_seconds("invalid"), 0)


class ArxivTests(unittest.TestCase):
    def test_daily_atom_identifiers_authors_and_announcement_dates(self):
        feed = '''<feed xmlns="http://www.w3.org/2005/Atom"
            xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:dc="http://purl.org/dc/elements/1.1/">
            <entry><id>oai:arXiv.org:2611.12345v2</id><title>Underwater Image Enhancement</title>
            <summary>arXiv:2611.12345v2 Announce Type: replace Abstract: We restore underwater images.</summary>
            <published>2026-11-23T00:00:00-05:00</published><updated>2026-11-23T05:00:30Z</updated>
            <arxiv:announce_type>replace</arxiv:announce_type><arxiv:DOI>10.1234/example</arxiv:DOI>
            <dc:creator>John Smith, Jane Doe</dc:creator></entry></feed>'''
        paper = collector.parse_arxiv_feed(feed)[0]
        self.assertEqual(paper.paper_url, "https://arxiv.org/abs/2611.12345")
        self.assertEqual(paper.authors, ["John Smith", "Jane Doe"])
        self.assertEqual(paper.abstract, "We restore underwater images.")
        self.assertEqual(paper.doi, "10.1234/example")
        self.assertEqual(paper.published_date, "")
        self.assertEqual(paper.updated_date, "")
        self.assertEqual(paper.raw["announcement_date"], "2026-11-23")

    def test_related_html_is_not_mistaken_for_pdf(self):
        paper = collector.parse_arxiv_feed(ATOM)[0]
        self.assertEqual(paper.pdf_url, "https://arxiv.org/pdf/2611.12345")
        self.assertEqual(paper.external_id, "2611.12345")

    def test_http_200_api_error_feed_is_rejected(self):
        feed = ATOM.replace("http://arxiv.org/abs/2611.12345v2", "http://arxiv.org/api/errors#incorrect_id")
        with self.assertRaisesRegex(RuntimeError, "arXiv API error"):
            collector.parse_arxiv_feed(feed)

    def test_invalid_response_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Atom feed"):
            collector.parse_arxiv_feed("<html></html>")

    def test_api_failure_uses_daily_feed_and_marks_degradation(self):
        warnings = []
        with patch.object(collector, "fetch_url", side_effect=[RuntimeError("HTTP 429"), ATOM]) as fetch:
            papers = collector.collect_arxiv(80, 0, warnings)
        self.assertEqual(len(papers), 1)
        self.assertIn("current announcement batch", warnings[0])
        self.assertEqual(papers[0].raw["acquisition"], "daily-atom-fallback")
        self.assertEqual(fetch.call_args_list[0].kwargs["min_delay"], 3.2)

    def test_api_and_fallback_failure_propagate(self):
        with patch.object(collector, "fetch_url", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "daily Atom fallback"):
                collector.collect_arxiv(80, 3.2)


class ScreeningTests(unittest.TestCase):
    def test_architecture_and_venue_cannot_make_off_topic_papers_relevant(self):
        for title in (
            "Hierarchical Diffusion Transformer for Image Generation",
            "AURA: Audio-Driven Streaming Avatar",
            "Spark-H3: Sparse Attention for Video Diffusion Models",
            "Low-Light Action Recognition with a Transformer",
            "Underwater Object Detection with a Transformer",
            "Physics-Guided Protein Folding with Mamba",
        ):
            with self.subTest(title=title):
                paper = collector.Paper(title=title, source="CVF Open Access", venue="CVPR 2026",
                                        abstract="We use diffusion models and self-attention for our task.",
                                        code_url="https://github.com/example/code")
                collector.classify_paper(paper)
                self.assertEqual(paper.score, 0)
                self.assertFalse(paper.worth_reading)

    def test_background_restoration_does_not_override_generation_task(self):
        paper = collector.Paper(title="Diffusion Transformers for Image Generation",
                                abstract="Image restoration is a common application. We instead synthesize images.")
        collector.classify_paper(paper)
        self.assertEqual(paper.score, 0)

    def test_unicode_hyphens_and_task_variants_match(self):
        paper = collector.Paper(title="Zero-Shot Low‑Light Image Enhancement with Diffusion",
                                abstract="We enhance dark images with a diffusion prior.")
        collector.classify_paper(paper)
        self.assertGreaterEqual(paper.score, 24)
        self.assertIn("low-light enhancement", paper.matched_keywords)

    def test_missing_abstract_does_not_receive_top_rating(self):
        paper = collector.Paper(title="Underwater Image Enhancement with Diffusion and Mamba")
        collector.classify_paper(paper)
        self.assertNotIn(paper.recommendation, {"S", "A"})

    def test_submitted_openreview_venue_has_no_acceptance_bonus(self):
        paper = collector.Paper(title="Image Restoration with Diffusion", source="OpenReview",
                                venue="Submitted to ICLR 2026", abstract="We restore degraded images.")
        collector.classify_paper(paper)
        self.assertNotIn("已标注接收状态", paper.raw["screening"]["score_breakdown"])
        self.assertEqual(paper.score, sum(paper.raw["screening"]["score_breakdown"].values()))


class CvfTests(unittest.TestCase):
    venue = ("https://openaccess.thecvf.com/CVPR2026", "CVPR 2026")

    @staticmethod
    def listing(titles):
        return "".join(f'<a href="/content/CVPR2026/html/{index}_paper.html">{title}</a>'
                       for index, title in enumerate(titles))

    def paper(self, index, title="Image Restoration"):
        return collector.Paper(title=title, source="CVF Open Access", venue="CVPR 2026",
                               paper_url=f"https://openaccess.thecvf.com/content/CVPR2026/html/{index}_paper.html",
                               abstract="We address image restoration.")

    def test_underwater_candidates_precede_generic_architecture_papers(self):
        titles = ["Physics-Guided Image Restoration with Diffusion", "Underwater Image Enhancement"]
        with patch.object(collector, "discover_cvf_venues", return_value=[self.venue]), \
             patch.object(collector, "fetch_url", return_value=self.listing(titles)) as fetch, \
             patch.object(collector, "parse_cvf_detail", return_value=self.paper(1, titles[1])) as detail:
            papers = collector.collect_cvf(1, 1)
        self.assertIn("1_paper.html", detail.call_args.args[0])
        self.assertEqual(len(papers), 1)
        self.assertEqual(fetch.call_count, 1)  # The listing is not fetched twice.

    def test_cached_details_do_not_consume_network_budget(self):
        cache = collector.DetailCache(None)
        cache.put(self.paper(1))
        with patch.object(collector, "discover_cvf_venues", return_value=[self.venue]), \
             patch.object(collector, "fetch_url", return_value=self.listing(["Image Restoration A", "Image Restoration B"])), \
             patch.object(collector, "parse_cvf_detail", return_value=self.paper(0)) as detail:
            papers = collector.collect_cvf(1, 1, cache)
        self.assertEqual(len(papers), 2)
        self.assertEqual(detail.call_count, 1)

    def test_failed_details_consume_budget(self):
        warnings = []
        with patch.object(collector, "discover_cvf_venues", return_value=[self.venue]), \
             patch.object(collector, "fetch_url", return_value=self.listing(["Image Restoration"] * 8)), \
             patch.object(collector, "parse_cvf_detail", side_effect=RuntimeError("offline")) as detail:
            with self.assertRaisesRegex(RuntimeError, "All attempted CVF"):
                collector.collect_cvf(1, 2, warnings=warnings)
        self.assertEqual(detail.call_count, 2)
        self.assertEqual(len(warnings), 2)

    def test_repeated_failures_open_circuit_before_large_budget(self):
        with patch.object(collector, "discover_cvf_venues", return_value=[self.venue]), \
             patch.object(collector, "fetch_url", return_value=self.listing(["Image Restoration"] * 10)), \
             patch.object(collector, "parse_cvf_detail", side_effect=RuntimeError("offline")) as detail:
            with self.assertRaises(RuntimeError):
                collector.collect_cvf(1, 90)
        self.assertEqual(detail.call_count, 3)

    def test_missing_venue_links_is_source_failure(self):
        with patch.object(collector, "discover_cvf_venues", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "no supported venues"):
                collector.collect_cvf(1, 1)

    def test_cache_expiry_corruption_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text("not json", encoding="utf-8")
            cache = collector.DetailCache(path)
            self.assertIsNone(cache.get("missing"))
            paper = self.paper(0)
            cache.put(paper)
            cache.save()
            restored = collector.DetailCache(path)
            self.assertEqual(restored.get(paper.paper_url).title, paper.title)
            restored.ttl = 0
            self.assertIsNone(restored.get(paper.paper_url))


class OpenReviewTests(unittest.TestCase):
    def test_author_objects_are_names_not_dictionary_representations(self):
        value = {"value": [{"fullname": "Aiping Yang", "username": "~Aiping_Yang1"},
                            {"name": "John Smith"}, "Jane Doe", {}, None]}
        self.assertEqual(collector.normalize_openreview_authors(value), ["Aiping Yang", "John Smith", "Jane Doe"])

    def test_v1_v2_wrappers_and_replies(self):
        payload = {"notes": [
            {"id": "v2", "forum": "v2", "content": {"title": {"value": "Image Restoration"},
             "abstract": {"value": "We restore images."}, "authors": {"value": ["John Smith"]}},
             "tcdate": 1767225600000, "tmdate": 1767312000000},
            {"id": "reply", "replyto": "v2", "content": {"title": {"value": "Review"}}},
        ]}
        with patch.dict(collector.os.environ, {}, clear=True), \
             patch.object(collector, "FOCUS_QUERIES", ["image restoration"]), \
             patch.object(collector, "fetch_url", return_value=json.dumps(payload)) as fetch:
            papers = collector.collect_openreview(8, 0)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].authors, ["John Smith"])
        self.assertEqual(papers[0].published_date, "2026-01-01")
        self.assertEqual(papers[0].updated_date, "2026-01-02")
        self.assertIn("api2.openreview.net", fetch.call_args_list[0].args[0])
        self.assertIn("source=forum", fetch.call_args_list[0].args[0])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(papers[0].pdf_url, "")  # No attachment metadata, no invented PDF URL.

    def test_v2_failure_falls_back_to_legacy_parameters(self):
        legacy = {"notes": [{"id": "legacy", "content": {"title": "Image Restoration",
                            "abstract": "We restore images.", "authors": ["John Smith"]}}]}
        warnings = []
        with patch.dict(collector.os.environ, {}, clear=True), \
             patch.object(collector, "FOCUS_QUERIES", ["image restoration"]), \
             patch.object(collector, "fetch_url", side_effect=[RuntimeError("offline"), json.dumps(legacy)]) as fetch:
            papers = collector.collect_openreview(1, 0, warnings)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].authors, ["John Smith"])
        self.assertIn("api.openreview.net", fetch.call_args_list[1].args[0])
        self.assertIn("source=papers", fetch.call_args_list[1].args[0])
        self.assertTrue(warnings)

    def test_all_failed_searches_are_not_reported_as_empty_success(self):
        with patch.object(collector, "FOCUS_QUERIES", ["underwater"]), \
             patch.object(collector, "fetch_url", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "All OpenReview searches failed"):
                collector.collect_openreview(1, 0)


class DatabaseAndRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.conn = collector.connect_db(self.root / "papers.db")
        self.addCleanup(self.conn.close)
        self.run_id = collector.start_run(self.conn, "2026-11-23", ["arxiv"], "2026-11-23T08:00:00+08:00")
        self.conn.commit()

    def upsert(self, paper):
        with self.conn:
            return collector.upsert_paper(self.conn, paper, self.run_id, "2026-11-23T08:00:00+08:00")

    def test_arxiv_version_and_title_changes_update_same_record(self):
        first = collector.Paper(title="Image Restoration Version One", source="arXiv cs.CV", external_id="2611.12345v1")
        paper_id, _ = self.upsert(first)
        # Simulate source keys written by the original collector.
        self.conn.execute("UPDATE paper_sources SET source_key = 'arXiv cs.CV:2611.12345v1'")
        self.conn.commit()
        second = collector.Paper(title="A Completely Revised Restoration Title", source="arXiv cs.CV", external_id="2611.12345v2")
        updated_id, is_new = self.upsert(second)
        self.assertEqual(paper_id, updated_id)
        self.assertFalse(is_new)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)

    def test_old_inflated_score_can_fall(self):
        paper = collector.Paper(title="Image Generation with Diffusion Transformers", source="arXiv cs.CV",
                                external_id="2611.12345", abstract="We generate images.")
        paper_id, _ = self.upsert(paper)
        self.conn.execute("UPDATE papers SET score = 999, recommendation = 'S' WHERE id = ?", (paper_id,))
        self.conn.commit()
        self.upsert(paper)
        row = self.conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        self.assertEqual(row["score"], 0)
        self.assertEqual(row["recommendation"], "C")

    def test_legacy_screening_upgrade_preserves_observation_history(self):
        paper = collector.Paper(title="Image Generation with Diffusion Transformers", source="arXiv cs.CV")
        paper_id, _ = self.upsert(paper)
        self.conn.execute("UPDATE papers SET raw_json = '{}', score = 999, recommendation = 'S'")
        before = dict(self.conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone())
        self.assertEqual(collector.rescore_legacy_papers(self.conn), 1)
        after = dict(self.conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone())
        for field in ("id", "first_seen", "last_seen", "title", "source", "fingerprint"):
            self.assertEqual(before[field], after[field])
        self.assertEqual(after["score"], 0)
        self.assertEqual(collector.rescore_legacy_papers(self.conn), 0)

    def test_source_merge_uses_merged_metadata_and_latest_update(self):
        first = collector.Paper(title="Image Restoration with Diffusion", source="CVF Open Access", venue="CVPR 2026",
                                abstract="We restore images.", updated_date="2026-11-20", pdf_url="https://cvf/paper.pdf")
        paper_id, _ = self.upsert(first)
        second = collector.Paper(title=first.title, source="arXiv cs.CV", abstract="We restore images.",
                                 code_url="https://github.com/example/code", updated_date="2026-11-23",
                                 pdf_url="https://arxiv.org/pdf/2611.12345")
        self.upsert(second)
        row = self.conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        self.assertEqual(row["source"], "CVF Open Access")
        self.assertEqual(row["pdf_url"], first.pdf_url)
        self.assertEqual(row["updated_date"], "2026-11-23")
        self.assertEqual(row["score"], 42)

    def run_main(self, papers, failed=False):
        status = {"source": "arxiv", "status": "failed" if failed else "ok", "raw_count": len(papers),
                  "kept_count": len(papers), "duration_seconds": 0, "warnings": ["offline"] if failed else []}
        args = ["--db", str(self.root / "papers.db"), "--reports-dir", str(self.root / "reports"),
                "--cache-dir", str(self.root / "cache"), "--sources", "arxiv", "--new-only"]
        now = dt.datetime(2026, 11, 23, 8, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with patch.object(collector, "collect_sources", return_value=(papers, status["warnings"], [status])), \
             patch.object(collector, "local_now", return_value=now), \
             patch.dict(collector.os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            return collector.main(args)

    def test_all_sources_failed_preserves_existing_report(self):
        report = self.root / "reports" / "2026-11-23.md"
        report.parent.mkdir()
        report.write_text("existing successful report", encoding="utf-8")
        self.assertEqual(self.run_main([], failed=True), 1)
        self.assertEqual(report.read_text(encoding="utf-8"), "existing successful report")
        diagnostics = json.loads((self.root / "cache" / "last-run.json").read_text(encoding="utf-8"))
        self.assertTrue(diagnostics["all_sources_failed"])

    def test_successful_empty_query_remains_successful(self):
        self.assertEqual(self.run_main([]), 0)
        report = (self.root / "reports" / "2026-11-23.md").read_text(encoding="utf-8")
        self.assertIn("今天没有新增匹配论文", report)

    def test_same_day_rerun_keeps_daily_new_papers(self):
        paper = collector.Paper(title="Underwater Image Enhancement", source="arXiv cs.CV",
                                abstract="We enhance underwater images.", external_id="2611.12345")
        self.assertEqual(self.run_main([paper]), 0)
        self.assertEqual(self.run_main([]), 0)
        payload = json.loads((self.root / "reports" / "2026-11-23.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["new_papers"], 0)
        self.assertEqual(payload["daily_new_papers"], 1)
        self.assertEqual(len(payload["papers"]), 1)

    def test_cli_rejects_invalid_sources_limits_and_dates(self):
        for args in (["--sources", "typo"], ["--sources", ""], ["--max-cvf-detail-pages", "-1"],
                     ["--http-retries", "6"], ["--min-score", "nan"], ["--report-date", "2026-13-01"]):
            with self.subTest(args=args), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as caught:
                    collector.parse_args(args)
                self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
