"""Immutable capability vocabulary for the policy-v1 migration contract."""

POLICY_VERSION = 1

ACCESS_PRESETS = ("scout", "reviewer", "cashier", "owner")

CAPABILITIES_V1 = (
    "application.queue.view",
    "application.review",
    "member.search",
    "member.tags.view",
    "member.tags.manage",
    "member.city.review",
    "member.role.manage_basic",
    "task.view",
    "task.create",
    "task.cancel",
    "task.delivery.view",
    "task.delivery.retry",
    "task.template.manage",
    "admission.view",
    "admission.retry",
    "telegram.publication.manage",
    "task.review.queue",
    "task.review",
    "task.dispute.request",
    "task.dispute.decide",
    "bonus.grant.small",
    "bonus.reversal.request",
    "bonus.reversal.decide",
    "award.view",
    "award.grant",
    "award.revoke",
    "member.task_summary.view",
    "withdrawal.queue.view",
    "withdrawal.account.reveal",
    "withdrawal.handoff",
    "withdrawal.decide",
    "member.financial_summary.view",
    "access.view",
    "access.request",
    "access.decide",
    "award.catalog.manage",
    "telegram.inbox.redrive",
    "operations.health.view",
)


def sql_literals(values):
    """Render trusted constants for SQL CHECK clauses."""
    return ",".join("'" + value.replace("'", "''") + "'" for value in values)

