"""Response headers that constrain what a browser will do with this application.

The Content-Security-Policy here is deliberately not the strictest expressible policy: the source
viewer renders page previews and PDF artifacts streamed from the API, and a policy that blocked
them would make a security header break the evidence-inspection feature that exists to let a
reader check a citation. `img-src` and `object-src`/`frame-src` are therefore opened to `self` and
`blob:`, and nothing wider.

Headers are applied to every response including errors, because an error page is exactly where a
reflected payload would otherwise land.
"""

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

#: Applies to responses the browser renders. The API returns JSON, but a mistyped response or a
#: future HTML surface must not be able to execute injected script.
BASE_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    # Page previews and figure crops are streamed from the API and rendered from object URLs.
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    # The browser talks to this origin only. No provider endpoint is reachable from the page.
    "connect-src 'self'; "
    "media-src 'self' blob:; "
    "worker-src 'self' blob:"
)


def security_headers(
    *, hsts: bool, csp: str = BASE_CSP
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    """Middleware factory. `hsts` is opt-in because it is a promise the deployment must keep.

    Sending Strict-Transport-Security from a service reached over plain HTTP — as it is in local
    development — pins the browser to HTTPS for a host that does not serve it, and the user cannot
    easily undo that. It is therefore enabled only where TLS termination is actually controlled.
    """

    async def middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", csp)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        # This application needs no camera, microphone, geolocation or payment capability.
        headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        if hsts:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    return middleware


def install(app: ASGIApp, *, hsts: bool) -> None:  # pragma: no cover - thin wiring helper
    app.middleware("http")(security_headers(hsts=hsts))  # type: ignore[attr-defined]
