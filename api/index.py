import sys
from pathlib import Path

# Ensure the root directory is on the Python path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.main import app as fastapi_app


async def app(scope, receive, send):
    """
    ASGI entrypoint for Vercel Serverless Functions.
    Normalizes the request path so that Vercel rewrites to /api/index.py
    properly map to the intended FastAPI routes (/, /app, /api/status, etc.).
    """
    if scope["type"] == "http":
        headers = dict(scope.get("headers", []))
        matched_path = headers.get(b"x-matched-path", b"").decode("latin-1")
        forwarded_uri = headers.get(b"x-forwarded-uri", b"").decode("latin-1")

        orig_path = matched_path or forwarded_uri
        if orig_path:
            orig_path = orig_path.split("?")[0]
            if not orig_path.startswith("/api/index"):
                scope["path"] = orig_path
                scope["raw_path"] = orig_path.encode("latin-1")

        path = scope.get("path", "")
        for prefix in ("/api/index.py", "/api/index"):
            if path.startswith(prefix):
                cleaned = path[len(prefix):]
                if not cleaned:
                    cleaned = "/"
                elif not cleaned.startswith("/"):
                    cleaned = "/" + cleaned
                scope["path"] = cleaned
                scope["raw_path"] = cleaned.encode("latin-1")
                break

        if scope.get("path") == "/api":
            scope["path"] = "/"
            scope["raw_path"] = b"/"

    await fastapi_app(scope, receive, send)
