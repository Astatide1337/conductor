"""Skills Gateway client — validates required skills and fetches skill metadata."""

from dataclasses import dataclass, field
from typing import Optional

import httpx

from conductor.config import SkillsGatewayClientConfig
from conductor.logging import get_logger

logger = get_logger()


@dataclass
class SkillInfo:
    id: str
    name: str
    description: str = ""
    version: str = ""
    risk_level: str = "low"
    tags: list[str] = field(default_factory=list)


class BaseSkillsGatewayClient:
    def list_skills(self) -> list[SkillInfo]:
        raise NotImplementedError

    def inspect_skill(self, skill_id: str) -> dict | None:
        raise NotImplementedError

    def read_skill(self, skill_id: str) -> str | None:
        raise NotImplementedError


class MockSkillsGatewayClient(BaseSkillsGatewayClient):
    def __init__(self, known_skills: list[SkillInfo] | None = None) -> None:
        self._skills: dict[str, SkillInfo] = {}
        if known_skills:
            for s in known_skills:
                self._skills[s.id] = s

    def register(self, id: str, name: str, description: str = "", version: str = "1.0", risk_level: str = "low") -> None:
        self._skills[id] = SkillInfo(id=id, name=name, description=description, version=version, risk_level=risk_level)

    def list_skills(self) -> list[SkillInfo]:
        return list(self._skills.values())

    def inspect_skill(self, skill_id: str) -> dict | None:
        s = self._skills.get(skill_id)
        if not s:
            return None
        return {
            "id": s.id, "name": s.name, "description": s.description,
            "version": s.version, "risk_level": s.risk_level,
        }

    def read_skill(self, skill_id: str) -> str | None:
        s = self._skills.get(skill_id)
        if not s:
            return None
        return f"# {s.name}\n{s.description}"


class HttpSkillsGatewayClient(BaseSkillsGatewayClient):
    def __init__(self, config: SkillsGatewayClientConfig) -> None:
        self.config = config
        self._client = httpx.Client(base_url=config.url.rstrip("/"), timeout=config.timeout_seconds)
        self._auth_header: dict[str, str] = {}
        if config.auth_mode == "internal-only" and config.internal_token:
            self._auth_header["X-Auth-Internal-Token"] = config.internal_token

    def list_skills(self) -> list[SkillInfo]:
        r = self._client.get("/skills", headers=self._auth_header)
        r.raise_for_status()
        data = r.json()
        skills_list = data.get("skills", data.get("data", data if isinstance(data, list) else []))
        if not isinstance(skills_list, list):
            skills_list = []
        return [SkillInfo(
            id=s.get("id", ""), name=s.get("name", ""),
            description=s.get("description", ""),
            version=s.get("version", ""),
            risk_level=s.get("risk_level", "low"),
        ) for s in skills_list]

    def inspect_skill(self, skill_id: str) -> dict | None:
        r = self._client.get(f"/skills/{skill_id}", headers=self._auth_header)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def read_skill(self, skill_id: str) -> str | None:
        r = self._client.get(f"/skills/{skill_id}/read", headers=self._auth_header)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text


def validate_required_skills(
    client: BaseSkillsGatewayClient,
    required_skills: list[str],
) -> tuple[bool, list[str], list[str]]:
    if not required_skills:
        return True, [], []

    known = {s.id for s in client.list_skills()}
    valid = [s for s in required_skills if s in known]
    missing = [s for s in required_skills if s not in known]

    if missing:
        logger.warning("missing_skills skills=%s", missing)
        return False, valid, missing
    return True, valid, []