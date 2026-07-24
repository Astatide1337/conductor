"""Tests for Composer plan validation — DAG, uniqueness, harness, skills."""

import pytest

from conductor.composer.models import (
    ComposerContext,
    ComposerPlan,
    IntegrationNode,
    LLMIntegrationNode,
    LLMTaskNode,
    PlanResult,
    PlanValidationResult,
    TaskNode,
    VerificationCommand,
    VerificationSpec,
)
from conductor.composer.planner import (
    validate_plan,
    validate_plan_result,
    _has_cycle,
)


def _ctx(harness_names=None, skill_ids=None, caps=None):
    from conductor.composer.models import (
        CapabilityInfo,
        HarnessProfileInfo,
        SkillInfo,
    )
    return ComposerContext(
        harness_profiles=[
            HarnessProfileInfo(name=n, runnable=True) for n in (harness_names or ["pi-coding-agent"])
        ],
        skills=[SkillInfo(id=s) for s in (skill_ids or [])],
        capabilities=[CapabilityInfo(capability=c, available=True) for c in (caps or [])],
    )


def _ctx_harness(profiles, skill_ids=None, caps=None):
    """Like _ctx but each entry is (name, runnable) so tests can
    construct a mix of runnable and non-runnable profiles."""
    from conductor.composer.models import (
        CapabilityInfo,
        HarnessProfileInfo,
        SkillInfo,
    )
    return ComposerContext(
        harness_profiles=[
            HarnessProfileInfo(name=name, runnable=runnable)
            for (name, runnable) in profiles
        ],
        skills=[SkillInfo(id=s) for s in (skill_ids or [])],
        capabilities=[CapabilityInfo(capability=c, available=True) for c in (caps or [])],
    )


