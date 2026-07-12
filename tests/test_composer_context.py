"""Tests for Composer context builder and goal construction."""

import os
import tempfile

import pytest

from conductor.composer.context import build_composer_context, context_to_prompt, _get
from conductor.composer.models import (
    CapabilityInfo,
    ComposerContext,
    GatewayInfo,
    HarnessProfileInfo,
    NormalizedSpec,
    SkillInfo,
    SpecRepository,
    TaskNode,
    VerificationCommand,
    VerificationSpec,
)
from conductor.composer.goals import build_task_brief, build_integration_brief
from conductor.clients.agents_gateway import MockAgentsGatewayClient


class TestGetHelper:
    def test_dict_input(self):
        assert _get({"name": "foo"}, "name") == "foo"
        assert _get({"name": "foo"}, "missing", "default") == "default"

    def test_none_input(self):
        assert _get(None, "name", "default") == "default"

    def test_object_input(self):
        from dataclasses import dataclass

        @dataclass
        class Obj:
            name: str = "bar"

        assert _get(Obj(), "name") == "bar"
        assert _get(Obj(), "missing", "d") == "d"


class TestBuildContext:
    def test_empty_context(self):
        ctx = build_composer_context("obj_1", None)
        assert isinstance(ctx, ComposerContext)
        assert ctx.spec == {}
        assert ctx.gateways == []

    def test_context_with_spec(self):
        spec = {"normalized_spec": {"goal": "build", "repository": {"url": "http://example.com"}}}
        ctx = build_composer_context("obj_1", spec, repo_path="/nonexistent")
        assert ctx.spec == spec
        assert ctx.repository["url"] == "http://example.com"

    def test_context_with_harness_profiles(self):
        gw = MockAgentsGatewayClient()
        gw.register_harness_profile("opencode-deepseek")
        ctx = build_composer_context("obj_1", None, agents_gateway_client=gw)
        assert len(ctx.harness_profiles) == 1
        assert ctx.harness_profiles[0].name == "opencode-deepseek"
        assert ctx.harness_profiles[0].runnable is True

    def test_context_with_harness_profiles_unavailable(self):
        gw = MockAgentsGatewayClient()
        gw.register_harness_profile("unknown-harness", runnable=False)
        ctx = build_composer_context("obj_1", None, agents_gateway_client=gw)
        assert len(ctx.harness_profiles) == 1
        assert ctx.harness_profiles[0].runnable is False

    def test_project_context_from_repo(self, tmp_path):
        # Create a mini project
        (tmp_path / "README.md").write_text("# Test Project\nA calculator.")
        (tmp_path / "AGENTS.md").write_text("## Instructions\nFollow TDD.")
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()

        ctx = build_composer_context("obj_1", None, repo_path=str(tmp_path))
        assert "readme" in ctx.project_context
        assert "# Test Project" in ctx.project_context["readme"]
        assert "agent_instructions" in ctx.project_context
        assert "tree_summary" in ctx.project_context
        assert "src/" in ctx.project_context["tree_summary"]
        assert "tests/" in ctx.project_context["tree_summary"]


class TestContextToPrompt:
    def test_empty_context(self):
        ctx = ComposerContext()
        prompt = context_to_prompt(ctx)
        assert prompt == ""

    def test_with_goal(self):
        ctx = ComposerContext(spec={"normalized_spec": {"goal": "Build a calculator"}})
        prompt = context_to_prompt(ctx)
        assert "Goal: Build a calculator" in prompt

    def test_with_requirements(self):
        ctx = ComposerContext(spec={
            "normalized_spec": {
                "goal": "Build X",
                "requirements": ["Add multiply", "Add divide"],
            }
        })
        prompt = context_to_prompt(ctx)
        assert "Add multiply" in prompt
        assert "Add divide" in prompt

    def test_with_harness(self):
        ctx = ComposerContext(
            harness_profiles=[
                HarnessProfileInfo(name="opencode-deepseek", runnable=True),
                HarnessProfileInfo(name="disabled", runnable=False),
            ]
        )
        prompt = context_to_prompt(ctx)
        assert "opencode-deepseek" in prompt
        assert "disabled" not in prompt

    def test_with_skills(self):
        ctx = ComposerContext(skills=[SkillInfo(id="tdc", name="TDD")])
        prompt = context_to_prompt(ctx)
        assert "tdc" in prompt

    def test_with_capabilities(self):
        ctx = ComposerContext(capabilities=[
            CapabilityInfo(capability="test.cap", available=True),
            CapabilityInfo(capability="missing.cap", available=False),
        ])
        prompt = context_to_prompt(ctx)
        assert "test.cap" in prompt
        assert "missing.cap" not in prompt

    def test_with_readme(self):
        ctx = ComposerContext(project_context={"readme": "# Project\nDescription"})
        prompt = context_to_prompt(ctx)
        assert "README" in prompt
        assert "# Project" in prompt


