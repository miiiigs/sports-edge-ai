"""Cost-safe API-Football fixture provider optimizations.

The operational football scope is already defined by the configured league map.
API-Football supports a hyphen-separated league filter for its ``live`` query, so
requesting only those leagues avoids downloading and parsing every live match in
the provider feed while preserving the exact supported fixture set and cadence.
"""

from __future__ import annotations

from .fixture_provider import ApiFootballFixtureProvider, FixtureProviderNotConfiguredError
from .models import FIXTURE_STATUS_HALFTIME, FIXTURE_STATUS_LIVE


class ScopedApiFootballFixtureProvider(ApiFootballFixtureProvider):
    """Use one live request scoped to the configured competition IDs.

    A provider-side error on the scoped form falls back to the proven ``live=all``
    request so this optimization can never turn a provider syntax/regression issue
    into missing live data. An ordinary empty successful response does not trigger
    a second call.
    """

    def _configured_live_scope(self) -> str:
        league_ids = sorted(set(int(value) for value in self._league_map().values()))
        return "-".join(str(value) for value in league_ids)

    def get_live_fixtures(self):
        if not self.configured():
            raise FixtureProviderNotConfiguredError("API-Football is not configured")

        live_scope = self._configured_live_scope()
        if not live_scope:
            return []

        rows = self._request_fixtures(params={"live": live_scope})
        if not rows:
            diagnostics = self.sync_diagnostics()
            queries = list(diagnostics.get("queries") or [])
            last_query = queries[-1] if queries else {}
            if last_query.get("errors"):
                return super().get_live_fixtures()

        league_ids = set(self._league_map().values())
        fixtures = []
        for row in rows:
            league_id = int(((row.get("league") or {}).get("id") or 0))
            if league_id not in league_ids:
                continue
            normalized = self._normalize_fixture(row)
            if normalized is not None and normalized.status in {
                FIXTURE_STATUS_LIVE,
                FIXTURE_STATUS_HALFTIME,
            }:
                fixtures.append(normalized)
        return self._dedupe(fixtures)
