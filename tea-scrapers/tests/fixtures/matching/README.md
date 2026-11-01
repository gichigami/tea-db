# Canonical-matcher labeled pair set (ROADMAP H.1)

Hand-labeled **product** and **producer** pairs for evaluating the canonical
matchers (`normalize/canonical.py`): the V1 trigram baseline now, and the
hybrid matcher in H.5 (scrapers spec §12 "Hybrid canonical matcher").

> **Review status: AGENT-PROPOSED.** Every label was proposed by the
> qa-engineer agent from catalog evidence and is awaiting owner review
> (`review_status = "agent_proposed"`). Start with the 39 low-confidence
> rows in `low_confidence_review.csv`.

## Files

| File | What |
|---|---|
| `product_pairs.jsonl` | 402 product pairs (155 match / 247 non_match) |
| `producer_pairs.jsonl` | 115 producer pairs (51 match / 64 non_match) |
| `low_confidence_review.csv` | The 39 `label_confidence = low` pairs (30 product + 9 producer), flattened for a spreadsheet, with blank `owner_label` / `owner_note` columns |
| `name_embeddings_bge_m3.jsonl.gz` | BGE-M3 (`BAAI/bge-m3` @ `5617a9f6…`) dense float16 + sparse vectors for every pair text (672 matching keys / producer norms, 1.44 MB). Built by `scripts/build_name_embedding_fixtures.py` (needs the `[embed]` extra; `--check` does not). Read cache-only by `tea_scrapers.embed.fixture_embedder`, so the embedding harness (`python -m tea_scrapers.eval.embedding_eval`) runs offline. Regenerate after any label or `matching_key` change — `test_fixture_covers_every_pair_text` fails until you do |
| `owner_review.csv` | *(absent until review)* Owner decisions; the generator applies them |

Generator: `tea-scrapers/scripts/build_matcher_pairs.py`. Schema: `tea_scrapers.eval.pairs`.
Harness: `tea_scrapers.eval.matcher_eval`. Tests: `tests/unit/test_matcher_pair_fixtures.py`
(schema + harness logic, no DB) and `tests/integration/test_matcher_eval_trigram.py` (pg_trgm smoke).

**Do not hand-edit the JSONL.** The schema test fails on lines that are not in
generator output form. Change the generator (keep its spec lists append-only
so pair_ids stay stable) or record owner decisions in `owner_review.csv`.

## Schema (one JSON object per line, sorted keys)

```text
pair_id           "prod-0001" | "prdc-0001"   (sequential per file)
entity            "product" | "producer"
left, right       PairSide:
  name            the string the matcher sees (product: Shopify title / Steepster name)
  producer        product only: producer hint (YS Producer_ tag, else Shopify vendor field)
  harvest_year    product only: mapper output (YS harvest tag, else ^(19|20)\d\d title regex)
  weight_grams    product only: ONE variant weight, parsed by the mapper from option1/title
  source_ref      provenance (see below)
  vendor          source key (yunnan_sourcing_us, crimson_lotus, steepster, ...)
label             "match" | "non_match"
label_confidence  "high" | "low"
category          primary category (vocabulary below)
tags              secondary facets (one_sided_weight, one_sided_year, cross_source,
                  year_not_parsed, recipe, teaware_model, hanzi, romanization, control, ...)
source            "cassette" | "cassette_derived" | "synthetic"
rationale         short reason for the label
review_status     "agent_proposed" | "owner_confirmed" | "owner_corrected"
```

### Label semantics (policy, please confirm)

