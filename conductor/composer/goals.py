"""Goal construction — build focused execution briefs for plan nodes."""

from __future__ import annotations

from conductor.composer.models import ComposerPlan, NormalizedSpec, TaskNode

__all__ = ["build_task_brief", "build_integration_brief"]


def build_task_brief(
    node: TaskNode,
    spec: NormalizedSpec,
    completed_deps: list[TaskNode] | None = None,
    overall_summary: str = "",
) -> str:
    """Build a focused execution brief for an individual task node.

    Bounded in size — does not dump the entire specification verbatim.
    """
    lines: list[str] = []
    lines.append("You are implementing one task from a predefined specification.")
    lines.append("")

    if overall_summary:
        lines.append(f"Overall objective: {overall_summary[:500]}")
        lines.append("")

    lines.append(f"Your task: {node.goal}")
    lines.append("")

    if node.file_scope:
        lines.append("File ownership:")
        for f in node.file_scope:
            lines.append(f"  - {f}")
        lines.append("")

    if node.ownership_notes:
        lines.append(node.ownership_notes)
        lines.append("")

    if completed_deps:
        lines.append("Relevant completed dependencies:")
        for dep in completed_deps:
            branch_ref = ""
            if dep.branch:
                branch_ref = f" (branch: {dep.branch}"
                if dep.commit_sha:
                    branch_ref += f", commit: {dep.commit_sha[:8]}"
                branch_ref += ")"
            lines.append(f"  - {dep.node_id}: {dep.title}{branch_ref}")
        lines.append("")

    if node.required_skills:
        lines.append(f"Required skills: {', '.join(node.required_skills)}")
        lines.append("")

    if node.required_capabilities:
        lines.append(f"Required capabilities: {', '.join(node.required_capabilities)}")
        lines.append("")

    if node.verification.commands:
        lines.append("Verification:")
        for cmd in node.verification.commands:
            req = "required" if cmd.required else "optional"
            lines.append(f"  - {cmd.name}: `{cmd.command}` ({req})")
        lines.append("")

    lines.append("Work only in your assigned worktree.")
    lines.append("Do not declare completion until all required verification passes.")
    lines.append("When tests fail, diagnose and continue working.")
    lines.append("Follow the supplied specification; do not invent unrelated product behavior.")
    lines.append("Record any implementation assumptions in the final report.")

    return "\n".join(lines)


def build_integration_brief(
    spec: NormalizedSpec,
    completed_tasks: list[TaskNode],
    integration_profile: str = "opencode-deepseek",
    base_branch: str = "master",
) -> str:
    """Build the goal text for the integration task."""
    lines: list[str] = []
    lines.append("Integrate the completed task branches into one final branch.")
    lines.append("")

    if completed_tasks:
        lines.append("Dependency outputs:")
        for t in completed_tasks:
            branch = t.branch or "unknown"
            commit = t.commit_sha or "unknown"
            lines.append(f"  - {t.node_id}: branch {branch}, commit {commit[:12] if commit != 'unknown' else 'unknown'}")
        lines.append("")

    lines.append("Merge or cherry-pick these commits in the listed order.")
    lines.append("Resolve integration conflicts according to the specification.")
    lines.append("")

    if spec.acceptance_criteria:
        lines.append("Acceptance criteria:")
        for ac in spec.acceptance_criteria:
            lines.append(f"  - {ac}")
        lines.append("")

    lines.append("Run the complete project test suite and required live E2E.")
    lines.append("Do not mark complete until all verification passes.")
    lines.append(f"Base your work on the `{base_branch}` branch.")
    lines.append("")

    return "\n".join(lines)
