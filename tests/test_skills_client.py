"""Tests for Skills Gateway client and skill validation."""

import pytest

from conductor.clients.skills_gateway import (
    MockSkillsGatewayClient,
    validate_required_skills,
    SkillInfo,
)


@pytest.fixture
def skills_client():
    client = MockSkillsGatewayClient()
    client.register("code-review", "Code Review", "Review code", "1.0", "low")
    client.register("security-audit", "Security Audit", "Audit security", "2.0", "high")
    return client


class TestMockSkillsClient:
    def test_list_skills(self, skills_client):
        skills = skills_client.list_skills()
        assert len(skills) == 2

    def test_inspect_skill(self, skills_client):
        info = skills_client.inspect_skill("code-review")
        assert info is not None
        assert info["name"] == "Code Review"

    def test_inspect_unknown(self, skills_client):
        assert skills_client.inspect_skill("nonexistent") is None

    def test_read_skill(self, skills_client):
        content = skills_client.read_skill("code-review")
        assert content is not None
        assert "Code Review" in content

    def test_read_unknown(self, skills_client):
        assert skills_client.read_skill("nonexistent") is None


class TestValidateSkills:
    def test_all_known(self, skills_client):
        ok, valid, missing = validate_required_skills(skills_client, ["code-review", "security-audit"])
        assert ok is True
        assert len(missing) == 0
        assert valid == ["code-review", "security-audit"]

    def test_some_missing(self, skills_client):
        ok, valid, missing = validate_required_skills(skills_client, ["code-review", "nonexistent"])
        assert ok is False
        assert valid == ["code-review"]
        assert missing == ["nonexistent"]

    def test_all_missing(self, skills_client):
        ok, valid, missing = validate_required_skills(skills_client, ["nonexistent1", "nonexistent2"])
        assert ok is False
        assert len(valid) == 0
        assert len(missing) == 2

    def test_empty_skills(self, skills_client):
        ok, valid, missing = validate_required_skills(skills_client, [])
        assert ok is True
        assert len(valid) == 0
        assert len(missing) == 0