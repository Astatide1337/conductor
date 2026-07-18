"""Tests for Composer Pydantic models and enums."""

from conductor.composer.models import (
    ComposerContext,
    ComposerPlan,
    ComposerReport,
    ComposerSpec,
    ComposerSpecCreate,
    InteractionDecision,
    IntegrationNode,
    NormalizedSpec,
    NormalizedSpecResult,
    PlanResult,
    PlanValidationResult,
    SpecRepository,
    TaskNode,
    VerificationCommand,
    VerificationSpec,
    GatewayInfo,
    CapabilityInfo,
    HarnessProfileInfo,
    SkillInfo,
    LLMTaskNode,
    LLMIntegrationNode,
    InteractionResult,
    FinalSummaryResult,
)


class TestSpecModels:
    def test_spec_repository_defaults(self):
        r = SpecRepository()
        assert r.url == ""
        assert r.base_branch == "master"

    def test_spec_repository_with_values(self):
        r = SpecRepository(url="https://github.com/foo/bar.git", base_branch="main")
        assert r.url == "https://github.com/foo/bar.git"
        assert r.base_branch == "main"

    def test_normalized_spec_defaults(self):
        ns = NormalizedSpec()
        assert ns.goal == ""
        assert ns.requirements == []
        assert ns.acceptance_criteria == []
        assert ns.required_live_verification == []
        assert ns.constraints == []
        assert ns.non_goals == []

    def test_composer_spec_defaults(self):
        s = ComposerSpec(id="spec_1", objective_id="obj_1", title="Test")
        assert s.status == "received"
        assert s.raw_spec == ""
        assert s.normalized_spec.goal == ""

    def test_composer_spec_create(self):
        c = ComposerSpecCreate(title="Build", spec="do things")
        assert c.auto_start is True
        assert c.repository is None

    def test_spec_statuses_defined(self):
        from conductor.composer.models import SPEC_STATUSES, SPEC_TERMINAL
        assert "received" in SPEC_STATUSES
        assert "completed" in SPEC_STATUSES
        assert "completed" in SPEC_TERMINAL
        assert "received" not in SPEC_TERMINAL


class TestPlanModels:
    def test_verification_command(self):
        vc = VerificationCommand(name="tests", command="uv run pytest -q", required=True)
        assert vc.name == "tests"
        assert vc.command == "uv run pytest -q"
        assert vc.required is True

    def test_verification_spec_defaults(self):
        vs = VerificationSpec()
        assert vs.required is True
        assert vs.commands == []
        assert vs.live_e2e is None

    def test_task_node_defaults(self):
        t = TaskNode(node_id="task_1")
        assert t.title == ""
        assert t.task_type == "implementation"
        assert t.status == "pending"
        assert t.harness_profile == "opencode-deepseek"
        assert t.dependencies == []
        assert t.file_scope == []
        assert t.conductor_task_id is None
        assert t.commit_sha is None

    def test_integration_node_defaults(self):
        i = IntegrationNode()
        assert i.required is True
        assert i.node_id == "integration"
        assert i.title == "Integrate completed task branches"
        assert i.status == "pending"
        assert i.dependencies == []

    def test_composer_plan_defaults(self):
        p = ComposerPlan(id="plan_1", objective_id="obj_1", spec_id="spec_1")
        assert p.version == 1
        assert p.status == "draft"
        assert p.tasks == []
        assert p.integration is None
        assert p.activated_at is None
        assert p.completed_at is None

    def test_plan_statuses_defined(self):
        from conductor.composer.models import PLAN_STATUSES, PLAN_TERMINAL
        assert "draft" in PLAN_STATUSES
        assert "active" in PLAN_STATUSES
        assert "completed" in PLAN_TERMINAL

    def test_task_node_statuses_defined(self):
        from conductor.composer.models import TASK_NODE_STATUSES, TASK_NODE_TERMINAL
        assert "pending" in TASK_NODE_STATUSES
        assert "running" in TASK_NODE_STATUSES
        assert "completed" in TASK_NODE_TERMINAL

    def test_task_node_types_defined(self):
        from conductor.composer.models import TASK_NODE_TYPES
        assert "implementation" in TASK_NODE_TYPES
        assert "integration" in TASK_NODE_TYPES


class TestInteractionModels:
    def test_interaction_result_defaults(self):
        r = InteractionResult()
        assert r.action == "reply"
        assert r.reply == ""
        assert r.decision_summary == ""

    def test_interaction_decision(self):
        d = InteractionDecision(
            interaction_id="int_1",
            task_node_id="task_a",
            action="reply",
            reply="do this",
        )
        assert d.action == "reply"
        assert d.reply == "do this"

    def test_interaction_actions(self):
        from conductor.composer.models import INTERACTION_ACTIONS
        assert "reply" in INTERACTION_ACTIONS
        assert "redirect" in INTERACTION_ACTIONS
        assert "restart_task" in INTERACTION_ACTIONS
        assert "mark_external_blocker" in INTERACTION_ACTIONS


class TestReportModels:
    def test_composer_report_defaults(self):
        r = ComposerReport(id="rep_1", objective_id="obj_1")
        assert r.status == "completed"
        assert r.html_artifact_ref == ""
        assert r.pr_url is None

    def test_report_statuses(self):
        from conductor.composer.models import REPORT_STATUSES
        assert "completed" in REPORT_STATUSES
        assert "failed" in REPORT_STATUSES


class TestLLMResultModels:
    def test_normalized_spec_result(self):
        r = NormalizedSpecResult(title="Test", goal="Build feature X")
        assert r.title == "Test"
        assert r.requirements == []

    def test_plan_result_defaults(self):
        p = PlanResult()
        assert p.summary == ""
        assert p.tasks == []
        assert p.integration.required is True

    def test_llm_task_node(self):
        t = LLMTaskNode(node_id="task_a")
        assert t.node_id == "task_a"
        assert t.task_type == "implementation"

    def test_llm_integration_node(self):
        i = LLMIntegrationNode()
        assert i.node_id == "integration"
        assert i.required is True

    def test_final_summary_result(self):
        r = FinalSummaryResult(summary="Done")
        assert r.summary == "Done"
        assert r.assumptions == []
        assert r.blockers == []


class TestContextModels:
    def test_composer_context_defaults(self):
        ctx = ComposerContext()
        assert ctx.spec == {}
        assert ctx.gateways == []
        assert ctx.harness_profiles == []
        assert ctx.skills == []
        assert ctx.memory == []

    def test_gateway_info(self):
        g = GatewayInfo(id="gw_1", name="agents", enabled=True)
        assert g.id == "gw_1"
        assert g.enabled is True

    def test_capability_info(self):
        c = CapabilityInfo(capability="execution.task.create", available=True)
        assert c.available is True

    def test_harness_profile_info(self):
        h = HarnessProfileInfo(name="opencode-deepseek", runnable=True)
        assert h.runnable is True

    def test_skill_info(self):
        s = SkillInfo(id="tdc", name="test-driven-development")
        assert s.id == "tdc"


class TestValidationResult:
    def test_plan_validation_result(self):
        v = PlanValidationResult(valid=True, errors=[], warnings=[])
        assert v.valid is True
        assert v.errors == []
        assert v.warnings == []
