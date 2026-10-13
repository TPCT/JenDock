"""The host helper: a minimal privileged process that manages Apache for the platform.

Everything in this package uses the standard library plus ``cryptography`` only, so it can run
on the host outside the platform's containers. It never imports the rest of ``backend``.
"""
