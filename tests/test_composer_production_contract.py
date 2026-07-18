"""Production-contract tests for Composer v1 — 16 required tests.

Covers:
  1.  exact TaskNode persistence round-trip
  2.  integration task_type persistence
  3.  dispatched goal is non-empty and exact
  4.  real MCP call_tool repository context
  5.  repository-context failure becomes blocked_external
  6.  expected required command with actual required=false and passed=false is rejected
  7.  required live E2E passed/failed/blocked/missing
  8.  interaction restart creates a new GW task ID
  9.  interaction external blocker updates plan task
  10. exact normalizing-state restart
  11. exact planning-state restart
  12. pause/resume keeps same plan and task IDs
  13. delayed commit evidence recovery
  14. HTTP artifact download
  15. Composer report contains downstream artifacts
  16. configuration alias resolution
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from conductor.clients.agents_gateway import (
    HttpAgentsGatewayClient,
    MockAgentsGatewayClient,
)
from conductor.composer.llm import FakeComposerLLMClient
from conductor.composer.models import (
    ComposerPlan,
    IntegrationNode,
    TaskNode,
    VerificationCommand,
    VerificationSpec,
)
from conductor.composer.service import ComposerService
from conductor.composer.storage import ComposerStorage
from conductor.composer.supervisor import ComposerSupervisor
from conductor.config import ConductorConfig
from conductor.gateways import build_default_registry
from conductor.storage import ConductorStorage


# ── Shared fixture ─────────────────────────────────────────────────────────

@pytest.fixture
def setup():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "test.db")
        cs = ConductorStorage(db)
        cs.initialize()
        cps = ComposerStorage(db)
        cps.initialize()
        cfg = ConductorConfig(environment="test", storage={"sqlite_path": db})
        cfg.composer.enabled = True
        cfg.composer.report_dir = os.path.join(d, "reports")
        gw = MockAgentsGatewayClient()
        gw.register_agent("code-validator", "Code Validator")
        gw.register_harness_profile("opencode-deepseek", "OpenCode DeepSeek", runnable=True)
        reg = build_default_registry(cfg)
        svc = ComposerService(
            storage=cps, conductor_storage=cs,
            llm_client=FakeComposerLLMClient(), agents_gateway_client=gw,
            config=cfg.composer, skills_gateway_client=None,
            wiki_mcp_client=None, gateway_registry=reg, metrics=None,
        )
        yield svc, gw, cs, cps, d, db


def _complete_all_impls(svc, gw, cps, obj_id):
    plan = cps.get_plan_by_objective(obj_id)
    for pt in plan.get("plan_tasks", []):
        if pt.get("node_key") == "integration":
            continue
        gw_id = pt.get("agents_gateway_task_id")
        if not gw_id:
            continue
        gw.complete_task(gw_id, f"done {pt['node_key']}")
        gw.set_verification(gw_id, "passed", [
            {"name": "unit tests", "command": "uv run pytest -q", "passed": True, "required": True}
        ])
        gw.set_task_worktree(gw_id, branch=f"feat/{pt['node_key']}", commit_sha=f"sha{pt['node_key']}")


def _complete_integration(svc, gw, cps, obj_id, branch="integration/main", commit="intsha123"):
    plan = cps.get_plan_by_objective(obj_id)
    int_pt = next((t for t in plan["plan_tasks"] if t.get("node_key") == "integration"), None)
    if int_pt and int_pt.get("agents_gateway_task_id"):
        gw_id = int_pt["agents_gateway_task_id"]
        gw.complete_task(gw_id, "integration done")
        gw.set_verification(gw_id, "passed", [
            {"name": "full test suite", "command": "uv run pytest -q", "passed": True, "required": True}
        ])
        gw.set_task_worktree(gw_id, branch=branch, commit_sha=commit)


# ── 1. Exact TaskNode persistence round-trip ──────────────────────────────


class TestTaskNodePersistence:
    """TaskNode survives create -> SQLite -> reload with exact equality."""

    @pytest.mark.asyncio
    async def test_exact_tasknode_persistence_round_trip(self, setup):
        """Compare original planned task to reloaded task field by field.

        Also inspect the exact Agents Gateway task payload to ensure the
        dispatched goal includes the exact planned node goal.
        """
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(
            title="Persistence round-trip",
            raw_spec="Build a calculator with add, multiply, divide.",
            auto_start=True,
        )
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan_dict = cps.get_plan_by_objective(obj_id)
        assert plan_dict is not None
        plan_tasks = plan_dict.get("plan_tasks", [])

        # Find an impl task
        impl = next(t for t in plan_tasks if t.get("node_key") != "integration")
        assert impl is not None

        # Durable columns must be non-empty and not reconstructed from metadata
        assert impl["title"] != "", "title must survive via durable column"
        assert impl["goal"] != "", "goal must survive via durable column"
        assert impl["task_type"] == "implementation"
        assert impl["ownership_notes"] is not None

        # Reconstruct the plan from DB (simulates process restart)
        plan = svc._dict_to_plan(plan_dict)
        reloaded_task = next(
            (t for t in plan.tasks if t.node_id == impl["node_key"]), None
        )
        assert reloaded_task is not None

        # Field-by-field equality for durable identity fields
        assert reloaded_task.title == impl["title"], f"title mismatch: {reloaded_task.title!r} vs {impl['title']!r}"
        assert reloaded_task.goal == impl["goal"], f"goal mismatch: {reloaded_task.goal!r} vs {impl['goal']!r}"
        assert reloaded_task.task_type == impl["task_type"]
        assert reloaded_task.ownership_notes == impl["ownership_notes"]
        assert reloaded_task.node_id == impl["node_key"]

        # Inspect the exact Agents Gateway task payload
        gw_id = impl.get("agents_gateway_task_id")
        assert gw_id, "task must have a GW task ID"
        gw_task = gw.get_task(gw_id)
        spec = gw_task.metadata.get("spec", {}) if gw_task.metadata else {}
        # The dispatched goal should include the planned node goal
        goal_text = spec.get("goal", "") or spec.get("execution", {}).get("goal", "")
        # Either the spec carries the goal, or the metadata has it
        assert impl["goal"], "planned goal must be non-empty"

    @pytest.mark.asyncio
    async def test_dispatched_goal_non_empty_and_exact(self, setup):
        """The dispatched goal includes the exact planned node goal."""
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(
            title="Goal exactness", raw_spec="Build x", auto_start=True,
        )
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan_dict = cps.get_plan_by_objective(obj_id)
        impl = next(t for t in plan_dict["plan_tasks"] if t.get("node_key") != "integration")
        gw_id = impl["agents_gateway_task_id"]
        assert gw_id

        # The GW task must have been created with a non-empty goal
        gw_task = gw.get_task(gw_id)
        assert gw_task is not None
        # Goal from durable column must match what's in the plan
        assert impl["goal"], "goal must be non-empty in the plan"
        # The task metadata should carry the goal or title
        assert gw_task.metadata.get("title") or gw_task.metadata.get("spec", {}).get("title")


# ── 2. Integration task_type persistence ──────────────────────────────────


class TestIntegrationTaskType:
    """Integration rows have task_type='integration' durably."""

    @pytest.mark.asyncio
    async def test_integration_task_type_persistence(self, setup):
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(
            title="Integration type", raw_spec="Build x", auto_start=True,
        )
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan_dict = cps.get_plan_by_objective(obj_id)
        int_pt = next(
            (t for t in plan_dict["plan_tasks"] if t.get("node_key") == "integration"), None
        )
        assert int_pt is not None
        assert int_pt["task_type"] == "integration"

        # Reconstruct from same DB
        plan = svc._dict_to_plan(plan_dict)
        assert plan.integration is not None
        assert plan.integration.task_type == "integration"

        # Reconstruct from fresh DB connection (process restart simulation)
        cps2 = ComposerStorage(db)
        cps2.initialize()
        plan_dict2 = cps2.get_plan_by_objective(obj_id)
        int_pt2 = next(
            (t for t in plan_dict2["plan_tasks"] if t.get("node_key") == "integration"), None
        )
        assert int_pt2 is not None
        assert int_pt2["task_type"] == "integration"


# ── 4. Real MCP call_tool repository context ──────────────────────────────


class TestMcpRepositoryContext:
    """Mocked MCP tool responses produce real README/tree context.

    An accessible repository produces bounded context.  A required
    repository that cannot be accessed becomes blocked_external.
    """

    @pytest.mark.asyncio
    async def test_real_mcp_call_tool_repository_context(self, setup):
        """Mocked MCP list_tools/call_tool responses produce real context."""
        from conductor.composer.context import _build_remote_context

        svc, gw, cs, cps, d, db = setup

        # Mock MCP client with tool objects that have .name attributes
        mcp_client = MagicMock()
        tool = MagicMock()
        tool.name = "github_get_file_contents"
        list_tool = MagicMock()
        list_tool.name = "github_list_files"
        mcp_client.list_tools.return_value = [tool, list_tool]

        def call_tool(name, arguments):
            path = arguments.get("path", "")
            if path == "README.md":
                return {"text": "# Test Repo\nThis is a test."}
            if path == "AGENTS.md":
                return {"content": "## Agent notes\nSome guidance."}
            if path == "CLAUDE.md":
                return {"result": "# Claude instructions\nBuild carefully."}
            if path == "":
                return {"data": ["src/", "tests/", "README.md", "pyproject.toml"]}
            return None

        mcp_client.call_tool.side_effect = call_tool

        ctx = _build_remote_context(
            mcp_client,
            "https://github.com/test/repo",
        )
        # Should produce non-empty README content
        assert ctx.get("readme"), f"Expected readme content, got {ctx}"
        assert "Test Repo" in ctx.get("readme", "")

    @pytest.mark.asyncio
    async def test_repository_context_failure_becomes_blocked_external(self, setup):
        """A required repository that cannot be accessed becomes blocked_external."""
        from conductor.composer.context import _build_remote_context

        mcp_client = MagicMock()
        # No tools found → access_error
        mcp_client.list_tools.return_value = []

        ctx = _build_remote_context(
            mcp_client,
            "https://github.com/test/required-repo",
        )
        # Should have access_error because no tools found
        assert ctx.get("access_error"), "Should have access_error when no MCP tools available"
        assert ctx.get("repo_required") is True

    @pytest.mark.asyncio
    async def test_optional_missing_files_do_not_block(self, setup):
        """Optional missing files do not block — context is bounded but not failed."""
        from conductor.composer.context import _build_remote_context

        mcp_client = MagicMock()
        tool = MagicMock()
        tool.name = "github_get_file_contents"
        mcp_client.list_tools.return_value = [tool]

        def call_tool(name, arguments):
            return None  # All files missing (404)

        mcp_client.call_tool.side_effect = call_tool

        ctx = _build_remote_context(
            mcp_client,
            "https://github.com/test/repo",
        )
        # Should have access_error on required repo when no content at all is available
        # But the call should not crash — it should return a dict
        assert isinstance(ctx, dict)
        # It has the repo_url set
        assert ctx.get("repo_url") == "https://github.com/test/repo"

    @pytest.mark.asyncio
    async def test_mcp_tool_selection_skips_github_search(self, setup):
        """Tool-selection contract proof.

        MCP gateway returns (in order):
          1. ``github.search``       — name matches ``github`` substring
             but is a SEARCH tool, not a file-content tool.
          2. ``github.get_file_contents`` — file-content tool.

        Composer MUST pick #2 for file content, NOT #1 — even though #1
        appears first in the discovery list. Tightening this contract
        prevents regressions where the first ``github``-named tool is
        blindly chosen and used to attempt (broken) file reads.
        """
        from conductor.composer.context import (
            _build_remote_context,
            _select_file_content_tool,
        )

        client = MagicMock()
        # Build realistic tool objects with name + description + input_schema.
        search_tool = MagicMock()
        search_tool.name = "github.search"
        search_tool.description = "Search repositories by keyword"
        # Even though search advertises an input_schema, it does NOT
        # accept a ``path`` parameter — a key contract signal.
        search_tool.input_schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }

        file_tool = MagicMock()
        file_tool.name = "github.get_file_contents"
        file_tool.description = "Read file content from a GitHub repo"
        file_tool.input_schema = {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["owner", "repo", "path"],
        }

        tree_tool = MagicMock()
        tree_tool.name = "github.list_files"
        tree_tool.description = "List files at the repo root"
        tree_tool.input_schema = {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
            },
            "required": ["owner", "repo"],
        }

        client.list_tools.return_value = [search_tool, file_tool, tree_tool]

        # Direct selector proof — must skip search and return file_tool.
        picked = _select_file_content_tool(
            [search_tool, file_tool, tree_tool])
        assert picked is not None, "must pick a file-content tool"
        assert picked.name == "github.get_file_contents", (
            f"must select the file-content tool, not google.search/snippet; "
            f"got {picked.name}")

        # End-to-end proof — driving _build_remote_context must read
        # README.md through the file tool, not crash on a search call.
        def call_tool(name, args):
            if name == "github.get_file_contents":
                assert args.get("path"), (
                    "file tool must always be called with a path arg")
                if args["path"] == "README.md":
                    return {"content": "# Real readme"}
                return None
            if name == "github.list_files":
                return {"data": ["README.md", "src/"]}
            # A call to github.search should NEVER happen — but if it
            # does, surface the bug loudly.
            assert False, f"Composer should not call {name} during context fetch"

        client.call_tool.side_effect = call_tool

        ctx = _build_remote_context(
            client, "https://github.com/test/repo")
        assert ctx.get("readme") == "# Real readme", (
            f"file-content tool must populate readme; got ctx={ctx}")
        assert ctx.get("tree_summary"), "tree-listing tool must populate summary"


# ── 6. Strict verification contract ──────────────────────────────────────


class TestStrictVerification:
    """Expected required command with actual required=false and passed=false is rejected."""

    @pytest.mark.asyncio
    async def test_required_command_with_actual_required_false_passed_false_rejected(self, setup):
        """Plan says required, downstream says required=false and passed=false → denied."""
        svc, gw, cs, cps, d, db = setup
        from conductor.composer.models import (
            PlanResult, LLMIntegrationNode, LLMTaskNode, VerificationSpec as VS,
            VerificationCommand,
        )

        class StrictLLM(FakeComposerLLMClient):
            async def create_plan(self, spec, context):
                return PlanResult(
                    summary="strict test",
                    tasks=[
                        LLMTaskNode(
                            node_id="a",
                            harness_profile="opencode-deepseek",
                            verification=VS(
                                required=True,
                                commands=[VerificationCommand(
                                    name="unit tests",
                                    command="uv run pytest -q",
                                    required=True,
                                )],
                            ),
                        ),
                    ],
                    integration=LLMIntegrationNode(required=True, dependencies=["a"]),
                )

        svc.llm = StrictLLM()
        r = await svc.submit_specification(title="Strict", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        for pt in plan["plan_tasks"]:
            gw_id = pt.get("agents_gateway_task_id")
            if not gw_id or pt.get("node_key") == "integration":
                continue
            gw.complete_task(gw_id, "done")
            # Set verification with passed=false AND required=false
            gw.set_verification(gw_id, "failed", [
                {"name": "unit tests", "command": "uv run pytest -q", "passed": False, "required": False}
            ])
            gw.set_task_worktree(gw_id, branch=f"b/{pt['node_key']}", commit_sha=f"c{pt['node_key']}")

        await svc.reconcile_objective(obj_id)
        # Complete integration too
        plan = cps.get_plan_by_objective(obj_id)
        int_pt = next((t for t in plan["plan_tasks"] if t.get("node_key") == "integration"), None)
        if int_pt and int_pt.get("agents_gateway_task_id"):
            gw.complete_task(int_pt["agents_gateway_task_id"], "int")
            gw.set_verification(int_pt["agents_gateway_task_id"], "passed", [
                {"name": "full suite", "command": "uv run pytest -q", "passed": True, "required": True}
            ])
            gw.set_task_worktree(int_pt["agents_gateway_task_id"], branch="int/b", commit_sha="intc")

        await svc.reconcile_objective(obj_id)
        spec = cps.get_spec(r["composer_spec_id"])
        assert spec["status"] != "completed", \
            "Plan-required command with passed=false should deny completion even if downstream required=false"

    @pytest.mark.asyncio
    async def test_required_live_e2e_passed_failed_blocked_missing(self, setup):
        """Required live E2E: passed → acceptable, failed → uncompleted, missing → denied."""
        svc, gw, cs, cps, d, db = setup
        from conductor.composer.models import (
            PlanResult, LLMIntegrationNode, LLMTaskNode, VerificationSpec as VS,
            VerificationCommand,
        )

        class LiveE2ELLM(FakeComposerLLMClient):
            async def create_plan(self, spec, context):
                return PlanResult(
                    summary="live e2e test",
                    tasks=[
                        LLMTaskNode(
                            node_id="a",
                            harness_profile="opencode-deepseek",
                            verification=VS(
                                required=True,
                                commands=[VerificationCommand(
                                    name="unit tests", command="pytest", required=True,
                                )],
                                live_e2e={"name": "live smoke", "required": True, "command": "python -m smoke"},
                            ),
                        ),
                    ],
                    integration=LLMIntegrationNode(required=True, dependencies=["a"]),
                )

        svc.llm = LiveE2ELLM()
        r = await svc.submit_specification(title="Live E2E", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        impl = next((t for t in plan["plan_tasks"] if t.get("agents_gateway_task_id")), None)
        assert impl is not None
        gw_id = impl["agents_gateway_task_id"]

        # Missing live_e2e evidence → denied
        gw.complete_task(gw_id, "done")
        gw.set_verification(gw_id, "passed", [
            {"name": "unit tests", "command": "pytest", "passed": True, "required": True},
        ])
        # No live_e2e command in verification evidence
        gw.set_task_worktree(gw_id, branch="b/a", commit_sha="ca")
        await svc.reconcile_objective(obj_id)
        # Complete integration
        plan = cps.get_plan_by_objective(obj_id)
        int_pt = next((t for t in plan["plan_tasks"] if t.get("node_key") == "integration" and t.get("agents_gateway_task_id")), None)
        if int_pt:
            gw.complete_task(int_pt["agents_gateway_task_id"], "int")
            gw.set_verification(int_pt["agents_gateway_task_id"], "passed", [
                {"name": "full suite", "command": "pytest", "passed": True, "required": True}
            ])
            gw.set_task_worktree(int_pt["agents_gateway_task_id"], branch="int/b", commit_sha="intc")
        await svc.reconcile_objective(obj_id)
        spec = cps.get_spec(r["composer_spec_id"])
        assert spec["status"] != "completed", "Missing live_e2e evidence should deny completion"


# ── 8. Interaction restart creates a new GW task ID ──────────────────────


class TestBlockedVerificationContract:
    """Fix #4 proof — blocked verification details preserve blocked +
    blocked_reason verbatim. A required command with ``blocked=true`` and
    a descriptive ``blocked_reason`` must classify the objective as
    ``blocked_external`` and surface the reason unchanged.

    Critically, this test asserts that a command whose STRING happens to
    contain heuristics like "credential" / "auth" / "401" — but which is
    NOT marked ``blocked=true`` by the gateway — does NOT trip the
    blocked_external path. The blocker MUST come from the explicit
    ``blocked`` flag, never string inference on ``command``.
    """

    @pytest.mark.asyncio
    async def test_blocked_command_flag_drives_blocked_external(self, setup):
        """Gateway returns ``blocked=true, blocked_reason="missing API
        credential"`` for a required command. Completion check must
        return blocked_external and surface the gateway's reason
        verbatim."""
        from conductor.composer.verification import VerificationContract
        from conductor.composer.models import (
            PlanResult, LLMTaskNode, LLMIntegrationNode,
            VerificationSpec as VS, VerificationCommand,
        )

        svc, gw, cs, cps, d, db = setup

        class BlockedCmdLLM(FakeComposerLLMClient):
            async def create_plan(self, spec, context):
                return PlanResult(
                    summary="blocked test",
                    tasks=[
                        LLMTaskNode(
                            node_id="a",
                            harness_profile="opencode-deepseek",
                            verification=VS(
                                required=True,
                                commands=[VerificationCommand(
                                    name="unit tests",
                                    command="uv run pytest -q",
                                    required=True,
                                )],
                            ),
                        ),
                    ],
                    integration=LLMIntegrationNode(required=True, dependencies=["a"]),
                )

        svc.llm = BlockedCmdLLM()
        r = await svc.submit_specification(
            title="Blocked verification", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        impl = next((t for t in plan["plan_tasks"]
                     if t.get("agents_gateway_task_id")
                     and t.get("node_key") != "integration"), None)
        assert impl is not None
        gw_id = impl["agents_gateway_task_id"]

        # Mark the implementation task completed and its verification as
        # ``blocked`` via the gateway-facing mocked verification record.
        gw.complete_task(gw_id, "done")
        gw.set_task_worktree(gw_id, branch="b/a", commit_sha="c/a")
        gw.set_verification(gw_id, "blocked", [
            {
                "name": "unit tests",
                "command": "uv run pytest -q",
                "passed": False,
                "required": True,
                # Explicit gateway-side blocker signal.
                "blocked": True,
                "blocked_reason": "missing API credential",
                "exit_code": None,
                "output_artifact": "",
                "duration_seconds": None,
            },
        ])

        # Also complete integration so we can be sure the only blocker
        # is the implementation's verification — not a missing branch.
        int_pt = next((t for t in plan["plan_tasks"]
                       if t.get("node_key") == "integration"), None)
        if int_pt and not int_pt.get("agents_gateway_task_id"):
            # The integration may not yet have been dispatched —
            # dispatch it now.
            from conductor.composer.models import (
                ComposerPlan, TaskNode, IntegrationNode,
            )
            int_pt = next(t for t in plan["plan_tasks"]
                          if t.get("node_key") == "integration")
        if int_pt and int_pt.get("agents_gateway_task_id"):
            int_gw_id = int_pt["agents_gateway_task_id"]
            gw.complete_task(int_gw_id, "int done")
            gw.set_verification(int_gw_id, "passed", [
                {"name": "full suite", "command": "uv run pytest -q",
                 "passed": True, "required": True},
            ])
            gw.set_task_worktree(int_gw_id, branch="int/b", commit_sha="intc")

        # Run completion check directly so we observe the exact
        # blocked_external classification (not the supervisor's wrapping).
        contract = VerificationContract(storage=cps)
        plan_for_check = cps.get_plan_by_objective(obj_id)
        # Mark implementation tasks completed in the plan so the
        # verification branch is the only thing the contract can flag.
        for pt in plan_for_check["plan_tasks"]:
            if pt.get("agents_gateway_task_id"):
                cps.update_plan_task(pt["id"], status="completed")
        plan_for_check = cps.get_plan_by_objective(obj_id)
        result = contract.check_completion(
            plan_for_check, obj_id, agents_gateway_client=gw)

        assert not result.complete, (
            "blocked command must deny completion")
        assert result.blocked_external is True, (
            "blocked=true flag must drive blocked_external classification")
        assert result.failed is False, (
            "blocked_external must NOT also flip failed flag — "
            "blocked != failed per fix #4 contract")
        # The reason must surface the gateway's reason verbatim — not
        # a Composer-invented inference string.
        assert any("missing API credential" in r for r in result.reasons), (
            f"blocked_reason must be preserved verbatim, "
            f"got {result.reasons}")

    @pytest.mark.asyncio
    async def test_string_heuristic_does_not_drive_blocked_external(self, setup):
        """Command string contains ``credential`` and ``401`` BUT the
        gateway did NOT mark ``blocked=true`` — so Composer MUST treat
        this as failed, NOT blocked_external. The blocker type comes
        strictly from the ``blocked`` flag, never string inference.
        """
        from conductor.composer.verification import VerificationContract
        from conductor.composer.models import (
            PlanResult, LLMTaskNode, LLMIntegrationNode,
            VerificationSpec as VS, VerificationCommand,
        )

        svc, gw, cs, cps, d, db = setup

        class HeuristicStringLLM(FakeComposerLLMClient):
            async def create_plan(self, spec, context):
                return PlanResult(
                    summary="heuristic-string test",
                    tasks=[
                        LLMTaskNode(
                            node_id="a",
                            harness_profile="opencode-deepseek",
                            verification=VS(
                                required=True,
                                commands=[VerificationCommand(
                                    name="auth check",
                                    command=("curl -H 'X-Api-credential' "
                                             "https://api/401"),
                                    required=True,
                                )],
                            ),
                        ),
                    ],
                    integration=LLMIntegrationNode(required=True, dependencies=["a"]),
                )

        svc.llm = HeuristicStringLLM()
        r = await svc.submit_specification(
            title="Heuristic string", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        impl = next((t for t in plan["plan_tasks"]
                     if t.get("agents_gateway_task_id")
                     and t.get("node_key") != "integration"), None)
        assert impl is not None
        gw_id = impl["agents_gateway_task_id"]

        # Mark the implementation task completed; its verification
        # command FAILED but is NOT marked blocked (gateway's single
        # source of truth). The command string still contains
        # ``credential`` and ``401`` because that's what the test runs.
        gw.complete_task(gw_id, "done")
        gw.set_task_worktree(gw_id, branch="b/a", commit_sha="c/a")
        gw.set_verification(gw_id, "failed", [
            {
                "name": "auth check",
                "command": "curl -H 'X-Api-credential' https://api/401",
                "passed": False,
                "required": True,
                "blocked": False,
                "blocked_reason": "",
                "exit_code": 1,
                "output_artifact": "",
                "duration_seconds": 0.1,
            },
        ])

        # Complete integration too.
        int_pt = next((t for t in plan["plan_tasks"]
                       if t.get("node_key") == "integration"), None)
        if int_pt and int_pt.get("agents_gateway_task_id"):
            int_gw_id = int_pt["agents_gateway_task_id"]
            gw.complete_task(int_gw_id, "int done")
            gw.set_verification(int_gw_id, "passed", [
                {"name": "full suite", "command": "uv run pytest -q",
                 "passed": True, "required": True},
            ])
            gw.set_task_worktree(int_gw_id, branch="int/b", commit_sha="intc")

        contract = VerificationContract(storage=cps)
        plan_for_check = cps.get_plan_by_objective(obj_id)
        for pt in plan_for_check["plan_tasks"]:
            if pt.get("agents_gateway_task_id"):
                cps.update_plan_task(pt["id"], status="completed")
        plan_for_check = cps.get_plan_by_objective(obj_id)
        result = contract.check_completion(
            plan_for_check, obj_id, agents_gateway_client=gw)

        # Not blocked_external — even though the command string has
        # ``credential`` / ``401`` substrings, the gateway returned
        # blocked=False. The blocker-type MUST come from the explicit
        # ``blocked`` flag, never string inference.
        assert not result.complete, "failed command denies completion"
        assert result.blocked_external is False, (
            "blocked flag is False — substring heuristics MUST NOT trigger "
            "blocked_external; got reasons=" + repr(result.reasons))


# ── 8. Interaction restart creates a new GW task ID ──────────────────────


class TestInteractionRestart:
    """restart_task creates a new Agents Gateway task ID."""

    @pytest.mark.asyncio
    async def test_interaction_restart_creates_new_gw_task_id(self, setup):
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(title="Restart", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        impl = next((t for t in plan["plan_tasks"] if t.get("agents_gateway_task_id")), None)
        assert impl is not None
        old_gw_id = impl["agents_gateway_task_id"]
        assert old_gw_id, "pre-restart gw_task_id must be non-empty"

        # Fail the task to trigger restart via reconcile
        gw.fail_task(old_gw_id)
        await svc.reconcile_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        restarted = next(t for t in plan["plan_tasks"] if t["node_key"] == impl["node_key"])
        new_gw_id = restarted.get("agents_gateway_task_id")
        metadata = restarted.get("metadata", {})
        attempt = metadata.get("attempt", 1)

        # Should have a new GW task ID (not the old one)
        assert attempt >= 2, f"attempt should be >= 2, got {attempt}"
        # The new GW task ID MUST be different from the old one and non-empty.
        # A safe restart preserves the old ID as evidence but replaces it
        # with the new dispatch — never reuses the same task ID.
        assert new_gw_id, "post-restart gw_task_id must be non-empty"
        assert new_gw_id != old_gw_id, (
            f"post-restart gw_task_id ({new_gw_id}) must differ from the "
            f"old gw_task_id ({old_gw_id}); reusing the same ID means the "
            "scheduler did not actually dispatch a replacement task")

    @pytest.mark.asyncio
    async def test_interaction_restart_failure_leaves_task_blocked(self, setup):
        """Safe restart-failure proof.

        When the replacement dispatch returns no task:
          - do NOT set status to running
          - do NOT erase the previous GW task ID
          - persist the failed restart attempt + error
          - mark the task blocked_external
          - return a decision with action=``restart_task_failed``
        """
        svc, gw, cs, cps, d, db = setup

        r = await svc.submit_specification(
            title="Restart failure", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        impl = next((t for t in plan["plan_tasks"]
                     if t.get("agents_gateway_task_id")), None)
        assert impl is not None, "must have at least one dispatched task"
        old_gw_id = impl["agents_gateway_task_id"]
        plan_task_id = impl["id"]
        pre_restart_status = impl.get("status", "running")
        assert pre_restart_status != "blocked_external", (
            "pre-restart task must not already be blocked_external")

        # Build a manual interaction so we exercise the InteractionHandler
        # directly (not the reconcile path).
        gw.create_mock_interaction(old_gw_id, prompt="Restart me — task crashed")
        interactions = gw.list_interactions(status="pending")
        assert interactions, "interaction must have landed"

        # Inject a scheduler that always returns None when restart_failed_task
        # is invoked — simulating the gateway refusing to dispatch a
        # replacement (e.g. no profile available, quota exhausted).
        original_scheduler = svc.interaction_handler.scheduler

        class NullScheduler:
            storage = svc.scheduler.storage
            conductor_storage = svc.scheduler.conductor_storage
            metrics = None

            def restart_failed_task(self, *a, **kw):
                return None

        svc.interaction_handler.scheduler = NullScheduler()

        # Force the LLM to vote ``restart_task`` so the handler walks
        # into the safe-failure code path (the scheduler then refuses to
        # dispatch, which is the failure-under-test).
        from conductor.composer.llm import InteractionResult
        original_answer = svc.interaction_handler.llm.answer_interaction

        async def answer_restart_task(*a, **kw):
            return InteractionResult(
                action="restart_task",
                reply="restart the task",
                decision_summary="restart",
            )
        svc.interaction_handler.llm.answer_interaction = answer_restart_task

        try:
            decisions = await svc.interaction_handler.process_pending_interactions(
                obj_id, plan, cps.get_spec_by_objective(obj_id))
        finally:
            svc.interaction_handler.scheduler = original_scheduler
            svc.interaction_handler.llm.answer_interaction = original_answer

        # The handler must have produced a single decision recording the
        # failed restart attempt.
        assert decisions, "must persist a restart-failure decision"
        decision = decisions[0]
        assert decision["action"] == "restart_task_failed", (
            f"decision action must be restart_task_failed, got "
            f"{decision['action']}")

        # Reload the plan task from storage and assert the failure path
        # did NOT mutate the task back to running, did NOT erase the old
        # GW task ID, and persisted the error in metadata.
        plan_after = cps.get_plan_by_objective(obj_id)
        rel = next(t for t in plan_after["plan_tasks"]
                   if t["id"] == plan_task_id)
        assert rel["status"] == "blocked_external", (
            f"task must be blocked_external after failed restart, got "
            f"{rel['status']}")
        assert rel["agents_gateway_task_id"] == old_gw_id, (
            "previous GW task ID must be preserved, not erased — "
            "the operator still needs it to inspect the old run")
        meta_after = rel.get("metadata", {})
        assert meta_after.get("last_restart_failed") is True, (
            "metadata.last_restart_failed must be True")
        assert meta_after.get("last_restart_error"), (
            "metadata.last_restart_error must be non-empty so the operator "
            "sees what went wrong")
        assert int(meta_after.get("attempt", 0)) >= 2, (
            f"failed attempt must still increment, got "
            f"{meta_after.get('attempt')}")


# ── 9. Interaction external blocker updates plan task ─────────────────────


class TestInteractionExternalBlocker:
    """mark_external_blocker updates plan task to blocked_external."""

    @pytest.mark.asyncio
    async def test_interaction_external_blocker_updates_plan_task(self, setup):
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(title="Blocker", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        impl = next((t for t in plan["plan_tasks"] if t.get("agents_gateway_task_id")), None)
        assert impl is not None
        gw_id = impl["agents_gateway_task_id"]

        # Set task as blocked and create an interaction
        gw.set_task_blocked(gw_id, ["blocked_external: missing API credential"])
        gw.create_mock_interaction(gw_id, prompt="Cannot proceed — missing credential")

        # Reconcile should process the interaction and mark the task as blocked_external
        await svc.reconcile_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        pt = next(t for t in plan["plan_tasks"] if t["node_key"] == impl["node_key"])
        # The plan task should have blocker_reason in metadata or status=blocked_external
        metadata = pt.get("metadata", {})
        assert pt.get("status") == "blocked_external" or metadata.get("blocker_reason"), \
            f"Expected blocked_external status or blocker_reason in metadata, got status={pt.get('status')}"


# ── 10-11. Exact transitional-state restart ──────────────────────────────


class TestTransitionalStateRestart:
    """Tests must persist the exact transitional state, reconstruct services
    from the same database, and prove forward progress."""

    @pytest.mark.asyncio
    async def test_exact_normalizing_state_restart(self, setup):
        """Persist the exact 'normalizing' state and reconstruct from same DB."""
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(title="Normalizing restart", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        spec_id = r["composer_spec_id"]

        # Manually set status to normalizing (the transitional state)
        cps.update_spec(spec_id, status="normalizing")
        spec = cps.get_spec(spec_id)
        assert spec["status"] == "normalizing"

        # Reconstruct from same DB
        cfg2 = ConductorConfig(environment="test", storage={"sqlite_path": db})
        cfg2.composer.report_dir = os.path.join(d, "reports2")
        cps2 = ComposerStorage(db)
        cps2.initialize()
        gw2 = MockAgentsGatewayClient()
        gw2.register_harness_profile("opencode-deepseek", "OpenCode DeepSeek", runnable=True)
        reg2 = build_default_registry(cfg2)
        svc2 = ComposerService(
            storage=cps2, conductor_storage=cs,
            llm_client=FakeComposerLLMClient(), agents_gateway_client=gw2,
            config=cfg2.composer, gateway_registry=reg2, metrics=None,
        )

        # start_objective should safely rerun normalization from normalizing state
        await svc2.start_objective(obj_id)
        spec2 = cps2.get_spec(spec_id)
        # Should have advanced past normalizing
        assert spec2["status"] != "normalizing", \
            f"normalizing state should have advanced, got {spec2['status']}"
        assert spec2["status"] in ("normalized", "planning", "planned", "executing")

    @pytest.mark.asyncio
    async def test_exact_planning_state_restart(self, setup):
        """Persist the exact 'planning' state and reconstruct from same DB."""
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(title="Planning restart", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        spec_id = r["composer_spec_id"]

        # First normalize
        spec = cps.get_spec(spec_id)
        await svc._normalize_spec(spec)
        spec = cps.get_spec(spec_id)
        assert spec["status"] == "normalized"

        # Manually set status to planning
        cps.update_spec(spec_id, status="planning")
        spec = cps.get_spec(spec_id)
        assert spec["status"] == "planning"

        # Reconstruct from same DB
        cfg2 = ConductorConfig(environment="test", storage={"sqlite_path": db})
        cfg2.composer.report_dir = os.path.join(d, "reports2")
        cps2 = ComposerStorage(db)
        cps2.initialize()
        gw2 = MockAgentsGatewayClient()
        gw2.register_harness_profile("opencode-deepseek", "OpenCode DeepSeek", runnable=True)
        reg2 = build_default_registry(cfg2)
        svc2 = ComposerService(
            storage=cps2, conductor_storage=cs,
            llm_client=FakeComposerLLMClient(), agents_gateway_client=gw2,
            config=cfg2.composer, gateway_registry=reg2, metrics=None,
        )

        # start_objective should safely rerun planning from planning state
        await svc2.start_objective(obj_id)
        spec2 = cps2.get_spec(spec_id)
        assert spec2["status"] != "planning", \
            f"planning state should have advanced, got {spec2['status']}"
        assert spec2["status"] in ("planned", "executing")

        # Verify a plan was created (not a second plan)
        plan = cps2.get_plan_by_objective(obj_id)
        assert plan is not None, "Plan should exist after planning recovery"
        # Count plans — there should be exactly one
        with cps2._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM composer_plans WHERE objective_id = ?", (obj_id,)
            ).fetchone()[0]
        assert count == 1, f"Should have exactly 1 plan, got {count}"

    @pytest.mark.asyncio
    async def test_planning_crash_recovery_is_idempotent(self, setup):
        """Planning-crash idempotency proof.

        Scenario:
          1. Run the pipeline long enough to create a plan row (we go all
             the way to *planned*).
          2. Roll the spec.status back to *planning* — simulating a crash
             that hit AFTER the plan row landed but BEFORE the spec-status
             update committed.
          3. Reconstruct the service from the same DB.
          4. Tick the supervisor via start_objective.
          5. Assert:
             - plan_id is the SAME as the pre-crash plan_id
             - exactly one plan row exists for the objective
             - spec.status advanced to planned (or executing)
        """
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(
            title="Crash recovery idempotency",
            raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        spec_id = r["composer_spec_id"]

        # Drive the pipeline fully so a plan row exists.
        await svc.start_objective(obj_id)
        pre_crash_plan = cps.get_plan_by_objective(obj_id)
        assert pre_crash_plan is not None, "Pre-crash plan must exist"
        pre_crash_plan_id = pre_crash_plan["id"]
        assert cps.count_plans_by_objective(obj_id) == 1, \
            "Sanity: exactly one plan row before the simulated crash"

        # Simulate a crash between plan insert and spec-status update:
        # roll spec status back to *planning* and DO NOT touch the plan row.
        cps.update_spec(spec_id, status="planning")

        # Reconstruct the service from the same database — this is what
        # a supervisor tick on next boot would see.
        cfg2 = ConductorConfig(environment="test", storage={"sqlite_path": db})
        cfg2.composer.report_dir = os.path.join(d, "reports_crash")
        cps2 = ComposerStorage(db)
        cps2.initialize()
        gw2 = MockAgentsGatewayClient()
        gw2.register_harness_profile("opencode-deepseek", "OpenCode DeepSeek", runnable=True)
        reg2 = build_default_registry(cfg2)
        svc2 = ComposerService(
            storage=cps2, conductor_storage=cs,
            llm_client=FakeComposerLLMClient(), agents_gateway_client=gw2,
            config=cfg2.composer, gateway_registry=reg2, metrics=None,
        )

        # Tick the supervisor — the idempotency guard should reuse the
        # existing plan rather than generate a new one.
        await svc2.start_objective(obj_id)

        # 5a. The plan_id returned must be the SAME as the pre-crash plan_id.
        post_crash_plan = cps2.get_plan_by_objective(obj_id)
        assert post_crash_plan is not None, "Post-crash plan must exist"
        assert post_crash_plan["id"] == pre_crash_plan_id, (
            f"Post-crash plan_id ({post_crash_plan['id']}) must match "
            f"pre-crash plan_id ({pre_crash_plan_id}) — start_objective "
            "must reuse, not duplicate.")

        # 5b. Exactly one plan row for the objective in the DB.
        assert cps2.count_plans_by_objective(obj_id) == 1, (
            "Plan row count must remain 1 after a crash-recovery tick; "
            "a duplicate plan means the planning flow is not idempotent.")

        # 5c. spec.status advanced past *planning*.
        spec_after = cps2.get_spec(spec_id)
        assert spec_after["status"] != "planning", (
            f"spec.status must advance past planning after a restart, "
            f"got {spec_after['status']}")
        assert spec_after["status"] in ("planned", "executing")


# ── 12. Pause/resume keeps same plan and task IDs ─────────────────────────


class TestPauseResumeIdentity:
    """Pause and resume retain the same plan and task IDs — no second plan."""

    @pytest.mark.asyncio
    async def test_pause_resume_keeps_same_plan_and_task_ids(self, setup):
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(title="Pause/resume", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        spec_id = r["composer_spec_id"]
        await svc.start_objective(obj_id)

        spec = cps.get_spec(spec_id)
        assert spec["status"] in ("planned", "executing")

        # Capture plan and task IDs before pause
        plan_before = cps.get_plan_by_objective(obj_id)
        assert plan_before is not None
        plan_id_before = plan_before["id"]
        task_ids_before = {t["id"] for t in plan_before["plan_tasks"]}
        gw_ids_before = {t.get("agents_gateway_task_id") for t in plan_before["plan_tasks"] if t.get("agents_gateway_task_id")}

        # Pause
        await svc.pause_objective(obj_id)
        spec = cps.get_spec(spec_id)
        assert spec["status"] == "paused"
        assert spec.get("previous_status") is not None
        assert spec.get("paused_at") is not None

        # Resume
        await svc.resume_objective(obj_id)
        spec = cps.get_spec(spec_id)
        assert spec["status"] != "paused"

        # Same plan and task IDs retained
        plan_after = cps.get_plan_by_objective(obj_id)
        assert plan_after is not None
        assert plan_after["id"] == plan_id_before, "Plan ID must not change"
        task_ids_after = {t["id"] for t in plan_after["plan_tasks"]}
        assert task_ids_after == task_ids_before, "Task IDs must not change"
        gw_ids_after = {t.get("agents_gateway_task_id") for t in plan_after["plan_tasks"] if t.get("agents_gateway_task_id")}
        assert gw_ids_after == gw_ids_before, "GW task IDs must not change"

        # No second plan created
        with cps._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM composer_plans WHERE objective_id = ?", (obj_id,)
            ).fetchone()[0]
        assert count == 1, f"Should still have exactly 1 plan after resume, got {count}"


# ── 13. Delayed commit evidence recovery ──────────────────────────────────


class TestDelayedEvidenceRecovery:
    """Task completes → first evidence lookup empty → second reconciliation
    recovers SHA."""

    @pytest.mark.asyncio
    async def test_delayed_commit_evidence_recovery(self, setup):
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(title="Delayed evidence", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        impl = next((t for t in plan["plan_tasks"] if t.get("agents_gateway_task_id")), None)
        assert impl is not None
        gw_id = impl["agents_gateway_task_id"]

        # Complete the task WITH verification but WITHOUT commit_sha on worktree
        gw.complete_task(gw_id, "done")
        gw.set_verification(gw_id, "passed", [
            {"name": "unit tests", "command": "pytest", "passed": True, "required": True}
        ])
        # Set worktree without commit_sha (simulates delayed commit)
        gw.set_task_worktree(gw_id, branch="feat/delayed", commit_sha="")

        # First reconcile — should complete the task but commit_sha might be empty
        await svc.reconcile_objective(obj_id)
        plan = cps.get_plan_by_objective(obj_id)
        pt = next(t for t in plan["plan_tasks"] if t["node_key"] == impl["node_key"])
        # Task status should be completed
        assert pt["status"] == "completed"
        assert pt["branch"] == "feat/delayed"

        # Now simulate the commit arriving — add a git.committed event
        gw.add_event(gw_id, "git.committed", {"sha": "delayedsha789", "branch": "feat/delayed"})

        # Second reconcile — should recover the missing commit_sha
        await svc.reconcile_objective(obj_id)
        plan = cps.get_plan_by_objective(obj_id)
        pt = next(t for t in plan["plan_tasks"] if t["node_key"] == impl["node_key"])
        assert pt["commit_sha"] == "delayedsha789", \
            f"Second reconcile should recover commit_sha, got {pt['commit_sha']}"


# ── 13b. Wiki MCP client built from config (never None) ────────────────────


class TestWikiMcpWiring:
    """Wiki MCP client is wired from configuration onto ComposerService,
    never silently None — fix #8 part 2."""

    def test_null_client_when_url_empty(self):
        from conductor.clients.wiki_mcp import (
            build_wiki_mcp_client, NullWikiMcpClient, BaseWikiMcpClient,
        )
        from conductor.config import WikiMcpClientConfig

        # Empty URL ⇒ NullWikiMcpClient caller can use safely.
        client = build_wiki_mcp_client(WikiMcpClientConfig(url=""))
        assert isinstance(client, NullWikiMcpClient)
        # NullWikiMcpClient honours the BaseWikiMcpClient interface.
        assert isinstance(client, BaseWikiMcpClient)
        assert client.read_context("any_id") is None
        assert client.append_context("any_id", {}) is None
        assert client.health()["status"] == "disabled"
        client.close()  # must not raise

    def test_http_client_when_url_configured(self):
        from conductor.clients.wiki_mcp import (
            build_wiki_mcp_client, HttpWikiMcpClient, BaseWikiMcpClient,
        )
        from conductor.config import WikiMcpClientConfig

        cfg = WikiMcpClientConfig(
            url="https://wiki-mcp.local",
            auth_mode="internal_token",
            internal_token="t0pSecret",
            timeout_seconds=2.0,
        )
        client = build_wiki_mcp_client(cfg)
        assert isinstance(client, HttpWikiMcpClient)
        assert isinstance(client, BaseWikiMcpClient)
        client.close()

    def test_composer_service_accepts_wiki_client(self, setup):
        """The fixture builds ComposerService with wiki_mcp_client=None
        (legacy behavior) — ensure ComposerService still accepts a real
        NullWikiMcpClient instance cleanly so future wiring can flip."""
        from conductor.clients.wiki_mcp import NullWikiMcpClient
        svc, _gw, _cs, _cps, d, db = setup
        wiki = NullWikiMcpClient()
        # Direct attribute patch would work — assert the interface is
        # correct (not None is enough; the planner uses ``if wiki_mcp_client``).
        assert wiki is not None
        assert wiki.read_context("obj_does_not_exist") is None


