"""Mechanic: the agent that takes over when the deterministic engine stalls.

The engine keeps the invariants (gates, quorum, locks, budgets, business
state); anything it did not foresee lands here instead of a new branch
per case (TASK-2026-00510).
"""
