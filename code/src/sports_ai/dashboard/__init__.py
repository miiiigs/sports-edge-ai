"""Operational football dashboard package."""

import os

# Quota-safe scheduler defaults for hosted production. Explicit environment
# configuration still wins, so operators can tune these without another deploy.
os.environ.setdefault("SPORTS_AI_FIXTURE_LIVE_REFRESH_SECONDS", "300")
os.environ.setdefault("SPORTS_AI_FIXTURE_FUTURE_REFRESH_SECONDS", "1800")

from . import app as _app_module
from . import services as _services_module
from .football_intelligence_shadow import build_football_intelligence_payload as _build_shadow_intelligence_payload
from .production_decision_policy import (
    current_decision_state as _production_current_decision_state,
    humanize_pick_status as _production_humanize_pick_status,
    safeguard_label as _production_safeguard_label,
    safeguard_reason as _production_safeguard_reason,
)

# Existing Analysis routes resolve this app-module global at request time. Keep
# route structure stable while extending only the read-only intelligence payload.
_app_module.build_football_intelligence_payload = _build_shadow_intelligence_payload

# Context safeguards were added after the original dashboard presentation
# helpers. Patch their module-level policies once for both scheduler and the
# separate low-memory web process so persisted context VETO/BLOCKED states are
# never rendered as a generic PASS.
_services_module._current_decision_state = _production_current_decision_state
_services_module._safeguard_label = _production_safeguard_label
_services_module._safeguard_reason_from_row = _production_safeguard_reason
_services_module._humanize_pick_status = _production_humanize_pick_status

app = _app_module.app
create_app = _app_module.create_app

__all__ = ["app", "create_app"]
