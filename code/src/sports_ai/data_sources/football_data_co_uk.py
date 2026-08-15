"""Helpers for football-data.co.uk historical CSV profiling.

This module is intentionally small. Its job is to help us validate whether a
league-season file is usable for historical football research before we build
larger ingestion or modeling layers on top of it.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

import httpx

BASE_URL = "https://www.football-data.co.uk/mmz4281"
FOOTBALL_DATA_REQUIRED_FIELDS = (
    "Date",
    "HomeTeam",
    "AwayTeam",
    "FTHG",
    "FTAG",
    "FTR",
    "HC",
    "AC",
    "HY",
    "AY",
    "B365CH",
    "B365CD",
    "B365CA",
)


@dataclass(frozen=True)
class FootballCSVProfile:
    """A lightweight profile of one football-data.co.uk CSV."""

    season_code: str
    division_code: str
    source_url: str
    row_count: int
    column_count: int
    first_match_date: str | None
    last_match_date: str | None
    missing_required_fields: tuple[str, ...]
    has_corners: bool
    has_cards: bool
    has_bet365_closing_odds: bool
    has_pinnacle_closing_odds: bool
    column_names: tuple[str, ...]

    def to_json(self) -> str:
        """Serialize the profile for CLI and logging use."""

        return json.dumps(asdict(self), indent=2)


def build_csv_url(season_code: str, division_code: str) -> str:
    """Build the canonical football-data.co.uk CSV URL."""

    if len(season_code) != 4 or not season_code.isdigit():
        raise ValueError("season_code must be a 4-digit value like '2526'")
    if not division_code or not division_code.replace("0", "").isalnum():
        raise ValueError("division_code must look like E0, E1, SC0, etc.")
    return f"{BASE_URL}/{season_code}/{division_code}.csv"


def _unique_missing(headers: Sequence[str], required_fields: Iterable[str]) -> tuple[str, ...]:
    header_set = set(headers)
    missing = [field for field in required_fields if field not in header_set]
    return tuple(missing)


def profile_csv_text(
    csv_text: str,
    *,
    season_code: str,
    division_code: str,
    source_url: str,
    required_fields: Iterable[str] = FOOTBALL_DATA_REQUIRED_FIELDS,
) -> FootballCSVProfile:
    """Profile already-downloaded CSV text."""

    normalized_text = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(normalized_text))
    headers = tuple(reader.fieldnames or ())
    rows = list(reader)
    dates = [row.get("Date") for row in rows if row.get("Date")]
    missing_required_fields = _unique_missing(headers, required_fields)

    return FootballCSVProfile(
        season_code=season_code,
        division_code=division_code,
        source_url=source_url,
        row_count=len(rows),
        column_count=len(headers),
        first_match_date=dates[0] if dates else None,
        last_match_date=dates[-1] if dates else None,
        missing_required_fields=missing_required_fields,
        has_corners=all(field in headers for field in ("HC", "AC")),
        has_cards=all(field in headers for field in ("HY", "AY")),
        has_bet365_closing_odds=all(field in headers for field in ("B365CH", "B365CD", "B365CA")),
        has_pinnacle_closing_odds=all(field in headers for field in ("PSCH", "PSCD", "PSCA")),
        column_names=headers,
    )


def fetch_competition_profile(
    season_code: str,
    division_code: str,
    *,
    required_fields: Iterable[str] = FOOTBALL_DATA_REQUIRED_FIELDS,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> FootballCSVProfile:
    """Download and profile a league-season CSV."""

    source_url = build_csv_url(season_code, division_code)
    close_client = False
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=timeout)
        close_client = True

    try:
        response = client.get(source_url)
        response.raise_for_status()
    finally:
        if close_client:
            client.close()

    return profile_csv_text(
        response.text,
        season_code=season_code,
        division_code=division_code,
        source_url=source_url,
        required_fields=required_fields,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Small CLI to inspect one league-season file."""

    import argparse

    parser = argparse.ArgumentParser(description="Profile football-data.co.uk CSV coverage.")
    parser.add_argument("--season", required=True, help="Season code like 2526 or 2627.")
    parser.add_argument("--division", required=True, help="Division code like E0, E1, E2, E3, SC0.")
    args = parser.parse_args(argv)

    profile = fetch_competition_profile(args.season, args.division)
    print(profile.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
