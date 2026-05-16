"""Click CLI entrypoint.

Spec reference: specs/tea_scrapers_v1_spec.md §7 (CLI Interface).

Subcommands are scaffolded with placeholder behavior; real implementations
land in subsequent roadmap steps (HttpClient, JsonlWriter, Shopify scraper).
Exit codes (per spec §7): 0 success, 1 partial failure, 2 terminal failure.
"""

from __future__ import annotations

import click
import structlog
from ulid import ULID

from tea_scrapers.config import VendorConfig, get_settings, load_shopify_vendors
from tea_scrapers.http.client import HttpClient
from tea_scrapers.logging import configure_logging
from tea_scrapers.sources import ShopifyScraper
from tea_scrapers.storage.raw import JsonlWriter
from tea_scrapers.storage.run_tracker import RunTracker

_log = structlog.get_logger(__name__)


@click.group()
@click.version_option(package_name="tea-scrapers")
def cli() -> None:
    """tea-scrape — scrape, load, normalize tea data through the medallion pipeline."""
    configure_logging()


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@cli.group()
def ingest() -> None:
    """Scrape an upstream source → raw JSONL."""


@ingest.command("shopify")
@click.option("--vendor", "vendor", type=str, default=None, help="Vendor key from config/vendors.yaml.")
@click.option("--all", "all_vendors", is_flag=True, default=False, help="Run all configured Shopify vendors.")
@click.option(
    "--mode",
    type=click.Choice(["full", "incremental"]),
    default="incremental",
    show_default=True,
)
@click.pass_context
def ingest_shopify(ctx: click.Context, vendor: str | None, all_vendors: bool, mode: str) -> None:
    """Scrape Shopify products (generic scraper, vendor config in YAML)."""
    if not vendor and not all_vendors:
        raise click.UsageError("Pass either --vendor <key> or --all.")

    vendors = load_shopify_vendors()
    if vendor:
        if vendor not in vendors:
            raise click.UsageError(
                f"unknown vendor '{vendor}'. Known: {sorted(vendors)}"
            )
        targets = [vendors[vendor]]
    else:
        targets = list(vendors.values())

    successes = 0
    failures = 0
    for target in targets:
        try:
            _run_one_shopify_vendor(target, mode)
            successes += 1
        except Exception as exc:  # noqa: BLE001 — per-vendor isolation in --all mode
            failures += 1
            _log.error(
                "scrape.vendor.failed",
                source=target.source_key,
                error=type(exc).__name__,
                message=str(exc),
            )
            # Single-vendor invocation surfaces the failure as exit 2.
            if vendor:
                ctx.exit(2)

    if failures == 0:
        ctx.exit(0)
    if successes == 0:
        ctx.exit(2)
    ctx.exit(1)


def _run_one_shopify_vendor(vendor: VendorConfig, mode: str) -> None:
    """Open a single tracker+client+writer triple and drive ShopifyScraper.run()."""
    run_id = str(ULID())
    settings = get_settings()
    with RunTracker(source=vendor.source_key, mode=mode, run_id=run_id) as tracker:
        with HttpClient() as http_client:
            with JsonlWriter(run_id=run_id, base_dir=settings.raw_data_dir) as writer:
                scraper = ShopifyScraper(
                    vendor=vendor,
                    http_client=http_client,
                    writer=writer,
                    tracker=tracker,
                    run_id=run_id,
                )
                scraper.run(mode)


@ingest.command("steepster")
@click.option("--vendor", "vendor", required=True, type=str, help="Steepster vendor slug.")
@click.pass_context
def ingest_steepster(ctx: click.Context, vendor: str) -> None:
    """Scrape Steepster tea pages + tasting notes for a vendor."""
    click.echo("not implemented")
    ctx.exit(0)


@ingest.command("teadb")
@click.pass_context
def ingest_teadb(ctx: click.Context) -> None:
    """Scrape TeaDB.org posts via WordPress JSON API."""
    click.echo("not implemented")
    ctx.exit(0)


@ingest.command("reddit")
@click.option("--subreddit", required=True, type=str, help="Subreddit name without leading r/.")
@click.option("--since", default="7d", show_default=True, help="Lookback window, e.g. 7d, 30d.")
@click.pass_context
def ingest_reddit(ctx: click.Context, subreddit: str, since: str) -> None:
    """Scrape Reddit submissions + comments via PRAW."""
    click.echo("not implemented")
    ctx.exit(0)


# ---------------------------------------------------------------------------
# load / normalize / status
# ---------------------------------------------------------------------------


@cli.command("load")
@click.option("--since", required=True, type=str, help="UTC date YYYY-MM-DD; load JSONL on/after this date.")
@click.pass_context
def load_cmd(ctx: click.Context, since: str) -> None:
    """Load raw JSONL → bronze `raw_product_snapshot`."""
    click.echo("not implemented")
    ctx.exit(0)


@cli.command("normalize")
@click.option("--since", required=True, type=str, help="UTC date YYYY-MM-DD; normalize bronze rows on/after this date.")
@click.pass_context
def normalize_cmd(ctx: click.Context, since: str) -> None:
    """Run canonical-ID matching + bronze → silver normalization."""
    click.echo("not implemented")
    ctx.exit(0)


@cli.command("status")
@click.option("--source", "source", default=None, type=str, help="Limit to a single source key.")
@click.pass_context
def status_cmd(ctx: click.Context, source: str | None) -> None:
    """Report recent scrape-run health."""
    click.echo("not implemented")
    ctx.exit(0)


if __name__ == "__main__":
    cli()
