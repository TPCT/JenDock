"""Steps that do not need Docker: echo, error and the block steps."""

from __future__ import annotations

from shiftcodes.execution.context import StepContext
from shiftcodes.execution.errors import DeadlineExceeded, StepFailed
from shiftcodes.execution.retry import run_with_retries
from shiftcodes.execution.scope import describe
from shiftcodes.execution.step_base import Step
from shiftcodes.pipeline.ast import StringValue

TIME_UNITS = {"SECONDS": 1, "MINUTES": 60, "HOURS": 3600}


class EchoStep(Step):
    """``echo 'message'``."""

    name = "echo"

    def summary(self) -> str:
        """The source text of the message."""
        return describe(self.required_string("message"))

    def execute(self, ctx: StepContext) -> None:
        """Write the message to the log."""
        for line in ctx.text(self.required_string("message")).splitlines() or [""]:
            ctx.line(line)


class ErrorStep(Step):
    """``error 'message'``: fails the step."""

    name = "error"

    def summary(self) -> str:
        """The source text of the message."""
        return describe(self.required_string("message"))

    def execute(self, ctx: StepContext) -> None:
        """Fail with the message."""
        raise StepFailed(ctx.text(self.required_string("message")))


class RetryStep(Step):
    """``retry(n) { ... }``."""

    name = "retry"

    def summary(self) -> str:
        """The number of attempts."""
        return f"{self.integer('count', 1)} attempts"

    def execute(self, ctx: StepContext) -> None:
        """Run the body up to ``count`` times."""
        count = self.integer("count", 1)
        body = self.call.body or ()

        def attempt(number: int) -> None:
            ctx.line(f"retry: attempt {number} of {count}")
            ctx.run_body(body, ctx.scope)

        run_with_retries(count, attempt, lambda n, error: ctx.line(f"attempt {n} failed: {error}"))


def timeout_seconds(time_value: int, unit: str) -> float:
    """Convert a ``timeout`` argument to seconds."""
    return float(time_value * TIME_UNITS[unit])


class TimeoutStep(Step):
    """``timeout(time: n, unit: 'MINUTES') { ... }``."""

    name = "timeout"

    def _seconds(self) -> tuple[float, str]:
        unit = self.string("unit")
        unit_text = unit.plain if unit is not None and unit.plain else "MINUTES"
        return timeout_seconds(self.integer("time", 1), unit_text), unit_text

    def summary(self) -> str:
        """The configured duration."""
        return f"{self.integer('time', 1)} {self._seconds()[1]}"

    def execute(self, ctx: StepContext) -> None:
        """Run the body with a deadline."""
        seconds, _unit = self._seconds()
        ctx.line(f"timeout: {self.summary()}")
        try:
            ctx.run_body(self.call.body or (), ctx.scope.with_timeout(seconds))
        except DeadlineExceeded as error:
            raise DeadlineExceeded(f"timeout of {self.summary()} exceeded: {error}") from error


class DirStep(Step):
    """``dir('relative/path') { ... }``."""

    name = "dir"

    def summary(self) -> str:
        """The source text of the path."""
        return describe(self.required_string("path"))

    def execute(self, ctx: StepContext) -> None:
        """Run the body in a sub directory of the workspace."""
        relative = ctx.text(self.required_string("path"))
        if relative.startswith("/") or ".." in relative.split("/"):
            raise StepFailed(f"directory must stay inside the workspace: {relative!r}")
        ctx.run_body(self.call.body or (), ctx.scope.with_directory(relative))


class WithEnvStep(Step):
    """``withEnv(['NAME=value']) { ... }``."""

    name = "withEnv"

    def summary(self) -> str:
        """The names that are set (values are not shown)."""
        names = []
        for item in self.items("overrides"):
            if isinstance(item, StringValue):
                names.append(describe(item).partition("=")[0])
        return ", ".join(names)

    def execute(self, ctx: StepContext) -> None:
        """Run the body with additional environment variables."""
        values: dict[str, str] = {}
        for item in self.items("overrides"):
            if not isinstance(item, StringValue):
                raise StepFailed("withEnv entries must be strings")
            name, equals, value = ctx.text(item).partition("=")
            if not equals:
                raise StepFailed("withEnv entries must look like NAME=value")
            values[name] = value
        ctx.run_body(self.call.body or (), ctx.scope.with_env(values))
