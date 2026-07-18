"""Objective-level report generation — HTML and JSON."""

from __future__ import annotations

import json
import logging
import os
import re

from conductor.composer.models import ComposerPlan, ComposerReport, ComposerSpec
from conductor.composer.storage import ComposerStorage

logger = logging.getLogger(__name__)

__all__ = ["ReportGenerator"]


# Patterns matching common credential formats that must never appear in reports.
_SECRET_PATTERNS = [
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*[\"']?[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)token\s*[:=]\s*[\"']?[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)secret\s*[:=]\s*[\"']?[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)password\s*[:=]\s*[\"']?[A-Za-z0-9_.\-!@#$%^&*]{4,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_-]{16,}"),
]


def _redact_str(text: str) -> str:
    """Replace any credential-shaped substring with [REDACTED]."""
    redacted = text
    for pat in _SECRET_PATTERNS:
        redacted = pat.sub("[REDACTED]", redacted)
    return redacted


def _redact(obj):
    """Recursively redact secrets in any JSON-serializable structure."""
    if isinstance(obj, str):
        return _redact_str(obj)
    if isinstance(obj, dict):
        return {(_redact_str(k) if isinstance(k, str) else k): _redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


class ReportGenerator:
    """Generates HTML and JSON review reports for completed objectives."""

    def __init__(
        self,
        storage: ComposerStorage,
        report_dir: str = "/var/lib/conductor/composer-reports",
    ) -> None:
        self.storage = storage
        self.report_dir = report_dir

    def generate_report(
        self,
        objective_id: str,
        spec: dict | None = None,
        plan: dict | None = None,
        final_status: str = "completed",
        final_branch: str = "",
        final_commit_sha: str = "",
        pr_url: str | None = None,
        interactions: list[dict] | None = None,
        verification_results: list[dict] | None = None,
        summary: dict | None = None,
        downstream_artifacts: list[dict] | None = None,
    ) -> dict:
        """Generate HTML + JSON reports.  Returns the report dict."""
        os.makedirs(os.path.join(self.report_dir, objective_id), exist_ok=True)

        tasks = (plan or {}).get("plan_tasks", [])
        interactions = interactions or self.storage.list_interaction_decisions(objective_id)
        spec = spec or {}
        summary = summary or {}
        downstream_artifacts = downstream_artifacts or []

        # Redact any credential-shaped substrings to keep secrets out of reports.
        spec = _redact(spec)
        plan = _redact(plan)
        interactions = _redact(interactions)
        verification_results = _redact(verification_results)
        summary = _redact(summary)
        downstream_artifacts = _redact(downstream_artifacts)

        json_report = self._build_json_report(
            objective_id, spec, plan, final_status, final_branch, final_commit_sha, tasks, interactions, verification_results, summary, downstream_artifacts
        )

        html_report = self._build_html_report(
            objective_id, spec, plan, final_status, final_branch, final_commit_sha, tasks, interactions, verification_results, summary, downstream_artifacts
        )

        html_path = os.path.join(self.report_dir, objective_id, "review-report.html")
        json_path = os.path.join(self.report_dir, objective_id, "result.json")

        with open(html_path, "w") as f:
            f.write(html_report)
        with open(json_path, "w") as f:
            f.write(json.dumps(json_report, indent=2, default=str))

        report = self.storage.create_report(
            objective_id=objective_id,
            status=final_status,
            html_artifact_ref=html_path,
            json_artifact_ref=json_path,
            final_branch=final_branch,
            final_commit_sha=final_commit_sha,
            pr_url=pr_url,
        )

        return report

    def _build_json_report(
        self, objective_id, spec, plan, status, branch, commit, tasks, interactions, verification, summary,
        downstream_artifacts=None,
    ) -> dict:
        result = {
            "objective_id": objective_id,
            "spec": spec,
            "plan": plan,
            "status": status,
            "final_branch": branch,
            "final_commit_sha": commit,
            "task_graph": [
                {
                    "node_id": t.get("node_key", ""),
                    "title": t.get("title", ""),
                    "status": t.get("status", ""),
                    "branch": t.get("branch"),
                    "commit_sha": t.get("commit_sha"),
                    "harness_profile": t.get("harness_profile", ""),
                }
                for t in tasks
            ],
            "interactions": interactions,
            "verification": verification or [],
            "downstream_artifacts": downstream_artifacts or [],
        }
        if summary:
            result["summary"] = summary.get("summary", "")
            result["assumptions"] = summary.get("assumptions", [])
            result["blockers"] = summary.get("blockers", [])
        return result

    def _build_html_report(
        self, objective_id, spec, plan, status, branch, commit, tasks, interactions, verification, summary,
        downstream_artifacts=None,
    ) -> str:
        ns = spec.get("normalized_spec", {})
        title = spec.get("title", "")
        goal = ns.get("goal", "") if isinstance(ns, dict) else ""
        summary_text = (summary or {}).get("summary", "")
        assumptions = (summary or {}).get("assumptions", [])
        blockers = (summary or {}).get("blockers", [])
        downstream_artifacts = downstream_artifacts or []

        task_rows = ""
        for t in tasks:
            task_rows += f"""
            <tr>
              <td>{t.get('node_key', '')}</td>
              <td>{t.get('harness_profile', '')}</td>
              <td>{t.get('status', '')}</td>
              <td>{t.get('branch', '') or '—'}</td>
              <td>{(t.get('commit_sha') or '')[:12]}</td>
            </tr>"""

        interaction_rows = ""
        for i in interactions:
            interaction_rows += f"""
            <tr>
              <td>{i.get('action', '')}</td>
              <td>{i.get('decision_summary', '')}</td>
              <td>{i.get('reply', '')[:200]}</td>
            </tr>"""

        verification_rows = ""
        for v in (verification or []):
            verification_rows += f"""
            <tr>
              <td>{v.get('name', '')}</td>
              <td>{v.get('status', '')}</td>
              <td>{v.get('passed', '')}</td>
            </tr>"""

        artifact_rows = ""
        for da in downstream_artifacts:
            node_key = da.get("node_key", "")
            gw_task_id = da.get("gw_task_id", "")
            for art in da.get("artifacts", []):
                artifact_rows += f"""
                <tr>
                  <td>{node_key}</td>
                  <td>{gw_task_id}</td>
                  <td>{art.get('name', '')}</td>
                  <td>{art.get('artifact_type', '')}</td>
                  <td>{art.get('size_bytes', 0)}</td>
                </tr>"""

        assumption_rows = ""
        for a in assumptions:
            assumption_rows += f"<li>{a}</li>"
        blocker_rows = ""
        for b in blockers:
            blocker_rows += f"<li>{b}</li>"

        artifacts_section = (
            """
  <h2>Downstream Agents Gateway Artifacts</h2>
  <table>
    <tr><th>Node</th><th>GW Task ID</th><th>Artifact</th><th>Type</th><th>Size (bytes)</th></tr>
    """ + artifact_rows + """
  </table>"""
            if artifact_rows
            else """
  <h2>Downstream Agents Gateway Artifacts</h2>
  <p>No downstream artifacts collected.</p>"""
        )

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Composer Review Report — {title}</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; margin: 2em; max-width: 1200px; }}
    h1 {{ color: #333; }}
    h2 {{ color: #555; border-bottom: 1px solid #ddd; padding-bottom: .3em; margin-top: 2em; }}
    table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
    th, td {{ border: 1px solid #ddd; padding: .5em .8em; text-align: left; }}
    th {{ background: #f5f5f5; font-weight: 600; }}
    .status-completed {{ color: green; font-weight: bold; }}
    .status-blocked {{ color: #d35400; font-weight: bold; }}
    .status-failed {{ color: red; font-weight: bold; }}
    .meta {{ color: #777; font-size: .9em; }}
    .goal {{ background: #f9f9f9; padding: 1em; border-left: 3px solid #ccc; }}
    code {{ background: #f4f4f4; padding: .1em .3em; border-radius: 3px; }}
  </style>
</head>
<body>
  <h1>Composer Review Report</h1>
  <p class="meta">Objective: <code>{objective_id}</code></p>
  <p class="meta">Status: <span class="status-{status}">{status}</span></p>
  <p class="meta">Branch: <code>{branch}</code></p>
  <p class="meta">Commit: <code>{commit}</code></p>

  <h2>Executive Summary</h2>
  <div class="goal">{summary_text}</div>

  <h2>Specification</h2>
  <p><strong>{title}</strong></p>
  <div class="goal">{goal}</div>

  <h2>Task Graph</h2>
  <table>
    <tr><th>Node</th><th>Harness Profile</th><th>Status</th><th>Branch</th><th>Commit</th></tr>
    {task_rows}
  </table>

  <h2>Interactions & Assumptions</h2>
  <table>
    <tr><th>Action</th><th>Summary</th><th>Reply</th></tr>
    {interaction_rows}
  </table>

  <h2>Assumptions</h2>
  <ul>{assumption_rows}</ul>

  <h2>Blockers</h2>
  <ul>{blocker_rows}</ul>

  <h2>Verification Matrix</h2>
  <table>
    <tr><th>Name</th><th>Status</th><th>Passed</th></tr>
    {verification_rows}
  </table>
{artifacts_section}

  <h2>Final Result</h2>
  <p>Status: <span class="status-{status}">{status}</span></p>
  <p>Final branch: <code>{branch}</code></p>
  <p>Final commit: <code>{commit}</code></p>
</body>
</html>
"""
