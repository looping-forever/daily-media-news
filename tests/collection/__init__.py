"""tests/collection -- offline tests for the source-truth layer.

Covers scripts/source_health.py (pure derivation), the source-health wiring and
the honesty fixes in validate_stories.py (research_status override, the
anti-fabrication drop gate, the honest data cutoff, the label strip) and the
collector's retry policy in collect_news.py.

No network access anywhere: collect_news.fetch_bytes -- the single network
injection point -- is monkeypatched in every test that exercises the collector.
"""
