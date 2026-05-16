# tea-scrapers

Scraping + medallion-pipeline package for the tea recommendation engine. This package owns ingestion of upstream catalog and review sources (Shopify vendors, Steepster, TeaDB, Reddit) and the subsequent transform layers that produce the canonical product graph the recommender queries.

The pipeline is medallion-shaped: `scrape → load → normalize → extract`. Scrapers write immutable raw JSONL under `data/raw/source={source}/date=YYYY-MM-DD/run={ulid}.jsonl` with payloads passed through verbatim. A separate `load` step ingests JSONL into the bronze `raw_product_snapshot` table. `normalize` matches canonical product IDs and populates the silver tables (`product`, `vendor_product`, `product_snapshot`). LLM extraction (later phases) runs over silver to produce flavor / mouthfeel / qì profiles with quote-evidence. Each stage is independently re-runnable, so prompt or schema changes never require re-hitting external sources.

The authoritative implementation spec is `../specs/tea_scrapers_v1_spec.md`. The umbrella product/design doc is `../tea_rec_engine_design_v2.md`. When intuition disagrees with either, the docs win — especially the scraper spec's §11 anti-patterns, which were authored deliberately to override generic best practices.

## Quickstart

Create the venv outside the repo. `~/Desktop` is an iCloud-synced location, and iCloud marks files inside an in-repo `.venv/` as `hidden`, which makes Python silently skip the editable-install `.pth` file and breaks `import tea_scrapers`.

```bash
python3.13 -m venv ~/.venvs/tea-scrapers
~/.venvs/tea-scrapers/bin/pip install -e ".[dev]"
docker compose up -d
```

Activate with `source ~/.venvs/tea-scrapers/bin/activate`, or invoke the tools by absolute path (`~/.venvs/tea-scrapers/bin/tea-scrape ...`).
