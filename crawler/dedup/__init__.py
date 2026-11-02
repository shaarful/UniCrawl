"""Multi-layer duplicate suppression.

Checks are run in sequence; any positive match suppresses the article:
    1. URL fingerprint
    2. Content hash (sha256 of normalized text)
    3. Semantic similarity (threshold default 0.85, configurable)

Every suppression decision is audit-logged with the duplicate-reference and reason.
"""
