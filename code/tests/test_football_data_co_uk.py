import httpx
import pytest

from sports_ai.data_sources.football_data_co_uk import (
    FOOTBALL_DATA_REQUIRED_FIELDS,
    build_csv_url,
    fetch_competition_profile,
    profile_csv_text,
)


SAMPLE_CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HC,AC,HY,AY,B365CH,B365CD,B365CA,PSCH,PSCD,PSCA
E0,08/08/2026,12:30,Alpha,Beta,2,1,H,7,3,2,1,1.90,3.50,4.20,1.95,3.55,4.10
E0,09/08/2026,15:00,Gamma,Delta,1,1,D,5,5,3,2,2.10,3.20,3.60,2.12,3.18,3.58
"""


def test_build_csv_url():
    assert build_csv_url("2526", "E0") == "https://www.football-data.co.uk/mmz4281/2526/E0.csv"


def test_build_csv_url_rejects_bad_season_code():
    with pytest.raises(ValueError):
        build_csv_url("25", "E0")


def test_profile_csv_text_extracts_coverage_flags():
    profile = profile_csv_text(
        SAMPLE_CSV,
        season_code="2627",
        division_code="E0",
        source_url="https://example.test/E0.csv",
    )

    assert profile.row_count == 2
    assert profile.column_count == 18
    assert profile.first_match_date == "08/08/2026"
    assert profile.last_match_date == "09/08/2026"
    assert profile.has_corners is True
    assert profile.has_cards is True
    assert profile.has_bet365_closing_odds is True
    assert profile.has_pinnacle_closing_odds is True
    assert profile.missing_required_fields == ()


def test_profile_csv_text_reports_missing_fields():
    csv_text = """Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR
E0,08/08/2026,Alpha,Beta,2,1,H
"""
    profile = profile_csv_text(
        csv_text,
        season_code="2627",
        division_code="E0",
        source_url="https://example.test/E0.csv",
        required_fields=FOOTBALL_DATA_REQUIRED_FIELDS,
    )

    assert "HC" in profile.missing_required_fields
    assert "B365CH" in profile.missing_required_fields
    assert profile.has_corners is False
    assert profile.has_cards is False
    assert profile.has_bet365_closing_odds is False
    assert profile.has_pinnacle_closing_odds is False


def test_fetch_competition_profile_uses_http_client():
    class DummyClient:
        def get(self, url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(200, request=request, text=SAMPLE_CSV)

    profile = fetch_competition_profile("2627", "E0", client=DummyClient())
    assert profile.source_url.endswith("/2627/E0.csv")
    assert profile.row_count == 2
