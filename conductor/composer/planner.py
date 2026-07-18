"""Plan validation — deterministic checks before activating a plan."""

from __future__ import annotations

from conductor.composer.models import (
    ComposerContext,
    ComposerPlan,
    LLMIntegrationNode,
    LLMTaskNode,
    PlanResult,
    PlanValidationResult,
)

__all__ = ["validate_plan", "validate_plan_result"]


def validate_plan_result(
    plan: PlanResult,
    context: ComposerContext,
) -> PlanValidationResult:
    """Validate plan LLM output before it becomes a ComposerPlan."""
    errors: list[str] = []
    warnings: list[str] = []

    # Unique node IDs
    node_ids = [t.node_id for t in plan.tasks]
    if plan.integration and plan.integration.required:
        node_ids.append(plan.integration.node_id)

    seen: set[str] = set()
    for nid in node_ids:
        if not nid:
            errors.append("Task with empty node_id")
            continue
        if nid in seen:
            errors.append(f"Duplicate node_id: {nid}")
        seen.add(nid)

    # Dependencies reference existing nodes
    all_node_ids = set(node_ids)
    for t in plan.tasks:
        for dep in t.dependencies:
            if dep not in all_node_ids:
                errors.append(
                    f"Task '{t.node_id}' depends on unknown node '{dep}'"
                )

    # Integration depends on all required implementation tasks
    if plan.integration and plan.integration.required:
        for t in plan.tasks:
            if t.node_id not in plan.integration.dependencies:
                warnings.append(
                    f"Implementation task '{t.node_id}' is not a dependency of the integration task"
                )

    # No self-dependency
    for t in plan.tasks:
        if t.node_id in t.dependencies:
            errors.append(f"Task '{t.node_id}' depends on itself")

    # Acyclic
    errors.extend(_check_dag(plan))

    # Harness profiles exist
    available_profiles = {p.name for p in context.harness_profiles}
    for t in plan.tasks:
        if not t.harness_profile:
            errors.append(f"Task '{t.node_id}' has no harness_profile")
        elif available_profiles and t.harness_profile not in available_profiles:
            errors.append(f"Task '{t.node_id}' uses unknown harness profile '{t.harness_profile}'")
    if plan.integration and plan.integration.required:
        if not available_profiles or plan.integration.node_id not in available_profiles:
            # integration uses default profile — warn, not error
            pass

    # Skills exist
    available_skill_ids = {s.id for s in context.skills if s.id}
    for t in plan.tasks:
        for skill in t.required_skills:
            if available_skill_ids and skill not in available_skill_ids:
                errors.append(f"Task '{t.node_id}' requires unknown skill '{skill}'")

    # Capabilities have providers
    available_caps = {c.capability for c in context.capabilities if c.available}
    for t in plan.tasks:
        for cap in t.required_capabilities:
            if available_caps and cap not in available_caps:
                errors.append(f"Task '{t.node_id}' requires unavailable capability '{cap}'")

    # Verification defined
    for t in plan.tasks:
        if t.verification.required and not t.verification.commands:
            errors.append(f"Task '{t.node_id}' has required verification but no commands")

    # At least two implementation tasks
    impl_tasks = [t for t in plan.tasks if t.task_type == "implementation"]
    if len(impl_tasks) < 2:
        warnings.append("Plan should have at least two implementation tasks for parallel execution")

    # File overlap
    _check_file_overlap(plan, warnings)

    valid = len(errors) == 0
    return PlanValidationResult(valid=valid, errors=errors, warnings=warnings)


