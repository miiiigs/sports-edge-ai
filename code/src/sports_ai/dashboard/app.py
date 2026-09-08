"""FastAPI app for the operational football dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import AuthSettings, AuthStore, SESSION_COOKIE_NAME, SESSION_DAYS
from .client_history import build_client_history_payload
from .fixture_provider import FixtureProvider
from ..market_data.providers import MarketProvider as CurrentMarketProvider
from .services import DashboardConfig, DashboardServices


def _template_directory() -> Path:
    return Path(__file__).resolve().parent / "templates"


def _static_directory() -> Path:
    return Path(__file__).resolve().parent / "static"


def _build_query_href(request: Request, *, anchor: str | None = None, **updates: object) -> str:
    params = dict(request.query_params)
    params.pop("partial", None)
    for key, value in updates.items():
        if value in {None, ""}:
            params.pop(key, None)
            continue
        if (key == "page" or key.endswith("_page")) and str(value) == "1":
            params.pop(key, None)
            continue
        params[key] = str(value)
    query = urlencode(params)
    href = request.url.path if not query else f"{request.url.path}?{query}"
    if anchor:
        href = f"{href}#{anchor}"
    return href


def _safe_next_path(value: str | None) -> str:
    candidate = (value or "/").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


async def _urlencoded_form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


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
    auth_settings = AuthSettings.from_env()
    auth_store = AuthStore(auth_settings.db_path)
    if auth_settings.enabled:
        auth_store.initialize()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.dashboard_services = resolved_services
        app.state.auth_store = auth_store
        app.state.auth_settings = auth_settings
        await resolved_services.initialize()
        yield
        await resolved_services.shutdown()

    app = FastAPI(title="Sports Edge AI Football Dashboard", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(_static_directory())), name="static")

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        request.state.user = None
        if not auth_settings.enabled:
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE_NAME)
        user = auth_store.get_user_for_session(token)
        request.state.user = user
        public_path = request.url.path in {"/login", "/signup", "/health"} or request.url.path.startswith("/static/")
        if public_path or user is not None:
            return await call_next(request)

        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)

        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        return RedirectResponse(url=f"/login?{urlencode({'next': next_path})}", status_code=303)

    def _context(request: Request, *, page: str, payload: dict) -> dict:
        return {
            "request": request,
            "page": page,
            "payload": payload,
            "current_user": getattr(request.state, "user", None),
            "query_href": lambda anchor=None, **updates: _build_query_href(request, anchor=anchor, **updates),
            "set_query_param_href": (
                lambda param_name, param_value=None, anchor=None: _build_query_href(
                    request,
                    anchor=anchor,
                    **{str(param_name): param_value},
                )
            ),
        }

    def _auth_context(
        request: Request,
        *,
        mode: str,
        next_path: str,
        error: str | None = None,
        email: str = "",
    ) -> dict:
        return {
            "request": request,
            "mode": mode,
            "next_path": next_path,
            "error": error,
            "email": email,
        }

    def _with_client_history(payload: dict) -> dict:
        """Attach a presentation-only normalized history snapshot without mutating ledger truth."""
        return {
            **payload,
            "client_history": build_client_history_payload(
                paper_db_path=resolved_services.config.paper_trading_db_path,
                dashboard_db_path=resolved_services.config.dashboard_db_path,
                timezone_name=resolved_services.config.display_timezone,
            ),
        }

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str | None = Query(default=None)):
        next_path = _safe_next_path(next)
        if auth_settings.enabled and getattr(request.state, "user", None) is not None:
            return RedirectResponse(next_path, status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context=_auth_context(request, mode="login", next_path=next_path),
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        form = await _urlencoded_form(request)
        email = form.get("email", "")
        password = form.get("password", "")
        next_path = _safe_next_path(form.get("next"))
        user = auth_store.authenticate(email, password) if auth_settings.enabled else None
        if user is None:
            return templates.TemplateResponse(
                request=request,
                name="auth.html",
                context=_auth_context(
                    request,
                    mode="login",
                    next_path=next_path,
                    error="Email or password is incorrect.",
                    email=email,
                ),
                status_code=400,
            )
        token = auth_store.create_session(user["id"])
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=SESSION_DAYS * 24 * 60 * 60,
            httponly=True,
            secure=auth_settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/signup", response_class=HTMLResponse)
    def signup_page(request: Request, next: str | None = Query(default=None)):
        next_path = _safe_next_path(next)
        if auth_settings.enabled and getattr(request.state, "user", None) is not None:
            return RedirectResponse(next_path, status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context=_auth_context(request, mode="signup", next_path=next_path),
        )

    @app.post("/signup", response_class=HTMLResponse)
    async def signup_submit(request: Request):
        form = await _urlencoded_form(request)
        email = form.get("email", "")
        password = form.get("password", "")
        confirm_password = form.get("confirm_password", "")
        next_path = _safe_next_path(form.get("next"))
        if password != confirm_password:
            return templates.TemplateResponse(
                request=request,
                name="auth.html",
                context=_auth_context(
                    request,
                    mode="signup",
                    next_path=next_path,
                    error="Passwords do not match.",
                    email=email,
                ),
                status_code=400,
            )
        try:
            user = auth_store.create_user(email, password)
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="auth.html",
                context=_auth_context(
                    request,
                    mode="signup",
                    next_path=next_path,
                    error=str(exc),
                    email=email,
                ),
                status_code=400,
            )
        token = auth_store.create_session(user["id"])
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=SESSION_DAYS * 24 * 60 * 60,
            httponly=True,
            secure=auth_settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/logout")
    def logout(request: Request):
        auth_store.delete_session(request.cookies.get(SESSION_COOKIE_NAME))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    @app.get("/today", response_class=HTMLResponse)
    def dashboard_home(
        request: Request,
        picks_page: int = Query(default=1, ge=1),
        matches_page: int = Query(default=1, ge=1),
        desk_page: int = Query(default=1, ge=1),
    ):
        payload = _with_client_history(
            resolved_services.get_dashboard_payload(
                picks_page=picks_page,
                matches_page=matches_page,
                desk_page=desk_page,
            )
        )
        template_name = "dashboard_sections_clean.html" if request.query_params.get("partial") == "1" else "dashboard.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="today", payload=payload),
        )

    @app.get("/matches", response_class=HTMLResponse)
    def matches_page(
        request: Request,
        competition: str | None = Query(default=None),
        team: str | None = Query(default=None),
        prediction_status: str | None = Query(default=None),
        status_bucket: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
    ):
        payload = resolved_services.get_matches_payload(
            competition=competition,
            team=team,
            prediction_status=prediction_status,
            status_bucket=status_bucket,
            page=page,
        )
        template_name = "matches_table_clean.html" if request.query_params.get("partial") == "1" else "matches.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="matches", payload=payload),
        )

    @app.get("/matches/{fixture_id}", response_class=HTMLResponse)
    @app.get("/pick-detail/{fixture_id}", response_class=HTMLResponse)
    def match_detail_page(request: Request, fixture_id: str):
        payload = resolved_services.get_fixture_detail_payload(fixture_id)
        if payload is None:
            return HTMLResponse("Fixture not found", status_code=404)
        template_name = "match_detail_content.html" if request.query_params.get("partial") == "1" else "match_detail.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="pick_detail", payload=payload),
        )

    @app.get("/predictions", response_class=HTMLResponse)
    def predictions_page(request: Request, page: int = Query(default=1, ge=1)):
        payload = resolved_services.get_predictions_payload(page=page)
        template_name = "predictions_table.html" if request.query_params.get("partial") == "1" else "predictions.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="predictions", payload=payload),
        )

    @app.get("/candidates", response_class=HTMLResponse)
    @app.get("/betting-desk", response_class=HTMLResponse)
    def candidates_page(request: Request, page: int = Query(default=1, ge=1)):
        payload = resolved_services.get_betting_desk_payload(page=page)
        template_name = "betting_desk_content_clean.html" if request.query_params.get("partial") == "1" else "betting_desk.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="betting_desk", payload=payload),
        )

    @app.get("/performance", response_class=HTMLResponse)
    @app.get("/model-performance", response_class=HTMLResponse)
    def performance_page(request: Request):
        payload = _with_client_history(resolved_services.get_performance_payload())
        template_name = "performance_content_clean.html" if request.query_params.get("partial") == "1" else "performance.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="performance", payload=payload),
        )

    @app.get("/member-card", response_class=HTMLResponse)
    def member_card_page(request: Request, page: int = Query(default=1, ge=1)):
        payload = resolved_services.get_member_card_payload(page=page)
        template_name = "member_card_content.html" if request.query_params.get("partial") == "1" else "member_card.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="member_card", payload=payload),
        )

    @app.get("/segment-explorer", response_class=HTMLResponse)
    def segment_explorer_page(request: Request):
        payload = resolved_services.get_segment_explorer_payload()
        template_name = "segment_explorer_content.html" if request.query_params.get("partial") == "1" else "segment_explorer.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="segment_explorer", payload=payload),
        )

    @app.get("/error-centre", response_class=HTMLResponse)
    def error_centre_page(request: Request):
        payload = resolved_services.get_error_centre_payload()
        template_name = "error_centre_content.html" if request.query_params.get("partial") == "1" else "error_centre.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="error_centre", payload=payload),
        )

    @app.get("/no-bet-archive", response_class=HTMLResponse)
    def no_bet_archive_page(request: Request, page: int = Query(default=1, ge=1)):
        payload = resolved_services.get_no_bet_archive_payload(page=page)
        template_name = "no_bet_archive_content.html" if request.query_params.get("partial") == "1" else "no_bet_archive.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="no_bet_archive", payload=payload),
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request):
        payload = resolved_services.get_reports_payload()
        template_name = "reports_content.html" if request.query_params.get("partial") == "1" else "reports.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="reports", payload=payload),
        )

    @app.get("/status", response_class=HTMLResponse)
    @app.get("/system-status", response_class=HTMLResponse)
    def status_page(request: Request):
        payload = {"system_status": resolved_services.get_system_status()}
        template_name = "status_content.html" if request.query_params.get("partial") == "1" else "status.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_context(request, page="status", payload=payload),
        )

    @app.get("/api/fixtures/live")
    def api_live_fixtures():
        payload = resolved_services.get_dashboard_payload()
        return JSONResponse({"fixtures": payload["live"], "count": len(payload["live"])})

    @app.get("/api/fixtures/today")
    def api_today_fixtures():
        payload = resolved_services.get_dashboard_payload()
        return JSONResponse({"fixtures": payload["today"], "count": len(payload["today"])})

    @app.get("/api/fixtures/upcoming")
    def api_upcoming_fixtures():
        payload = resolved_services.get_dashboard_payload()
        return JSONResponse({"fixtures": payload["upcoming"], "count": len(payload["upcoming"])})

    @app.get("/api/fixtures/next")
    def api_next_fixtures():
        return JSONResponse(resolved_services.get_next_fixtures_payload())

    @app.get("/api/fixtures/{fixture_id}")
    def api_fixture_detail(fixture_id: str):
        payload = resolved_services.get_fixture_detail_payload(fixture_id)
        if payload is None:
            return JSONResponse({"detail": "Fixture not found"}, status_code=404)
        return JSONResponse(payload)

    @app.get("/api/predictions")
    def api_predictions():
        return JSONResponse(resolved_services.get_predictions_payload())

    @app.get("/api/predictions/{fixture_id}")
    def api_prediction_detail(fixture_id: str):
        prediction = resolved_services.get_prediction_payload(fixture_id)
        if prediction is None:
            return JSONResponse({"detail": "Prediction not found"}, status_code=404)
        return JSONResponse(prediction)

    @app.get("/api/candidates")
    def api_candidates():
        return JSONResponse(resolved_services.get_candidates_payload())

    @app.get("/api/performance")
    def api_performance():
        return JSONResponse(_with_client_history(resolved_services.get_performance_payload()))

    @app.get("/api/system/status")
    def api_system_status():
        return JSONResponse(resolved_services.get_system_status())

    @app.get("/api/reconciliation/report")
    def api_reconciliation_report():
        """Deterministic DRY-RUN report (read-only, non-mutating)."""
        return JSONResponse(resolved_services.get_reconciliation_report())

    @app.get("/health")
    def api_health():
        return JSONResponse(resolved_services.get_health_payload())

    @app.post("/api/fixtures/refresh")
    async def api_refresh_fixtures():
        payload = await resolved_services.manual_refresh()
        return JSONResponse(payload)

    return app


app = create_app()
