"""Compatibility shim → `crawlers.eu.ingest_esco`. Prefer the new path in new code."""
from __future__ import annotations

import runpy
from importlib import import_module

_mod = import_module("crawlers.eu.ingest_esco")
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("_")})

if __name__ == "__main__":
    runpy.run_module("crawlers.eu.ingest_esco", run_name="__main__")
