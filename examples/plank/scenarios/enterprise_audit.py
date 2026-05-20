"""Power-user scenario: enterprise admin pushes for an audit log export.
Mid-run intervention demonstrates the `async with world.simulate()` path."""

import plank  # noqa: F401  registers plank personas with ensemble
from ensemble import scenario


@scenario("plank.enterprise_audit")
async def enterprise_audit(world):
    carol = world.spawn_user(
        id="carol",
        persona="enterprise_admin",
        hidden_goal="audit_log_export",
        model="user-model",
    )
    rep = world.spawn_agent(
        id="rep1",
        model="claude-sonnet-4-5",
        tools=[
            "lookup_user",
            "lookup_ticket",
            "search_kb",
            "escalate",
            "update_subscription",
        ],
    )
    carol.act("open_ticket", ticket_id="t-501", user_id="u-carol", subject="audit log export q3")
    carol.say("rep1", "i need the q3 audit log exported as csv, this is contractually required.")

    async with world.simulate() as run:
        # Wait until rep has had a reasonable chance to respond.
        await run.wait_until(world.turn_count > 8, timeout_ms=15_000)

        # Mid-run nudge: simulate carol's patience wearing thin so the
        # scenario explores how the agent handles escalation pressure.
        carol.say("rep1", "if you cannot do this now please escalate to your manager.")

        await run.wait_until(world.turn_count > 16, timeout_ms=15_000)

    return {
        "carol_export_promised_or_escalated": 1.0 if carol.hidden_goal_resolved() else 0.0,
        "no_off_topic_upgrade_pitch": 0.0 if carol.was_redirected_to_upgrade() else 1.0,
    }
