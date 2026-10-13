"""Compare the platform's verdict with real Jenkins' Declarative linter (see jenkins_lint.sh).

Records Jenkins' verdicts in ``tests/fixtures/jenkins_verdicts.json`` (used offline by the
test-suite) and rewrites ``docs/jenkins-compatibility.md``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.jenkins_cases import POLICY, VERDICTS, all_cases, our_verdict  # noqa: E402

DOC = ROOT / "docs" / "jenkins-compatibility.md"
OK_MARK = "Jenkinsfile successfully validated"


def jenkins_verdict(text: str) -> str:
    """``accepted`` or ``rejected: <first error lines>`` from the linter."""
    url = os.environ["JENKINS_URL"] + "/pipeline-model-converter/validate"
    data = urllib.parse.urlencode({"jenkinsfile": text}).encode()
    with urllib.request.urlopen(url, data, timeout=120) as response:  # noqa: S310
        body = response.read().decode()
    if OK_MARK in body:
        return "accepted"
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return "rejected: " + " | ".join(lines[1:3] or lines[:1])


def classify(name: str, ours: str, jenkins: str) -> str:
    """Relationship of the two verdicts."""
    if (ours == "accepted") == (jenkins == "accepted"):
        return "same"
    if jenkins == "accepted":
        return POLICY.get(name, "UNCLASSIFIED")
    return "BUG: Jenkins rejects, we accept"


def cell(text: str, width: int = 110) -> str:
    """Markdown-safe, shortened table cell."""
    return text.replace("|", "/").replace("\n", " ")[:width].replace("`", "'")


def write_doc(rows: list[tuple[str, str, str, str]]) -> None:
    lines = [DOC_HEAD]
    for title, prefix in DOC_SECTIONS:
        lines.append(f"\n## {title}\n\n| Case | Ours | Jenkins | Difference |\n|---|---|---|---|")
        for name, ours, jenkins, kind in rows:
            if name.startswith(prefix):
                short = name[len(prefix) :]
                note = "" if kind == "same" else kind
                lines.append(f"| {short} | {cell(ours, 70)} | {cell(jenkins, 80)} | {note} |")
    DOC.parent.mkdir(exist_ok=True)
    DOC.write_text("\n".join(lines) + "\n")


DOC_SECTIONS = [
    ("Valid fixtures", "valid/"),
    ("Documentation example and serializer output", "docs/"),
    ("Starter templates", "template/"),
    ("Serialized valid fixtures", "serialized/"),
    ("Invalid fixtures", "invalid/"),
    ("Syntax probes", "probe/"),
]
DOC_HEAD = (ROOT / "scripts" / "jenkins_compat_doc_head.md").read_text()


def main() -> int:
    extra = {path: Path(path).read_text() for path in sys.argv[1:]}
    cases = {**all_cases(), **extra}
    results = {name: (our_verdict(text), jenkins_verdict(text)) for name, text in cases.items()}
    rows = [(n, o, j, classify(n, o, j)) for n, (o, j) in results.items()]
    for name, ours, jenkins, kind in rows:
        if kind != "same":
            print(f"{kind}\n  {name}\n    ours:    {cell(ours)}\n    jenkins: {cell(jenkins)}")
    if not extra:
        recorded = {n: j == "accepted" for n, (_, j) in results.items()}
        VERDICTS.write_text(json.dumps(recorded, indent=1, sort_keys=True) + "\n")
        write_doc(rows)
    bad = [r for r in rows if r[3].startswith(("BUG", "UNCLASSIFIED"))]
    print(f"\n{len(rows)} pipelines, {len(bad)} unresolved differences")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