def validate_plan(plan: ComposerPlan, context: ComposerContext) -> PlanValidationResult:
    """Validate an existing ComposerPlan object."""
    errors: list[str] = []
    warnings: list[str] = []

    node_ids = [t.node_id for t in plan.tasks]
    if plan.integration:
        node_ids.append(plan.integration.node_id)

    seen: set[str] = set()
    for nid in node_ids:
        if not nid:
            errors.append("Task with empty node_id")
            continue
        if nid in seen:
            errors.append(f"Duplicate node_id: {nid}")
        seen.add(nid)

    all_node_ids = set(node_ids)
    for t in plan.tasks:
        for dep in t.dependencies:
            if dep not in all_node_ids:
                errors.append(f"Task '{t.node_id}' depends on unknown node '{dep}'")

    if plan.integration:
        for t in plan.tasks:
            if t.node_id not in plan.integration.dependencies:
                warnings.append(
                    f"Implementation task '{t.node_id}' is not a dependency of the integration task"
                )

    for t in plan.tasks:
        if t.node_id in t.dependencies:
            errors.append(f"Task '{t.node_id}' depends on itself")

    # DAG check
    node_map = {t.node_id: t for t in plan.tasks}
    if plan.integration:
        node_map[plan.integration.node_id] = plan.integration  # type: ignore
    errors.extend(_check_dag_nodes(node_map))

    available_profiles = {p.name for p in context.harness_profiles}
    for t in plan.tasks:
        if available_profiles and t.harness_profile and t.harness_profile not in available_profiles:
            errors.append(f"Task '{t.node_id}' uses unknown harness profile '{t.harness_profile}'")

    available_caps = {c.capability for c in context.capabilities if c.available}
    for t in plan.tasks:
        for cap in t.required_capabilities:
            if available_caps and cap not in available_caps:
                errors.append(f"Task '{t.node_id}' requires unavailable capability '{cap}'")

    for t in plan.tasks:
        if t.verification.required and not t.verification.commands:
            errors.append(f"Task '{t.node_id}' has required verification but no commands")

    _check_file_overlap_plan(plan, warnings)

    return PlanValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)


def _check_dag(plan: PlanResult) -> list[str]:
    """Check acyclicity of plan result."""
    errors: list[str] = []
    node_map: dict[str, list[str]] = {}
    for t in plan.tasks:
        node_map[t.node_id] = t.dependencies
    if plan.integration and plan.integration.required:
        node_map[plan.integration.node_id] = plan.integration.dependencies

    if _has_cycle(node_map):
        errors.append("Plan task graph has a cycle")
    return errors


def _check_dag_nodes(node_map: dict) -> list[str]:
    errors: list[str] = []
    dep_map = {nid: (n.dependencies if hasattr(n, "dependencies") else []) for nid, n in node_map.items()}
    if _has_cycle(dep_map):
        errors.append("Plan task graph has a cycle")
    return errors


_CYCLE_WHITE = 0
_CYCLE_GRAY = 1
_CYCLE_BLACK = 2


def _has_cycle(node_map: dict[str, list[str]]) -> bool:
    color = {n: _CYCLE_WHITE for n in node_map}
    for node in node_map:
        if color[node] == _CYCLE_WHITE:
            if _dfs_cycle(node, node_map, color):
                return True
    return False


def _dfs_cycle(node: str, node_map: dict[str, list[str]], color: dict) -> bool:
    color[node] = _CYCLE_GRAY
    for dep in node_map.get(node, []):
        if dep not in color:
            continue
        if color[dep] == _CYCLE_GRAY:
            return True
        if color[dep] == _CYCLE_WHITE and _dfs_cycle(dep, node_map, color):
            return True
    color[node] = _CYCLE_BLACK
    return False


def _check_file_overlap(plan: PlanResult, warnings: list[str]) -> None:
    """Warn about overlapping file scopes across parallel tasks."""
    task_scopes: dict[str, set[str]] = {}
    for t in plan.tasks:
        if t.file_scope:
            task_scopes[t.node_id] = set(t.file_scope)

    # All tasks with no dependencies run in parallel
    independent = {t.node_id for t in plan.tasks if not t.dependencies}
    if len(independent) > 1:
        # Check if any of the independent tasks have overlapping file scopes
        all_scopes = set()
        for nid in independent:
            for f in task_scopes.get(nid, set()):
                if f in all_scopes:
                    warnings.append(
                        f"Parallel tasks {independent} have overlapping file scopes — serialize or assign to integration"
                    )
                    return
                all_scopes.add(f)



def _check_file_overlap_plan(plan: ComposerPlan, warnings: list[str]) -> None:
    task_scopes: dict[str, set[str]] = {}
    for t in plan.tasks:
        if t.file_scope:
            task_scopes[t.node_id] = set(t.file_scope)

    independent = {t.node_id for t in plan.tasks if not t.dependencies}
    if len(independent) > 1:
        all_scopes = set()
        for nid in independent:
            for f in task_scopes.get(nid, set()):
                if f in all_scopes:
                    warnings.append(
                        f"Parallel tasks {independent} have overlapping file scopes — serialize or assign to integration"
                    )
                    return
                all_scopes.add(f)
