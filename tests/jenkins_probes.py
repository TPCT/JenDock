# ruff: noqa: E501
"""Syntax probes compared against real Jenkins by ``scripts/jenkins_lint.sh``.

Each entry is a small pipeline text exercising one syntax or rule question. Verdicts recorded
from a real Jenkins live in ``fixtures/jenkins_verdicts.json``; ``test_jenkins_compat`` uses them
offline.
"""

from __future__ import annotations


def _pipeline(body: str, agent: str = "agent any") -> str:
    return f"pipeline {{\n{agent}\n{body}\n}}\n"


def _steps(steps: str, agent: str = "agent any", extra: str = "") -> str:
    stage = f"stages {{\nstage('a') {{\nsteps {{\n{steps}\n}}\n}}\n}}"
    return _pipeline(f"{extra}\n{stage}", agent)


def _stages(stage: str, agent: str = "agent any", extra: str = "") -> str:
    return _pipeline(f"{extra}\nstages {{\n{stage}\n}}", agent)


ECHO = "echo 'x'"
DOCKER = "agent { docker 'node:22' }"

PROBES: dict[str, str] = {
    "same-line agent then stages": "pipeline {\n agent { docker { image 'alpine:3' } } stages { stage('a') { steps { echo 'x' } } }\n}\n",
    "same-line agent any then stages": "pipeline {\n agent any stages { stage('a') { steps { echo 'x' } } }\n}\n",
    "same-line sibling stages": "pipeline { agent any\n stages { stage('a') { steps { echo 'x' } } stage('b') { steps { echo 'y' } } } }\n",
    "same-line sibling sections in stage": "pipeline { agent any\n stages { stage('a') { steps { echo 'x' } } } }\n".replace(
        "steps { echo 'x' } }", "environment { A = 'b' } steps { echo 'x' } }", 1
    ),
    "same-line two steps": _steps("echo 'a' echo 'b'"),
    "semicolon steps": _steps("echo 'a'; echo 'b'"),
    "one-liner": "pipeline{agent any;stages{stage('a'){steps{echo 'x'}}}}",
    "semicolon after section": "pipeline { agent any; stages { stage('a') { steps { echo 'x' } } } }",
    "parallel branch with stages": _stages(
        "stage('p') { parallel { stage('b') { stages { stage('c') { steps { echo 'x' } } } } } }"
    ),
    "stages in stages in stages": _stages(
        "stage('p') { stages { stage('b') { stages { stage('c') { steps { echo 'x' } } } } } }"
    ),
    "parallel in sequential child": _stages(
        "stage('p') { stages { stage('b') { parallel { stage('c') { steps { echo 'x' } } } } } }"
    ),
    "parallel in parallel": _stages(
        "stage('p') { parallel { stage('b') { parallel { stage('c') { steps { echo 'x' } } } } } }"
    ),
    "stage with parallel and steps": _stages(
        "stage('p') { steps { echo 'x' } parallel { stage('c') { steps { echo 'x' } } } }"
    ),
    "stage with steps and stages": _stages(
        "stage('p') { steps { echo 'x' }\n stages { stage('c') { steps { echo 'x' } } } }"
    ),
    "docker short form": _pipeline(
        "stages { stage('a') { steps { echo 'x' } } }", "agent { docker 'alpine:3' }"
    ),
    "empty steps": _steps(""),
    "empty stages in stage": _stages("stage('p') { stages { } }"),
    "empty parallel": _stages("stage('p') { parallel { } }"),
    "empty post always": _steps(ECHO, extra="post { always { } }"),
    "empty post": _steps(ECHO, extra="post { }"),
    "empty environment": _steps(ECHO, extra="environment { }"),
    "empty options": _steps(ECHO, extra="options { }"),
    "empty parameters": _steps(ECHO, extra="parameters { }"),
    "empty triggers": _steps(ECHO, extra="triggers { }"),
    "empty when": _stages("stage('a') { when { }\n steps { echo 'x' } }"),
    "empty allOf": _stages("stage('a') { when { allOf { } }\n steps { echo 'x' } }"),
    "not with two": _stages(
        "stage('a') { when { not { branch 'a'\n branch 'b' } }\n steps { echo 'x' } }"
    ),
    "when beforeAgent": _stages(
        "stage('a') { when { beforeAgent true\n branch 'a' }\n steps { echo 'x' } }"
    ),
    "when branch pattern named": _stages(
        "stage('a') { when { branch pattern: 'a*', comparator: 'GLOB' }\n steps { echo 'x' } }"
    ),
    "empty block step": _steps("retry(3) { }"),
    "dir cmd syntax with block": _steps("dir 'x' { echo 'x' }"),
    "unknown escape": _steps("echo 'a\\qb'"),
    "lone dollar dq": _steps('echo "cost $5"'),
    "dollar name dot": _steps('echo "$A.txt"'),
    "leading zero int": _steps("retry(03) { echo 'x' }"),
    "underscore int": _steps("timeout(time: 1_000) { echo 'x' }"),
    "negative int": _steps("retry(-1) { echo 'x' }"),
    "env int value": _steps(ECHO, extra="environment { A = 5 }"),
    "env bool value": _steps(ECHO, extra="environment { A = true }"),
    "docker label option": _pipeline(
        "stages { stage('a') { steps { echo 'x' } } }",
        "agent { docker { image 'x:1'\n label 'z' } }",
    ),
    "docker reuseNode": _pipeline(
        "stages { stage('a') { steps { echo 'x' } } }",
        "agent { docker { image 'x:1'\n reuseNode true } }",
    ),
    "agent label": _pipeline("stages { stage('a') { steps { echo 'x' } } }", "agent { label 'x' }"),
    "list trailing comma": _steps("withEnv(['A=1',]) { echo 'x' }"),
    "quoted map key": _steps("dockerBuild(image: 'x')\ndeploy services: ['a-b': 'x']"),
    "stages before agent": "pipeline {\n stages { stage('a') { steps { echo 'x' } } }\n agent any\n}\n",
    "post before stages": "pipeline {\n agent any\n post { always { echo 'x' } }\n stages { stage('a') { steps { echo 'x' } } }\n}\n",
    "duplicate agent": "pipeline {\n agent any\n agent none\n stages { stage('a') { steps { echo 'x' } } }\n}\n",
    "duplicate environment": _steps(ECHO, extra="environment { A = 'a' }\nenvironment { B = 'b' }"),
    "newline before brace": "pipeline {\n agent any\n stages\n {\n stage('a')\n {\n steps\n {\n echo 'x'\n }\n }\n }\n}\n",
    "stage name interpolated": _stages("stage(\"a${BUILD_NUMBER}\") { steps { echo 'x' } }"),
    "empty stage name": _stages("stage('') { steps { echo 'x' } }"),
    "timeout unit DAYS": _steps("timeout(time: 1, unit: 'DAYS') { echo 'x' }"),
    "timeout no time": _steps("timeout(unit: 'MINUTES') { echo 'x' }"),
    "timeout string time": _steps("timeout(time: '5') { echo 'x' }"),
    "timeout no block": _steps("timeout(time: 5)"),
    "retry no block": _steps("retry(3)"),
    "retry string": _steps("retry('a') { echo 'x' }"),
    "echo no args": _steps("echo()"),
    "echo int": _steps("echo 5"),
    "sh no args": _steps("sh()"),
    "sh unknown arg": _steps("sh script: 'x', nope: 1"),
    "sh positional two": _steps("sh 'x', 'y'"),
    "sh positional + named": _steps("sh 'x', label: 'y'"),
    "dir no block": _steps("dir('x')"),
    "option unknown": _steps(ECHO, extra="options { buildDiscarder() }"),
    "option timeout DAYS": _steps(ECHO, extra="options { timeout(time: 1, unit: 'DAYS') }"),
    "option retry block": _steps(ECHO, extra="options { retry(2) { } }"),
    "param no name": _steps(ECHO, extra="parameters { string() }"),
    "param unknown type": _steps(ECHO, extra="parameters { text(name: 'A') }"),
    "param empty choices": _steps(ECHO, extra="parameters { choice(name: 'A', choices: []) }"),
    "param dup": _steps(ECHO, extra="parameters { string(name: 'A')\n string(name: 'A') }"),
    "param bool string default": _steps(
        ECHO, extra="parameters { booleanParam(name: 'A', defaultValue: 'x') }"
    ),
    "unknown section tools": _steps(ECHO, extra="tools { maven 'x' }"),
    "unknown post condition": _steps(ECHO, extra="post { fixed { } }"),
    "duplicate post condition": _steps(ECHO, extra="post { always { }\n always { } }"),
    "post condition fixed ok?": _steps(ECHO, extra="post { regression { } }"),
    "stage unknown section input": _stages(
        "stage('a') { input { message 'x' }\n steps { echo 'x' } }"
    ),
    "env dup": _steps(ECHO, extra="environment { A = 'a'\n A = 'b' }"),
    "env lowercase": _steps(ECHO, extra="environment { a = 'a' }"),
    "env credentials non-literal": _steps(ECHO, extra="environment { A = credentials(B) }"),
    "agent none stage agent": _pipeline(
        "stages { stage('a') { agent { docker 'x:1' }\n steps { echo 'x' } } }", "agent none"
    ),
    "two pipelines": "pipeline { agent any\n stages { stage('a') { steps { echo 'x' } } } }\npipeline { agent any }\n",
    "semicolon only top": ";pipeline { agent any\n stages { stage('a') { steps { echo 'x' } } } }",
    "script in post": _steps(ECHO, extra="post { always { script { echo 'x' } } }"),
    "unknown stage options": _stages("stage('a') { options { retry(2) }\n steps { echo 'x' } }"),
    "stage-level parameters": _stages(
        "stage('a') { parameters { string(name: 'A') }\n steps { echo 'x' } }"
    ),
    "mixed blank lines & comments": "// c\n\npipeline { // c\n agent any /* c */\n stages { /* c */ stage('a') { steps { echo 'x' } } }\n}\n// end\n",
    "triple single with escapes": _steps("sh '''\n echo \\'a\\' \\\\ \\n\n'''", DOCKER),
    "dq multi-line interp": _steps('sh """\n echo ${BUILD_NUMBER}\n"""', DOCKER),
    "map empty": _steps("deploy services: [:]"),
    "trailing comma args": _steps("sh(script: 'x',)", DOCKER),
    "string concat": _steps("echo 'a' + 'b'"),
    "env in steps unknown var": _steps('echo "${nope}"'),
    "timeout positional": _steps("timeout(5) { echo 'x' }"),
    "timeout positional unit": _steps("timeout(5, 'MINUTES') { echo 'x' }"),
    "retry named": _steps("retry(count: 3) { echo 'x' }"),
    "dir named": _steps("dir(path: 'x') { echo 'x' }"),
    "echo named": _steps("echo message: 'x'"),
    "error named": _steps("error message: 'x'"),
    "withEnv named": _steps("withEnv(overrides: ['A=1']) { echo 'x' }"),
    "echo paren dq": _steps('echo("x")'),
    "sh label only": _steps("sh label: 'x'", DOCKER),
    "sh script dq paren": _steps('sh(script: "x")', DOCKER),
    "empty string sh": _steps("sh ''", DOCKER),
    "mixed list": _steps("deploy services: [a: [b: 1]]"),
    "list of lists": _steps("withEnv([['A=1']]) { echo 'x' }"),
    "bool arg": _steps("sh script: 'x', returnStatus: false", DOCKER),
    "failFast in parallel": _stages(
        "stage('p') { failFast true\n parallel { stage('a') { steps { echo 'x' } } } }"
    ),
    "parallel failFast inside": _stages(
        "stage('p') { parallel { failFast true\n stage('a') { steps { echo 'x' } } } }"
    ),
    "parallel under sequential under parallel": _stages(
        "stage('p') { parallel { stage('b') { stages { stage('c') { parallel { stage('d') { steps { echo 'x' } } } } } } } }"
    ),
    "sequential x4": _stages(
        "stage('p') { stages { stage('b') { stages { stage('c') { stages { stage('d') { steps { echo 'x' } } } } } } } }"
    ),
    "stage agent+env+options+when+post nested": _stages(
        "stage('p') { stages { stage('b') { agent any\n environment { A = 'a' }\n options { retry(2) }\n when { branch 'x' }\n steps { echo 'x' }\n post { always { echo 'x' } } } } }"
    ),
    "stage post with parallel": _stages(
        "stage('p') { parallel { stage('a') { steps { echo 'x' } } }\n post { always { echo 'x' } } }"
    ),
    "comment between args": _steps("sh(script: 'x', // c\n label: 'y')", DOCKER),
    "block comment same line": _steps("echo 'a' /* c */ ; echo 'b'"),
    "crlf": _steps("echo 'a'").replace("\n", "\r\n"),
    "tabs": _steps("\techo 'a'"),
    "stage name unicode": _stages("stage('Café ☕') { steps { echo 'x' } }"),
    "stage name quotes": _stages("stage('it\\'s') { steps { echo 'x' } }"),
    "stage dq plain": _stages('stage("plain") { steps { echo "x" } }'),
    "env dq with escaped dollar": _steps(ECHO, extra='environment { A = "\\$HOME" }'),
    "env triple": _steps(ECHO, extra="environment { A = '''x\ny''' }"),
    "env single credential paren newline": _steps(
        ECHO, extra="environment { A = credentials(\n'NPM_TOKEN'\n) }"
    ),
    "options after stages": "pipeline {\n agent any\n stages { stage('a') { steps { echo 'x' } } }\n options { retry(2) }\n}\n",
    "agent docker newline brace": "pipeline {\n agent {\n docker\n {\n image 'x:1'\n }\n }\n stages { stage('a') { steps { echo 'x' } } }\n}\n",
    "agent newline brace": "pipeline {\n agent\n {\n docker 'x:1'\n }\n stages { stage('a') { steps { echo 'x' } } }\n}\n",
    "newline after stage paren": _stages("stage('a')\n{\n steps\n {\n echo 'x'\n }\n}"),
    "retry paren newline brace step": _steps("retry(3)\n{\n echo 'x'\n}"),
    "parameters trailing newline commas": _steps(
        ECHO,
        extra="parameters {\n choice(name: 'A',\n choices: ['a',\n 'b'],\n description: 'd')\n}",
    ),
    "when env cond paren": _stages(
        "stage('a') { when { environment(name: 'A', value: 'b') }\n steps { echo 'x' } }"
    ),
    "when branch paren": _stages("stage('a') { when { branch('a') }\n steps { echo 'x' } }"),
    "when anyOf one line": _stages(
        "stage('a') { when { anyOf { branch 'a'; branch 'b' } }\n steps { echo 'x' } }"
    ),
    "semicolons everywhere": "pipeline { agent any;\n stages { stage('a') { steps { echo 'x'; }; }; };\n};\n",
    "trailing semicolon top": "pipeline { agent any\n stages { stage('a') { steps { echo 'x' } } } };",
    "leading blank lines+bom": "﻿pipeline { agent any\n stages { stage('a') { steps { echo 'x' } } } }",
    "timeout seconds/hours": _steps(
        "timeout(time: 30, unit: 'SECONDS') { timeout(time: 1, unit: 'HOURS') { echo 'x' } }"
    ),
    "retry 10": _steps("retry(10) { echo 'x' }"),
    "post cleanup stage": _stages(
        "stage('a') { steps { echo 'x' }\n post { cleanup { echo 'x' }\n unstable { echo 'x' }\n aborted { echo 'x' }\n changed { echo 'x' } } }"
    ),
    "error step": _steps("error 'boom'"),
    "unknown step with block": _steps("foo { echo 'x' }"),
    "agent docker args dq": _pipeline(
        "stages { stage('a') { steps { echo 'x' } } }",
        'agent { docker { image "node:22"\n args "--cpus 1" } }',
    ),
    "same name parent and child": _stages(
        "stage('a') { stages { stage('a') { steps { echo 'x' } } } }"
    ),
    "same name across top and parallel": _stages(
        "stage('a') { steps { echo 'x' } }\n stage('p') { parallel { stage('a') { steps { echo 'x' } } } }"
    ),
    "same name in two parallel groups": _stages(
        "stage('p') { parallel { stage('a') { steps { echo 'x' } } } }\n stage('q') { parallel { stage('a') { steps { echo 'x' } } } }"
    ),
    "same name in two sequential groups": _stages(
        "stage('p') { stages { stage('a') { steps { echo 'x' } } } }\n stage('q') { stages { stage('a') { steps { echo 'x' } } } }"
    ),
    "duplicate retry options": _steps(ECHO, extra="options { retry(2)\n retry(3) }"),
    "duplicate timeout options": _steps(
        ECHO, extra="options { timeout(time: 1)\n timeout(time: 2) }"
    ),
    "duplicate disableConcurrent": _steps(
        ECHO, extra="options { disableConcurrentBuilds()\n disableConcurrentBuilds() }"
    ),
    "stage option duplicate": _stages(
        "stage('a') { options { retry(2)\n retry(3) }\n steps { echo 'x' } }"
    ),
    "parallel branch with env only": _stages(
        "stage('p') { parallel { stage('a') { environment { A = 'b' }\n steps { echo 'x' } } } }"
    ),
    "stage env var same as pipeline": _steps(ECHO, extra="environment { A = 'a' }").replace(
        "stage('a') {", "stage('a') { environment { A = 'b' }"
    ),
    "post at nested stage": _stages(
        "stage('p') { stages { stage('a') { steps { echo 'x' }\n post { always { echo 'x' } } } } }"
    ),
    "agent none no stage agent steps": _pipeline(
        "stages { stage('a') { steps { echo 'x' } } }", "agent none"
    ),
    "two when": _stages(
        "stage('a') { when { branch 'a' }\n when { branch 'b' }\n steps { echo 'x' } }"
    ),
    "environment cond missing value": _stages(
        "stage('a') { when { environment name: 'A' }\n steps { echo 'x' } }"
    ),
    "param name invalid": _steps(ECHO, extra="parameters { string(name: 'a b') }"),
    "param string default int": _steps(
        ECHO, extra="parameters { string(name: 'A', defaultValue: 5) }"
    ),
    "choices non string": _steps(ECHO, extra="parameters { choice(name: 'A', choices: [1, 2]) }"),
    "credentials two args": _steps(ECHO, extra="environment { A = credentials('a', 'b') }"),
    "docker image empty": _pipeline(
        "stages { stage('a') { steps { echo 'x' } } }", "agent { docker '' }"
    ),
    "docker no image": _pipeline(
        "stages { stage('a') { steps { echo 'x' } } }", "agent { docker { args 'x' } }"
    ),
    "docker image int": _pipeline(
        "stages { stage('a') { steps { echo 'x' } } }", "agent { docker { image 5 } }"
    ),
    "stage no name": _stages("stage { steps { echo 'x' } }"),
    "stage two names": _stages("stage('a', 'b') { steps { echo 'x' } }"),
    "retry zero": _steps("retry(0) { echo 'x' }"),
    "env var interp string no braces": _steps('echo "a$B"', extra="environment { B = 'x' }"),
}
