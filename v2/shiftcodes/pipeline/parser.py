"""Recursive-descent parser: Jenkinsfile text to a typed ``Pipeline``."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from shiftcodes.pipeline.ast import (
    Agent,
    AgentAny,
    AgentDocker,
    AgentNone,
    AllOfCondition,
    AnyOfCondition,
    BranchCondition,
    Condition,
    Credentials,
    EnvironmentCondition,
    EnvVar,
    NotCondition,
    ParallelBody,
    Pipeline,
    Post,
    PostBlock,
    PostCondition,
    SequentialBody,
    Stage,
    StageBody,
    StepCall,
    StepsBody,
    StringValue,
    When,
)
from shiftcodes.pipeline.cursor import TokenCursor
from shiftcodes.pipeline.errors import Span
from shiftcodes.pipeline.lexer import Token, TokenKind, tokenize
from shiftcodes.pipeline.steps import OPTIONS, PARAMETERS, STEPS
from shiftcodes.pipeline.steps_parser import SCRIPT_MESSAGE, StepParser
from shiftcodes.pipeline.values_parser import ValueParser

TRIGGERS_MESSAGE = (
    "triggers are configured in the platform (GitHub webhook); remove the triggers section"
)
PARALLEL_MESSAGE = "parallel cannot be nested inside another parallel"
EXPRESSION_MESSAGE = "expression conditions (free Groovy) are not supported in when"
_GROOVY_TOP_LEVEL = {"def", "import", "package", "class", "interface", "enum"}
_POST_ORDER = list(PostCondition)
_CONDITIONS = ("branch", "environment", "not", "allOf", "anyOf")


def parse(text: str) -> Pipeline:
    """Parse ``text``; raises ``PipelineSyntaxError`` for anything unsupported."""
    cursor = TokenCursor(text, tokenize(text))
    return _PipelineParser(cursor).parse()


@dataclass(slots=True)
class _Collected:
    """Sections of one pipeline or stage while parsing."""

    agent: Agent | None = None
    environment: tuple[EnvVar, ...] = ()
    options: tuple[StepCall, ...] = ()
    parameters: tuple[StepCall, ...] = ()
    when: When | None = None
    body: StageBody | None = None
    stages: tuple[Stage, ...] = ()
    post: Post | None = None
    seen: set[str] = field(default_factory=set)


Handler = Callable[[_Collected, Span], None]


class _PipelineParser:
    def __init__(self, cursor: TokenCursor) -> None:
        self._cur = cursor
        self._values = ValueParser(cursor)
        self._steps = StepParser(cursor, self._values)

    def parse(self) -> Pipeline:
        self._cur.skip_separators()
        head = self._cur.peek()
        self._expect_pipeline(head)
        self._cur.advance()
        collected = self._sections(
            {
                "agent": self._agent_section,
                "environment": self._environment_section,
                "options": self._options_section,
                "parameters": self._parameters_section,
                "triggers": self._triggers_section,
                "stages": self._pipeline_stages_section,
                "post": self._post_section,
            },
            "pipeline",
        )
        self._cur.skip_separators()
        if not self._cur.check(TokenKind.EOF):
            found = self._cur.peek()
            message = "code after the pipeline { } block is not supported"
            raise self._cur.fail(message, found.span)
        return Pipeline(
            collected.agent,
            collected.environment,
            collected.options,
            collected.parameters,
            collected.stages,
            collected.post,
            head.span,
        )

    def _expect_pipeline(self, head: Token) -> None:
        if head.kind is TokenKind.IDENT and head.text == "pipeline":
            return
        if head.kind is TokenKind.IDENT and head.text in _GROOVY_TOP_LEVEL:
            message = f"'{head.text}' and other Groovy code outside pipeline {{ }} is not supported"
            raise self._cur.fail(message, head.span)
        raise self._cur.fail("a Jenkinsfile must consist of a single pipeline { ... }", head.span)

    # --- generic section handling -------------------------------------------------

    def _sections(self, handlers: dict[str, Handler], owner: str) -> _Collected:
        collected = _Collected()
        self._cur.block(lambda: self._section(handlers, owner, collected))
        return collected

    def _section(self, handlers: dict[str, Handler], owner: str, collected: _Collected) -> None:
        name = self._cur.expect(TokenKind.IDENT, "expected a section name")
        if name.text == "script":
            raise self._cur.fail(SCRIPT_MESSAGE, name.span)
        handler = handlers.get(name.text)
        if handler is None:
            supported = ", ".join(handlers)
            message = f"section '{name.text}' is not supported in {owner}; supported: {supported}"
            raise self._cur.fail(message, name.span)
        if name.text in collected.seen:
            raise self._cur.fail(f"duplicate section '{name.text}'", name.span)
        collected.seen.add(name.text)
        handler(collected, name.span)

    # --- pipeline sections --------------------------------------------------------

    def _agent_section(self, collected: _Collected, span: Span) -> None:
        collected.agent = self._agent(span)

    def _environment_section(self, collected: _Collected, span: Span) -> None:
        collected.environment = self._non_empty(self._cur.block(self._env_var), "environment", span)

    def _options_section(self, collected: _Collected, span: Span) -> None:
        collected.options = self._non_empty(self._steps.steps_block(OPTIONS), "options", span)

    def _parameters_section(self, collected: _Collected, span: Span) -> None:
        calls = self._steps.steps_block(PARAMETERS)
        collected.parameters = self._non_empty(calls, "parameters", span)

    def _triggers_section(self, _collected: _Collected, span: Span) -> None:
        raise self._cur.fail(TRIGGERS_MESSAGE, span)

    def _non_empty[T](self, items: Sequence[T], name: str, span: Span) -> tuple[T, ...]:
        if not items:
            raise self._cur.fail(f"{name} cannot be empty; remove the section or fill it", span)
        return tuple(items)

    def _pipeline_stages_section(self, collected: _Collected, _span: Span) -> None:
        collected.stages = self._stage_block(in_parallel=False)

    def _post_section(self, collected: _Collected, span: Span) -> None:
        collected.post = self._post(span)

    # --- agent --------------------------------------------------------------------

    def _agent(self, span: Span) -> Agent:
        self._cur.skip_newlines_before_brace()
        if self._cur.check(TokenKind.IDENT):
            word = self._cur.advance()
            if word.text == "any":
                return AgentAny(span)
            if word.text == "none":
                return AgentNone(span)
            raise self._cur.fail(_agent_message(word.text), word.span)
        found = self._cur.peek()
        if not self._cur.check(TokenKind.LBRACE):
            raise self._cur.fail("expected 'any', 'none' or { docker ... } after agent", found.span)
        agents = self._cur.block(lambda: self._docker_agent(span))
        if len(agents) != 1:
            raise self._cur.fail("agent { } needs exactly one docker declaration", span)
        return agents[0]

    def _docker_agent(self, span: Span) -> AgentDocker:
        word = self._cur.expect(TokenKind.IDENT, "expected 'docker'")
        if word.text != "docker":
            raise self._cur.fail(_agent_message(word.text), word.span)
        if self._cur.check(TokenKind.STRING):
            return AgentDocker(self._values.string(self._cur.advance()), None, span)
        options = self._cur.block(self._docker_option)
        image = next((v for k, v in options if k == "image"), None)
        args = next((v for k, v in options if k == "args"), None)
        if image is None:
            raise self._cur.fail("docker agent needs an image", word.span)
        if len(options) != len({k for k, _ in options}):
            raise self._cur.fail("duplicate docker agent option", word.span)
        return AgentDocker(image, args, span)

    def _docker_option(self) -> tuple[str, StringValue]:
        word = self._cur.expect(TokenKind.IDENT, "expected a docker agent option")
        if word.text not in {"image", "args"}:
            message = f"docker agent option '{word.text}' is not supported; supported: image, args"
            raise self._cur.fail(message, word.span)
        return word.text, self._values.string_literal(f"a string for '{word.text}'")

    # --- environment --------------------------------------------------------------

    def _env_var(self) -> EnvVar:
        name = self._cur.expect(TokenKind.IDENT, "expected an environment variable name")
        self._cur.expect(TokenKind.EQUALS, "expected '='")
        if self._cur.check_ident("credentials"):
            return EnvVar(name.text, self._credentials(), name.span)
        if self._cur.check(TokenKind.STRING):
            return EnvVar(name.text, self._values.string(self._cur.advance()), name.span)
        found = self._cur.peek()
        message = "environment values must be strings or credentials('NAME')"
        raise self._cur.fail(message, found.span)

    def _credentials(self) -> Credentials:
        word = self._cur.advance()
        self._cur.expect(TokenKind.LPAREN, "expected '(' after credentials")
        self._cur.skip_newlines()
        text, _ = self._values.plain_string("the credentials name")
        self._cur.skip_newlines()
        self._cur.expect(TokenKind.RPAREN, "expected ')'")
        return Credentials(text, word.span)

    # --- stages -------------------------------------------------------------------

    def _stage_block(self, *, in_parallel: bool) -> tuple[Stage, ...]:
        return tuple(self._cur.block(lambda: self._stage(in_parallel)))

    def _stage(self, in_parallel: bool) -> Stage:
        word = self._cur.expect(TokenKind.IDENT, "expected 'stage'")
        if word.text != "stage":
            message = f"expected 'stage', found '{word.text}'; only stages are allowed here"
            raise self._cur.fail(message, word.span)
        self._cur.expect(TokenKind.LPAREN, "expected '(' after stage")
        self._cur.skip_newlines()
        name, _ = self._values.plain_string("the stage name")
        self._cur.skip_newlines()
        self._cur.expect(TokenKind.RPAREN, "expected ')'")
        collected = self._sections(self._stage_handlers(in_parallel), f"stage '{name}'")
        if collected.body is None:
            raise self._cur.fail("a stage needs one of: steps, parallel, stages", word.span)
        return Stage(
            name,
            collected.agent,
            collected.environment,
            collected.options,
            collected.when,
            collected.body,
            collected.post,
            word.span,
        )

    def _stage_handlers(self, in_parallel: bool) -> dict[str, Handler]:
        bodies: dict[str, Callable[[Span], StageBody]] = {
            "steps": self._steps_body,
            "parallel": lambda span: self._parallel_body(span, in_parallel),
            "stages": lambda span: self._sequential_body(span, in_parallel),
        }
        handlers: dict[str, Handler] = {
            "agent": self._agent_section,
            "environment": self._environment_section,
            "options": self._options_section,
            "when": self._when_section,
            "post": self._post_section,
        }
        for name, body in bodies.items():
            handlers[name] = self._exclusive(body)
        return handlers

    def _exclusive(self, body: Callable[[Span], StageBody]) -> Handler:
        def run(collected: _Collected, span: Span) -> None:
            if collected.body is not None:
                message = "a stage can have only one of steps, parallel, stages"
                raise self._cur.fail(message, span)
            collected.body = body(span)

        return run

    def _steps_body(self, span: Span) -> StageBody:
        return StepsBody(self._steps.steps_block(STEPS), span)

    def _parallel_body(self, span: Span, in_parallel: bool) -> StageBody:
        if in_parallel:
            raise self._cur.fail(PARALLEL_MESSAGE, span)
        return ParallelBody(self._stage_block(in_parallel=True), span)

    def _sequential_body(self, span: Span, in_parallel: bool) -> StageBody:
        return SequentialBody(self._stage_block(in_parallel=in_parallel), span)

    # --- when ---------------------------------------------------------------------

    def _when_section(self, collected: _Collected, span: Span) -> None:
        conditions = self._cur.block(self._condition)
        if not conditions:
            raise self._cur.fail("when needs at least one condition", span)
        collected.when = When(tuple(conditions), span)

    def _condition(self) -> Condition:
        word = self._cur.expect(TokenKind.IDENT, "expected a when condition")
        builders: dict[str, Callable[[Token], Condition]] = {
            "branch": self._branch,
            "environment": self._environment_condition,
            "not": self._not,
            "allOf": lambda w: AllOfCondition(self._conditions(w), w.span),
            "anyOf": lambda w: AnyOfCondition(self._conditions(w), w.span),
        }
        if word.text == "expression":
            raise self._cur.fail(EXPRESSION_MESSAGE, word.span)
        builder = builders.get(word.text)
        if builder is None:
            message = f"when condition '{word.text}' is not supported; supported: " + ", ".join(
                _CONDITIONS
            )
            raise self._cur.fail(message, word.span)
        return builder(word)

    def _conditions(self, word: Token) -> tuple[Condition, ...]:
        conditions = self._cur.block(self._condition)
        if not conditions:
            raise self._cur.fail(f"{word.text} needs at least one condition", word.span)
        return tuple(conditions)

    def _not(self, word: Token) -> Condition:
        conditions = self._conditions(word)
        if len(conditions) != 1:
            raise self._cur.fail("not takes exactly one condition", word.span)
        return NotCondition(conditions[0], word.span)

    def _branch(self, word: Token) -> Condition:
        args = self._steps.arguments()
        value = args[0].value if len(args) == 1 and args[0].name is None else None
        if not isinstance(value, StringValue):
            raise self._cur.fail("branch takes one pattern string: branch 'main'", word.span)
        return BranchCondition(value, word.span)

    def _environment_condition(self, word: Token) -> Condition:
        args = {a.name: a.value for a in self._steps.arguments()}
        name, value = args.get("name"), args.get("value")
        valid = set(args) == {"name", "value"} and isinstance(value, StringValue)
        if not valid or not isinstance(name, StringValue) or name.plain is None:
            message = "environment condition needs: environment name: 'X', value: 'y'"
            raise self._cur.fail(message, word.span)
        assert isinstance(value, StringValue)  # noqa: S101 - narrowed by ``valid``
        return EnvironmentCondition(name.plain, value, word.span)

    # --- post ---------------------------------------------------------------------

    def _post(self, span: Span) -> Post:
        blocks = self._non_empty(self._cur.block(self._post_block), "post", span)
        names = [block.condition for block in blocks]
        if len(names) != len(set(names)):
            raise self._cur.fail("duplicate post condition", span)
        ordered = sorted(blocks, key=lambda block: _POST_ORDER.index(block.condition))
        return Post(tuple(ordered), span)

    def _post_block(self) -> PostBlock:
        word = self._cur.expect(TokenKind.IDENT, "expected a post condition")
        try:
            condition = PostCondition(word.text)
        except ValueError:
            supported = ", ".join(c.value for c in PostCondition)
            message = f"post condition '{word.text}' is not supported; supported: {supported}"
            raise self._cur.fail(message, word.span) from None
        return PostBlock(condition, self._steps.steps_block(STEPS), word.span)


def _agent_message(kind: str) -> str:
    return (
        f"agent type '{kind}' is not supported; use agent any, agent none or "
        "agent { docker { image '...' } }"
    )
