"""Attach Probability Calibration V1 to the existing read-only intelligence payload.

The operational dashboard imports its intelligence builder before this module is
loaded. The dashboard package initializer swaps the app module's builder reference
to this wrapper so existing routes remain unchanged while calibration stays
strictly diagnostic.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from .football_calibration import build_shadow_calibration_payload
from .football_intelligence import (
    _load_normalized_rows,
    build_football_intelligence_payload as build_base_intelligence_payload,
)


def build_football_intelligence_payload(
    *,
    paper_db_path: Path,
    dashboard_db_path: Path,
    timezone_name: str = "Asia/Manila",
    selected_window: str = "30",
    as_of: date | None = None,
) -> dict[str, Any]:
    payload = build_base_intelligence_payload(
        paper_db_path=paper_db_path,
        dashboard_db_path=dashboard_db_path,
        timezone_name=timezone_name,
        selected_window=selected_window,
        as_of=as_of,
    )

    # Keep the prospective shadow candidate anchored to all available normalized
    # evidence so its coefficients do not change merely because the UI window is
    # switched between 7D/30D/90D/All. The ordinary calibration panel above it
    # remains window-specific.
    try:
        normalized_rows, _raw_line_count, _collapsed_line_count = _load_normalized_rows(
            paper_db_path=paper_db_path,
            dashboard_db_path=dashboard_db_path,
        )
        calibration = build_shadow_calibration_payload(normalized_rows)
    except (sqlite3.Error, OSError, ValueError):
        calibration = build_shadow_calibration_payload([])

    return {
        **payload,
        "shadow_calibration": calibration,
    }
