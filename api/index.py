"""Expose the FastAPI application to Vercel's Python runtime."""

from web.main import app

__all__ = ["app"]
