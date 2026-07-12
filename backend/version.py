"""Single source of truth for version info. Everything that displays a
version (API /healthz, Streamlit footer, release notes) imports from here."""

__version__ = "1.0.0-rc1"

# Bump when scoring weights or gate logic change -- clients and the events
# log use this to distinguish scoring regimes (events also snapshot scores).
FORMULA_VERSION = "1"
