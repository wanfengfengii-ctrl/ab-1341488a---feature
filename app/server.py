"""HTTP layer for the calibration translation service.

Implemented with the Python standard library only, so the Docker image
needs no network access at build time.
"""

from __future__ import annotations

import json
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .calibration import CalibrationError, translate

DEFAULT_PORT = 8000
MAX_BODY_BYTES = 1_000_000

logger = logging.getLogger("calibration")


class CalibrationHandler(BaseHTTPRequestHandler):
    server_version = "CalibrationService/1.0"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") in ("", "/health", "/healthz"):
            if self.path.rstrip("/") == "":
                self._send_json(HTTPStatus.OK, {"status": "ok", "service": "calibration"})
            else:
                self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/api/calibrations/translate":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"code": "invalid_request", "message": "invalid Content-Length"}},
            )
            return
        if length <= 0:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"code": "invalid_request", "message": "empty request body"}},
            )
            return
        if length > MAX_BODY_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": {"code": "invalid_request", "message": "request body too large"}},
            )
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"code": "invalid_request", "message": f"invalid JSON: {exc}"}},
            )
            return

        try:
            result = translate(payload)
        except CalibrationError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
            if exc.code == "conflict":
                error["cycle"] = exc.cycle  # type: ignore[attr-defined]
            elif exc.code == "unreachable":
                error["source"] = exc.source  # type: ignore[attr-defined]
                error["target"] = exc.target  # type: ignore[attr-defined]
            self._send_json(exc.status, {"error": error})
            return
        except Exception:  # pragma: no cover - defensive
            logger.exception("unexpected failure while handling translation request")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"code": "internal_error", "message": "internal server error"}},
            )
            return

        self._send_json(HTTPStatus.OK, result.to_json())

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)


def build_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), CalibrationHandler)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    server = build_server("0.0.0.0", port)
    logger.info("calibration service listening on 0.0.0.0:%s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