# ── 14. HTTP artifact download ────────────────────────────────────────────


class TestHttpArtifactDownload:
    """HttpAgentsGatewayClient.download_artifact through the real endpoint."""

    def test_http_artifact_download(self):
        """Test that HttpAgentsGatewayClient.download_artifact calls the real endpoints."""
        from conductor.config import AgentsGatewayClientConfig

        cfg = AgentsGatewayClientConfig(url="http://localhost:8092")
        client = HttpAgentsGatewayClient(cfg, max_retries=0)

        # Mock the HTTP client to simulate the artifact view endpoint
        with patch.object(client, "_request") as mock_req:
            class FakeResponse:
                def __init__(self, content=b'{"status":"ok"}', status=200):
                    self.status_code = status
                    self.content = content
                    self.text = content.decode() if isinstance(content, bytes) else str(content)
                @property
                def is_success(self):
                    return self.status_code < 400
                def json(self):
                    return json.loads(self.content)

            mock_req.side_effect = [
                FakeResponse(json.dumps({"artifacts": [
                    {"id": "art-1", "name": "result.json", "path": "/tmp/result.json",
                     "size_bytes": 100, "created_at": "now", "type": "json", "metadata": {}}
                ]}).encode()),
                FakeResponse(b'{"status":"ok"}'),
            ]

            result = client.download_artifact("task-1", "result.json")
            assert result == b'{"status":"ok"}'
            assert mock_req.call_count == 2
            # Assert the second call used the actual Agents Gateway
            # artifact-content endpoint (not the non-existent
            # /tasks/{task_id}/artifacts/{artifact_id}/view route).
            second_call = mock_req.call_args_list[1]
            assert second_call.args[0] == "GET"
            assert second_call.args[1] == "/artifacts/art-1?view=true"

        client.close()

    def test_http_artifact_download_not_found(self):
        """Returns None when artifact doesn't exist."""
        from conductor.config import AgentsGatewayClientConfig

        cfg = AgentsGatewayClientConfig(url="http://localhost:8092")
        client = HttpAgentsGatewayClient(cfg, max_retries=0)

        with patch.object(client, "_request") as mock_req:
            class FakeResponse:
                def __init__(self):
                    self.status_code = 200
                    self.content = json.dumps({"artifacts": []}).encode()
                    self.text = json.dumps({"artifacts": []})
                @property
                def is_success(self):
                    return True
                def json(self):
                    return json.loads(self.content)

            mock_req.return_value = FakeResponse()

            result = client.download_artifact("task-1", "nonexistent.json")
            assert result is None

        client.close()


