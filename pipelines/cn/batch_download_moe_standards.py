"""Compatibility shim → `crawlers.cn.batch_download_moe_standards`. Prefer the new path in new code."""
from __future__ import annotations

import runpy
from importlib import import_module

_mod = import_module("crawlers.cn.batch_download_moe_standards")
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("_")})

if __name__ == "__main__":
    runpy.run_module("crawlers.cn.batch_download_moe_standards", run_name="__main__")
