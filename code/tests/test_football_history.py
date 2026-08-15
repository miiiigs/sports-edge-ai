import httpx

from sports_ai.data_sources.football_history import (
    bundle_preview,
    export_bundle,
    fetch_historical_bundle,
    ingest_historical_csv_text,
)


SAMPLE_CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR,Referee,HS,AS,HST,AST,HF,AF,HC,AC,HY,AY,HR,AR,B365CH,B365CD,B365CA,PSCH,PSCD,PSCA,MaxCH,MaxCD,MaxCA,AvgCH,AvgCD,AvgCA
E0,08/08/2026,12:30,Alpha FC,Beta FC,2,1,H,1,1,D,Ref One,11,7,5,3,10,12,6,4,2,1,0,0,1.82,3.50,4.50,1.85,3.60,4.40,1.90,3.65,4.55,1.84,3.55,4.42
E0,09/08/2026,15:00,Gamma FC,Delta FC,1,1,D,0,1,A,Ref Two,8,9,4,4,9,8,5,5,3,2,0,1,2.10,3.20,3.60,,,,2.16,3.25,3.70,2.08,3.18,3.58
"""


def test_ingest_historical_csv_text_builds_separated_rows():
    bundle = ingest_historical_csv_text(
        SAMPLE_CSV,
        season_code="2627",
        division_code="E0",
        source_url="https://example.test/E0.csv",
    )

    assert bundle.competition_id == "ENG1"
    assert bundle.coverage_summary.event_count == 2
    assert bundle.coverage_summary.market_row_count == 7
    assert bundle.coverage_summary.missing_profile_fields == ()

    first_sports = bundle.sports_rows[0]
    assert first_sports.event_id == "evt_football_20260808_alpha_fc_beta_fc"
    assert first_sports.match_date == "2026-08-08"
    assert first_sports.home_corners == 6
    assert first_sports.away_yellow_cards == 1

    first_market = bundle.market_rows[0]
    assert first_market.bookmaker == "Bet365"
    assert first_market.selection_prices["home"] == 1.82
    assert first_market.selection_prices["draw"] == 3.5
    assert first_market.selection_prices["away"] == 4.5


def test_ingest_historical_csv_text_reports_coverage():
    partial_csv = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HC,AC,HY,AY,B365CH,B365CD,B365CA
E0,08/08/2026,12:30,Alpha FC,Beta FC,2,1,H,6,4,2,1,1.82,3.50,4.50
"""
    bundle = ingest_historical_csv_text(
        partial_csv,
        season_code="2627",
        division_code="E0",
        source_url="https://example.test/E0.csv",
    )

    assert "Referee" in bundle.coverage_summary.missing_profile_fields
    assert bundle.coverage_summary.key_blind_field_coverage["Referee"] == 0.0
    assert bundle.coverage_summary.key_blind_field_coverage["HC"] == 1.0


def test_bundle_preview_is_compact_and_serializable():
    bundle = ingest_historical_csv_text(
        SAMPLE_CSV,
        season_code="2627",
        division_code="E0",
        source_url="https://example.test/E0.csv",
    )
    preview = bundle_preview(bundle, limit=1)

    assert preview["competition_id"] == "ENG1"
    assert len(preview["sports_preview"]) == 1
    assert len(preview["market_preview"]) == 1


def test_fetch_historical_bundle_uses_http_client():
    class DummyClient:
        def get(self, url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(200, request=request, text=SAMPLE_CSV)

    bundle = fetch_historical_bundle("2627", "E0", client=DummyClient())
    assert bundle.source_url.endswith("/2627/E0.csv")
    assert bundle.coverage_summary.event_count == 2


def test_export_bundle_writes_reusable_files(tmp_path):
    bundle = ingest_historical_csv_text(
        SAMPLE_CSV,
        season_code="2627",
        division_code="E0",
        source_url="https://example.test/E0.csv",
    )
    exported = export_bundle(bundle, tmp_path)

    assert (tmp_path / "sports_rows.jsonl").exists()
    assert (tmp_path / "market_rows.jsonl").exists()
    assert (tmp_path / "coverage_summary.json").exists()
    assert (tmp_path / "bundle_manifest.json").exists()
    assert exported["output_dir"] == str(tmp_path)
