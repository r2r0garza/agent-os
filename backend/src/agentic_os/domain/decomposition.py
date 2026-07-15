from __future__ import annotations

from dataclasses import dataclass, field

from agentic_os.domain.capabilities import CAPABILITY_CATALOG

"""Deterministic goal decomposition for the first supported multi-task workflow.

Decomposition is template-based rather than model-generated so its output is
reproducible for tests and never references a capability outside the
explicit, inspectable catalog. Selecting eligible agent versions for the
produced capabilities is out of scope here; this module only produces the
inspectable task graph and the rationale the scheduler will later use.
"""


class UnsupportedWorkflowError(ValueError):
    def __init__(self, workflow: str) -> None:
        super().__init__(f"unsupported decomposition workflow: {workflow!r}")
        self.workflow = workflow


class UnknownCapabilityError(ValueError):
    def __init__(self, capability: str) -> None:
        super().__init__(f"decomposition referenced an unsupported capability: {capability!r}")
        self.capability = capability


@dataclass(frozen=True)
class ExpectedOutputSpec:
    name: str
    kind: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class ResourceIntentSpec:
    resource_key: str
    intent: str


@dataclass(frozen=True)
class CapabilityRationale:
    reason: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskBlueprint:
    client_id: str
    title: str
    description: str | None
    required_capabilities: dict[str, bool]
    capability_rationale: dict[str, CapabilityRationale]
    expected_outputs: tuple[ExpectedOutputSpec, ...] = field(default_factory=tuple)
    resource_intent: tuple[ResourceIntentSpec, ...] = field(default_factory=tuple)
    depends_on: tuple[str, ...] = field(default_factory=tuple)


def _research_brief_template(title: str, description: str | None) -> list[TaskBlueprint]:
    del description  # not yet used to branch template shape; reserved for future constrained workflows
    return [
        TaskBlueprint(
            client_id="gather-research",
            title=f"Research: {title}",
            description="Gather and record source material supporting the goal.",
            required_capabilities={"research": True},
            capability_rationale={
                "research": CapabilityRationale(
                    reason="The draft cannot be written before source material has been gathered.",
                    evidence=("agent_version.capability_manifest.capabilities contains 'research'",),
                )
            },
            expected_outputs=(
                ExpectedOutputSpec(
                    name="research-notes", kind="artifact", description="Findings and sources supporting the goal."
                ),
            ),
            resource_intent=(ResourceIntentSpec(resource_key="notes/research.md", intent="write"),),
        ),
        TaskBlueprint(
            client_id="draft-output",
            title=f"Draft: {title}",
            description="Produce a draft using the gathered research.",
            required_capabilities={"writing": True},
            capability_rationale={
                "writing": CapabilityRationale(
                    reason="The goal's output must be authored before it can be reviewed.",
                    evidence=("agent_version.capability_manifest.capabilities contains 'writing'",),
                )
            },
            expected_outputs=(ExpectedOutputSpec(name="draft", kind="artifact", description="Draft output."),),
            resource_intent=(
                ResourceIntentSpec(resource_key="notes/research.md", intent="read"),
                ResourceIntentSpec(resource_key="drafts/output.md", intent="write"),
            ),
            depends_on=("gather-research",),
        ),
        TaskBlueprint(
            client_id="review-output",
            title=f"Review: {title}",
            description="Review the draft and produce the final reviewed result.",
            required_capabilities={"review": True},
            capability_rationale={
                "review": CapabilityRationale(
                    reason="A drafted output requires review before the goal can be considered complete.",
                    evidence=("agent_version.capability_manifest.capabilities contains 'review'",),
                )
            },
            expected_outputs=(
                ExpectedOutputSpec(name="final-report", kind="artifact", description="Reviewed final result."),
            ),
            resource_intent=(
                ResourceIntentSpec(resource_key="drafts/output.md", intent="read"),
                ResourceIntentSpec(resource_key="reports/final.md", intent="write"),
            ),
            depends_on=("draft-output",),
        ),
    ]


_WORKFLOW_TEMPLATES = {"research_brief": _research_brief_template}


def _validate_capabilities(blueprints: list[TaskBlueprint]) -> None:
    for blueprint in blueprints:
        for capability_name in blueprint.required_capabilities:
            if capability_name not in CAPABILITY_CATALOG:
                raise UnknownCapabilityError(capability_name)
        if set(blueprint.capability_rationale) != set(blueprint.required_capabilities):
            raise UnknownCapabilityError(
                next(iter(set(blueprint.capability_rationale) ^ set(blueprint.required_capabilities)))
            )


def decompose_goal(
    *, title: str, description: str | None = None, workflow: str = "research_brief"
) -> list[TaskBlueprint]:
    template = _WORKFLOW_TEMPLATES.get(workflow)
    if template is None:
        raise UnsupportedWorkflowError(workflow)
    blueprints = template(title, description)
    _validate_capabilities(blueprints)
    return blueprints
