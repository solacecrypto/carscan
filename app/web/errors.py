"""A readable page for HTTP errors in the browser; JSON for htmx and API-style clients."""

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from app.web.templating import templates

TITLES = {
    400: "That did not work",
    403: "Not allowed",
    404: "Not found",
    409: "Already taken",
    429: "Slow down",
}


async def http_error_handler(request: Request, exc: HTTPException) -> Response:
    wants_html = "text/html" in request.headers.get("accept", "")
    if request.headers.get("HX-Request") or not wants_html:
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )
    back = request.headers.get("referer") or "/listings"
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "title": TITLES.get(exc.status_code, f"Error {exc.status_code}"),
            "status": exc.status_code,
            "detail": exc.detail,
            "back": back,
        },
        status_code=exc.status_code,
        headers=exc.headers,
    )
