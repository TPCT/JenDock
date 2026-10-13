#!/usr/bin/env bash
# Lint pipelines with a real Jenkins (Declarative linter) in a throwaway container and compare
# the verdicts with the platform's own engine. Usage:
#   scripts/jenkins_lint.sh [--purge] [extra.groovy ...]
# Without arguments it lints every fixture, the README example and the serializer output of each
# valid fixture, and rewrites docs/jenkins-compatibility.md. The container is always removed on
# exit; the plugin image is kept for fast reruns unless --purge is given.
set -euo pipefail

TAG="2.541.3-lts-jdk17"
IMAGE="shiftcodes-jenkins-lint:${TAG}"
NAME="shiftcodes-jenkins-lint-$$"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-${ROOT}/.venv/bin/python}"
PURGE=0
if [[ "${1:-}" == "--purge" ]]; then PURGE=1; shift; fi

cleanup() {
  docker rm -f "${NAME}" >/dev/null 2>&1 || true
  if [[ "${PURGE}" == 1 ]]; then docker rmi -f "${IMAGE}" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  docker build -q -t "${IMAGE}" - <<DOCKERFILE
FROM jenkins/jenkins:${TAG}
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"
RUN jenkins-plugin-cli --plugins workflow-aggregator pipeline-model-definition docker-workflow
RUN mkdir -p /usr/share/jenkins/ref/init.groovy.d \
 && printf '%s\n' \
  'import jenkins.model.*' 'import hudson.security.*' \
  'def j = Jenkins.get()' 'j.setCrumbIssuer(null)' \
  'j.setSecurityRealm(SecurityRealm.NO_AUTHENTICATION)' \
  'j.setAuthorizationStrategy(AuthorizationStrategy.UNSECURED)' 'j.save()' \
  > /usr/share/jenkins/ref/init.groovy.d/open.groovy
DOCKERFILE
fi

docker run -d --name "${NAME}" -p 127.0.0.1::8080 "${IMAGE}" >/dev/null
PORT="$(docker port "${NAME}" 8080/tcp | head -1 | sed 's/.*://')"
export JENKINS_URL="http://127.0.0.1:${PORT}"
for _ in $(seq 1 120); do
  if curl -fs "${JENKINS_URL}/api/json" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fs "${JENKINS_URL}/api/json" >/dev/null || { echo "Jenkins did not start" >&2; exit 1; }

cd "${ROOT}"
"${PYTHON}" scripts/jenkins_compat.py "$@"
