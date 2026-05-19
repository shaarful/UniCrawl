"""End-to-end crawl pipeline for a single source (SRS FR-08, FR-11, FR-12, FR-13, FR-23).

Stage order:
    1. fetch          — pull content via the appropriate fetcher.
    2. extract        — extract main text body (HTML parsing / RSS field mapping).
    3. dedup          — URL fingerprint → content hash → semantic similarity (≥ 0.85
                        configurable). Suppress and link to original if matched.
    4. detect language — record source language; do not drop non-English content (FR-12).
    5. keyword filter — match against the active disease list.
    6. persist raw    — write RawArticle with provenance (FR-13).
    7. publish        — push matched records onto the LLM-handoff queue (FR-08
                        end-to-end SLA: ≤ 60 seconds).
"""
from __future__ import annotations


def run_for_source(source_id: int) -> None:
    """Execute the full pipeline for a single source. To be implemented."""
    raise NotImplementedError
