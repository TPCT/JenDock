"""AST to editing model (see ``model.py``)."""

from __future__ import annotations

from backend.pipeline import ast, model
from backend.pipeline.model_template import encode


def to_model(pipeline: ast.Pipeline) -> model.Model:
    """The editing model of ``pipeline``; ``to_ast`` of the result equals ``pipeline``.

    An argument written twice in one call keeps only its first occurrence, because the model
    keys arguments by name; the validator reports the duplicate on the original text.
    """
    return model.Model(
        agent=_agent(pipeline.agent),
        environment=[_env_var(var) for var in pipeline.environment],
        options=[_call(call) for call in pipeline.options],
        parameters=[_call(call) for call in pipeline.parameters],
        stages=[_stage(stage) for stage in pipeline.stages],
        post=_post(pipeline.post),
    )


def _string(value: ast.StringValue) -> str | model.TemplateText:
    plain = value.plain
    return plain if plain is not None else model.TemplateText(template=encode(value.parts))


def _value(value: ast.Value) -> model.Value:
    if isinstance(value, ast.StringValue):
        return _string(value)
    if isinstance(value, ast.IntValue | ast.BoolValue):
        return value.value
    if isinstance(value, ast.ListValue):
        return [_value(item) for item in value.items]
    entries = [model.MapItem(key=entry.key, value=_value(entry.value)) for entry in value.entries]
    return model.MapText(map=entries)


def _call(call: ast.StepCall) -> model.Call:
    args: dict[str, model.Value] = {}
    unnamed = 0
    for argument in call.args:
        if argument.name is None:
            key = model.unnamed_key(unnamed)
            unnamed += 1
        else:
            key = argument.name
        args.setdefault(key, _value(argument.value))
    body = None if call.body is None else [_call(inner) for inner in call.body]
    return model.Call(name=call.name, args=args, body=body)


def _agent(agent: ast.Agent | None) -> model.Agent | None:
    if agent is None:
        return None
    if isinstance(agent, ast.AgentAny):
        return model.AgentAny()
    if isinstance(agent, ast.AgentNone):
        return model.AgentNone()
    args = None if agent.args is None else _string(agent.args)
    return model.AgentDocker(image=_string(agent.image), args=args)


def _env_var(var: ast.EnvVar) -> model.EnvVar:
    if isinstance(var.value, ast.Credentials):
        return model.EnvVar(name=var.name, value=model.CredentialsRef(credentials=var.value.name))
    return model.EnvVar(name=var.name, value=_string(var.value))


def _condition(condition: ast.Condition) -> model.Condition:
    if isinstance(condition, ast.BranchCondition):
        return model.BranchCondition(pattern=_string(condition.pattern))
    if isinstance(condition, ast.EnvironmentCondition):
        return model.EnvironmentCondition(name=condition.name, value=_string(condition.value))
    if isinstance(condition, ast.NotCondition):
        return model.NotCondition(condition=_condition(condition.condition))
    inner = [_condition(item) for item in condition.conditions]
    if isinstance(condition, ast.AllOfCondition):
        return model.AllOfCondition(conditions=inner)
    return model.AnyOfCondition(conditions=inner)


def _post(post: ast.Post | None) -> model.Post | None:
    if post is None:
        return None
    blocks = [
        model.PostBlock(condition=block.condition, steps=[_call(step) for step in block.steps])
        for block in post.blocks
    ]
    return model.Post(blocks=blocks)


def _body(body: ast.StageBody) -> model.StageBody:
    if isinstance(body, ast.StepsBody):
        return model.StepsBody(steps=[_call(step) for step in body.steps])
    stages = [_stage(stage) for stage in body.stages]
    if isinstance(body, ast.ParallelBody):
        return model.ParallelBody(stages=stages)
    return model.StagesBody(stages=stages)


def _stage(stage: ast.Stage) -> model.Stage:
    when = None if stage.when is None else [_condition(item) for item in stage.when.conditions]
    return model.Stage(
        name=stage.name,
        agent=_agent(stage.agent),
        environment=[_env_var(var) for var in stage.environment],
        options=[_call(call) for call in stage.options],
        when=when,
        body=_body(stage.body),
        post=_post(stage.post),
    )
