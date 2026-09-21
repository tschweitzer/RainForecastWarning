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
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from rainalert import subscriptions as svc
from rainalert.api.mail import confirmation_message, deletion_receipt, manage_link_message
from rainalert.api.metrics import render as render_metrics
from rainalert.api.ratelimit import client_ip, hit_and_check
from rainalert.config import Settings, get_settings
from rainalert.db.models import Channel, Subscriber, Subscription, TokenPurpose
from rainalert.db.schema import schema_complaint
from rainalert.db.session import make_engine, make_session_factory
from rainalert.notify import Notifier, build_notifier
from rainalert.radar.overlay import LAYER_OPACITY, legend
from rainalert.storage import GCSOverlayStore, LocalOverlayStore, OverlayStore
from rainalert.timeline import build_timeline
from rainalert.tokens import (
    csrf_token,
    hash_address,
    session_token,
    verify_csrf_token,
    verify_session_token,
    verify_unsubscribe_token,
)

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

#: Leaflet is loaded from a CDN because this environment cannot vendor it. That means every
#: visitor's browser tells unpkg.com their IP, which sits badly with a service whose whole
#: privacy story is data minimisation. **Vendor Leaflet into static/ before deploying** (M6) and
#: drop these two origins back to 'self'.
MAP_SCRIPT_SRC = "https://unpkg.com"

#: The settings-page session. Not prefixed `__Host-`, which would be the stronger choice, because
#: that prefix requires Secure and this service is served over plain http in development - a
#: cookie the browser silently refuses to store is a page that silently never logs in.
MANAGE_COOKIE = "rainalert_manage"
#: Echoed back on every write from the settings page. A custom header cannot be set by a plain
#: cross-site form, so requiring one already forces a preflight; the value being unguessable is
#: what makes the preflight pointless to attempt (SECURITY_REVIEW.md F-16).
CSRF_HEADER = "X-Rain-CSRF"


