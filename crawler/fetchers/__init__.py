"""Pluggable fetchers, dispatched by source content type.

The pipeline picks a fetcher based on Source.content_type:
    news / bulletin → html.HTMLFetcher
    rss             → rss.RSSFetcher
    eios            → eios.EIOSFetcher (authenticated; FR-07)
"""
