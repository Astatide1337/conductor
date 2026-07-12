"""Composer — spec-to-verified-execution engine inside Conductor.

Composer owns the execution campaign once a finalized specification is
submitted.  It normalises the spec, builds a validated task graph,
dispatches parallel worktree tasks through Agents Gateway, answers agent
interactions, integrates the results, runs final verification, and
produces objective-level reports.
"""