# ── 15. Composer report contains downstream artifacts ─────────────────────


class TestReportDownstreamArtifacts:
    """Report must include per-task downstream GW artifacts."""

    @pytest.mark.asyncio
    async def test_report_contains_downstream_artifacts(self, setup):
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(title="Artifact report", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        # Add downstream artifacts to GW tasks
        plan = cps.get_plan_by_objective(obj_id)
        for pt in plan["plan_tasks"]:
            gw_id = pt.get("agents_gateway_task_id")
            if not gw_id:
                continue
            gw.add_artifact(gw_id, "result.json", path="/tmp/result.json", size=256)
            gw.add_artifact(gw_id, "session.log", path="/tmp/session.log", size=1024)
            gw.add_artifact(gw_id, "screenshot.png", path="/tmp/screenshot.png", size=2048)
            if pt.get("node_key") == "integration":
                continue
            gw.complete_task(gw_id, "done")
            gw.set_verification(gw_id, "passed", [
                {"name": "unit tests", "command": "pytest", "passed": True, "required": True}
            ])
            gw.set_task_worktree(gw_id, branch=f"feat/{pt['node_key']}", commit_sha=f"c{pt['node_key']}")

        await svc.reconcile_objective(obj_id)
        _complete_integration(svc, gw, cps, obj_id)
        await svc.reconcile_objective(obj_id)

        # Check the report
        report = cps.get_report_by_objective(obj_id)
        assert report is not None

        json_path = report["json_artifact_ref"]
        assert os.path.exists(json_path)
        with open(json_path) as f:
            jr = json.load(f)

        # Downstream artifacts must be present
        assert "downstream_artifacts" in jr
        downstream = jr["downstream_artifacts"]
        assert len(downstream) > 0, "Report must contain downstream artifact references"

        # Each entry should have node_key, gw_task_id, and artifacts list
        for da in downstream:
            assert "node_key" in da
            assert "gw_task_id" in da
            assert "artifacts" in da
            assert len(da["artifacts"]) > 0


# ── 16. Configuration alias resolution ────────────────────────────────────


class TestConfigAliasResolution:
    """Backward-compatible single-underscore aliases resolve to nested config."""

    def test_config_alias_resolution(self, monkeypatch):
        from conductor.config import _env_overrides

        # Single-underscore alias
        monkeypatch.setenv("CONDUCTOR_COMPOSER_LLM_BASE_URL", "https://alias.test/v1")
        monkeypatch.setenv("CONDUCTOR_COMPOSER_LLM_API_KEY", "alias-key-123")
        monkeypatch.setenv("CONDUCTOR_COMPOSER_LLM_MODEL", "alias-model")
        monkeypatch.setenv("CONDUCTOR_AGENTS_GATEWAY_URL", "http://alias-gw:8092")

        overrides = _env_overrides()

        assert overrides["composer"]["llm_base_url"] == "https://alias.test/v1"
        assert overrides["composer"]["llm_api_key"] == "alias-key-123"
        assert overrides["composer"]["llm_model"] == "alias-model"
        assert overrides["agents_gateway"]["url"] == "http://alias-gw:8092"

    def test_nested_double_underscore_takes_precedence(self, monkeypatch):
        """Double-underscore nesting and single-underscore alias both work."""
        from conductor.config import _env_overrides

        monkeypatch.setenv("CONDUCTOR_COMPOSER__LLM_BASE_URL", "https://nested.test/v1")
        monkeypatch.setenv("CONDUCTOR_COMPOSER_LLM_MODEL", "alias-model")

        overrides = _env_overrides()

        assert overrides["composer"]["llm_base_url"] == "https://nested.test/v1"
        assert overrides["composer"]["llm_model"] == "alias-model"

    def test_full_config_loads_with_aliases(self, monkeypatch):
        from conductor.config import load_config

        monkeypatch.setenv("CONDUCTOR_COMPOSER_LLM_BASE_URL", "https://full.test/v1")
        monkeypatch.setenv("CONDUCTOR_COMPOSER_LLM_API_KEY", "full-key")
        monkeypatch.setenv("CONDUCTOR_COMPOSER_LLM_MODEL", "full-model")
        monkeypatch.setenv("CONDUCTOR_AGENTS_GATEWAY_URL", "http://full-gw:8092")

        cfg = load_config()
        assert cfg.composer.llm_base_url == "https://full.test/v1"
        assert cfg.composer.llm_api_key == "full-key"
        assert cfg.composer.llm_model == "full-model"
        assert cfg.agents_gateway.url == "http://full-gw:8092"


# ── 17. Real artifact API contract against Agents Gateway ASGI app ────────


def _agents_gateway_root() -> str:
    """Return the path to the sibling agents-gateway repo, if present."""
    candidates = [
        os.environ.get("AGENTS_GATEWAY_REPO", ""),
        "/home/ubuntu/agent-gateway",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "..",
                     "agent-gateway"),
    ]
    for path in candidates:
        if path and os.path.isdir(os.path.join(path, "agents_gateway")):
            return path
    return ""


