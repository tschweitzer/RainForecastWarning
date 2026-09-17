"""FastAPI application: the REST API and the server-rendered pages.

Shape notes that are security decisions rather than style:

* Everything subscriber-scoped is addressed as ``/me`` and resolved from the bearer token. There is
  no object id in any path, so the entire IDOR class does not exist.
* ``POST /api/v1/subscriptions`` answers identically whether or not the address is known, so it
  cannot be used to test whether someone is subscribed.
* Confirm and unsubscribe are **side-effect free on GET**. Mail scanners follow links; a
  state-changing GET means the scanner consumes the token and the real user is told it is already
  used, and on unsubscribe it would silently delete the account (SECURITY_REVIEW.md F-4).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from rainalert import subscriptions as svc
from rainalert.api.mail import confirmation_message, deletion_receipt
from rainalert.api.ratelimit import client_ip, hit_and_check
from rainalert.config import Settings, get_settings
from rainalert.db.models import Subscriber, Subscription, TokenPurpose
from rainalert.db.session import make_engine, make_session_factory
from rainalert.notify import Notifier, build_notifier
from rainalert.tokens import hash_email, verify_unsubscribe_token

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class SubscribeRequest(BaseModel):
    # allow_inf_nan=False: json.loads accepts the bare token NaN, and a NaN latitude that reaches
    # the database is re-evaluated every cycle forever (SECURITY_REVIEW.md F-3).
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    #: EmailStr also rejects RFC 2606 special-use domains (.invalid, .test, .localhost). That is
    #: wanted: an address we can never deliver to is one we should never store, and a bounce we
    #: can predict is a bounce we should not generate.
    email: EmailStr
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class LocationRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


def create_app(
    settings: Settings | None = None,
    session_factory=None,
    notifier: Notifier | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    session_factory = session_factory or make_session_factory(make_engine(settings.database_url))
    notifier = notifier or build_notifier(settings.notifier, settings)

    app = FastAPI(title="RainAlert", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.notifier = notifier

    def get_session():
        with session_factory() as session:
            yield session

    def deliver(message) -> bool:
        """Send, and never let a delivery failure become the caller's problem.

        The state change is already committed by this point, so raising here would 500 a request
        that actually succeeded - and on the subscribe path it would also leak, through the status
        code, whether a mail was attempted at all. A failure is logged and counted; the address is
        not logged.
        """
        try:
            result = app.state.notifier.send(message)
        except Exception:  # delivery must never break the request
            logger.exception("notifier raised while sending %r", message.subject)
            return False
        if not result.ok:
            logger.error("delivery failed: %s", result.error)
        return result.ok

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Report validation failures without echoing the input back.

        FastAPI's default handler includes the offending value, and serialising a NaN raises
        ("Out of range float values are not JSON compliant") - so a bare NaN in the body turned a
        clean 422 into a 500. Dropping `input` and `ctx` fixes that and stops the endpoint
        reflecting arbitrary user input into its own response.
        """
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": [
                    {
                        "loc": list(err.get("loc", ())),
                        "msg": err.get("msg", ""),
                        "type": err.get("type", ""),
                    }
                    for err in exc.errors()
                ]
            },
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        # Without this the token in a confirm URL leaks to any third-party resource the page loads.
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    # ---- health ---------------------------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness only. Deliberately touches nothing: see /readyz."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(session: Session = Depends(get_session)) -> dict[str, str]:
        """Readiness, which by definition opens a database connection.

        That makes it an amplifier: unauthenticated traffic here scales instances and can exhaust
        the connection ceiling the ingest job needs. `--max-instances` is the control, and it is a
        deploy-time setting, not something this handler can fix (SECURITY_REVIEW.md F-6).
        """
        session.execute(sql_text("SELECT 1"))
        return {"status": "ready"}

    # ---- subscribe ------------------------------------------------------------------------
    @app.post("/api/v1/subscriptions", status_code=status.HTTP_202_ACCEPTED)
    def create_subscription(
        payload: SubscribeRequest,
        request: Request,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        ip = client_ip(request, settings.trusted_proxy_hops)
        within_ip = hit_and_check(
            session, f"subscribe:ip:{ip}", settings.subscribe_limit_per_hour, timedelta(hours=1)
        )
        within_email = hit_and_check(
            session,
            f"subscribe:email:{hash_email(payload.email).hex()}",
            settings.subscribe_limit_per_hour,
            timedelta(hours=1),
        )
        if not (within_ip and within_email):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts, try later")

        try:
            result = svc.subscribe(
                session,
                settings,
                email=payload.email,
                lat=payload.lat,
                lon=payload.lon,
                client_ip=ip,
                user_agent=request.headers.get("user-agent"),
            )
        except svc.ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

        if result.confirm_token:
            deliver(confirmation_message(settings, payload.email, result.confirm_token))
        # Identical response either way: no account enumeration.
        return JSONResponse({"status": "check your email"}, status_code=status.HTTP_202_ACCEPTED)

    # ---- confirm --------------------------------------------------------------------------
    @app.get("/confirm", response_class=HTMLResponse, include_in_schema=False)
    def confirm_page(request: Request, token: str = "") -> HTMLResponse:
        """Renders a button. Changes nothing - a mail scanner may follow this freely."""
        return TEMPLATES.TemplateResponse(
            request, "confirm.html", {"token": token, "settings": settings}
        )

    @app.post("/confirm", response_class=HTMLResponse, include_in_schema=False)
    def confirm_submit(
        request: Request, token: str = Form(""), session: Session = Depends(get_session)
    ) -> HTMLResponse:
        try:
            result = svc.confirm(session, settings, token=token)
        except svc.ValidationError as exc:
            return TEMPLATES.TemplateResponse(
                request, "error.html", {"message": str(exc), "settings": settings}, status_code=400
            )
        return TEMPLATES.TemplateResponse(
            request,
            "confirmed.html",
            {
                "api_token": result.api_token,
                "unsubscribe_token": result.unsubscribe_token,
                "settings": settings,
            },
        )

    # ---- authenticated API ----------------------------------------------------------------
    def current_subscriber(
        request: Request, session: Session = Depends(get_session)
    ) -> tuple[Subscriber, Session]:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        try:
            subscriber = svc.resolve_token(session, token=header[7:], purpose=TokenPurpose.API)
        except svc.ValidationError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised") from exc
        return subscriber, session

    @app.get("/api/v1/subscriptions/me")
    def read_me(current=Depends(current_subscriber)) -> dict:
        subscriber, session = current
        sub = session.query(Subscription).filter_by(subscriber_id=subscriber.id).one()
        return {
            "email": subscriber.email,
            "status": sub.status.value,
            "lat": sub.lat,
            "lon": sub.lon,
            "location_updated_at": sub.location_updated_at.isoformat(),
            "radius_m": sub.radius_m,
            "threshold_mm_5min": float(sub.threshold_mm_5min),
            "lead_time_minutes": sub.lead_time_minutes,
            "timezone": sub.timezone,
            "health_note": sub.health_note,
        }

    @app.put("/api/v1/subscriptions/me/location", status_code=status.HTTP_204_NO_CONTENT)
    def put_location(
        payload: LocationRequest, request: Request, current=Depends(current_subscriber)
    ) -> Response:
        subscriber, session = current
        ip = client_ip(request, settings.trusted_proxy_hops)
        if not hit_and_check(
            session, f"location:ip:{ip}", settings.location_limit_per_hour, timedelta(hours=1)
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many updates")
        try:
            svc.update_location(session, subscriber, lat=payload.lat, lon=payload.lon)
        except svc.ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.delete("/api/v1/subscriptions/me", status_code=status.HTTP_204_NO_CONTENT)
    def delete_me(current=Depends(current_subscriber)) -> Response:
        subscriber, session = current
        email = subscriber.email
        svc.delete_subscriber(session, subscriber)
        deliver(deletion_receipt(settings, email))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ---- unsubscribe ----------------------------------------------------------------------
    @app.get("/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
    def unsubscribe_page(request: Request, token: str = "") -> HTMLResponse:
        """Side-effect free: a prefetching client must not be able to delete an account."""
        return TEMPLATES.TemplateResponse(
            request, "unsubscribe.html", {"token": token, "settings": settings}
        )

    @app.post("/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
    def unsubscribe_submit(
        request: Request, token: str = Form(""), session: Session = Depends(get_session)
    ) -> HTMLResponse:
        subscriber_id = verify_unsubscribe_token(token, settings.secret_key)
        subscriber = session.get(Subscriber, subscriber_id) if subscriber_id else None
        if subscriber is None:
            return TEMPLATES.TemplateResponse(
                request,
                "error.html",
                {"message": "Dieser Abmeldelink ist nicht gültig.", "settings": settings},
                status_code=400,
            )
        email = subscriber.email
        svc.delete_subscriber(session, subscriber)
        deliver(deletion_receipt(settings, email))
        return TEMPLATES.TemplateResponse(request, "unsubscribed.html", {"settings": settings})

    # ---- pages ----------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "index.html", {"settings": settings})

    @app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
    def privacy(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "privacy.html", {"settings": settings})

    return app
