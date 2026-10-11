# MySQL-family application identity checkpoint

The fixed application identity helper authenticates the requested application
account, current database and precise MySQL/MariaDB family using only
`SELECT DATABASE(),CURRENT_USER(),VERSION()`. Writable accounts remain valid.
Requests cannot supply SQL, network destinations or driver options. Private
credentials travel through bounded stdin to an isolated immutable helper image.

Real local acceptance used MySQL 8.4.11 and MariaDB 11.4.13. On each engine it:

- Authenticated the initialized application account through the actual helper.
- Created a synthetic table, inserted a sentinel and read it back using that
  application account in a separate client invocation.
- Rejected the other engine family, invalid credentials and caller-supplied SQL.
- Removed exact owned fixture containers and helper journals, verifying that the
  original container ID set was restored. Database storage used disposable tmpfs.

`application-identity-evidence-20261011.json` records image IDs and source hashes.
The reproducible opt-in fixture is `tests/live_application_identity.py`.
Its scope is real engines plus helper transport. It does not test application
bridge grants, application cutover, whole-pipeline deployment or production.

The canonical offline helper build could not install uncached pinned packages.
It was stopped and its build process/container terminated. For runtime testing,
the exact updated source was copied onto the existing immutable helper dependency
image without network access. This candidate is not a freshly built release.
Existing review tags and production services were not replaced.

Worker orchestration verifies current authority, accepted binding, credentials
and candidate identity around the helper. The probe has one bounded deadline,
including Docker inventory and Compose provenance inspection. Queue/scheduler
transport preserves its existing overall request deadline and socket. An expired
deadline may leave an exact-owned helper journal for recovery on the next call;
timeout cleanup is not represented as immediately successful. Stopping uses its
separate existing path and does not require a successful database login.

Independent review found and drove fixes for candidate-resolution deadline
overrun and continuity from the authenticated candidate to network attachment.
The final backend suite passed **1,351 tests** using
`.runtime/mcp-venv/bin/python -m unittest discover -s tests -q`. The initial system
Python invocation lacked MCP and PostgreSQL dependencies; it was rerun using the
existing project environment without installing packages. An independent reviewer
then passed all nine probe tests and found no remaining actionable continuity
issue. No push or production deployment is claimed.