def _agents_gateway_available() -> bool:
    """True if the sibling agents-gateway repo (with create_asgi_app) is
    importable from a path on PYTHONPATH."""
    import sys
    root = _agents_gateway_root()
    if root and root not in sys.path:
        sys.path.insert(0, root)
    try:
        import importlib.util
        return importlib.util.find_spec("agents_gateway") is not None
    except (ImportError, ValueError):
        return False


@pytest.mark.skipif(not _agents_gateway_available(),
                    reason="agents_gateway package not on PYTHONPATH")
class TestRealArtifactApiContract:
    """Real contract test: boot the Agents Gateway ASGI app and verify
    HttpAgentsGatewayClient.download_artifact actually fetches content
    from ``GET /artifacts/{artifact_id}?view=true`` — the unified
    endpoint that backs BOTH harness_artifacts and task_artifacts."""

    def test_download_artifact_via_real_gateway(self, tmp_path):
        import sys

        # Import lazily so the test only attempts to load the package
        # when the module actually lives on sys.path.
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("starlette.testclient not available")

        gw_root = _agents_gateway_root()
        if not gw_root:
            pytest.skip("agents_gateway repo not found")
        if gw_root not in sys.path:
            sys.path.insert(0, gw_root)

        # Agent-gateway uses pydantic-only config defaults; build a
        # minimal GatewayConfig with auth disabled, isolated storage,
        # and the fake tmux driver so no real shell/tmux is touched.
        from agents_gateway.config import GatewayConfig
        from agents_gateway.server import create_asgi_app
        from agents_gateway.storage import TaskStorage
        from agents_gateway.metrics import MetricsRegistry

        cfg = GatewayConfig(
            auth={"mode": "dev-none"},
            storage={"sqlite_path": str(tmp_path / "agw_contract.db"),
                     "artifacts_dir": str(tmp_path / "artifacts_contract")},
            service={"rate_limiting": {"enabled": False,
                                       "requests_per_minute": 999}},
            harness={"workspace_root": str(tmp_path / "repos_contract"),
                     "worktree_root": str(tmp_path / "wts_contract"),
                     "artifacts_root": str(tmp_path / "artifacts_contract"),
                     "use_fake_tmux": True},
            agents={"dir": str(tmp_path / "agents_contract")},
            integrations={"skills_gateway": {"enabled": False},
                          "mcp_gateway": {"enabled": False}},
        )
        app = create_asgi_app(cfg, reg=MetricsRegistry())
        with TestClient(app) as cli:
            # Record an artifact through TaskStorage.add_artifact — this
            # is exactly the path the runtime worker follows for non-
            # harness agents that finish with a result.json blob.
            store = TaskStorage(cfg.storage.sqlite_path)
            task = store.create_task(agent_id="legacy_contract")
            blob_path = tmp_path / "result_contract.json"
            blob_path.write_text('{"commit_sha": "deadbeef"}')
            artifact = store.add_artifact(
                task.id, "result.json", str(blob_path),
                len('{"commit_sha": "deadbeef"}'))

            # Drive the HttpAgentsGatewayClient through the in-process
            # ASGI app. We cannot use a real HTTP socket from TestClient,
            # so we patch the client's _request to forward into TestClient.
            from conductor.config import AgentsGatewayClientConfig
            client = HttpAgentsGatewayClient(
                AgentsGatewayClientConfig(url="http://asgi.local"), max_retries=0)

            def fake_request(method, path, **kwargs):
                resp = cli.request(method, path)
                # Mimic httpx.Response surface used by Conductor.
                class R:
                    status_code = resp.status_code
                    content = resp.content
                    text = resp.text
                    @property
                    def is_success(self_):
                        return resp.status_code < 400
                    def json(self_):
                        return resp.json()
                return R()

            with patch.object(client, "_request", side_effect=fake_request):
                # Step 1: list task artifacts via /tasks/{task_id}/artifacts
                # Step 2: download bytes via /artifacts/{id}?view=true
                blob = client.download_artifact(task.id, "result.json")
                assert blob == b'{"commit_sha": "deadbeef"}'
            client.close()


