"""Runtime storage path helpers for the operational football dashboard."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR_ENV_VAR = "SPORTS_AI_DATA_DIR"
DEFAULT_DASHBOARD_DB_FILENAME = "football_dashboard.sqlite"
DEFAULT_PAPER_TRADING_DB_FILENAME = "football_paper_trading.sqlite"


@dataclass(frozen=True)
class DashboardOperationalPaths:
    data_dir: Path
    dashboard_db_path: Path
    paper_trading_db_path: Path
    mode: str
    persistent_path_configured: bool
    source_env_var: str


def resolve_dashboard_operational_paths(
    repo_root: Path,
    *,
    data_dir_env_var: str = DEFAULT_DATA_DIR_ENV_VAR,
) -> DashboardOperationalPaths:
    raw_data_dir = os.environ.get(data_dir_env_var, "").strip()
    if raw_data_dir:
        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute():
            data_dir = data_dir.resolve()
        return DashboardOperationalPaths(
            data_dir=data_dir,
            dashboard_db_path=data_dir / DEFAULT_DASHBOARD_DB_FILENAME,
            paper_trading_db_path=data_dir / DEFAULT_PAPER_TRADING_DB_FILENAME,
            mode="persistent",
            persistent_path_configured=True,
            source_env_var=data_dir_env_var,
        )

    data_dir = repo_root / "research" / "product_outputs" / "dashboard"
    return DashboardOperationalPaths(
        data_dir=data_dir,
        dashboard_db_path=data_dir / DEFAULT_DASHBOARD_DB_FILENAME,
        paper_trading_db_path=data_dir / DEFAULT_PAPER_TRADING_DB_FILENAME,
        mode="local_repo_default",
        persistent_path_configured=False,
        source_env_var=data_dir_env_var,
    )


def ensure_operational_data_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
