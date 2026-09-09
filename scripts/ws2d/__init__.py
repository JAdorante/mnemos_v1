"""WS2d validation harness (browser URL anchor).

NOT production code. `app/perception/uia_url.py` is the shipped reader; this
package exists to answer the two questions that gate it:

  * Phase -1 — what does requesting accessibility cost the user's browser?
  * Phase 0  — what is the domain precision and recall, per browser?

The URL source is pluggable (`url_sources.py`) so the harness itself can be
built and debugged on Linux against AT-SPI, then run on Windows against the
real UIA reader with only the source swapped. Linux L0 stays out of scope:
nothing here is imported by `app/`.
"""
