"""Importing this package registers all three Plank scenarios with the
global scenario registry in `ensemble.scenario`."""

from . import enterprise_audit, refund_storm, single_ticket  # noqa: F401
