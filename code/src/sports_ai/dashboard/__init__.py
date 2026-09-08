"""Operational football dashboard package."""

from . import app as _app_module
from .football_intelligence_shadow import build_football_intelligence_payload as _build_shadow_intelligence_payload

# Existing Analysis routes resolve this app-module global at request time. Keep
# route structure stable while extending only the read-only intelligence payload.
_app_module.build_football_intelligence_payload = _build_shadow_intelligence_payload

app = _app_module.app
create_app = _app_module.create_app

__all__ = ["app", "create_app"]
