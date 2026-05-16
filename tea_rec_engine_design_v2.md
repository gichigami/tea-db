# Tea Recommendation Engine: Design Document

**Status:** Draft v2 (historical catalog in scope)
**Author:** Gary Johnson (with assistant)
**Last updated:** May 2026
**Changes from v1:** Historical/discontinued products moved into V1 scope. Data quality tiering added. Dual-surface UI model (shoppable + reference). Steepster join promoted from V2 to V1. Plan extended from 2 to 3 weeks.

---

## Abstract

A precision tea recommendation engine combining a **shoppable surface** (currently-available products from premium English-language vendors) with a **reference surface** (historical catalog including discontinued and legendary teas). The goal is to match the per-product analytical depth of expert recommendations, the kind that distinguishes a $20 commodity-grade Pu'er tin from a 2009 Menghai gōng tíng cake, and generalize that rigor across tea traditions (Chinese pu'er and oolong, Japanese green, etc.) with enough transparency to excite the data-curious tea drinker.

The defining product idea: every recommendation is **traceable to source phrasing**, every badge is **explainable by axis**, every comparison reveals **structured difference**. Historical products provide both training data for the recommender and reference value to enthusiasts looking up legendary cakes.

---

## 1. Scope

**In scope (V1):**
- All products from premium English-language Shopify vendors (current AND discontinued)
- Stratified data quality tiers (see §3)
- Pu'er (shēng and shóu), oolong, white, green, black, hēi chá
- Single-tea profiles, similarity search, cross-vendor comparison
- Recommendation by palate similarity, region/style preference, value tier
- **"Find currently-available similar to [discontinued tea]"** pivot feature
- Steepster scrape and producer-level join (promoted from V2)

**Out of scope (V1):**
- Secondary-market pricing (eBay, Taobao, Yahoo Japan)
- Live community discussion ingestion (Reddit live, Discord)
- Direct ecommerce / fulfillment / checkout

