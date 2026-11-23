"""Learning-to-rank (Phase 6: the dataset export only; no training yet).

backend.ltr.export turns every job with a decision or an outcome into one
labelled row (backend/ltr/export.py, docs/API.md). Training starts once
MIN_LABELLED_OUTCOMES (50) rows exist.
"""
MIN_LABELLED_OUTCOMES = 50