- **Product `match`** means "the matcher should resolve both sides to the **same
  `product` row**". That row's identity is §8's `(producer, harvest_year, name,
  weight_grams)`. Two consequences follow, labeled as policy (`high`):
  - The 25 g sample and the 357 g cake of one pressing are a `non_match`
    (`hard_negative_weight`).
  - Two storefronts that tag different harvest years on the same evergreen
    listing are a `non_match` (`low`, since those could be stale tags).
- A Steepster entry has **no weight**. When it names the same pressing as a
  Shopify listing it is labeled `match` and tagged `one_sided_weight`. These
  rows measure the recall cost of spec §12 sub-question #7 (one-sided
  attributes), which is the historical→current pivot.
- A Seattle-warehouse twin (`" :: Seattle Inventory"`) is a `match`. It is `low`
  when its variant carries no parseable weight.
- **Producer `match`** means the same producing entity (factory, brand, workshop
  or artisan). Retailer tags such as "Yunnan Sourcing Tea Shop" and placeholder
  buckets such as "Other" / "Other Factories" are `low`. The owner should rule
  on whether placeholders should be producer rows at all.

### Categories

| entity | category | n | match | low | what |
|---|---|---|---|---|---|
| product | `suffix_variant` | 37 | 37 | 12 | CL `:: Seattle Inventory` twins (incl. 7 with word-order changes) |
| product | `cross_vendor_same_tea` | 74 | 68 | 14 | 47 Steepster↔CL Shopify (46 match; historical→current); 27 YS .us↔.com storefronts |
| product | `duplicate_listing` | 7 | 6 | 3 | Steepster duplicate pages, same-title relists, packaging variants |
| product | `cross_script` | 48 | 44 | 0 | Hanzi renderings, romanization/spacing variants, 4 non-match controls |
| product | `hard_negative_recipe_code` | 28 | 0 | 0 | 7572/7262, 7542/7540, No.15/No.2 Rougui, Pt.1/Pt.2, model #s |
| product | `hard_negative_year` | 58 | 0 | 0 | Recurring names across years; 10 where the year is not at title start |
| product | `hard_negative_weight` | 27 | 0 | 0 | Same listing at different weights; full cake vs "Mini(s)" |
| product | `hard_negative_sheng_shou` | 16 | 0 | 0 | Raw vs Ripe twins with the same producer, year and weight |
| product | `hard_negative_tea_type` | 21 | 0 | 0 | Same garden as black, white or raw |
| product | `hard_negative_name_variant` | 46 | 0 | 1 | Template siblings: Autumn X vs X, Gold vs Silver Impression, Reserve |
| product | `cross_vendor_lookalike` | 12 | 0 | 0 | Different vendors, shared origin/style tokens |
| product | `easy_negative` | 28 | 0 | 0 | Same producer/year/weight block, unrelated names (sha256-sampled) |
| producer | `producer_alias` | 37 | 37 | 4 | Tag spellings, title tokens (DaYi, YiPinTang, FT, Zhong Cha), abbreviations (W2T, CLT) |
| producer | `cross_script` | 17 | 14 | 0 | 金家棋 / 下关 / 天地人 attested; 大益, 勐海茶厂 and others synthetic; 3 controls |
| producer | `hard_negative_producer` | 35 | 0 | 5 | Menghai Tea Factory vs Menghai Tian Di Ren, artisans who share a surname, Printify |
| producer | `easy_negative` | 26 | 0 | 0 | Unrelated producers (sha256-sampled) |

About half of the product pairs (196 of 402) are hard negatives, by design.
**Precision on this set is therefore not production precision.** It is a
stress test for the precision-first auto-match target (≥ ~0.98).

## Provenance

The generator reads only committed repo data and makes no network calls:

- the four Shopify `/products.json` cassettes (2,462 products)
- the gzipped Steepster cassette (106 Crimson Lotus company-index names, plus
  tasting-note text used for attestation)
- the golden JSONL fixtures
- `config/vendors.yaml` (the Steepster slugs `dayi` and `menghai-tea-factory`)

Each pair's `source` is one of three levels. The generator enforces them and the
schema validates them:

- **`cassette`**: both names appear verbatim in the data.
- **`cassette_derived`**: one name was rewritten with a token that is
  **attested** in the repo corpus, and the generator checks this. Examples:
  - a hanzi token found in *that product's own* `body_html` (冰岛老寨 for Bing
    Dao Lao Zhai)
  - a romanization seen elsewhere in the catalog (Bangdong, Gushu, Tieguanyin)
  - a sibling recipe code that exists elsewhere (7262, 7562, 7540, 2503)
  - a producer string that appears inside a title
  - a config slug
- **`synthetic`**: the token is *not* attested anywhere, for example
  Laobanzhang, 大红袍, 大益, 勐海茶厂 or "Hailanghao". There are 7 product pairs
  and 18 producer pairs at this level. They are tagged, never silent.

The data contained real cross-script evidence only inside product descriptions,
never as a listing title. Every hanzi *name* is therefore constructed.

Each sampled family is ordered by `sha256`, not `random`. The output is
byte-identical on every run: `python scripts/build_matcher_pairs.py --check`
exits 1 when the fixtures are stale. **Refresh cadence:** regenerate whenever a
source cassette is re-recorded (annual, or when an endpoint shape changes). Then
re-check that the curated titles still exist; the generator fails loudly on a
missing title.

## Owner review workflow

1. Copy `low_confidence_review.csv` to `owner_review.csv` in this directory.
2. Fill `owner_label` (`match` / `non_match`) and optionally `owner_note`. You
   can append rows for high-confidence pairs too.
3. Run `python scripts/build_matcher_pairs.py`. Each decision is applied by
   `pair_id`, after checking that the names are unchanged. The pair's
   `review_status` becomes `owner_confirmed` or `owner_corrected` and its
   `label_confidence` becomes `high`. Commit `owner_review.csv` alongside.

## Running the baseline

```bash
# needs a Postgres with pg_trgm (any migrated test DB). Read-only: SELECT similarity() only.
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/tea_qa \
  python -m tea_scrapers.eval.matcher_eval [--product-policy v1|h2] [--confidence all|high] [--json out.json]
