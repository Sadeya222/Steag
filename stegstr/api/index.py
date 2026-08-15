"""Vercel serverless entrypoint (Python ASGI).

Vercel imports this module and looks for an ASGI ``app`` — the FastAPI app
from the Stegstr package.  Static assets (the web UI) are served from
``public/`` by Vercel's edge; every ``/api/*``, ``/docs`` and
``/openapi.json`` request lands here.

Deployment requires two env vars so state survives serverless invocations
(the Lambda filesystem is ephemeral):

    BLOB_READ_WRITE_TOKEN     Vercel Blob  -> carrier store
    UPSTASH_REDIS_REST_URL    Upstash KV  -> capsule log / contacts
    UPSTASH_REDIS_REST_TOKEN  (same)

Without them, the API still runs but carriers/logs are lost between
invocations.  See DEPLOYMENT.md -> "Deploy to Vercel".

Alternative if the native ASGI detection ever misbehaves on Vercel:
    pip install mangum
    from mangum import Mangum
    handler = Mangum(app)   # and export `handler` instead of `app`
"""

from stegstr.api import app