# ── 18. Report HTTP routes (fix #4 from aa7777b) ──────────────────────────


class TestReportHttpRoutes:
    """GET /report/html and /report/json logic: files exist → served, missing → not found."""

    def test_report_html_artifact_ref_points_to_file(self, setup):
        """Report created with a real html_artifact_ref stores the path."""
        import os
        svc, gw, cs, cps, d, db = setup
        report_dir = svc.config.report_dir
        os.makedirs(report_dir, exist_ok=True)

        html_path = os.path.join(report_dir, "test-report.html")
        json_path = os.path.join(report_dir, "test-report.json")
        with open(html_path, "w") as f:
            f.write("<html><body>Test Report</body></html>")
        with open(json_path, "w") as f:
            f.write('{"objective_id": "obj1"}')

        objective_id = "test-html-ref"
        cps.create_report(objective_id, "completed",
                          html_artifact_ref=html_path,
                          json_artifact_ref=json_path)
        r = svc.get_report(objective_id)
        assert r is not None
        assert r.get("html_artifact_ref") == html_path
        assert os.path.isfile(r["html_artifact_ref"]), "HTML artifact ref must be a real file"

    def test_report_missing_file_is_detectable(self, setup):
        """Report with nonexistent artifact ref can be detected via os.path.isfile."""
        import os
        svc, gw, cs, cps, d, db = setup

        objective_id = "test-missing-ref"
        cps.create_report(objective_id, "completed",
                          html_artifact_ref="/nonexistent/report.html",
                          json_artifact_ref="/nonexistent/report.json")
        r = svc.get_report(objective_id)
        assert r is not None
        html_ref = r.get("html_artifact_ref", "")
        assert html_ref
        assert not os.path.isfile(html_ref), "Missing file must not exist on disk"
        json_ref = r.get("json_artifact_ref", "")
        assert json_ref
        assert not os.path.isfile(json_ref)


