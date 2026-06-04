# Daily CV Paper Watch

This workspace contains a daily paper collector for high-level computer vision work related to underwater image enhancement.

It collects from:

- arXiv `cs.CV`
- CVF Open Access
- OpenReview

It focuses on underwater image enhancement/restoration, image restoration, physical priors, color correction, diffusion, Mamba, Transformer, and low-light enhancement.

## What It Produces

- `papers.db`: SQLite database with deduped paper records.
- `reports/YYYY-MM-DD.md`: daily Markdown report.

Each report includes title, authors, source, paper/PDF/code links, abstract, possible innovation, relationship to underwater image enhancement, close-reading flag, and an `S/A/B/C` recommendation.

## Local Run

```bash
python -m pip install -r requirements.txt
python scripts/collect_cv_papers.py --db papers.db --reports-dir reports
```

Useful options:

```bash
python scripts/collect_cv_papers.py \
  --sources arxiv,cvf,openreview \
  --max-per-source 80 \
  --openreview-limit-per-term 8 \
  --max-cvf-venues 8 \
  --max-cvf-detail-pages 90 \
  --min-score 12
```

## GitHub Actions

The workflow is at `.github/workflows/collect-cv-papers.yml`.

It runs daily at `07:30` Asia/Hong_Kong time and can also be started manually from the GitHub Actions tab. It commits updated `papers.db` and `reports/` files back to the repository.

## Deduplication

The collector deduplicates by:

- normalized DOI when available
- exact normalized title
- fallback title Jaccard similarity >= `0.90` when the first-author surname matches

When the same paper appears in multiple sources, CVF metadata is preferred over OpenReview, and OpenReview is preferred over arXiv. Missing PDF/code/abstract fields are filled from any source when available.

## arXiv Rate Limit

The script performs one arXiv API query per run by default and keeps `--arxiv-delay 3.2`, so future extensions that add more arXiv requests still leave more than 3 seconds between arXiv calls.

Set a more specific user agent before scheduled use:

```bash
export PAPER_BOT_USER_AGENT="cv-paper-watch/1.0 (mailto:you@example.com)"
```

## Source Notes

- arXiv API documentation: <https://info.arxiv.org/help/api/user-manual.html>
- CVF Open Access menu: <https://openaccess.thecvf.com/menu>
- OpenReview Python client: <https://github.com/openreview/openreview-py>
