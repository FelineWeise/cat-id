#!/usr/bin/env python3
"""Entry point – start the Similar Tracks Finder server."""

import os

import uvicorn

if __name__ == "__main__":
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    reload_enabled = app_env == "development" and os.getenv("NO_RELOAD", "").strip() == ""

    kwargs: dict = dict(host=host, port=port, reload=reload_enabled)
    use_local_ssl = app_env == "development" and os.getenv("NO_SSL", "").strip() == ""
    if use_local_ssl:
        kwargs.update(ssl_keyfile="key.pem", ssl_certfile="cert.pem")

    uvicorn.run("backend.main:app", **kwargs)