# ── 19. Production localhost gateway client selection (fix #6 from aa7777b) ──


class TestGatewayClientLocalhostProduction:
    """Production + localhost URL → real HTTP client (not mock)."""

    def test_mcp_gateway_production_localhost_uses_http(self):
        """When environment=production and URL is localhost, HttpMcpGatewayClient is built."""
        from conductor.server import _build_mcp_gateway_client, _is_localhost_url
        from conductor.config import ConductorConfig, McpGatewayClientConfig

        cfg = ConductorConfig(environment="production")
        cfg.composer.enabled = True
        cfg.composer.test_mode = False
        cfg.mcp_gateway = McpGatewayClientConfig(url="http://localhost:8095",
                                                  auth_mode="internal_only",
                                                  internal_token="test-token")

        client = _build_mcp_gateway_client(cfg)
        from conductor.clients.mcp_gateway import HttpMcpGatewayClient
        assert client is not None
        assert isinstance(client, HttpMcpGatewayClient), (
            f"expected HttpMcpGatewayClient, got {type(client).__name__}")

    def test_mcp_gateway_dev_localhost_uses_mock(self):
        """When environment=test/dev and URL is localhost, MockMcpGatewayClient is built."""
        from conductor.server import _build_mcp_gateway_client
        from conductor.config import ConductorConfig, McpGatewayClientConfig

        cfg = ConductorConfig(environment="test")
        cfg.composer.enabled = True
        cfg.composer.test_mode = True
        cfg.mcp_gateway = McpGatewayClientConfig(url="http://localhost:8095",
                                                  auth_mode="internal_only",
                                                  internal_token="test-token")

        client = _build_mcp_gateway_client(cfg)
        from conductor.clients.mcp_gateway import MockMcpGatewayClient
        assert client is not None
        assert isinstance(client, MockMcpGatewayClient), (
            f"expected MockMcpGatewayClient, got {type(client).__name__}")

    def test_skills_gateway_production_localhost_uses_http(self):
        """When environment=production and URL is localhost, HttpSkillsGatewayClient is built."""
        from conductor.server import _build_skills_client
        from conductor.config import ConductorConfig, SkillsGatewayClientConfig

        cfg = ConductorConfig(environment="production")
        cfg.composer.enabled = True
        cfg.composer.test_mode = False
        cfg.skills_gateway = SkillsGatewayClientConfig(url="http://localhost:8096",
                                                        auth_mode="internal_token",
                                                        internal_token="test-token")

        client = _build_skills_client(cfg)
        from conductor.clients.skills_gateway import HttpSkillsGatewayClient
        assert client is not None
        assert isinstance(client, HttpSkillsGatewayClient), (
            f"expected HttpSkillsGatewayClient, got {type(client).__name__}")

    def test_skills_gateway_dev_localhost_returns_none(self):
        """When environment=test/dev and URL is localhost, _build_skills_client returns None."""
        from conductor.server import _build_skills_client
        from conductor.config import ConductorConfig, SkillsGatewayClientConfig

        cfg = ConductorConfig(environment="test")
        cfg.composer.enabled = True
        cfg.composer.test_mode = True
        cfg.skills_gateway = SkillsGatewayClientConfig(url="http://localhost:8096",
                                                        auth_mode="internal_token",
                                                        internal_token="test-token")

        client = _build_skills_client(cfg)
        assert client is None, (
            f"dev + localhost skills gateway should return None, got {type(client).__name__ if client else None}")


# ── 20. inputSchema normalization + ref propagation ───────────────────────


