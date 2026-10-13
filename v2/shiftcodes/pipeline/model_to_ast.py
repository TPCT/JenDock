"""Editing model to AST (see ``model.py``). The nodes get a dummy span; positions of the
rendered text are found by parsing it again."""

from __future__ import annotations

from shiftcodes.pipeline import ast, model
from shiftcodes.pipeline.model_template import NO_SPAN, decode


def to_ast(value: model.Model) -> ast.Pipeline:
    """The pipeline AST of an editing model; ``to_model`` of the result equals ``value``."""
    return ast.Pipeline(
        agent=_agent(value.agent),
        environment=tuple(_env_var(var) for var in value.environment),
        options=tuple(_call(call) for call in value.options),
        parameters=tuple(_call(call) for call in value.parameters),
        stages=tuple(_stage(stage) for stage in value.stages),
        post=_post(value.post),
        span=NO_SPAN,
    )


def _string(value: str | model.TemplateText) -> ast.StringValue:
    if isinstance(value, str):
        parts: tuple[ast.StringPart, ...] = (ast.TextPart(value),) if value else ()
    else:
        parts = decode(value.template)
    return ast.StringValue(parts, NO_SPAN)


def _value(value: model.Value) -> ast.Value:
    if isinstance(value, bool):
        return ast.BoolValue(value, NO_SPAN)
    if isinstance(value, int):
        return ast.IntValue(value, NO_SPAN)
    if isinstance(value, str | model.TemplateText):
        return _string(value)
    if isinstance(value, list):
        return ast.ListValue(tuple(_value(item) for item in value), NO_SPAN)
    entries = tuple(ast.MapEntry(item.key, _value(item.value), NO_SPAN) for item in value.map)
    return ast.MapValue(entries, NO_SPAN)


def _call(call: model.Call) -> ast.StepCall:
    args = tuple(
        ast.Argument(None if model.UNNAMED_KEY.fullmatch(key) else key, _value(item), NO_SPAN)
        for key, item in call.args.items()
    )
    body = None if call.body is None else tuple(_call(inner) for inner in call.body)
    return ast.StepCall(call.name, args, body, NO_SPAN)


def _agent(agent: model.Agent | None) -> ast.Agent | None:
    if agent is None:
        return None
    if isinstance(agent, model.AgentAny):
        return ast.AgentAny(NO_SPAN)
    if isinstance(agent, model.AgentNone):
        return ast.AgentNone(NO_SPAN)
    args = None if agent.args is None else _string(agent.args)
    return ast.AgentDocker(_string(agent.image), args, NO_SPAN)


def _env_var(var: model.EnvVar) -> ast.EnvVar:
    if isinstance(var.value, model.CredentialsRef):
        return ast.EnvVar(var.name, ast.Credentials(var.value.credentials, NO_SPAN), NO_SPAN)
    return ast.EnvVar(var.name, _string(var.value), NO_SPAN)


def _condition(condition: model.Condition) -> ast.Condition:
    if isinstance(condition, model.BranchCondition):
        return ast.BranchCondition(_string(condition.pattern), NO_SPAN)
    if isinstance(condition, model.EnvironmentCondition):
        return ast.EnvironmentCondition(condition.name, _string(condition.value), NO_SPAN)
    if isinstance(condition, model.NotCondition):
        return ast.NotCondition(_condition(condition.condition), NO_SPAN)
    inner = tuple(_condition(item) for item in condition.conditions)
    if isinstance(condition, model.AllOfCondition):
        return ast.AllOfCondition(inner, NO_SPAN)
    return ast.AnyOfCondition(inner, NO_SPAN)


def _post(post: model.Post | None) -> ast.Post | None:
    if post is None:
        return None
    blocks = tuple(
        ast.PostBlock(block.condition, tuple(_call(step) for step in block.steps), NO_SPAN)
        for block in post.blocks
    )
    return ast.Post(blocks, NO_SPAN)


def _body(body: model.StageBody) -> ast.StageBody:
    if isinstance(body, model.StepsBody):
        return ast.StepsBody(tuple(_call(step) for step in body.steps), NO_SPAN)
    stages = tuple(_stage(stage) for stage in body.stages)
    if isinstance(body, model.ParallelBody):
        return ast.ParallelBody(stages, NO_SPAN)
    return ast.SequentialBody(stages, NO_SPAN)


def _stage(stage: model.Stage) -> ast.Stage:
    when = None if stage.when is None else ast.When(tuple(map(_condition, stage.when)), NO_SPAN)
    return ast.Stage(
        name=stage.name,
        agent=_agent(stage.agent),
        environment=tuple(_env_var(var) for var in stage.environment),
        options=tuple(_call(call) for call in stage.options),
        when=when,
        body=_body(stage.body),
        post=_post(stage.post),
        span=NO_SPAN,
    )
