"""FastAPI app for the operational football dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .fixture_provider import FixtureProvider
from ..market_data.providers import MarketProvider as CurrentMarketProvider
from .services import DashboardConfig, DashboardServices


def _template_directory() -> Path:
    return Path(__file__).resolve().parent / "templates"


def _static_directory() -> Path:
    return Path(__file__).resolve().parent / "static"


def create_app(
    config: DashboardConfig | None = None,
    *,
    fixture_provider: FixtureProvider | None = None,
    market_provider: CurrentMarketProvider | None = None,
    services: DashboardServices | None = None,
) -> FastAPI:
    resolved_services = services or DashboardServices(
        config=config,
        fixture_provider=fixture_provider,
        market_provider=market_provider,
    )
    templates = Jinja2Templates(directory=str(_template_directory()))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.dashboard_services = resolved_services
        await resolved_services.initialize()
        yield
        await resolved_services.shutdown()

    app = FastAPI(title="Sports Edge AI Football Dashboard", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(_static_directory())), name="static")

    def _context(request: Request, *, page: str, payload: dict) -> dict:
        return {
            "request": request,
            "page": page,
            "payload": payload,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_home(request: Request):
        payload = resolved_services.get_dashboard_payload()
        template_name = "dashboard_sections.html" if request.query_params.get("partial") == "1" else "dashboard.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="dashboard", payload=payload),
        )

    @app.get("/matches", response_class=HTMLResponse)
    async def matches_page(
        request: Request,
        competition: str | None = Query(default=None),
        team: str | None = Query(default=None),
        prediction_status: str | None = Query(default=None),
        status_bucket: str | None = Query(default=None),
        ):
        payload = resolved_services.get_matches_payload(
            competition=competition,
            team=team,
            prediction_status=prediction_status,
            status_bucket=status_bucket,
        )
        template_name = "matches_table.html" if request.query_params.get("partial") == "1" else "matches.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="matches", payload=payload),
        )

    @app.get("/matches/{fixture_id}", response_class=HTMLResponse)
    async def match_detail_page(request: Request, fixture_id: str):
        payload = resolved_services.get_fixture_detail_payload(fixture_id)
        if payload is None:
            return HTMLResponse("Fixture not found", status_code=404)
        template_name = "match_detail_content.html" if request.query_params.get("partial") == "1" else "match_detail.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="match_detail", payload=payload),
        )

    @app.get("/predictions", response_class=HTMLResponse)
    async def predictions_page(request: Request):
        payload = resolved_services.get_predictions_payload()
        template_name = "predictions_table.html" if request.query_params.get("partial") == "1" else "predictions.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="predictions", payload=payload),
        )

    @app.get("/candidates", response_class=HTMLResponse)
    async def candidates_page(request: Request):
        payload = resolved_services.get_candidates_payload()
        template_name = "candidates_content.html" if request.query_params.get("partial") == "1" else "candidates.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="candidates", payload=payload),
        )

    @app.get("/performance", response_class=HTMLResponse)
    async def performance_page(request: Request):
        payload = resolved_services.get_performance_payload()
        template_name = "performance_content.html" if request.query_params.get("partial") == "1" else "performance.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="performance", payload=payload),
        )

    @app.get("/status", response_class=HTMLResponse)
    @app.get("/system-status", response_class=HTMLResponse)
    async def status_page(request: Request):
        payload = {"system_status": resolved_services.get_system_status()}
        template_name = "status_content.html" if request.query_params.get("partial") == "1" else "status.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="status", payload=payload),
        )

    @app.get("/api/fixtures/live")
    async def api_live_fixtures():
        payload = resolved_services.get_dashboard_payload()
        return JSONResponse({"fixtures": payload["live"], "count": len(payload["live"])})

    @app.get("/api/fixtures/today")
    async def api_today_fixtures():
        payload = resolved_services.get_dashboard_payload()
        return JSONResponse({"fixtures": payload["today"], "count": len(payload["today"])})

    @app.get("/api/fixtures/upcoming")
    async def api_upcoming_fixtures():
        payload = resolved_services.get_dashboard_payload()
        return JSONResponse({"fixtures": payload["upcoming"], "count": len(payload["upcoming"])})

    @app.get("/api/fixtures/next")
    async def api_next_fixtures():
        return JSONResponse(resolved_services.get_next_fixtures_payload())

    @app.get("/api/fixtures/{fixture_id}")
    async def api_fixture_detail(fixture_id: str):
        payload = resolved_services.get_fixture_detail_payload(fixture_id)
        if payload is None:
            return JSONResponse({"detail": "Fixture not found"}, status_code=404)
        return JSONResponse(payload)

    @app.get("/api/predictions")
    async def api_predictions():
        return JSONResponse(resolved_services.get_predictions_payload())

    @app.get("/api/predictions/{fixture_id}")
    async def api_prediction_detail(fixture_id: str):
        prediction = resolved_services.get_prediction_payload(fixture_id)
        if prediction is None:
            return JSONResponse({"detail": "Prediction not found"}, status_code=404)
        return JSONResponse(prediction)

    @app.get("/api/candidates")
    async def api_candidates():
        return JSONResponse(resolved_services.get_candidates_payload())

    @app.get("/api/performance")
    async def api_performance():
        return JSONResponse(resolved_services.get_performance_payload())

    @app.get("/api/system/status")
    async def api_system_status():
        return JSONResponse(resolved_services.get_system_status())

    @app.get("/health")
    async def api_health():
        return JSONResponse(resolved_services.get_health_payload())

    @app.post("/api/fixtures/refresh")
    async def api_refresh_fixtures():
        payload = await resolved_services.manual_refresh()
        return JSONResponse(payload)

    return app


app = create_app()