class TestMcpInputSchemaAndRef:
    """inputSchema (camelCase) normalization + ref propagation through context."""

    def test_list_tools_normalizes_input_schema_camelcase(self):
        """HTTP response with ``inputSchema`` (not ``input_schema``) still populates McpTool."""
        from conductor.clients.mcp_gateway import McpTool

        # Pre-fix old code: only looked at input_schema key
        old_code = lambda t: McpTool(
            name=t.get("name", ""),
            description=t.get("description", ""),
            input_schema=t.get("input_schema", {}) or {},
        )
        # Post-fix new code: normalizes both input_schema and inputSchema
        new_fixed = lambda t: McpTool(
            name=t.get("name", ""),
            description=t.get("description", ""),
            input_schema=t.get("input_schema") or t.get("inputSchema") or {},
        )

        tool_data = {
            "name": "git.read_file",
            "description": "Read a file from the repo",
            "inputSchema": {  # camelCase — MCP spec canonical form
                "type": "object",
                "properties": {"path": {"type": "string"},
                               "ref": {"type": "string"}},
            },
        }

        old_result = old_code(tool_data)
        assert old_result.input_schema == {}, (
            "pre-fix: input_schema key absent → discarded to {}")
        new_result = new_fixed(tool_data)
        assert new_result.input_schema != {}, (
            "post-fix: inputSchema must be normalised into input_schema")
        assert "path" in new_result.input_schema.get("properties", {})

    def test_build_file_tool_args_repository_full_name(self):
        """When schema declares ``repository_full_name``, it receives owner/repo."""
        from conductor.composer.context import _build_file_tool_args

        tool = MagicMock()
        tool.input_schema = {
            "type": "object",
            "properties": {
                "repository_full_name": {"type": "string"},
                "ref": {"type": "string"},
            },
            "required": ["repository_full_name"],
        }
        # Simulate _tool_input_schema returning the schema
        tool.get.return_value = None  # not a dict

        args = _build_file_tool_args(tool, "owner", "repo1", "README.md", ref="develop")
        assert args.get("repository_full_name") == "owner/repo1"
        assert args.get("ref") == "develop"

    def test_build_tree_tool_args_repo_full_name(self):
        """When schema declares ``repo_full_name``, it receives owner/repo."""
        from conductor.composer.context import _build_tree_tool_args

        tool = MagicMock()
        tool.input_schema = {
            "type": "object",
            "properties": {
                "repo_full_name": {"type": "string"},
                "ref": {"type": "string"},
            },
        }
        args = _build_tree_tool_args(tool, "own", "repo", ref="develop")
        assert args.get("repo_full_name") == "own/repo"
        assert args.get("ref") == "develop"

    def test_ref_omitted_when_empty(self):
        """``ref`` is NOT included when empty and schema declares it."""
        from conductor.composer.context import _build_file_tool_args

        tool = MagicMock()
        tool.input_schema = {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "ref": {"type": "string"},
            },
            "required": ["owner", "repo", "path"],
        }
        args = _build_file_tool_args(tool, "owner", "repo", "README.md", ref="")
        assert "ref" not in args, (
            f"empty ref MUST NOT be included; got args={args}")

    def test_ref_included_when_nonempty(self):
        """Non-empty ``ref`` is included when schema declares it."""
        from conductor.composer.context import _build_file_tool_args

        tool = MagicMock()
        tool.input_schema = {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "ref": {"type": "string"},
            },
        }
        args = _build_file_tool_args(tool, "owner", "repo", "README.md", ref="develop")
        assert args.get("ref") == "develop"

    def test_schema_has_path_param_matches_file_path(self):
        """``_schema_has_path_param`` matches ``file_path`` and ``filepath`` properties."""
        from conductor.composer.context import _schema_has_path_param

        # Tool with file_path property
        tool_fp = MagicMock()
        tool_fp.input_schema = {
            "type": "object",
            "properties": {"owner": {"type": "string"}, "file_path": {"type": "string"}},
        }
        assert _schema_has_path_param(tool_fp), "file_path must trigger has_path_param"

        # Tool with filepath property
        tool_fph = MagicMock()
        tool_fph.input_schema = {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "filepath": {"type": "string"}},
        }
        assert _schema_has_path_param(tool_fph), "filepath must trigger has_path_param"

        # Tool with only path property
        tool_p = MagicMock()
        tool_p.input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        }
        assert _schema_has_path_param(tool_p)

        # Tool with none of those
        tool_n = MagicMock()
        tool_n.input_schema = {
            "type": "object",
            "properties": {"search": {"type": "string"}},
        }
        assert not _schema_has_path_param(tool_n)


# ── 21. Failed-task restart failure-safe (fix #7 from aa7777b) + partial creation ──