```

The two product policies:

- **`v1`** is a frozen mirror of `canonical.py` at `ef1a0f6`, the H.0 baseline:
  1. Block on producer, year and weight with `IS NOT DISTINCT FROM`. "Same
     producer" is decided by the V1 producer ladder on the two producer strings.
  2. Exact match on `normalize_name`.
  3. Trigram match when `similarity(canonical_name)` ≥ t.
- **`h2`** is the **live** H.2 ladder:
  1. The same blocking.
  2. Exact match on the suffix-stripped `matching_key`.
  3. Trigram on suffix-stripped names.
  4. `check_attribute_guard`, covering recipe code and sheng/shou, with the
     one-sided rule from `ONE_SIDED_POLICY`.

The producer policy follows V1 in both modes: normalized equality, which also
stands in for an alias hit, then trigram ≥ t.

Pairwise caveats:

- **AMBIGUOUS gap.** It never fires pairwise, because a pair has only one
  candidate. In corpus context it only converts matches into over-creates, so
  pairwise recall is an upper bound.
- **Alias learning.** Only the first encounter is modelled.

**Pluggable scorer for H.5.** Implement `PairScorer.score(list[(a, b)]) ->
list[float]` and optionally a `MatchPolicy(gate, exact, prepare)`. Then call
`evaluate()` / `sweep()` / `best_thresholds()`.

## Trigram baseline (2026-10-31, all 517 pairs, threshold 0.85)

| entity / policy | P | R | F1 | best-F1 t (P / R / F1) | lowest t with P ≥ 0.98 |
|---|---|---|---|---|---|
| product / v1 | 0.435 | 0.174 | 0.249 | 0.55 (0.388 / 0.426 / 0.406) | none in 0.50–0.95 |
| product / h2 (live) | 0.650 | 0.336 | 0.443 | 0.75 (0.574 / 0.374 / 0.453) | none in 0.50–0.95 |
| producer | 1.000 | 0.059 | 0.111 | 0.50 (0.650 / 0.255 / 0.366) | 0.70 (R = 0.157) |

On high-confidence rows only, product/v1 scores P 0.435, R 0.203 and F1 0.277,
and producer scores P 1.000, R 0.064 and F1 0.120.

Under V1, blocking vetoes 65 of the 155 product positives. That caps recall at
**0.581** whatever the threshold.

What fails:

1. **One-sided / missing attributes veto true matches.**
   - 45 of the 46 Steepster↔Shopify positives are vetoed: 37 by weight,
     because Steepster has no weight, and 8 by year, because the Shopify side is
     titled `Spring 2016 …` and parses no year. The one survivor is a teacup
     with an identical name and no weight on either side. The
     historical→current pivot has essentially zero recall under the V1 rules.
   - YS storefront harvest-tag drift vetoes 5 more positives, and a
     producer-tag drift vetoes 1.
   - H.2's provisional *style* one-sided veto adds 3 more lost positives:
     "2000 Old Warrior" vs "… Shou", plus two hanzi renderings where only one
     side states 生 or Ripe.
2. **A single distinguishing token sits at 0.85–0.95 similarity**, so it
   becomes a false merge:
   - recipe or series codes: 7542 vs 7540, Pt.1 vs Pt.2, Part 1 vs 2, #03 vs #04,
     Grade 2 vs 3, ZH03 vs ZH95
   - "Autumn X" vs "X"
   - Raw vs Ripe, which H.2's guard fixes: 5 FP → 0
   - the year *not at title start*. "Spring 2017 X" vs "Spring 2018 X" scores
     0.94. Year blocking can't fire because both sides parse `None`, and H.2's
     guard uses only the mapper year. **Suggestion for data-engineer:** fall
     back to `parse_name_attributes(...).year` in the guard.

   No threshold in 0.50–0.95 reaches P ≥ 0.98 for products, even with H.2.
3. **The suffix near-duplicate**: `:: Seattle Inventory` drops similarity to
   0.66–0.79. V1 matches 0 of 37 twins. H.2 fixes the 25 same-weight twins
   through `SUFFIX_EXACT`. The other 12 remain blocked by the unparsed weight.
4. **Producer aliases and cross-script names.** Trigram fails on:
   - short forms: "Xiaguan" vs "Xiaguan Tea Factory" scores 0.40
   - abbreviations: Dayi, W2T, CLT, FT, Zhong Cha all score ≤ 0.11
   - added or removed descriptors such as "Brand" or "(Hunan)"
   - hanzi, where similarity is 0.0

   At 0.85, producer recall is 0.059. Precision is perfect, but only because
   almost nothing matches.

Anything that changes labels or the matcher moves these numbers. The tests
deliberately do not pin them. Re-run the harness and update this table.