class TestValidatePlanResult:
    @pytest.fixture
    def valid_plan_result(self):
        return PlanResult(
            summary="Two tasks",
            tasks=[
                LLMTaskNode(
                    node_id="task_api",
                    title="API",
                    harness_profile="pi-coding-agent",
                    verification=VerificationSpec(
                        required=True,
                        commands=[VerificationCommand(name="tests", command="uv run pytest", required=True)],
                    ),
                ),
                LLMTaskNode(
                    node_id="task_db",
                    title="DB",
                    harness_profile="pi-coding-agent",
                    verification=VerificationSpec(
                        required=True,
                        commands=[VerificationCommand(name="tests", command="uv run pytest", required=True)],
                    ),
                ),
            ],
            integration=LLMIntegrationNode(
                required=True,
                node_id="integration",
                dependencies=["task_api", "task_db"],
                verification=VerificationSpec(
                    required=True,
                    commands=[VerificationCommand(name="suite", command="uv run pytest", required=True)],
                ),
            ),
        )

    def test_valid_plan(self, valid_plan_result):
        ctx = _ctx()
        result = validate_plan_result(valid_plan_result, ctx)
        assert result.valid
        assert result.errors == []

    def test_duplicate_node_ids(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="dup", harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
                LLMTaskNode(node_id="dup", harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration"),
        )
        ctx = _ctx()
        result = validate_plan_result(plan, ctx)
        assert not result.valid
        assert any("Duplicate" in e for e in result.errors)

    def test_empty_node_id(self):
        plan = PlanResult(
            tasks=[LLMTaskNode(node_id="")],
            integration=LLMIntegrationNode(node_id="integration"),
        )
        ctx = _ctx()
        result = validate_plan_result(plan, ctx)
        assert not result.valid
        assert any("empty" in e.lower() for e in result.errors)

    def test_missing_dependency(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="task_a", dependencies=["nonexistent"],
                             harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration", dependencies=["task_a"]),
        )
        ctx = _ctx()
        result = validate_plan_result(plan, ctx)
        assert not result.valid
        assert any("unknown node" in e for e in result.errors)

    def test_self_dependency(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="task_a", dependencies=["task_a"],
                             harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration", dependencies=["task_a"]),
        )
        ctx = _ctx()
        result = validate_plan_result(plan, ctx)
        assert not result.valid
        assert any("itself" in e for e in result.errors)

    def test_cycle_detection(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="a", dependencies=["b"],
                             harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
                LLMTaskNode(node_id="b", dependencies=["a"],
                             harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration", dependencies=["a", "b"]),
        )
        ctx = _ctx()
        result = validate_plan_result(plan, ctx)
        assert not result.valid
        assert any("cycle" in e for e in result.errors)

    def test_unknown_harness_profile(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="task_a", harness_profile="unknown-profile",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
                LLMTaskNode(node_id="task_b", harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration", dependencies=["task_a", "task_b"]),
        )
        ctx = _ctx(harness_names=["pi-coding-agent"])
        result = validate_plan_result(plan, ctx)
        assert not result.valid
        assert any("unknown harness" in e for e in result.errors)

    def test_nonrunnable_harness_profile_rejected(self):
        """A registered-but-not-runnable harness (binary missing on
        AGW host) must be rejected so the LLM planner retries with the
        repair plan and selects a runnable profile instead."""
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="task_a",
                             harness_profile="acme-busy",
                             verification=VerificationSpec(commands=[
                                 VerificationCommand(name="t", command="t", required=True)])),
                LLMTaskNode(node_id="task_b",
                             harness_profile="acme-ready",
                             verification=VerificationSpec(commands=[
                                 VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration",
                                           dependencies=["task_a", "task_b"]),
        )
        ctx = _ctx_harness(profiles=[
            ("acme-busy", False),
            ("acme-ready", True),
        ])
        result = validate_plan_result(plan, ctx)
        assert not result.valid
        assert any("non-runnable harness profile 'acme-busy'" in e
                   for e in result.errors)
        assert any("acme-ready" in e for e in result.errors)

    def test_unknown_skill(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="task_a", harness_profile="pi-coding-agent",
                             required_skills=["unknown-skill"],
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
                LLMTaskNode(node_id="task_b", harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration", dependencies=["task_a", "task_b"]),
        )
        ctx = _ctx(skill_ids=["test-driven-development"])
        result = validate_plan_result(plan, ctx)
        assert not result.valid
        assert any("unknown skill" in e for e in result.errors)

    def test_unavailable_capability(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="task_a", harness_profile="pi-coding-agent",
                             required_capabilities=["missing.cap"],
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
                LLMTaskNode(node_id="task_b", harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration", dependencies=["task_a", "task_b"]),
        )
        ctx = _ctx(caps=["execution.task.create"])
        result = validate_plan_result(plan, ctx)
        assert not result.valid
        assert any("unavailable capability" in e for e in result.errors)

    def test_required_verification_no_commands(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="task_a", harness_profile="pi-coding-agent",
                             verification=VerificationSpec(required=True, commands=[])),
                LLMTaskNode(node_id="task_b", harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration", dependencies=["task_a", "task_b"]),
        )
        ctx = _ctx()
        result = validate_plan_result(plan, ctx)
        assert not result.valid
        assert any("no commands" in e for e in result.errors)

    def test_warning_few_implementation_tasks(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="task_only", harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration", dependencies=["task_only"]),
        )
        ctx = _ctx()
        result = validate_plan_result(plan, ctx)
        assert result.valid  # warning, not error
        assert any("at least two" in w for w in result.warnings)

    def test_warning_integration_missing_dependency(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="task_a", harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
                LLMTaskNode(node_id="task_b", harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration", dependencies=["task_a"]),
        )
        ctx = _ctx()
        result = validate_plan_result(plan, ctx)
        assert result.valid
        assert any("task_b" in w and "dependency" in w for w in result.warnings)

    def test_file_overlap_warning(self):
        plan = PlanResult(
            tasks=[
                LLMTaskNode(node_id="task_a", file_scope=["src/"],
                             harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
                LLMTaskNode(node_id="task_b", file_scope=["src/"],
                             harness_profile="pi-coding-agent",
                             verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=LLMIntegrationNode(node_id="integration", dependencies=["task_a", "task_b"]),
        )
        ctx = _ctx()
        result = validate_plan_result(plan, ctx)
        assert result.valid
        assert any("overlapping" in w for w in result.warnings)


class TestHasCycle:
    def test_no_cycle(self):
        node_map = {"a": ["b"], "b": []}
        assert not _has_cycle(node_map)

    def test_simple_cycle(self):
        node_map = {"a": ["b"], "b": ["a"]}
        assert _has_cycle(node_map)

    def test_self_cycle(self):
        node_map = {"a": ["a"]}
        assert _has_cycle(node_map)

    def test_no_cycle_chain(self):
        node_map = {"a": ["b"], "b": ["c"], "c": []}
        assert not _has_cycle(node_map)

    def test_long_cycle(self):
        node_map = {"a": ["b"], "b": ["c"], "c": ["d"], "d": ["a"]}
        assert _has_cycle(node_map)

    def test_empty_map(self):
        node_map = {}
        assert not _has_cycle(node_map)


class TestValidatePlan:
    def test_valid_composer_plan(self):
        ctx = _ctx()
        plan = ComposerPlan(
            id="plan_1",
            objective_id="obj_1",
            spec_id="spec_1",
            tasks=[
                TaskNode(node_id="a", harness_profile="pi-coding-agent",
                          verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
                TaskNode(node_id="b", harness_profile="pi-coding-agent",
                          verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=IntegrationNode(dependencies=["a", "b"]),
        )
        result = validate_plan(plan, ctx)
        assert result.valid

    def test_composer_plan_cycle(self):
        ctx = _ctx()
        plan = ComposerPlan(
            id="plan_1",
            objective_id="obj_1",
            spec_id="spec_1",
            tasks=[
                TaskNode(node_id="a", dependencies=["b"],
                          harness_profile="pi-coding-agent",
                          verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
                TaskNode(node_id="b", dependencies=["a"],
                          harness_profile="pi-coding-agent",
                          verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)])),
            ],
            integration=IntegrationNode(dependencies=["a", "b"]),
        )
        result = validate_plan(plan, ctx)
        assert not result.valid
        assert any("cycle" in e for e in result.errors)
