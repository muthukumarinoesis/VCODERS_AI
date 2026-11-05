"""Request body limit before FastAPI sees the body (spec 9.2).

Wrap the whole app with it, outside FastAPI's own middleware stack:
    app = BodySizeLimit(fastapi_app, max_bytes=cap + 1 MB)
The proxy (nginx client_max_body_size 51m, or the load balancer) is the
first layer; this is the second.
"""

import json

TOO_LARGE = json.dumps(
    {"error": "too_large", "message": "This upload is over the size limit. Upload a smaller file."}
).encode()


class _Rejected(Exception):
    pass


class BodySizeLimit:
    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    too_big = int(value) > self.max_bytes
                except ValueError:
                    too_big = True
                if too_big:
                    return await _send_413(send)

        received, rejected, started = 0, False, False

        async def limited_receive():
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    rejected = True
                    if not started:
                        await _send_413(send)
                    raise _Rejected
            return message

        async def guarded_send(message):
            nonlocal started
            if rejected:
                return  # the app's own error response (often a 500) is dropped
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not rejected:
                raise


async def _send_413(send):
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"connection", b"close")],
        }
    )
    await send({"type": "http.response.body", "body": TOO_LARGE})
