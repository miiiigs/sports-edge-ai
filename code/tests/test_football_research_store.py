import json
import sqlite3

from sports_ai.data_sources.football_history import ingest_historical_csv_text
from sports_ai.storage.football_research_store import (
    connect,
    ensure_schema,
    snapshot_counts,
    store_bundle,
)


SAMPLE_CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR,Referee,HS,AS,HST,AST,HF,AF,HC,AC,HY,AY,HR,AR,B365CH,B365CD,B365CA,PSCH,PSCD,PSCA,MaxCH,MaxCD,MaxCA,AvgCH,AvgCD,AvgCA
E0,08/08/2026,12:30,Alpha FC,Beta FC,2,1,H,1,1,D,Ref One,11,7,5,3,10,12,6,4,2,1,0,0,1.82,3.50,4.50,1.85,3.60,4.40,1.90,3.65,4.55,1.84,3.55,4.42
E0,09/08/2026,15:00,Gamma FC,Delta FC,1,1,D,0,1,A,Ref Two,8,9,4,4,9,8,5,5,3,2,0,1,2.10,3.20,3.60,,,,2.16,3.25,3.70,2.08,3.18,3.58
"""


def build_bundle():
    return ingest_historical_csv_text(
        SAMPLE_CSV,
        season_code="2627",
        division_code="E0",
        source_url="https://example.test/E0.csv",
    )


def test_store_bundle_persists_tables(tmp_path):
    bundle = build_bundle()
    db_path = tmp_path / "football_research.sqlite"

    with connect(db_path) as conn:
        ensure_schema(conn)
        summary = store_bundle(conn, bundle)
        counts = snapshot_counts(conn)
        quote = conn.execute(
            "SELECT selection_prices_json FROM football_market_quotes WHERE bookmaker = 'Bet365' LIMIT 1"
        ).fetchone()

    assert summary["inserted_events"] == 2
    assert summary["inserted_market_quotes"] == 7
    assert counts["football_ingest_runs"] == 1
    assert counts["football_events"] == 2
    assert counts["football_market_quotes"] == 7
    assert counts["football_coverage_snapshots"] == 1
    assert json.loads(quote[0])["home"] == 1.82


def test_store_bundle_avoids_duplicate_events_and_quotes(tmp_path):
    bundle = build_bundle()
    db_path = tmp_path / "football_research.sqlite"

    with connect(db_path) as conn:
        first = store_bundle(conn, bundle)
        second = store_bundle(conn, bundle)
        counts = snapshot_counts(conn)

    assert first["inserted_events"] == 2
    assert first["inserted_market_quotes"] == 7
    assert second["inserted_events"] == 0
    assert second["inserted_market_quotes"] == 0
    assert counts["football_ingest_runs"] == 2
    assert counts["football_events"] == 2
    assert counts["football_market_quotes"] == 7
    assert counts["football_coverage_snapshots"] == 2