class TestBuildTaskBrief:
    def test_basic_brief(self):
        node = TaskNode(
            node_id="task_a",
            title="Implement A",
            goal="Implement feature A",
            file_scope=["src/a.py", "src/a_test.py"],
        )
        spec = NormalizedSpec(goal="Build a calculator")
        brief = build_task_brief(node, spec)
        assert "one task from a predefined specification" in brief
        assert "Implement feature A" in brief
        assert "src/a.py" in brief
        assert "src/a_test.py" in brief
        assert "assigned worktree" in brief
        assert "verification passes" in brief

    def test_brief_with_skills(self):
        node = TaskNode(
            node_id="task_a",
            required_skills=["test-driven-development", "verification-before-completion"],
        )
        spec = NormalizedSpec()
        brief = build_task_brief(node, spec)
        assert "Required skills:" in brief
        assert "test-driven-development" in brief

    def test_brief_with_capabilities(self):
        node = TaskNode(
            node_id="task_a",
            required_capabilities=["execution.task.create"],
        )
        spec = NormalizedSpec()
        brief = build_task_brief(node, spec)
        assert "Required capabilities:" in brief
        assert "execution.task.create" in brief

    def test_brief_with_verification(self):
        node = TaskNode(
            node_id="task_a",
            verification=VerificationSpec(
                commands=[VerificationCommand(name="unit tests", command="uv run pytest", required=True)],
            ),
        )
        spec = NormalizedSpec()
        brief = build_task_brief(node, spec)
        assert "Verification:" in brief
        assert "uv run pytest" in brief
        assert "unit tests" in brief

    def test_brief_with_completed_deps(self):
        dep = TaskNode(node_id="dep_a", title="Dependency A",
                       branch="feature/dep-a", commit_sha="abc123def")
        node = TaskNode(node_id="task_b", dependencies=["dep_a"])
        spec = NormalizedSpec()
        brief = build_task_brief(node, spec, completed_deps=[dep])
        assert "Relevant completed dependencies:" in brief
        assert "dep_a" in brief
        assert "feature/dep-a" in brief
        assert "abc123de" in brief  # truncated commit SHA in brief

    def test_brief_with_overall_summary(self):
        node = TaskNode(node_id="task_a", goal="Do A")
        spec = NormalizedSpec()
        brief = build_task_brief(node, spec, overall_summary="Build a calculator with multiply and divide")
        assert "Overall objective:" in brief
        assert "calculator" in brief


class TestBuildIntegrationBrief:
    def test_basic_integration_brief(self):
        spec = NormalizedSpec(goal="Build calculator", acceptance_criteria=["All tests pass"])
        completed = [
            TaskNode(node_id="task_a", branch="composer/branch-a", commit_sha="sha1"),
            TaskNode(node_id="task_b", branch="composer/branch-b", commit_sha="sha2"),
        ]
        brief = build_integration_brief(spec, completed)
        assert "Integrate" in brief
        assert "task_a" in brief
        assert "task_b" in brief
        assert "branch-a" in brief
        assert "All tests pass" in brief
        assert "Do not mark complete" in brief

    def test_integration_no_completed_tasks(self):
        spec = NormalizedSpec()
        brief = build_integration_brief(spec, [])
        assert "Integrate" in brief

    def test_integration_base_branch(self):
        spec = NormalizedSpec()
        brief = build_integration_brief(spec, [], base_branch="main")
        assert "main" in brief
