#!/usr/bin/env python
"""Serve the Law_RAG web app.

Default bind is 127.0.0.1 (this machine only) — the safe default for confidential
legal documents. To let other devices on your private tailnet reach it, set:

    LAWRAG_HOST=0.0.0.0 python scripts/serve.py

(only do that deliberately, and rely on the tailnet for access control).
"""
import os

import uvicorn

if __name__ == "__main__":
    host = os.getenv("LAWRAG_HOST", "127.0.0.1")
    port = int(os.getenv("LAWRAG_PORT", "8080"))
    uvicorn.run("lawrag.api:app", host=host, port=port, log_level="info")
