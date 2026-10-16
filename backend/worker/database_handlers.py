"""RPC methods of the database inspector. They run in the worker, which holds the key."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.domain.database import ConnectionRef, TableRef
from backend.domain.enums import EnvironmentName
from backend.inspector.model import MAX_PAGE_ROWS, MAX_STATEMENT_CHARS
from backend.services.database_detection import DatabaseDetectionService, to_wire
from backend.services.database_inspection import InspectionService
from backend.worker.rpc import BadRequestError, Handler, RpcCall

InspectionFactory = Callable[[], InspectionService]
DetectionFactory = Callable[[], DatabaseDetectionService]
MAX_NAME = 256


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ConnectionParams(_Params):
    slug: str = Field(max_length=64)
    env: EnvironmentName
    name: str = Field(max_length=48)


class _SchemasParams(_ConnectionParams):
    include_system: bool = False


class _TablesParams(_ConnectionParams):
    schema_name: str = Field(alias="schema", min_length=1, max_length=MAX_NAME)


class _TableParams(_TablesParams):
    table: str = Field(min_length=1, max_length=MAX_NAME)


class _RowsParams(_TableParams):
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_ROWS)
    offset: int = Field(default=0, ge=0)
    after: list[str | int] | None = None


class _QueryParams(_ConnectionParams):
    statement: str = Field(max_length=MAX_STATEMENT_CHARS * 4)


def _parse[P: _Params](model: type[P], call: RpcCall) -> P:
    try:
        return model.model_validate(call.params, strict=False)
    except PydanticValidationError as error:
        # Never echo the input: a console statement can contain anything.
        raise BadRequestError("invalid params") from error


def _ref(params: _ConnectionParams) -> ConnectionRef:
    return ConnectionRef(params.slug, params.env, params.name)


class DatabaseHandlers:
    """Adapts ``InspectionService`` to RPC calls; a fresh service per call."""

    def __init__(
        self, factory: InspectionFactory, detection: DetectionFactory | None = None
    ) -> None:
        self._factory = factory
        self._detection = detection

    def detect(self, call: RpcCall) -> object:
        """``db.detect``: discover credentials and create/test connections in worker only."""
        p = _parse(_DetectParams, call)
        if self._detection is None:
            raise BadRequestError("database detection unavailable")
        return to_wire(self._detection().detect(p.slug, p.env, call.require_actor()), p.env)

    def test(self, call: RpcCall) -> object:
        """``db.test``: connect through the environment network and report."""
        p = _parse(_ConnectionParams, call)
        return self._factory().test(_ref(p), call.require_actor())

    def schemas(self, call: RpcCall) -> object:
        """``db.schemas``."""
        p = _parse(_SchemasParams, call)
        call.require_actor()
        return self._factory().schemas(_ref(p), include_system=p.include_system)

    def tables(self, call: RpcCall) -> object:
        """``db.tables``."""
        p = _parse(_TablesParams, call)
        call.require_actor()
        return self._factory().tables(_ref(p), p.schema_name)

    def table(self, call: RpcCall) -> object:
        """``db.table``: columns, indexes, foreign keys."""
        p = _parse(_TableParams, call)
        call.require_actor()
        return self._factory().table(TableRef(_ref(p), p.schema_name, p.table))

    def rows(self, call: RpcCall) -> object:
        """``db.rows``: one page of table rows."""
        p = _parse(_RowsParams, call)
        page: dict[str, object] = {"limit": p.limit, "offset": p.offset, "after": p.after}
        ref = TableRef(_ref(p), p.schema_name, p.table)
        return self._factory().rows(ref, page, call.require_actor())

    def query(self, call: RpcCall) -> object:
        """``db.query``: one read-only console statement."""
        p = _parse(_QueryParams, call)
        return self._factory().query(_ref(p), p.statement, call.require_actor())


def build_database_handlers(
    factory: InspectionFactory, detection: DetectionFactory | None = None
) -> Mapping[str, Handler]:
    """Return the ``db.*`` part of the RPC allowlist."""
    handlers = DatabaseHandlers(factory, detection)
    return {
        "db.test": handlers.test,
        "db.schemas": handlers.schemas,
        "db.tables": handlers.tables,
        "db.table": handlers.table,
        "db.rows": handlers.rows,
        "db.query": handlers.query,
        "db.detect": handlers.detect,
    }


class _DetectParams(_Params):
    slug: str = Field(max_length=64)
    env: EnvironmentName