def tile_origin(tile_url: str) -> str:
    """The one origin img-src should allow for basemap tiles, or nothing.

    Derived from the configured template rather than hard-coded, so the policy can never be
    broader than the provider actually in use - and is empty when there is no provider, which is
    the default. A tile server sees every pan and zoom, so this is worth keeping narrow.
    """
    if not tile_url:
        return ""
    parsed = urlparse(tile_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    host = parsed.netloc
    # {s}.tiles.example.com is Leaflet's subdomain placeholder; allow the siblings, not the web.
    if host.startswith("{s}."):
        return f"{parsed.scheme}://*.{host[4:]}"
    return f"{parsed.scheme}://{host}"


#: Decimal places kept for a stored coordinate. Four is about 11 m, which is already far finer
#: than anything the service can act on: it samples a radius mask on a 1 km radar grid, so even
#: 100 m cannot change an answer. A phone reports seven decimals and a paste from a mapping site
#: often carries six; keeping them would be storing precise personal location data that no part of
#: this system reads. Rounding happens here, at the edge, so no route can store more by accident
#: (GDPR data minimisation, DESIGN.md 13).
COORD_DECIMALS = 4


def _round_coord(value: float) -> float:
    return round(value, COORD_DECIMALS)


class SubscribeRequest(BaseModel):
    # allow_inf_nan=False: json.loads accepts the bare token NaN, and a NaN latitude that reaches
    # the database is re-evaluated every cycle forever (SECURITY_REVIEW.md F-3).
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    #: EmailStr also rejects RFC 2606 special-use domains (.invalid, .test, .localhost). That is
    #: wanted: an address we can never deliver to is one we should never store, and a bounce we
    #: can predict is a bounce we should not generate.
    #: Required for email, absent for ntfy - the topic is generated by the service, never
    #: taken from the request, because a guessable topic is a location leak (subscriptions
    #: .new_ntfy_topic).
    email: EmailStr | None = None
    channel: Literal["email", "ntfy"] = "email"
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    _round = field_validator("lat", "lon")(_round_coord)

    @model_validator(mode="after")
    def _address_matches_channel(self) -> SubscribeRequest:
        if self.channel == "email" and not self.email:
            raise ValueError("email is required for the email channel")
        if self.channel == "ntfy" and self.email:
            # Refused rather than ignored: silently dropping an address someone supplied is how
            # they end up believing it was stored.
            raise ValueError("the ntfy channel does not take an email address")
        return self


class LocationRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    _round = field_validator("lat", "lon")(_round_coord)


class RuleRequest(BaseModel):
    """A partial update: every field optional, absent means "leave it alone".

    The real bounds live in `svc.validate_rule`, which reads them from settings - these are only
    the outer sanity limits, so that a number far outside any conceivable range is refused before
    it reaches a float conversion. Repeating the exact bounds here would be two places to change.
    """

    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    threshold_mm_5min: float | None = Field(default=None, gt=0, le=1000)
    lead_time_minutes: int | None = Field(default=None, ge=0, le=1000)
    radius_m: int | None = Field(default=None, ge=0, le=100_000)


class ManageLinkRequest(BaseModel):
    """Who to send a settings link to, in the same shape the subscribe form uses."""

    model_config = ConfigDict(extra="forbid")

    channel: Literal["email", "ntfy"] = "email"
    address: str = Field(min_length=1, max_length=254)


def create_app(
    settings: Settings | None = None,
    session_factory=None,
    notifier: Notifier | None = None,
    overlay_store: OverlayStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    session_factory = session_factory or make_session_factory(make_engine(settings.database_url))
    notifier = notifier or build_notifier(settings.notifier, settings)
    if overlay_store is None and settings.overlay_dir:
        overlay_store = LocalOverlayStore(settings.overlay_dir)
    elif overlay_store is None and settings.overlay_bucket:
        overlay_store = GCSOverlayStore(
            settings.overlay_bucket, settings.overlay_public_base_url or ""
        )

    app = FastAPI(title="RainAlert", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.notifier = notifier

    def get_session():
        with session_factory() as session:
            yield session

    # Said once, at boot, where `make logs` shows it - rather than leaving the first person to
    # click something to discover it as a 500. Never fatal: a database that is merely down at
    # start-up must not stop the process, and neither must a diagnostic.
    try:
        with session_factory() as session:
            complaint = schema_complaint(session)
        if complaint:
            logger.error("DATABASE SCHEMA IS OUT OF DATE: %s", complaint)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not check the database schema at start-up: %s", exc)

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
        # A per-request nonce, so the pages can keep their small inline scripts without
        # 'unsafe-inline'. Templates read it as request.state.csp_nonce.
        #
        # This is also the fix for a real bug: an earlier revision set `default-src 'self'` with no
        # script-src, which silently blocked the subscribe page's own inline script in any browser
        # that enforces CSP. The test asserted the header was present, not that the page still
        # worked - which is the difference between testing the assertion and testing the behaviour.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' {MAP_SCRIPT_SRC}; "
            f"style-src 'self' 'unsafe-inline' {MAP_SCRIPT_SRC}; "
            f"img-src 'self' data: {tile_origin(settings.map_tile_url)}; "
            "connect-src 'self'; "
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
        # Reachable is not the same as usable. New code on an un-migrated database connects
        # fine and then 500s on the first request that touches what the migration added, a long
        # way from the cause. On Cloud Run an unready revision also never takes traffic, which
        # is exactly the right outcome for a deploy that skipped its migration.
        complaint = schema_complaint(session)
        if complaint:
            logger.error("not ready: %s", complaint)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"database schema is out of date: {complaint}"
            )
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request, session: Session = Depends(get_session)) -> Response:
        """Prometheus metrics, behind a bearer token.

        Cloud Run has no notion of an internal-only route, so "internal" has to be expressed in the
        request. With no token configured the endpoint 404s - indistinguishable from not existing,
        which is the right answer for a monitoring surface nobody has set up yet.
        """
        if not settings.metrics_token:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        offered = request.headers.get("authorization", "")
        if not secrets.compare_digest(offered, f"Bearer {settings.metrics_token}"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")
        return Response(render_metrics(session, settings), media_type="text/plain; version=0.0.4")

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
        # A generated topic is unique every time, so there is nothing to rate limit on for
        # ntfy beyond the IP - which is already covered above.
        within_address = True
        if payload.email:
            within_address = hit_and_check(
                session,
                f"subscribe:email:{hash_address('email', payload.email).hex()}",
                settings.subscribe_limit_per_hour,
                timedelta(hours=1),
            )
        if not (within_ip and within_address):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts, try later")

        try:
            result = svc.subscribe(
                session,
                settings,
                channel=Channel(payload.channel),
                address=payload.email,
                lat=payload.lat,
                lon=payload.lon,
                client_ip=ip,
                user_agent=request.headers.get("user-agent"),
            )
        except svc.ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

        if result.confirm_token and result.address:
            deliver(
                confirmation_message(
                    settings, result.address, result.confirm_token, channel=payload.channel
                )
            )

        if payload.channel == "ntfy":
            # The topic has to come back: the subscriber cannot receive anything until their app
            # is subscribed to it, and they have no other way to learn what it is. Nothing is
            # disclosed by returning it - it was created for this request, a moment ago.
            return JSONResponse(
                {
                    "status": "subscribe to the topic, then tap the notification",
                    "channel": "ntfy",
                    "topic": result.address,
                    "server": settings.ntfy_server,
                    "subscribe_url": f"{settings.ntfy_server.rstrip('/')}/{result.address}",
                },
                status_code=status.HTTP_202_ACCEPTED,
            )
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
        """Two credentials, one identity.

        A `Bearer` token is the long-lived key handed out at confirmation, for an app. The
        settings-page session is a signed cookie that expires in half an hour. They authenticate
        the same subscriber and reach the same endpoints; what differs is CSRF, because only one
        of them is sent by the browser automatically. A bearer token has to be attached by script
        that has already read it, so a cross-site request cannot carry it. A cookie rides along on
        any request the browser makes, so a cookie-authenticated **write** must also present the
        CSRF value from the page (F-16).
        """
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            try:
                subscriber = svc.resolve_token(session, token=header[7:], purpose=TokenPurpose.API)
            except svc.ValidationError as exc:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised") from exc
            return subscriber, session

        cookie = request.cookies.get(MANAGE_COOKIE, "")
        subscriber_id = verify_session_token(cookie, settings.secret_key) if cookie else None
        if subscriber_id is None:
            if header:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            presented = request.headers.get(CSRF_HEADER, "")
            # Must match this session, not merely be a valid signature: otherwise anyone with a
            # session of their own holds a CSRF value good against everybody else's.
            if verify_csrf_token(presented, settings.secret_key) != subscriber_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or stale form token")

        subscriber = session.get(Subscriber, subscriber_id)
        if subscriber is None:
            # Signed, unexpired, and the account is gone - deleted since the session opened.
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")
        return subscriber, session

    def rule_bounds() -> dict:
        """The limits the page renders its inputs from, so the numbers live in one place."""
        return {
            "threshold_min": settings.min_threshold_mm_5min,
            "threshold_max": settings.plausibility_max_mm_5min,
            "lead_min": settings.min_lead_minutes,
            "lead_max": settings.max_lead_minutes,
            "lead_step": svc.LEAD_STEP_MINUTES,
            "radius_max": settings.max_radius_m,
            # The same bands the map is drawn and the legend labelled from, so "warn me at
            # orange" means one thing on both pages (radar/overlay.py INTENSITY_BANDS).
            "intensity_bands": legend(),
        }

    @app.get("/api/v1/subscriptions/me")
    def read_me(current=Depends(current_subscriber)) -> dict:
        subscriber, session = current
        sub = session.query(Subscription).filter_by(subscriber_id=subscriber.id).one()
        return {
            "channel": subscriber.channel.value,
            "address": subscriber.address,
            "status": sub.status.value,
            "lat": sub.lat,
            "lon": sub.lon,
            "location_updated_at": sub.location_updated_at.isoformat(),
            "radius_m": sub.radius_m,
            "threshold_mm_5min": float(sub.threshold_mm_5min),
            "lead_time_minutes": sub.lead_time_minutes,
            "timezone": sub.timezone,
            "health_note": sub.health_note,
            "bounds": rule_bounds(),
        }

    @app.patch("/api/v1/subscriptions/me", status_code=status.HTTP_204_NO_CONTENT)
    def patch_me(
        payload: RuleRequest, request: Request, current=Depends(current_subscriber)
    ) -> Response:
        """Threshold, lead time and radius. Absent fields are left alone."""
        subscriber, session = current
        ip = client_ip(request, settings.trusted_proxy_hops)
        if not hit_and_check(
            session, f"settings:ip:{ip}", settings.settings_limit_per_hour, timedelta(hours=1)
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many updates")
        try:
            svc.update_rule(
                session,
                settings,
                subscriber,
                threshold_mm_5min=payload.threshold_mm_5min,
                lead_time_minutes=payload.lead_time_minutes,
                radius_m=payload.radius_m,
            )
        except svc.ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
        address, channel = subscriber.address, subscriber.channel.value
        svc.delete_subscriber(session, subscriber)
        deliver(deletion_receipt(settings, address, channel=channel))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ---- settings-page session --------------------------------------------------------------
    def set_session_cookie(response: Response, subscriber_id) -> str:
        """Open a session and return the CSRF value the page must echo back."""
        ttl = settings.manage_session_ttl_minutes
        response.set_cookie(
            MANAGE_COOKIE,
            session_token(subscriber_id, settings.secret_key, ttl),
            max_age=ttl * 60,
            httponly=True,
            samesite="lax",
            # Only when the site is actually served over TLS. Setting it unconditionally makes
            # the cookie vanish in development, which looks like a broken login, not a policy.
            secure=settings.public_base_url.startswith("https://"),
            path="/",
        )
        return csrf_token(subscriber_id, settings.secret_key, ttl)

    @app.post("/api/v1/manage/link", status_code=status.HTTP_202_ACCEPTED)
    def request_manage_link(
        payload: ManageLinkRequest, request: Request, session: Session = Depends(get_session)
    ) -> dict:
        """Send a settings link to a channel that has already been confirmed.

        Always 202, whether or not the address is known. The endpoint takes an address someone
        typed and sends a message to it, so telling the caller which addresses exist would turn
        the settings page into a subscriber-list oracle - and the addresses are people's
        mailboxes.
        """
        ip = client_ip(request, settings.trusted_proxy_hops)
        if not hit_and_check(
            session, f"manage:ip:{ip}", settings.manage_link_limit_per_hour, timedelta(hours=1)
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests")

        channel = Channel(payload.channel)
        subscriber = svc.find_subscriber(session, channel=channel, address=payload.address)
        # Unconfirmed subscribers are excluded: confirmation is what proves the channel reaches
        # the person, and a settings link is not the place to take that on trust.
        if subscriber is not None and subscriber.confirmed_at is not None:
            token = svc.issue_manage_token(session, settings, subscriber)
            deliver(manage_link_message(settings, subscriber.address, token))
        return {"status": "check your messages"}

    @app.post("/api/v1/manage/session")
    def open_manage_session(
        response: Response, token: str = Form(""), session: Session = Depends(get_session)
    ) -> dict:
        """Spend the magic link, set the session cookie, hand back the CSRF value."""
        try:
            subscriber = svc.redeem_manage_token(session, token=token)
        except svc.ValidationError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        return {"csrf": set_session_cookie(response, subscriber.id)}

    @app.get("/api/v1/manage/csrf")
    def manage_csrf(request: Request) -> dict:
        """Hand the page a form token for a session it already holds.

        Safe to serve on a GET: it requires the session cookie, and the same-origin policy stops
        another site from reading the response - which is the same thing that makes the value
        worth anything in the first place.
        """
        cookie = request.cookies.get(MANAGE_COOKIE, "")
        subscriber_id = verify_session_token(cookie, settings.secret_key) if cookie else None
        if subscriber_id is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")
        return {
            "csrf": csrf_token(
                subscriber_id, settings.secret_key, settings.manage_session_ttl_minutes
            )
        }

    @app.post("/api/v1/manage/logout", status_code=status.HTTP_204_NO_CONTENT)
    def close_manage_session() -> Response:
        """Ends the session on this device. No credential needed - it only ever removes one."""
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(MANAGE_COOKIE, path="/")
        return response

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
        address, channel = subscriber.address, subscriber.channel.value
        svc.delete_subscriber(session, subscriber)
        deliver(deletion_receipt(settings, address, channel=channel))
        return TEMPLATES.TemplateResponse(request, "unsubscribed.html", {"settings": settings})

    # ---- map timeline ---------------------------------------------------------------------
    @app.get("/api/v1/overlays/timeline")
    def overlays_timeline(
        past_hours: int | None = None, session: Session = Depends(get_session)
    ) -> dict:
        """The slider manifest: −past_hours … +2 h, with gaps named explicitly.

        Unauthenticated: it is public radar imagery, the same data anyone can fetch from DWD, and
        it carries nothing subscriber-specific. Cached briefly so a page refresh is cheap.
        """
        if overlay_store is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "overlays are not configured")
        return build_timeline(session, settings, overlay_store, past_hours)

    # ---- pages ----------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "index.html", {"settings": settings, "has_map": overlay_store is not None}
        )

    #: What the range picker at the foot of the map offers. Every one of them is inside what
    #: DWD retains; the page drops any that exceed the configured maximum rather than showing a
    #: choice that would be silently clamped.
    WINDOW_CHOICES = (3, 6, 12, 24, 48)

    @app.get("/manage", response_class=HTMLResponse, include_in_schema=False)
    def manage_page(request: Request) -> HTMLResponse:
        """The settings page. Renders the same shell whether or not anyone is signed in.

        Deliberately side-effect free and identical for everyone: the magic link's token is in
        the URL *fragment*, which the browser never sends, so the server cannot know at render
        time whether this request carries one. The page asks.
        """
        return TEMPLATES.TemplateResponse(
            request,
            "manage.html",
            {
                "settings": settings,
                "has_map": overlay_store is not None,
                "bounds": rule_bounds(),
                "layer_opacity": LAYER_OPACITY,
            },
        )

    @app.get("/map", response_class=HTMLResponse, include_in_schema=False)
    def rain_map(request: Request, hours: str | None = None) -> HTMLResponse:
        # Resolved here rather than in the page's JavaScript, so the heading states the window
        # the page is actually showing instead of a number typed into the template - which is
        # how it came to say "12 Stunden" while serving 48.
        #
        # Taken as a string and parsed leniently on purpose. Declared as `int`, FastAPI answers
        # ?hours=abc with a 422 validation page; a rubbish query parameter should not cost
        # someone the map when there is a perfectly good default to fall back to.
        window = settings.timeline_default_hours
        if hours is not None:
            try:
                window = int(hours)
            except ValueError:
                window = settings.timeline_default_hours
        window = min(max(window, 1), settings.timeline_past_hours)
        return TEMPLATES.TemplateResponse(
            request,
            "map.html",
            {
                "settings": settings,
                "window_hours": window,
                "choices": [c for c in WINDOW_CHOICES if c <= settings.timeline_past_hours],
                "layer_opacity": LAYER_OPACITY,
            },
        )

    #: Only these may be turned into a QR code. The endpoint takes text from a query parameter
    #: and hands it to anyone with a camera, so without this it is a redirector: point it at any
    #: URL and the site vouches for it. Restricting it to the configured ntfy server means the
    #: only thing it can ever encode is a topic on the server we publish to.
    QR_ALLOWED_PREFIXES = (settings.ntfy_server.rstrip("/") + "/",)
    MAX_QR_TEXT = 512

    @app.get("/qr", include_in_schema=False)
    def qr_code(text: str = "") -> Response:
        """A QR for the ntfy subscribe URL, so a phone can join a topic shown on a desktop."""
        import io

        import segno

        if len(text) > MAX_QR_TEXT or not text.startswith(QR_ALLOWED_PREFIXES):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "refusing to encode that")

        buffer = io.BytesIO()
        segno.make(text, error="m").save(buffer, kind="svg", scale=4)
        return Response(
            buffer.getvalue(),
            media_type="image/svg+xml",
            # The topic is in the URL. Caches along the way have no business keeping it.
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
    def privacy(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "privacy.html", {"settings": settings})

    # The pages' own scripts. Served from 'self', which the CSP already allows, so the shared
    # geolocation helper does not have to be inlined into three templates and drift between them.
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    if settings.overlay_dir:
        # Development convenience. In production the overlays live in GCS behind a CDN, and the
        # raw archives must not share that prefix (SECURITY_REVIEW.md F-9).
        app.mount(
            "/overlays",
            StaticFiles(directory=settings.overlay_dir, check_dir=False),
            name="overlays",
        )

    return app
