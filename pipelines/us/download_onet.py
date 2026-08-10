"""Compatibility shim → `crawlers.us.download_onet`. Prefer the new path in new code."""
from __future__ import annotations

import runpy
from importlib import import_module

_mod = import_module("crawlers.us.download_onet")
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("_")})

if __name__ == "__main__":
    runpy.run_module("crawlers.us.download_onet", run_name="__main__")
