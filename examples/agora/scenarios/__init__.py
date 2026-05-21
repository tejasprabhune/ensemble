"""Importing this package registers all three Agora scenarios with the
global scenario registry in `ensemble.scenario`."""

from . import enterprise_audit, refund_storm, single_ticket  # noqa: F401
