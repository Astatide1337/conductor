"""Context builder for Composer planning.

Gathers a bounded, useful context package before planning.  Does not
blindly dump the entire repository into the LLM.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from conductor.composer.models import (
    CapabilityInfo,
    ComposerContext,
    GatewayInfo,
    HarnessProfileInfo,
    SkillInfo,
)
from conductor.gateways.capabilities import list_capabilities
from conductor.gateways.health import check_all_gateways
from conductor.gateways.registry import GatewayRegistry

logger = logging.getLogger(__name__)

__all__ = ["build_composer_context", "context_to_prompt"]


def _get(obj, attr: str, default=None):
    """Get attribute from either a dict (via key) or dataclass/object (via attr)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def build_composer_context(
    objective_id: str,
    spec: dict | None,
    *,
    repo_path: str = "",
    registry: GatewayRegistry | None = None,
    agents_gateway_client=None,
    skills_gateway_client=None,
    wiki_mcp_client=None,
    mcp_gateway_client=None,
) -> ComposerContext:
    """Build composer context for planning.

    Missing optional context (wiki, skills) does not block.
    Missing required repository access blocks (handled by caller).
    """
    gateway_infos: list[GatewayInfo] = []
    capability_infos: list[CapabilityInfo] = []
    harness_infos: list[HarnessProfileInfo] = []
    skill_infos: list[SkillInfo] = []
    memory: list[dict] = []

    # Gateway status
    if registry:
        statuses = check_all_gateways(registry)
        for st in statuses:
            gateway_infos.append(
                GatewayInfo(
                    id=st.id,
                    name=st.name,
                    kind=st.kind,
                    enabled=st.enabled,
                    configured=st.configured,
                    status=st.status,
                )
            )

        # Capabilities
        for cap in list_capabilities(registry):
            capability_infos.append(
                CapabilityInfo(
                    capability=cap.capability,
                    gateway_id=cap.gateway_id,
                    available=cap.available,
                )
            )

    # Harness profiles from Agents Gateway
    if agents_gateway_client:
        try:
            profiles = agents_gateway_client.list_harness_profiles()
            for p in profiles:
                p_name = _get(p, "name", "")
                # Try to get availability
                availability = None
                try:
                    availability = agents_gateway_client.check_harness_availability(p_name)
                except Exception:
                    pass
                harness_infos.append(
                    HarnessProfileInfo(
                        name=p_name,
                        harness=_get(p, "harness", ""),
                        display_name=_get(p, "display_name", p_name),
                        configured=_get(availability, "configured", False) if availability else False,
                        runnable=_get(availability, "runnable", False) if availability else False,
                        binary_present=_get(availability, "binary_present", False) if availability else False,
                        credentials_present=_get(availability, "credentials_present", False) if availability else False,
                        command=_get(availability, "command", "") if availability else _get(p, "command", ""),
                    )
                )
        except Exception as exc:
            logger.warning("Failed to list harness profiles: %s", exc)

    # Skills from Skills Gateway
    if skills_gateway_client:
        try:
            skills = skills_gateway_client.list_skills()
            for s in skills:
                skill_infos.append(
                    SkillInfo(
                        id=_get(s, "id", ""),
                        name=_get(s, "name", ""),
                        description=_get(s, "description", ""),
                        tags=_get(s, "tags", []),
                    )
                )
        except Exception as exc:
            logger.warning("Failed to list skills: %s", exc)

    # Wiki-mcp project context
    if wiki_mcp_client:
        try:
            ctx = wiki_mcp_client.read_context(objective_id)
            if ctx:
                memory.append(ctx if isinstance(ctx, dict) else {"raw": str(ctx)})
        except Exception as exc:
            logger.warning("Failed to read wiki context: %s", exc)

    # Project context from local repo or MCP gateway
    project_context: dict = {}
    repo_url = (spec or {}).get("normalized_spec", {}).get("repository", {}).get("url", "") if spec else ""
    if repo_path and os.path.isdir(repo_path):
        project_context = _build_project_context(repo_path)
    elif repo_url and mcp_gateway_client:
        try:
            project_context = _build_remote_context(mcp_gateway_client, repo_url)
        except Exception as exc:
            logger.warning("Failed to fetch remote repo context: %s", exc)
            project_context["repo_url"] = repo_url
    elif spec and spec.get("normalized_spec", {}).get("repository", {}).get("url"):
        project_context["repo_url"] = spec["normalized_spec"]["repository"]["url"]

    return ComposerContext(
        spec=spec or {},
        repository=(spec or {}).get("normalized_spec", {}).get("repository", {}),
        project_context=project_context,
        gateways=gateway_infos,
        capabilities=capability_infos,
        harness_profiles=harness_infos,
        skills=skill_infos,
        memory=memory,
    )


