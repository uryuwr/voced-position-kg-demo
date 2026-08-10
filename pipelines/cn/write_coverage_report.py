"""Compatibility shim → `crawlers.cn.write_coverage_report`. Prefer the new path in new code."""
from __future__ import annotations

import runpy
from importlib import import_module

_mod = import_module("crawlers.cn.write_coverage_report")
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("_")})

if __name__ == "__main__":
    runpy.run_module("crawlers.cn.write_coverage_report", run_name="__main__")
