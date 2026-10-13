"""Database inspector engine: adapters, SQL guard and the helper-container entrypoint.

This package depends on the standard library and the database drivers only, so the small
inspector image (``docker/inspector.Dockerfile``) can ship it without the rest of the platform.
"""
