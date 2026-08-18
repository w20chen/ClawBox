"""ClawTune research pipeline: observation -> KB -> prediction (ADR-008).

Self-contained, local-only package for building the paper's data pipeline from
real tool-execution traces.  No run/lease lifecycle is involved: it consumes
ClawTune v6 span JSONL + the tool-bridge execution JSONL, validates observations,
joins them by execution_id (exact, no time window), builds an offline dataset,
fits estimators, and produces immutable, provenance-tracked KB snapshots.
"""
