import random
from dataclasses import replace

import pytest

from shiftcodes.pipeline import Pipeline, parse, serialize
from shiftcodes.pipeline.ast import StepsBody, StringValue, TextPart
from shiftcodes.pipeline.errors import Span
from tests.pipeline_support import parse_wrapped, wrap

SAMPLE = """
// comments vanish
pipeline {
    stages { stage('Build') {
        post { success { echo 'ok' }; always { echo "b ${BUILD_NUMBER}" } }
        when { branch 'main'; environment name: 'ENV', value: 'x' }
        steps { sh 'make'; retry(2) { sh script: 'a', label: 'b' }; dockerBuild image: 'app' }
        options { retry(1) }
        environment { A = 'x'; S = credentials('K') }
        agent { docker { image 'node:22'; args '--cpus 1' } }
    } }
    agent any
    options { timeout(time: 5, unit: 'MINUTES') }
}
"""

CANONICAL = """pipeline {
    agent any
    options {
        timeout(time: 5, unit: 'MINUTES')
    }
    stages {
        stage('Build') {
            agent {
                docker {
                    image 'node:22'
                    args '--cpus 1'
                }
            }
            environment {
                A = 'x'
                S = credentials('K')
            }
            options {
                retry(1)
            }
            when {
                branch 'main'
                environment name: 'ENV', value: 'x'
            }
            steps {
                sh 'make'
                retry(2) {
                    sh(script: 'a', label: 'b')
                }
                dockerBuild(image: 'app')
            }
            post {
                always {
                    echo "b ${BUILD_NUMBER}"
                }
                success {
                    echo 'ok'
                }
            }
        }
    }
}
"""


def test_canonical_format_and_section_order() -> None:
    assert serialize(parse(SAMPLE)) == CANONICAL


def test_serialization_is_stable() -> None:
    assert serialize(parse(CANONICAL)) == CANONICAL


@pytest.mark.parametrize(
    "step",
    [
        "echo 'plain'",
        'echo "v ${BRANCH_NAME} and ${env.PROJECT} and $BUILD_NUMBER"',
        "echo 'it\\'s \"quoted\" \\\\ \\n \\t'",
        'echo "cost \\$5 \\" \\n"',
        "sh '''\n  line1\n  'q' \"d\" \\\\\n'''",
        'sh """\n  ${BUILD_NUMBER} \\$HOME "x"\n"""',
        "withEnv(['A=1', \"B=${BUILD_NUMBER}\"]) { echo 'x' }",
        "deploy services: ['a-b': 'x', 'true': 'y', c: 'z']",
        "dockerBuild image: 'a', args: [:]",
        "sh script: 'x', label: ''",
        "echo ''",
    ],
)
def test_string_forms_round_trip(step: str) -> None:
    pipeline = parse_wrapped(step)
    assert parse(serialize(pipeline)) == pipeline


def with_message(pipeline: Pipeline, text: str) -> Pipeline:
    """``pipeline`` with the message of its first echo replaced by the literal ``text``."""
    stage = pipeline.stages[0]
    assert isinstance(stage.body, StepsBody)
    call = stage.body.steps[0]
    value = StringValue((TextPart(text),) if text else (), Span(1, 1))
    new_call = replace(call, args=(replace(call.args[0], value=value),))
    new_stage = replace(stage, body=replace(stage.body, steps=(new_call,)))
    return replace(pipeline, stages=(new_stage,))


def test_random_strings_round_trip() -> None:
    rng = random.Random(1234)  # noqa: S311 - deterministic test data
    alphabet = ["a", " ", "'", '"', "\\", "$", "{", "}", "\n", "\t", "\r", "é", "${", "'''", '"""']
    base = parse_wrapped("echo 'x'")
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        changed = with_message(base, text)
        assert parse(serialize(changed)) == changed, repr(text)


def test_comments_are_not_preserved() -> None:
    assert "comments vanish" not in serialize(parse(SAMPLE))


def test_unknown_order_of_post_blocks_is_canonical() -> None:
    text = wrap("echo 'x'", "agent any").replace(
        "}\n}\n}\n", "}\n}\n}\npost { cleanup { }; always { } }\n", 1
    )
    assert serialize(parse(text)).index("always") < serialize(parse(text)).index("cleanup")
