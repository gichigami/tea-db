"""Offline evaluation harnesses (no network, no LLM, no model download).

- :mod:`tea_scrapers.eval.pairs` — schema + loader for the hand-labeled
  canonical-matcher pair set (ROADMAP H.1).
- :mod:`tea_scrapers.eval.matcher_eval` — pairwise P/R/F1 harness with a
  pluggable scorer; ships the V1 trigram baseline (H.1) and is the harness
  H.5 reuses for hybrid threshold calibration.
"""
