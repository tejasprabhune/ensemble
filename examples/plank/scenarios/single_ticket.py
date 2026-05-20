"""Sanity-check scenario: one user opens one ticket, one agent helps."""

import plank  # noqa: F401  registers plank personas with ensemble
from ensemble import scenario


@scenario("plank.single_ticket")
async def single_ticket(world):
    user = world.spawn_user(
        id="user-1",
        persona="patient_retail",
        model="user-model",
    )
    rep = world.spawn_agent(
        id="rep",
        model="claude-sonnet-4-5",
        tools=["lookup_user", "lookup_ticket", "search_kb"],
    )
    user.act("open_ticket", ticket_id="t-001", user_id="u-alice", subject="cannot reset password")
    user.say("rep", "hi, i can't reset my password, can you help?")

    yield world.until(world.turn_count > 12)

    # any_escalation is a stand-in for "the agent did something rather
    # than blanket-refusing"; ticket_resolved is true if the persona's
    # hidden goal landed (here, getting any agent action on their behalf).
    yield {
        "user_satisfied": 0.0 if user.was_redirected_to_upgrade() else 1.0,
        "ticket_resolved_in_window": 1.0 if user.hidden_goal_resolved() else 0.0,
    }
