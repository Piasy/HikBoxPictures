#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hikbox_pictures.product.make_live_photo_pair import *  # noqa: F403
from hikbox_pictures.product.make_live_photo_pair import main


if __name__ == "__main__":
    raise SystemExit(main())