**Future scope (V2+):**
- Counterfeit/authenticity modeling (requires Taobao integration)
- Cross-tradition latent space (e.g. shóu pu'er ↔ aged sencha similarity)
- Aging trajectory prediction with confidence intervals
- Tier D reference catalog expansion (curated legendary cakes outside any vendor)

---

## 2. Data Sources

### Tier 1: Core Shopify (Week 1)

All products including unavailable, via `/products.json?limit=250&page=N`. Availability captured as state, not used as filter.

| Vendor | All-time SKUs | Active SKUs | Effort | Notes |
|---|---|---|---|---|
| Yunnan Sourcing (.us + .com) | ~5000+ per site | ~2000+ per site | ~1.5 days | ~30% overlap, want both |
| white2tea | ~600 (since 2014) | ~250 | ~3 hr | Reference-grade descriptions |
| Crimson Lotus Tea | ~800 | ~400 | ~3 hr | Aged-shēng coverage |
| Bitterleaf Teas | ~400 | ~200 | ~3 hr | Young shēng coverage |

**Tier 1 deliverable:** ~12000 unique catalog products (current + historical) with structured catalog data and vendor prose.

### Tier 2: Community Reviews + Critical (Week 2)

Now V1-scope. Provides the training signal for cold-start on current products.

- **Steepster**: ~50k teas, ~500k+ tasting notes. Scrape (~2-3 days), then join to catalog at producer + style level (~2-3 days). Fuzzy matcher + LLM tiebreaker for the ambiguous 10%.
- **TeaDB.org**: ~500 critical posts, WordPress backend. ~2 days scrape + ~2-3 days NLP extraction. Many cover discontinued cakes that you now have catalog entries for, so they link properly.
- **Reddit /r/puer + /r/tea**: PRAW with date-windowed queries. ~1-2 days.

### Tier 3: Catalog Breadth (Week 3-4)

- **Yunomi**: Japanese tea marketplace, ~1500 SKUs, ~1 day with Japanese-specific extraction
- **Liquid Proust**: ~150 SKUs, Shopify, ~2 hr
- **Mei Leaf**: custom site, smaller catalog but unique flavor wheel metadata, ~1 day

### Tier 4: Targeted Fills (Month 2)

- Ippodo, Hibiki-an, Maeda-en, Den's (premium Japanese, ~4-5 days total)
- Adagio, Harney (Western-friendly with user reviews, ~2-3 days)

### Tier 5: Tier D Reference Ingestion (V2)

For legendary cakes outside any current vendor catalog. Sourced from TeaDB deep-dives, books, archived forums (TeaChat, Half-Dipper), auction house catalogs. No structured data; treat as read-only reference entries with curated metadata. Defer unless a specific reference need surfaces.

### Tier 6: Skip Unless Critical

Taobao/Tmall, eBay sold listings, Yahoo Japan Auctions, Discord servers, academic literature.

### Cross-Cutting Constraints

- Respect robots.txt; identifiable User-Agent with project contact info
- Shopify endpoints tolerate ~2 req/sec without complaint
- Daily snapshot model: fact-table inserts for price/inventory state, dimension upserts for product attributes
- SCD Type 2 on slowly-changing dimensions (region renames, reformulations)

---

## 3. Dimensional Model

Star schema, fact table per snapshot, dimension tables for product attributes.

### Hard Dimensions

- **Type**: white, green, yellow, oolong, black, dark
- **Style**: Yancha, Anxi Tieguanyin, Dancong, Sencha, Gyokuro, Shēng, Shóu, etc.
- **Origin hierarchy**: Country → Province → County → Mountain → Village → Garden. SCD Type 2.
- **Cultivar**: Yabukita, Mei Zhan, Jin Xuan, Yúnnán Dà Yè Zhǒng, etc.
- **Harvest**: year, season, flush, pre/post-Qīngmíng (明前/雨前) flags
- **Processing**: oxidation %, roast level, wet-pile parameters, shading days, steaming intensity
- **Producer**: factory/farmer/brand with reputation tier as derived attribute
- **Format**: loose, cake, brick, tuocha, ball, bagged, with weight per unit
- **Grade**: vendor-assigned, normalized to a common ordinal where possible
- **Storage** (aged only): years aged, storage region, dry vs humid
- **Price**: per gram and per gōngfū session, with currency and date
- **Data quality tier**: enum {A_current, B_recent_discontinued, C_archived, D_reference}
- **Availability state**: enum {available, sold_out, archived, never_for_sale}, time-varying via fact-table snapshots

### Data Quality Tiers

- **Tier A**: current vendor products. Full structured data, fresh vendor description.
- **Tier B**: recently discontinued vendor products. Full data + vendor description + strong community signal. The transfer-learning sweet spot.
- **Tier C**: archived vendor products (2009-2020 YS, etc.). Lower-quality descriptions, often shorter. Community signal varies.
- **Tier D**: legendary/reference cakes outside any current vendor catalog. Read-only, curated metadata. V2.

### Soft Dimensions

Modeled separately because they aren't really "flavor":
- **Flavor**: see §4
- **Mouthfeel** (5 axes, 0-5): thickness, astringency, coating, cooling, texture
- **Hóu yùn** (喉韵): 0-5
- **Huí gān** (回甘): 0-5
- **Shēng jīn** (生津): 0-5
- **Chá qì** (茶气) vector: warming ↔ cooling, stimulating ↔ calming, heady ↔ bodied
- **Aging state**: enum {young, transitional, mature, fully_aged, declining}

### User-Side Dimensions

Experience tier, brewing setup, budget per session AND per gram, cultural framing preference, personal palate vector.

---

## 4. Vocabulary Normalization

Three-level flavor hierarchy plus separate axes for the sensory concepts that aren't really "flavor."

### Flavor hierarchy

**L1 macro categories** (8-10, color/sort keys for badges):
Earth/Mineral, Wood/Bark, Sweet/Sugar, Floral, Fruit, Vegetal/Herbaceous, Roast/Smoke, Marine/Umami, Medicinal/Spice, Aged/Resin.

**L2 descriptor families:**
- Sweet: honey, cane sugar, brown sugar, caramel, malt, vanilla, longan
- Wood: cedar, sandalwood, oak, fresh-cut, old library, camphor
- Earth: wet stone, petrichor, loam, forest floor, mushroom

**L3 specific descriptors** (leaf nodes, the surface vocabulary):
- Honey → "clover honey", "wildflower honey", "dark honey", "longan honey"

Each L3 maps to exactly one L2, which maps to one L1.

```python
{
  "id": "honey_longan",
  "label_en": "longan honey",
  "label_zh": "桂圆蜜香",
  "label_zh_pinyin": "guì yuán mì xiāng",
  "parent": "honey",
  "synonyms": ["dried longan sweetness", "Chinese date honey"],
  "tradition_hints": ["aged_sheng", "shou"]
}
```

### Why hierarchical and multilingual

Chinese tea vocabulary doesn't map 1:1 to English. 回甘, 喉韵, 山韵, 岩韵, 蜜香 each have specific meaning that gets diluted in translation. The ontology must support multiple lexical surfaces over shared conceptual structure, or it loses the precision we're after.

### Extraction schema

Per-tea LLM extraction with structured output:

```python
class TeaProfile(BaseModel):
    flavor_tags: list[FlavorTag]  # {l3_id, intensity: 1-5, confidence: 0-1}
    mouthfeel: dict[str, int]
    hou_yun: int | None
    hui_gan: int | None
    sheng_jin: int | None
    cha_qi: dict[str, int]
    aging_state: AgingState
    quote_evidence: dict[str, str]  # field_id → source sentence
```

The `quote_evidence` field is critical. Every badge is traceable to the source phrase that produced it.

### Vendor description quality calibration

Now achievable in V1 because you have historical vendor products joined to community ground truth on Steepster. Per producer, derive reliability score by comparing description language to independent community consensus on their discontinued products. Apply confidence as weighting on their current descriptions.

White2tea calibrates high; an unknown vendor calibrates low until proven otherwise. This was deferred in v1; now it's a Week 2 deliverable.

---

## 5. Display

### Two surfaces

**Shoppable (default).** Filters default to `available: true`. Recommendations rank only currently-purchasable products. This is the V1 primary surface.

**Reference (toggle).** "Show all" surfaces the historical catalog. Greyed-out card treatment for discontinued items. Click any historical tea → profile + reviews + "find similar in-stock alternative."

### The tea card

1. **Hero badge row** (3-5 tags): top flavor descriptors, color-coded by L1 category. Each pill shows descriptor name plus a 5-dot intensity indicator.
2. **Mouthfeel mini-pill**: condensed multi-attribute render. Tap to expand.
3. **Qì badge cluster**: small color-coded icons (cooling, warming, stimulating, calming). Multiple can coexist.
4. **Aging state pill**: "Young, primed to age" / "Transitional (5yr)" / "Mature (12yr)" / "Vintage (25yr+)".
5. **Provenance line**: producer + mountain/village + year + format, structured fields not prose.
6. **Quote evidence on tap**: any badge → source phrase.
7. **Data quality indicator.** Small confidence badge showing profile depth: "vendor + 23 community notes + 1 critical review" = high; "vendor description only" = lower.
8. **Availability state pill** (for non-current). "Sold out (Mar 2024)" / "Discontinued" / "Reference only." Click → "see currently-available alternatives."

### Comparison views

**Overlay radar chart.** Up to 5 teas on the same 8-12 axis chart. One color per tea.

**Sensory matrix.** Rows = axes, columns = selected teas. Cells are horizontal bars sized to intensity. Auto-highlight deltas > 2.

**Similarity galaxy.** UMAP projection of the catalog as 2D scatter, color-coded by type. Click a tea, neighborhood lights up.

**Vendor consensus chart.** When multi-source data exists (vendor + Steepster + TeaDB), show alignment as stacked horizontal bars per axis.

**Aging trajectory curve.** For pu'er: 30-year horizontal timeline showing predicted flavor evolution drawn from transfer-learning data. Drag slider to see future projection.

**Historical → current pivot.** Select a discontinued tea, see ranked list of currently-available teas with highest structured similarity. Card grid shows the comparison radar overlay between the historical reference and each in-stock candidate. **The killer feature.**

### Filter UI

- Numeric range sliders per axis
- "More like this" pivot from any tea
- Saved palate profiles
- Boolean filters for processing flags (gǔ shù, single-origin, wild-arbor, naturally-grown)
- Toggle for "show discontinued"
- Filter for data quality tier minimum

---

## 6. Differentiating Features

1. **Quote evidence on every badge.** Nothing opaque. Tap, see the source. Trust comes from transparency.
2. **Vendor reliability scoring.** Small confidence indicator on every product, derived from vendor language calibrated against community ground truth on their older products.
3. **Personal palate vector.** After 5-10 ratings, surface the user's vector explicitly: "you skew high on cocoa/earth/cooling, low on floral/grassy." They didn't know that about themselves.
4. **"Why this rec" annotation.** Each recommendation explains itself in axis terms.
5. **Aging prediction with confidence intervals.** "Likely moves toward camphor and dried date over 10-15 years, 60% confidence, based on 23 similar profiles."
6. **Generative flavor fingerprint.** Unique SVG signature per tea, deterministically derived from the structured profile. Same profile, same fingerprint. The marketing-lead feature.
7. **Discontinued tea reference + alternative finder.** Users can look up famous/legendary teas they can't buy, read about them, pivot to similar in-stock alternatives ranked by structured similarity. The Vivino-for-tea move.
8. **Producer body-of-work view.** Aggregate view of a vendor's full output over time, current and historical. Style evolution, consistent strengths, failures. No current tea site does this.

---

## 7. Hard Problems

1. **Vocabulary normalization across languages and traditions.** Embedding clustering plus expert curation. 2-3 months of labeling work even with LLM assistance.
2. **Vendor copy trust calibration.** Now achievable in V1 thanks to historical join.
3. **Cold start on individual products.** Largely solved by transfer learning from historical analogues. A new 2025 Bulang shóu inherits signal from the producer's 2018 and 2020 Bulang shóu.
4. **Cross-tradition recommendation.** Bridge has to be mouthfeel + intensity + qì vectors in a learned latent space.
5. **Data quality stratification.** Model and UI both need to be tier-aware. Lower confidence indicators on Tier C/D entries, but they remain queryable.
6. **Canonical product ID matching across years.** 15 years of inconsistent vendor naming. Composite key (producer, year, name normalized, weight) plus fuzzy fallback. Build it day 1.

---

## 8. V1 Implementation Plan (3 weeks)

| Week | Task | Effort |
|---|---|---|
| 1.1 | Tier 1 Shopify scrape, all products (current + historical) | ~2 days |
| 1.2 | Canonical product ID matching across years and vendors | ~1 day |
| 1.3 | Daily snapshot job → `product_snapshot` fact table | ~1 day |
| 1.4 | LLM extraction validated against ~200 hand-labeled set | ~3 days |
| 2.1 | Steepster scrape | ~2-3 days |
| 2.2 | Steepster → catalog join at producer + style level | ~2 days |
| 2.3 | TeaDB scrape + NLP extraction | ~2-3 days |
| 2.4 | Vendor reliability scoring | ~1 day |
| 2.5 | Description embeddings (text-embedding-3-large) → pgvector | ~1 day |
| 3.1 | Recommender v1: filter + soft scoring | ~2 days |
| 3.2 | UI: tea card with badges, dual surfaces (shoppable + reference) | ~2 days |
| 3.3 | Historical → current pivot feature | ~1 day |
| 3.4 | Comparison view (radar overlay) | ~1 day |

**V1 deliverable**: working recommender over ~12000 catalog products (~2000-3000 currently-buyable), daily-fresh inventory state, with badge display, comparison view, and the "find current alternative to discontinued tea" feature.

---

## 9. Architecture Sketch

```
[Shopify endpoints]  ────┐
[Steepster]              │
[TeaDB]                  ├──→ [ingest workers] ──→ [raw_product_snapshot]
[Reddit]                 │                                  │
                         │                                  ↓
                         │                          [LLM extraction]
                         │                                  │
                         │                                  ↓
                         │             [structured_product (SCD Type 2)]
                         │                                  │
                         │                                  ↓
                         │                          [embedding pipeline]
                         │                                  │
                         │                                  ↓
                         │                            [pgvector index]
                         │                                  │
                         │                                  ↓
                         └──→ [community signal] ──→ [reliability scoring]
                                                            │
                                                            ↓
                                                   [recommender service]
                                                   ├── shoppable mode (filter on available)
                                                   └── reference mode (no filter)
                                                            │
                                                            ↓
                                                          [UI]
```

The recommender service distinguishes between shoppable mode (hard filter on availability) and reference mode (no availability filter). Both share the same underlying similarity computation. The historical → current pivot is a special query: start in reference mode, then constrain candidate set to `available: true` for the ranked results.

---

## 10. Open Questions

- **Build vs buy on the ontology**: curate a tea-specific one or fork/extend an existing flavor lexicon (SCA-style, wine lexicons)? Currently leaning curate.
- **Embedding model**: API (text-embedding-3-large) or local (BGE-large)? API for V1, revisit for cost as volume scales.
- **Privacy/auth model for personal palate vector storage**: TBD.
- **Vendor permission**: proactive contact with YS/white2tea, or rely on robots.txt and public endpoints? Probably proactive, this is a small world. Especially when ingesting full historical catalogs.
- **Display platform**: web (Next.js?), iOS (Chaizi stack), or both? Likely web for V1; iOS opportunity once palate vector model is proven.
- **Tier D ingestion strategy**: how aggressive on legendary cake reference data? V1 covers vendor history; V2 might want curated manual entries for famous teas outside any vendor system. Could be a Notion-style community curation surface.
- **Availability state freshness budget**: daily snapshots are cheap on Shopify endpoints, but recommender invalidation cost rises with catalog growth. Plan for it.

---

## 11. Glossary

- **Cha qi** (茶气, chá qì): bodily/energetic effect of a tea
- **Hui gan** (回甘, huí gān): sweet return in the throat after sipping
- **Sheng jin** (生津, shēng jīn): mouth-watering response
- **Hou yun** (喉韵, hóu yùn): throat sensation, sense of tea "going down well"
- **Shan yun** (山韵, shān yùn): mountain character, terroir-driven
- **Yan yun** (岩韵, yán yùn): rock character, Wuyi yancha specific
- **Sheng / Shou** (生 / 熟): raw / ripe pu'er
- **Wo dui / Dui wei** (渥堆 / 堆味): wet-pile process / its lingering funk
- **Gu shu** (古树, gǔ shù): old tree, 100+ years
- **Gong ting** (宫廷, gōng tíng): imperial grade shóu (tiny tippy buds)
- **Mao cha** (毛茶, máo chá): loose unpressed leaf, pre-compression
- **Bing cha** (饼茶, bǐng chá): pressed cake, 357g typical
- **Tuo cha** (沱茶, tuó chá): dome/bowl-shaped compression
- **Long zhu** (龙珠, lóng zhū): dragon ball, 7-8g pressed nugget
