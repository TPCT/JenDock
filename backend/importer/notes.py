"""Findings of an import: plain warnings and the subset that needs an operator decision."""

from __future__ import annotations

from dataclasses import dataclass, field

AMBIGUOUS_IMAGE = "ambiguous service -> image guesses"
UNMAPPED_SERVICE = "services without an image (not deployed by the generated pipeline)"
MISSPELLED_BRANCH = "branches that look misspelled (kept as written in v1)"
BRANCH_ASSIGNMENT = "single v1 branch assigned to an environment by name (not by v1 data)"
MISSING_ENVIRONMENT = "other environment has no v1 data (defaults used)"
DROPPED_TMPFS = "tmpfs mounts dropped from tests (v2 has no equivalent)"
CREDENTIAL_LIKE_ENV = "test environment variables that look like credentials"

# Order of the summary at the top of a dry run.
DECISION_ORDER = (
    AMBIGUOUS_IMAGE,
    UNMAPPED_SERVICE,
    MISSPELLED_BRANCH,
    BRANCH_ASSIGNMENT,
    MISSING_ENVIRONMENT,
    DROPPED_TMPFS,
    CREDENTIAL_LIKE_ENV,
)


@dataclass(frozen=True, slots=True)
class Decision:
    """Something a human has to look at, with its category."""

    category: str
    text: str


@dataclass(slots=True)
class Notes:
    """Collects warnings; ``decide`` records a warning that also needs an operator decision."""

    warnings: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)

    def warn(self, text: str) -> None:
        """An informational warning."""
        self.warnings.append(text)

    def decide(self, category: str, text: str) -> None:
        """A warning the operator must decide on (also listed in the summary)."""
        self.warnings.append(text)
        self.decisions.append(Decision(category, text))
