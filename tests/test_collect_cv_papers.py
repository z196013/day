import tempfile
import unittest
from pathlib import Path

from scripts.collect_cv_papers import (
    Paper,
    classify_paper,
    connect_db,
    first_author_surname,
    jaccard_similarity,
    normalize_title,
    start_run,
    upsert_paper,
)


class CollectorLogicTests(unittest.TestCase):
    def test_normalize_title_removes_punctuation_and_stopwords(self):
        self.assertEqual(
            normalize_title("A Physical Prior for Underwater Image Enhancement!"),
            "physical prior underwater image enhancement",
        )

    def test_first_author_surname_handles_common_formats(self):
        self.assertEqual(first_author_surname(["Smith, John"]), "smith")
        self.assertEqual(first_author_surname(["John Smith"]), "smith")

    def test_jaccard_similarity_catches_near_duplicate_title(self):
        self.assertGreaterEqual(
            jaccard_similarity(
                "Underwater Image Enhancement with Physical Priors",
                "Underwater Image Enhancement Using Physical Prior",
            ),
            0.5,
        )

    def test_classify_direct_underwater_is_worth_reading(self):
        paper = Paper(
            title="Physics-Guided Underwater Image Enhancement with Diffusion Priors",
            authors=["A. Researcher"],
            source="arXiv cs.CV",
            abstract="We restore underwater images using a physical prior and diffusion model.",
        )
        classify_paper(paper)
        self.assertIn(paper.recommendation, {"S", "A"})
        self.assertTrue(paper.worth_reading)
        self.assertIn("underwater image enhancement", paper.matched_keywords)

    def test_upsert_deduplicates_same_normalized_title(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "papers.db"
            conn = connect_db(db_path)
            try:
                with conn:
                    run_id = start_run(conn, "2026-06-04", ["arxiv", "cvf"], "2026-06-04T08:00:00+08:00")
                    first = Paper(
                        title="Underwater Image Enhancement With Physical Priors",
                        authors=["John Smith"],
                        source="arXiv cs.CV",
                        venue="arXiv cs.CV",
                        paper_url="https://arxiv.org/abs/2606.00001",
                        abstract="Underwater image enhancement with physical priors.",
                    )
                    second = Paper(
                        title="Underwater Image Enhancement with Physical Priors!",
                        authors=["John Smith"],
                        source="CVF Open Access",
                        venue="CVPR 2026",
                        paper_url="https://openaccess.thecvf.com/content/CVPR2026/html/example.html",
                        abstract="Underwater image enhancement with physical priors and color correction.",
                    )
                    classify_paper(first)
                    classify_paper(second)
                    first_id, first_new = upsert_paper(conn, first, run_id, "2026-06-04T08:00:00+08:00")
                    second_id, second_new = upsert_paper(conn, second, run_id, "2026-06-04T08:01:00+08:00")
                    self.assertEqual(first_id, second_id)
                    self.assertTrue(first_new)
                    self.assertFalse(second_new)
                    count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
                    self.assertEqual(count, 1)
                    row = conn.execute("SELECT source FROM papers WHERE id = ?", (first_id,)).fetchone()
                    self.assertEqual(row["source"], "CVF Open Access")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
