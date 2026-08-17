"""Process-local MediaCrawler runtime adjustments loaded through PYTHONPATH."""

from __future__ import annotations

import os


if os.environ.get("MEDIACRAWLER_SUPPRESS_IMAGE_VIEWER") == "1":
    try:
        from PIL import ImageShow

        ImageShow.show = lambda *args, **kwargs: False
    except ImportError:
        pass
