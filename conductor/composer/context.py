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
                        configured=bool(_get(availability, "configured", False)) if availability else False,
                        runnable=bool(_get(availability, "runnable", False)) if availability else False,
                        binary_present=bool(_get(availability, "binary_present", False)) if availability else False,
                        credentials_present=bool(_get(availability, "credentials_present", False)) if availability else False,
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
    repo_info = spec.get("normalized_spec", {}).get("repository", {}) if spec else {}
    repo_url = repo_info.get("url", "") if isinstance(repo_info, dict) else ""
    repo_required = repo_info.get("required", True) if isinstance(repo_info, dict) else True
    if repo_path and os.path.isdir(repo_path):
        project_context = _build_project_context(repo_path)
    elif repo_url and mcp_gateway_client:
            ref = repo_info.get("base_branch", "") if isinstance(repo_info, dict) else ""
            try:
                project_context = _build_remote_context(mcp_gateway_client, repo_url, ref)
            except Exception as exc:
                logger.warning("Failed to fetch remote repo context: %s", exc)
                project_context["repo_url"] = repo_url
                project_context["access_error"] = str(exc)
    elif spec and spec.get("normalized_spec", {}).get("repository", {}).get("url"):
        project_context["repo_url"] = spec["normalized_spec"]["repository"]["url"]

    # Propagate required flag for caller to check
    project_context["repo_required"] = repo_required

    return ComposerContext(
        spec=spec or {},
        repository=repo_info,
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


def _build_remote_context(mcp_client, repo_url: str, ref: str = "") -> dict:
    """Fetch bounded repo context via MCP Gateway using list_tools/call_tool.

    Selects tools by contract — inspects discovered tools' names,
    descriptions, AND input schemas — so that the first tool whose name
    merely contains ``github`` (e.g. ``github.search``) is NOT used to
    read file content.  Instead:

      * file-content tools (must accept a ``path`` parameter AND
        have a name/description indicating file content reading):
        ``get_file_contents``, ``fetch_file``, ``read_repository_file``
        are preferred explicitly.  An ``input_schema`` showing
        ``properties.path`` is required.
      * tree/listing tools (must NOT require a ``path`` parameter;
        name/description indicating listing): ``list_files``,
        ``get_file_tree``, ``list_repository_files`` are preferred.

    The two categories are picked separately and used for their
    respective calls — never the same tool for both.
    """
    ctx: dict = {"repo_url": repo_url}

    try:
        tools = mcp_client.list_tools()
    except Exception as e:
        ctx["repo_required"] = True
        ctx["access_error"] = f"Failed to list MCP tools: {e}"
        return ctx

    file_tool = _select_file_content_tool(tools)
    tree_tool = _select_tree_listing_tool(tools)

    if not file_tool and not tree_tool:
        ctx["repo_required"] = True
        ctx["access_error"] = (
            "No file-content or tree-listing tool found in MCP Gateway "
            "(inspected tool names, descriptions, and input schemas)")
        return ctx

    files_to_fetch = [
        "README.md", "AGENTS.md", "CLAUDE.md",
        "docs/architecture.md", "docs/ARCHITECTURE.md",
        "ARCHITECTURE.md",
    ]

    owner = _extract_owner(repo_url)
    repo = _extract_repo(repo_url)

    if not owner or not repo:
        ctx["repo_required"] = True
        ctx["access_error"] = f"Could not parse owner/repo from URL: {repo_url}"
        return ctx

    any_success = False
    if file_tool is not None:
        for filename in files_to_fetch:
            try:
                args = _build_file_tool_args(file_tool, owner, repo, filename, ref)
                result = mcp_client.call_tool(file_tool.name, args)
                content = _extract_content_from_result(result)
                if content:
                    any_success = True
                    if "README" in filename:
                        ctx["readme"] = content[:4000]
                    elif filename in ("AGENTS.md", "CLAUDE.md"):
                        ctx["agent_instructions"] = (
                            ctx.get("agent_instructions", "") + "\n" + content[:2000]
                        )
                    elif "architecture" in filename.lower():
                        ctx["architecture_summary"] = content[:4000]
            except Exception:
                # Optional files may not exist — log and move on.
                pass

    # Tree listing via the dedicated tool, never the file-content tool.
    if tree_tool is not None:
        try:
            args = _build_tree_tool_args(tree_tool, owner, repo, ref)
            result = mcp_client.call_tool(tree_tool.name, args)
            tree = _extract_content_from_result(result)
            if tree:
                if isinstance(tree, list):
                    ctx["tree_summary"] = tree[:200]
                elif isinstance(tree, dict) and "tree" in tree:
                    ctx["tree_summary"] = [
                        f"{t.get('path', '')}/" if t.get("type") == "tree"
                        else t.get("path", "")
                        for t in tree.get("tree", [])
                    ][:200]
                elif isinstance(tree, str):
                    ctx["tree_summary"] = [
                        line.strip() for line in tree.splitlines() if line.strip()
                    ][:200]
        except Exception:
            pass

    # If no content could be fetched at all and this is a required repo, fail
    if not any_success and "tree_summary" not in ctx:
        ctx["repo_required"] = True
        ctx["access_error"] = "No repository content accessible via MCP Gateway"

    return ctx


# ── Tool selection by contract ────────────────────────────────────────────

# Explicitly preferred names — first wins. These are tools whose name
# (and ideally description + input schema) describe reading a single
# file's content from a remote repository.
_PREFERRED_FILE_TOOL_NAMES = (
    "get_file_contents",
    "fetch_file",
    "read_repository_file",
    "get_repository_file",
    "read_file",
    "get_file_content",
    "get_file",
)
# Explicitly preferred names — first wins. These are tools that list
# repository trees (root or path) WITHOUT returning file content.
_PREFERRED_TREE_TOOL_NAMES = (
    "list_files",
    "get_file_tree",
    "list_repository_files",
    "get_tree",
    "list_directory",
    "list_repo_files",
)


def _tool_name(tool) -> str:
    """Best-effort tool name extraction (object attribute or dict key)."""
    name = _get(tool, "name", "")
    return (name or "").lower() if isinstance(name, str) else ""


def _tool_description(tool) -> str:
    """Best-effort tool description extraction."""
    desc = _get(tool, "description", "")
    return (desc or "").lower() if isinstance(desc, str) else ""


def _tool_input_schema(tool) -> dict:
    """Best-effort tool input_schema extraction."""
    schema = _get(tool, "input_schema", None) or _get(tool, "inputSchema", None)
    return schema if isinstance(schema, dict) else {}


def _schema_has_path_param(tool) -> bool:
    """True iff the tool's input schema requires or accepts ``path`` or
    an equivalent file-path parameter."""
    schema = _tool_input_schema(tool)
    if not schema:
        # No schema advertised — we must NOT assume it accepts ``path``.
        return False
    props = schema.get("properties", {})
    return any(p in props for p in ("path", "file_path", "filepath"))


def _schema_required_keys(tool) -> set[str]:
    schema = _tool_input_schema(tool)
    req = schema.get("required", []) if schema else []
    return set(req) if isinstance(req, list) else set()


def _select_file_content_tool(tools) -> object | None:
    """Pick the best file-content tool from the discovered MCP tools.

    Selection contract:
      * Prefer explicitly named tools (in our preferred list).
      * Otherwise pick a tool whose name OR description strongly
        suggests file-content reading AND (whose input schema accepts
        a ``path`` parameter OR the tool's name carries a clear
        file-content signal like ``file_contents``).
      * NEVER pick a tool just because its name contains ``github``
        (``github.search`` is the canonical failure mode — it's the
        first ``github``-* tool returned but it does not read
        file content).
      * Skip tools whose name or description mentions ``search`` or
        ``tree`` or ``list`` — they are file-content tools' opposites.
    """
    if not tools:
        return None

    # 1. Explicit-preferred-name match wins (exact or name ends with
    #    one of the preferred names — handles ``github_get_file_contents``
    #    style prefixes whilst still preferring a contracted tool).
    for preferred in _PREFERRED_FILE_TOOL_NAMES:
        for t in tools:
            name = _tool_name(t)
            if name == preferred or name.endswith("_" + preferred) or name.endswith("." + preferred):
                return t

    # 2. Contract match: name OR description suggests file content
    #    AND (input schema accepts a ``path`` parameter OR name carries
    #    a strong file-content signal) AND tool is NOT a search/tree/list
    #    tool.
    candidates = []
    for t in tools:
        name = _tool_name(t)
        desc = _tool_description(t)
        # Reject search/list/tree tools explicitly.
        if any(kw in name for kw in ("search", "list", "tree", "glob", "grep", "find")):
            continue
        if any(kw in desc for kw in ("search", "list files", "list repo", "directory tree", "directory listing")):
            continue
        # Require EITHER a path argument in the schema OR a strong name
        # signal like ``file_contents`` / ``get_file`` so legacy MCP
        # gateways that don't advertise input schemas still get picked.
        strong_name_signal = any(kw in name for kw in (
            "file_contents", "file_content", "get_file", "read_file",
            "fetch_file", "file_blob", "blob",))
        if not (_schema_has_path_param(t) or strong_name_signal):
            continue
        # Strong signal: name suggests file content reading.
        name_signals = any(kw in name for kw in ("file", "content", "read", "fetch", "blob"))
        desc_signals = any(kw in desc for kw in (
            "file content", "read file", "fetch file", "blob", "read repository", "file contents"))
        if name_signals or desc_signals:
            candidates.append(t)
    if candidates:
        return candidates[0]
    return None


def _select_tree_listing_tool(tools) -> object | None:
    """Pick the best tree/listing tool from the discovered MCP tools.

    Selection contract:
      * Prefer explicitly named tools (in our preferred list).
      * Otherwise pick a tool whose name OR description suggests
        tree/listing AND whose input schema does NOT require ``path``
        (a listing tool should never mandate a single file path).
      * Skip tools whose name mentions ``search`` — search is not
        listing.
    """
    if not tools:
        return None

    # 1. Explicit-preferred-name match wins. Allow ``github_`` /
    #    ``github.`` prefixed forms so prefixed MCP gateways still
    #    satisfy the contracted preference.
    for preferred in _PREFERRED_TREE_TOOL_NAMES:
        for t in tools:
            name = _tool_name(t)
            if name == preferred or name.endswith("_" + preferred) or name.endswith("." + preferred):
                return t

    # 2. Contract match: name OR description suggests listing and the
    #    schema does NOT require``path``.
    candidates = []
    for t in tools:
        name = _tool_name(t)
        desc = _tool_description(t)
        if "search" in name:
            continue
        required_keys = _schema_required_keys(t)
        if "path" in required_keys:
            # A listing tool that requires a single path is suspect.
            continue
        name_signals = any(kw in name for kw in ("list", "tree", "directory", "glob"))
        desc_signals = any(kw in desc for kw in (
            "list files", "directory tree", "directory listing", "list of files",
            "tree of", "repository tree", "list repository"))
        if name_signals or desc_signals:
            candidates.append(t)
    if candidates:
        return candidates[0]
    return None


def _build_file_tool_args(tool, owner: str, repo: str, path: str, ref: str = "") -> dict:
    """Build the call_tool argument dict for a file-content tool.

    Honors the tool's input schema — only includes keys that the schema
    declares.  Falls back to ``owner``/``repo``/``path`` if the schema
    is unavailable (older MCP gateways).  ``ref`` is only included when
    both nonempty AND declared in the schema.
    """
    schema = _tool_input_schema(tool)
    if not schema:
        base = {"owner": owner, "repo": repo, "path": path}
        if ref:
            base["ref"] = ref
        return base
    props = schema.get("properties", {})
    args: dict = {}
    for key, source in (("owner", owner), ("repo", repo), ("path", path),
                        ("repository", repo), ("file_path", path),
                        ("filepath", path),
                        ("repository_full_name", f"{owner}/{repo}"),
                        ("repo_full_name", f"{owner}/{repo}")):
        if key in props:
            args[key] = source
    if ref and "ref" in props:
        args["ref"] = ref
    return args


def _build_tree_tool_args(tool, owner: str, repo: str, ref: str = "") -> dict:
    """Build the call_tool argument dict for a tree-listing tool."""
    schema = _tool_input_schema(tool)
    if not schema:
        base = {"owner": owner, "repo": repo}
        if ref:
            base["ref"] = ref
        return base
    props = schema.get("properties", {})
    args: dict = {}
    for key, source in (("owner", owner), ("repo", repo),
                        ("repository", repo),
                        ("repository_full_name", f"{owner}/{repo}"),
                        ("repo_full_name", f"{owner}/{repo}")):
        if key in props:
            args[key] = source
    if ref and "ref" in props:
        args["ref"] = ref
    return args


def _extract_owner(repo_url: str) -> str:
    """Extract owner from GitHub URL."""
    # Handles: https://github.com/owner/repo, git@github.com:owner/repo.git, etc.
    import re
    m = re.search(r"github\.com[/:]([^/]+)/", repo_url)
    if m:
        return m.group(1)
    return ""


def _extract_repo(repo_url: str) -> str:
    """Extract repo from GitHub URL."""
    import re
    m = re.search(r"github\.com[^/]+/[^/]+/([^/\.]+)", repo_url)
    if m:
        return m.group(1)
    m = re.search(r"github\.com[/:][^/]+/([^/\.]+)", repo_url)
    if m:
        return m.group(1)
    return ""


def _extract_content_from_result(result: dict) -> str | list | None:
    """Extract content from MCP tool call result."""
    if not result:
        return None
    # Result shape varies: {"result": "..."} or {"content": "..."} or {"data": "..."}
    if isinstance(result, dict):
        for key in ("result", "content", "data", "text"):
            if key in result and result[key]:
                val = result[key]
                if isinstance(val, str):
                    return val
                if isinstance(val, (list, dict)):
                    return val
    return None


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
        runnable = [p.name for p in ctx.harness_profiles if p.runnable]
        if runnable:
            lines.append(f"Runnable harness profiles: {', '.join(runnable)}")
            lines.append("You MUST use ONLY runnable profiles listed above for harness_profile.")
            lines.append("Do NOT pick any other profile name.")
    if ctx.skills:
        lines.append(f"Skills available: {', '.join(s.id or s.name for s in ctx.skills[:10])}")
    if ctx.capabilities:
        caps = [c.capability for c in ctx.capabilities if c.available]
        if caps:
            lines.append(f"Capabilities: {', '.join(caps[:15])}")
    return "\n".join(lines)