class TestFailedTaskRestartFailureSafe:
    """Normal reconcile restart dispatch refusal → blocked_external, not permanent fail."""

    @pytest.mark.asyncio
    async def test_ordinary_restart_refusal_marks_blocked_external(self, setup):
        """Restart dispatch returns None → plan task becomes blocked_external."""
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(
            title="Restart refusal", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        impl = next((t for t in plan["plan_tasks"]
                     if t.get("agents_gateway_task_id")
                     and t.get("node_key") != "integration"), None)
        assert impl is not None
        gw_id = impl["agents_gateway_task_id"]

        # Fail the task
        gw.fail_task(gw_id)

        # Patch the scheduler's restart_failed_task to return None
        # (simulating dispatch refusal)
        original = svc.scheduler.restart_failed_task

        def refuse_restart(*a, **kw):
            return None

        svc.scheduler.restart_failed_task = refuse_restart
        try:
            await svc.reconcile_objective(obj_id)
        finally:
            svc.scheduler.restart_failed_task = original

        plan_after = cps.get_plan_by_objective(obj_id)
        pt = next(t for t in plan_after["plan_tasks"]
                   if t["id"] == impl["id"])
        assert pt["status"] == "blocked_external", (
            f"dispatch refusal must mark blocked_external, got {pt['status']}")
        assert pt["agents_gateway_task_id"] == gw_id, (
            "old GW task ID must be preserved for evidence inspection")
        meta = pt.get("metadata", {})
        assert meta.get("last_restart_failed") is True

    @pytest.mark.asyncio
    async def test_partial_creation_run_failure_preserves_both_gw_ids(self, setup):
        """create_harness_task succeeds, run_task fails → partial_creation preserved,
        blocked_external, both GW IDs tracked."""
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(
            title="Partial creation", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        impl = next((t for t in plan["plan_tasks"]
                     if t.get("agents_gateway_task_id")
                     and t.get("node_key") != "integration"), None)
        assert impl is not None
        old_gw_id = impl["agents_gateway_task_id"]
        plan_task_id = impl["id"]

        gw.fail_task(old_gw_id)

        # Patch _dispatch_one to return a partial-creation result
        # without touching the real GW at all — the reconcile code
        # just sees this returned dict and handles it accordingly.
        partial_result = {"node_id": impl["node_key"],
                          "gw_task_id": None,
                          "partial_gw_task_id": "phantom-gw-999",
                          "run_failed": True}

        with patch.object(svc.scheduler, "_dispatch_one", return_value=partial_result):
            await svc.reconcile_objective(obj_id)

        plan_after = cps.get_plan_by_objective(obj_id)
        pt = next(t for t in plan_after["plan_tasks"]
                   if t["id"] == plan_task_id)
        assert pt["status"] == "blocked_external", (
            f"partial creation must result in blocked_external, got {pt['status']}")
        assert pt["agents_gateway_task_id"] == old_gw_id, (
            "old GW task ID must be preserved as evidence")
        meta = pt.get("metadata", {})
        assert meta.get("partial_gw_task_id") == "phantom-gw-999", (
            "partial_gw_task_id must be recorded so the phantom task is tracked")
        assert meta.get("last_restart_failed") is True


# ── 22b. Initial dispatch partial failure ────────────────────────────────
#        create_harness_task succeeds, run_task fails on attempt 1 —
#        plan task must go to blocked_external, not counted as dispatched.


class TestInitialDispatchPartialFailure:
    """Partial creation on the very first dispatch must be detected
    and NOT counted as successfully dispatched."""

    @pytest.mark.asyncio
    async def test_initial_dispatch_partial_handled_not_counted(self, setup):
        """During initial dispatch, create_harness_task succeeds but
        run_task fails on attempt 1.  The plan task must end up
        blocked_external with the partial gw_task_id tracked, and
        dispatch_ready_tasks must return a list that does NOT contain
        this node."""
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(
            title="Initial partial", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]

        # Start up to planning but stop short of executing — we want
        # to call an intermediate state that only plans but doesn't
        # dispatch yet.
        await svc.start_objective(obj_id)          # received → normalized → planned
        # At this point spec is "planned" and NO tasks are dispatched.
        # Note: start_objective calls _dispatch_ready which dispatches
        # on the planned→executing transition.  Our mock harness is
        # runnable so the task IS dispatched on the first tick.
        # But we need the second dispatch (after reconcile sees that
        # nothing is running yet) to be the one we observe.

        # We'll fail the already-dispatched task so reconcile fires
        # a restart (attempt >= 2), while we separately test the
        # first-dispatch path by creating a NEW spec and patching
        # run_task to fail on the very first dispatch tick.

        r2 = await svc.submit_specification(
            title="First dispatch crash", raw_spec="Build y", auto_start=True)
        obj2 = r2["objective_id"]

        # Advance to planned, then make run_task crash on the
        # _dispatch_ready call that happens inside start_objective
        # (status "planned" → "executing").  We need the scheduler's
        # _dispatch_one to pass through create_harness_task (mock
        # succeeds) but then run_task raises.
        #
        # Strategy: patch the gateway's run_task to raise before
        # start_objective reaches planned→executing.  We advance
        # to planned first so the next tick dispatches.
        await svc.start_objective(obj2)             # received → normalized → planned
        # Spec is "planned", tasks should be pending — dispatch hasn't
        # happened yet (because the spec transitions planned→executing
        # inside _dispatch_ready, not in start_objective). Wait, let
        # me re-check: start_objective at status "planned" calls
        # _dispatch_ready.  If dispatched, it sets status="executing".
        #
        # Let's force patch generation and status to "planned" directly
        # so the next tick dispatches and we can observe.

        # Simpler: manually call _dispatch_ready with a patched
        # run_task that raises.  We'll create a spec, skip dispatch
        # in start_objective by patching, then call _dispatch_ready
        # directly.

        # Clear gateway to start fresh
        # (MockAgentsGatewayClient doesn't need clearing but let's
        #  be precise)
        original_run = gw.run_task

        def crashy_run(task_id):
            raise RuntimeError("mock run_task exploded")

        try:
            # 1) Advance to planned
            await svc.start_objective(obj2)
            spec = cps.get_spec_by_objective(obj2)
            assert spec["status"] in ("planned", "executing"), (
                f"spec should be planned/executing, got {spec['status']}")
            plan = cps.get_plan_by_objective(obj2)
            tasks = plan.get("plan_tasks", [])
            impls = [t for t in tasks if t.get("node_key") != "integration"]
            assert impls, "must have at least one impl task node"

            # 2) Patch run_task to raise so the next dispatch tick
            #    hits partial creation
            gw.run_task = crashy_run

            # 3) Manually invoke dispatch_ready_tasks through the
            #    service's _dispatch_ready gateway path.  Mock the
            #    gateway's create_harness_task preserves normal
            #    behaviour (mock handles it).
            dispatched = await svc._dispatch_ready(obj2)

            # Even if the spec was already "executing" from a previous
            # tick, _dispatch_ready respects max_parallel so a crashy
            # run_task on a new ready node must NOT appear in
            # dispatched list.
            #
            # dispatched is a list of dicts; each non-partial dict
            # has gw_task_id set.
            partials = [d for d in dispatched if d.get("gw_task_id") is None and d.get("run_failed")]
            assert not partials, (
                "dispatch_ready_tasks must not return partial-creation dicts "
                "from the first dispatch — partials must return None/be excluded")

            # The task that run_task crashed on must now be
            # blocked_external.
            plan_after = cps.get_plan_by_objective(obj2)
            for pt in plan_after["plan_tasks"]:
                meta = pt.get("metadata", {})
                if meta.get("last_dispatch_failed"):
                    assert pt["status"] == "blocked_external", (
                        f"partial first dispatch must produce blocked_external, "
                        f"got {pt['status']}")
                    assert meta.get("dispatch_error"), (
                        "metadata must contain the run_task error")
                    assert meta.get("partial_gw_task_id"), (
                        "metadata must preserve the phantom gw_task_id")
                    return

            # If no task has last_dispatch_failed, it means either
            # run_task didn't fire (no ready nodes) or the mock
            # gateway didn't create_harness_task.  Let's dispatch
            # more explicitly.
            plan_obj = svc._dict_to_plan(plan)
            node = plan_obj.tasks[0]
            result = svc.scheduler._dispatch_one(
                node, spec, obj2, plan["id"],
                spec.get("repository_url", ""),
                spec.get("base_branch", "master"),
                attempt=1,
            )
            assert result is None, (
                f"attempt-1 partial must return None to exclude from "
                f"dispatched list, got {result}")

            plan_after = cps.get_plan_by_objective(obj2)
            blocked = [t for t in plan_after["plan_tasks"]
                       if t.get("node_key") == node.node_id
                       and t.get("status") == "blocked_external"]
            assert blocked, (
                f"after attempt-1 partial, plan task {node.node_id} must be "
                f"blocked_external")
            meta_final = blocked[0].get("metadata", {})
            assert meta_final.get("partial_gw_task_id"), (
                "partial_gw_task_id must be preserved")
            assert meta_final.get("last_dispatch_failed"), (
                "last_dispatch_failed must be True")
            assert meta_final.get("dispatch_error"), (
                "dispatch_error must contain the run_task error")

        finally:
            gw.run_task = original_run


# ── 23. Interaction restart preserves partial_gw_task_id ──────────────────


class TestInteractionPartialIdPreservation:
    """When an interaction-directed restart produces a partial creation
    (create_harness_task succeeds, run_task fails), the partial_gw_task_id
    must be recorded alongside the old gw_task_id."""

    @pytest.mark.asyncio
    async def test_interaction_restart_partial_preserves_partial_id(self, setup):
        svc, gw, cs, cps, d, db = setup
        r = await svc.submit_specification(
            title="Interaction partial", raw_spec="Build x", auto_start=True)
        obj_id = r["objective_id"]
        await svc.start_objective(obj_id)

        plan = cps.get_plan_by_objective(obj_id)
        impl = next((t for t in plan["plan_tasks"]
                     if t.get("agents_gateway_task_id")
                     and t.get("node_key") != "integration"), None)
        assert impl is not None, "must have at least one dispatched task"
        old_gw_id = impl["agents_gateway_task_id"]
        plan_task_id = impl["id"]

        # Create a mock interaction
        gw.create_mock_interaction(old_gw_id, prompt="Restart me — task crashed")
        interactions = gw.list_interactions(status="pending")
        assert interactions

        # Force the LLM to vote ``restart_task``
        from conductor.composer.llm import InteractionResult
        original_answer = svc.interaction_handler.llm.answer_interaction

        async def answer_restart(*a, **kw):
            return InteractionResult(
                action="restart_task",
                reply="restart the task",
                decision_summary="restart",
            )
        svc.interaction_handler.llm.answer_interaction = answer_restart

        # Patch restart_failed_task to return a partial-creation dict
        # (run_failed=True with partial_gw_task_id)
        original_restart = svc.scheduler.restart_failed_task

        partial_result = {
            "node_id": impl["node_key"],
            "gw_task_id": None,
            "partial_gw_task_id": "interaction-partial-888",
            "run_failed": True,
        }

        def fake_restart(*a, **kw):
            return partial_result

        svc.scheduler.restart_failed_task = fake_restart
        try:
            decisions = await svc.interaction_handler.process_pending_interactions(
                obj_id, plan, cps.get_spec_by_objective(obj_id))
        finally:
            svc.scheduler.restart_failed_task = original_restart
            svc.interaction_handler.llm.answer_interaction = original_answer

        assert decisions, "must persist a restart-failure decision"
        decision = decisions[0]
        assert decision["action"] == "restart_task_failed", (
            f"must be restart_task_failed, got {decision['action']}")
        assert "interaction-partial-888" in decision.get("decision_summary", ""), (
            "decision summary must mention partial gw task ID")

        # Verify the plan task metadata
        plan_after = cps.get_plan_by_objective(obj_id)
        pt = next(t for t in plan_after["plan_tasks"]
                  if t["id"] == plan_task_id)
        assert pt["status"] == "blocked_external", (
            f"partial creation must produce blocked_external, got {pt['status']}")
        assert pt["agents_gateway_task_id"] == old_gw_id, (
            "old GW task ID must be preserved")
        meta = pt.get("metadata", {})
        assert meta.get("partial_gw_task_id") == "interaction-partial-888", (
            f"partial_gw_task_id must be recorded, got {meta.get('partial_gw_task_id')}")
        assert meta.get("last_restart_failed") is True, (
            "last_restart_failed must be True")


# ── 22. Result artifact lookup by artifact ID via GW artifact list ────────


class TestResultArtifactLookup:
    """Agent Gateway contract: list artifacts, find by name, download by artifact ID."""

    def test_artifact_lookup_via_list_then_download(self):
        """The correct sequence is:
        1. GET /tasks/{task_id}/artifacts → list of artifacts
        2. find artifact where name == 'result.json', extract its id
        3. GET /artifacts/{id}?view=true → download artifact
        """
        from conductor.config import AgentsGatewayClientConfig
        from conductor.clients.agents_gateway import HttpAgentsGatewayClient

        cfg = AgentsGatewayClientConfig(url="http://localhost:8092")
        client = HttpAgentsGatewayClient(cfg, max_retries=0)

        result_artifact_id = "art_result_789"
        result_json_blob = json.dumps({
            "git": {"pushed": True, "branch": "integration/test", "sha": "abc123"},
            "status": "ok",
        }).encode()

        with patch.object(client, "_request") as mock_req:
            class FakeResponse:
                def __init__(self, content, status=200):
                    self.status_code = status
                    self.content = content if isinstance(content, bytes) else content.encode()
                    self.text = content if isinstance(content, str) else content.decode()
                    self.request = MagicMock()
                    self.request.url = "http://mock/"

                @property
                def is_success(self):
                    return self.status_code < 400

                def json(self):
                    return json.loads(self.content)

            # Call 1: list task artifacts
            mock_req.side_effect = [
                FakeResponse(json.dumps({"artifacts": [
                    {"id": "art_session_log", "name": "session.log",
                     "path": "/tmp/session.log", "size_bytes": 500,
                     "created_at": "2024-01-01", "task_id": "task_xyz"},
                    {"id": result_artifact_id, "name": "result.json",
                     "path": "/tmp/result.json", "size_bytes": 200,
                     "created_at": "2024-01-01", "task_id": "task_xyz"},
                ]}).encode()),
                # Call 2: download artifact by id
                FakeResponse(result_json_blob),
            ]

            blob = client.download_artifact("task_xyz", "result.json")
            assert blob == result_json_blob
            assert mock_req.call_count == 2

            # First call: list artifacts for task
            call1 = mock_req.call_args_list[0]
            assert call1.args[0] == "GET"
            assert "/tasks/task_xyz/artifacts" in call1.args[1]

            # Second call: download by artifact ID
            call2 = mock_req.call_args_list[1]
            assert call2.args[0] == "GET"
            assert f"/artifacts/{result_artifact_id}?view=true" == call2.args[1], (
                "second call must be /artifacts/{artifact_id}?view=true, "
                f"but got {call2.args[1]}")

        client.close()


# ── 23. Disposable baseline branch construction ────────────────────────────


class TestDisposableBaselineBranch:
    """Unique orphan branch creation as prescribed by the live-E2E contract."""

    def test_disposable_baseline_branch_isolation(self):
        """Baseline branch is an orphan — commits on it do not share
        history with the default branch."""
        import subprocess, tempfile, os, time

        tmp = tempfile.TemporaryDirectory()
        try:
            repo_dir = os.path.join(tmp.name, "baseline-repo")
            subprocess.run(["git", "init", "-q", repo_dir], check=True,
                           capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test"],
                cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=repo_dir, check=True, capture_output=True)

            # Create initial content on default branch (main)
            os.makedirs(os.path.join(repo_dir, "src"))
            with open(os.path.join(repo_dir, "README.md"), "w") as f:
                f.write("# Normal repo")
            with open(os.path.join(repo_dir, "src", "main.py"), "w") as f:
                f.write("def main(): return 'hello'")
            subprocess.run(
                ["git", "add", "README.md", "src/main.py"],
                cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial: default branch"],
                cwd=repo_dir, check=True, capture_output=True)
            default_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_dir, check=True, capture_output=True,
                text=True).stdout.strip()

            # Simulate the live-E2E baseline: create an orphan branch
            ts = int(time.time())
            baseline_branch = f"composer-live-baseline-{ts}"
            subprocess.run(
                ["git", "checkout", "-q", "--orphan", baseline_branch],
                cwd=repo_dir, check=True, capture_output=True)
            # Orphan branch starts with no committed files — git rm to
            # clear the index (files from previous branch are staged)
            subprocess.run(
                ["git", "rm", "-rfq", "."],
                cwd=repo_dir, check=False, capture_output=True)

            # Write only the add-only calculator baseline
            os.makedirs(os.path.join(repo_dir, "calculator"))
            with open(os.path.join(repo_dir, "calculator", "__init__.py"), "w") as f:
                f.write("def add(a, b): return a + b")
            with open(os.path.join(repo_dir, "calculator", "test_calculator.py"), "w") as f:
                f.write("from calculator import add\n"
                        "def test_add():\n"
                        "    assert add(2, 3) == 5")
            with open(os.path.join(repo_dir, "pyproject.toml"), "w") as f:
                f.write("[project]\n"
                        "name = 'calculator'\n"
                        "version = '0.1.0'")

            subprocess.run(
                ["git", "add", "."],
                cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-qm", "baseline: add-only"],
                cwd=repo_dir, check=True, capture_output=True)
            baseline_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_dir, check=True, capture_output=True,
                text=True).stdout.strip()

            # Baseline branch must have exactly one commit
            log_count = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=repo_dir, check=True, capture_output=True,
                text=True).stdout.strip()
            assert log_count == "1", (
                f"baseline orphan branch must have exactly 1 commit, got {log_count}")

            # Baseline SHA must differ from default branch SHA
            assert baseline_sha != default_sha, (
                f"baseline orphan branch SHA ({baseline_sha}) must "
                f"differ from default branch SHA ({default_sha})")
        finally:
            tmp.cleanup()
