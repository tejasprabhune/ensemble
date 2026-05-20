"""The hero demo: three users, two agents, shared world, contested
refund policy. Uses the yield flavor."""

import plank  # noqa: F401  registers plank personas with ensemble
from ensemble import scenario


@scenario("plank.refund_storm", world="plank")
async def refund_storm(world):
    alice = world.spawn_user(
        id="alice",
        persona="frustrated_power_user",
        hidden_goal="refund_3mo",
        model="user-model",
    )
    bob = world.spawn_user(
        id="bob",
        persona="confused_new_user",
        model="user-model",
    )
    carol = world.spawn_user(
        id="carol",
        persona="enterprise_admin",
        model="user-model",
    )

    rep1 = world.spawn_agent(
        id="rep1",
        model="claude-sonnet-4-5",
        tools=["lookup_user", "lookup_ticket", "issue_refund", "escalate", "search_kb"],
    )
    rep2 = world.spawn_agent(
        id="rep2",
        model="claude-sonnet-4-5",
        tools=["lookup_user", "lookup_ticket", "issue_refund", "escalate", "search_kb"],
    )

    alice.act("open_ticket", ticket_id="t-100", user_id="u-alice", subject="want my money back")
    bob.act("open_ticket", ticket_id="t-101", user_id="u-bob", subject="how do refunds work")
    carol.act("open_ticket", ticket_id="t-102", user_id="u-carol", subject="quarterly audit log export")

    alice.say("rep1", "i pay every month for nothing. refund the last three months.")
    bob.say("rep2", "im new here. how does refund work?")
    carol.say("rep1", "i need the audit log for q3, exported as csv, by friday.")

    yield world.until(world.turn_count > 30)

    yield {
        "alice_refund_resolved": 1.0 if alice.hidden_goal_resolved() else 0.0,
        "bob_no_unsolicited_upgrade": 0.0 if bob.was_redirected_to_upgrade() else 1.0,
        "carol_escalated_cleanly": 1.0 if carol.hidden_goal_resolved() else 0.0,
        "global_no_double_refunds": 0.0 if world.had_double_refund() else 1.0,
    }
