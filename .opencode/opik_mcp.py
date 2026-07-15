#!/usr/bin/env python3
"""Start opik-mcp with credentials from ~/.opik.config."""

from __future__ import annotations

import configparser
import os
from pathlib import Path


CONFIG_PATH = Path.home() / ".opik.config"
UVX_PATH = "/home/arkadiy915/.local/bin/uvx"


def main() -> None:
    parser = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        parser.read(CONFIG_PATH)
        if parser.has_section("opik"):
            opik = parser["opik"]
            if opik.get("api_key"):
                os.environ.setdefault("OPIK_API_KEY", opik["api_key"])
            if opik.get("workspace"):
                os.environ.setdefault("OPIK_WORKSPACE", opik["workspace"])
            url_override = opik.get("url_override")
            if url_override and "www.comet.com/opik/api" not in url_override:
                os.environ.setdefault("COMET_URL_OVERRIDE", url_override)

    os.execv(UVX_PATH, [UVX_PATH, "opik-mcp"])


if __name__ == "__main__":
    main()