def _build_project_context(repo_path: str) -> dict:
    """Read README, AGENTS.md, CLAUDE.md, and a tree summary."""
    root = Path(repo_path)
    readfiles = {"README.md": "readme", "README.rst": "readme", "README": "readme",
                 "AGENTS.md": "agent_instructions", "CLAUDE.md": "agent_instructions"}
    ctx: dict = {}
    for fname, key in readfiles.items():
        fpath = root / fname
        if fpath.is_file():
            try:
                content = fpath.read_text(errors="replace")
                ctx[key] = content[:4000]  # bounded
            except Exception:
                pass

    # Architecture docs
    arch_paths = [
        root / "docs" / "architecture.md",
        root / "docs" / "ARCHITECTURE.md",
        root / "ARCHITECTURE.md",
    ]
    for ap in arch_paths:
        if ap.is_file():
            try:
                ctx["architecture_summary"] = ap.read_text(errors="replace")[:4000]
            except Exception:
                pass
            break

    # Pared tree summary (depth-limited)
    tree_summary: list[str] = []
    for item in sorted(root.iterdir()):
        if item.name.startswith(".") and item.name not in (".env.example",):
            continue
        if item.is_dir():
            tree_summary.append(item.name + "/")
        else:
            tree_summary.append(item.name)
    ctx["tree_summary"] = tree_summary

    return ctx


def _build_remote_context(mcp_client, repo_url: str) -> dict:
    """Fetch bounded repo context via MCP Gateway/GitHub capability."""
    ctx: dict[str, any] = {"repo_url": repo_url}
    files_to_fetch = ["README.md", "AGENTS.md", "CLAUDE.md", "docs/architecture.md", "ARCHITECTURE.md"]
    for filename in files_to_fetch:
        try:
            content = mcp_client.read_file(repo_url, filename)
            if content:
                if "README" in filename:
                    ctx["readme"] = content[:4000]
                elif filename in ("AGENTS.md", "CLAUDE.md"):
                    ctx["agent_instructions"] = ctx.get("agent_instructions", "") + "\n" + content[:2000]
                elif "architecture" in filename.lower():
                    ctx["architecture_summary"] = content[:4000]
        except Exception:
            pass
    try:
        tree = mcp_client.list_files(repo_url, depth=2)
        if tree:
            ctx["tree_summary"] = tree[:200] if isinstance(tree, list) else str(tree)[:500]
    except Exception:
        pass
    return ctx


def context_to_prompt(ctx: ComposerContext) -> str:
    """Render context into a concise prompt string for the LLM."""
    lines: list[str] = []
    spec = ctx.spec or {}
    ns = spec.get("normalized_spec", {})
    if ns.get("goal"):
        lines.append(f"Goal: {ns['goal']}")
    if ns.get("requirements"):
        lines.append("Requirements:")
        for r in ns["requirements"]:
            lines.append(f"  - {r}")
    if ns.get("acceptance_criteria"):
        lines.append("Acceptance criteria:")
        for a in ns["acceptance_criteria"]:
            lines.append(f"  - {a}")
    pc = ctx.project_context
    if pc.get("readme"):
        lines.append(f"README excerpt:\n{pc['readme'][:1500]}")
    if pc.get("agent_instructions"):
        lines.append(f"Agent instructions:\n{pc['agent_instructions'][:1000]}")
    if pc.get("tree_summary"):
        lines.append(f"Repo files: {', '.join(pc['tree_summary'][:30])}")
    if ctx.harness_profiles:
        names = [p.name for p in ctx.harness_profiles if p.runnable]
        if names:
            lines.append(f"Runnable harness profiles: {', '.join(names)}")
    if ctx.skills:
        lines.append(f"Skills available: {', '.join(s.id or s.name for s in ctx.skills[:10])}")
    if ctx.capabilities:
        caps = [c.capability for c in ctx.capabilities if c.available]
        if caps:
            lines.append(f"Capabilities: {', '.join(caps[:15])}")
    return "\n".join(lines)
