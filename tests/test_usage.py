"""Usage ingestion + summary endpoints.

The load-bearing property is idempotence: message_id is the primary key and
ingest is INSERT OR IGNORE, which is what makes Stop-hook retries, backfill
re-runs, and resumed sessions (which copy prior history under a new session_id)
safe. Most of these tests exist to pin that down.
"""

import sqlite3

import pytest

from server.db import get_db

pytestmark = pytest.mark.asyncio


def _msg(message_id: str, **over):
    base = {
        "message_id": message_id,
        "ts": "2026-08-09T14:44:02.651Z",
        "model": "claude-opus-5",
        "cwd": "/home/giosue/projects/hydra",
        "effort": "xhigh",
        "input_tokens": 100,
        "output_tokens": 1000,
        "cache_read_tokens": 20000,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 4000,
    }
    base.update(over)
    return base


async def _post(client, session_id, messages, instance="pi"):
    return await client.post(
        "/api/usage/messages",
        json={"session_id": session_id, "messages": messages},
        headers={"X-Instance-Id": instance},
    )


def _codex_msg(message_id: str, session_id: str = "codex-root", **over):
    message = _msg(
        message_id,
        harness="codex-cli",
        model="gpt-5.6-sol",
        cache_write_1h_tokens=0,
    )
    message["session_id"] = session_id
    message.update(over)
    return message


async def _reconcile(client, messages, *, apply=False, instance="pi"):
    return await client.post(
        "/api/usage/reconcile/codex",
        json={"apply": apply, "messages": messages},
        headers={"X-Instance-Id": instance},
    )


async def _usage_row(message_id: str):
    db = await get_db()
    rows = list(await db.execute_fetchall(
        "SELECT * FROM usage_messages WHERE message_id = ?", (message_id,)
    ))
    return dict(rows[0]) if rows else None


async def test_codex_reconcile_preview_does_not_mutate(client):
    response = await _reconcile(client, [_codex_msg("codex:one")])

    assert response.status_code == 200
    assert response.json() == {
        "inserted": 1,
        "updated": 0,
        "unchanged": 0,
        "foreign_unchanged": 0,
        "conflicts": 0,
        "conflict_ids": [],
        "applied": False,
    }
    assert await _usage_row("codex:one") is None


async def test_codex_reconcile_apply_insert_and_idempotence(client):
    message = _codex_msg("codex:one", session_id="source-root")

    applied = await _reconcile(client, [message], apply=True)
    replayed = await _reconcile(client, [message], apply=True)
    row = await _usage_row("codex:one")

    assert applied.json()["inserted"] == 1
    assert applied.json()["applied"] is True
    assert replayed.json()["unchanged"] == 1
    assert row is not None
    assert row["session_id"] == "source-root"
    assert row["instance_id"] == "pi"
    assert row["harness"] == "codex-cli"


async def test_codex_reconcile_updates_exact_fields_and_preserves_provenance(client):
    await _post(
        client,
        "stored-session",
        [_msg("codex:one", harness="codex-cli", model="old", service_tier="priority")],
        instance="pi",
    )
    before = await _usage_row("codex:one")
    message = _codex_msg(
        "codex:one",
        session_id="new-source-session",
        ts="2026-09-01T00:00:00Z",
        cwd="/correct",
        model="gpt-6-astra",
        effort="high",
        is_subagent=True,
        agent_type="spawn",
        service_tier=None,
        speed="fast",
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=3,
        cache_write_5m_tokens=4,
        cache_write_1h_tokens=5,
        web_search_requests=6,
        web_fetch_requests=7,
    )

    response = await _reconcile(client, [message], apply=True)
    after = await _usage_row("codex:one")

    assert response.json()["updated"] == 1
    assert after is not None and before is not None
    for field in (
        "ts", "cwd", "model", "effort", "is_subagent", "agent_type", "speed",
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_5m_tokens", "cache_write_1h_tokens",
        "web_search_requests", "web_fetch_requests",
    ):
        expected = int(message[field]) if field == "is_subagent" else message[field]
        assert after[field] == expected
    assert after["service_tier"] == "priority"
    assert after["session_id"] == before["session_id"] == "stored-session"
    assert after["instance_id"] == before["instance_id"] == "pi"
    assert after["received_at"] == before["received_at"]


async def test_codex_reconcile_non_null_replayed_tier_is_canonical(client):
    await _post(
        client,
        "stored",
        [_msg("codex:tier", harness="codex-cli", model="gpt-6-astra")],
        instance="pi",
    )

    response = await _reconcile(
        client,
        [_codex_msg("codex:tier", service_tier="priority", model="gpt-6-astra")],
        apply=True,
    )

    assert response.json()["updated"] == 1
    row = await _usage_row("codex:tier")
    assert row is not None
    assert row["service_tier"] == "priority"


async def test_codex_reconcile_foreign_identical_is_unchanged(client):
    message = _codex_msg("codex:foreign")
    await _reconcile(client, [message], apply=True, instance="other")
    before = await _usage_row("codex:foreign")

    response = await _reconcile(client, [message], apply=True, instance="pi")

    assert response.json()["foreign_unchanged"] == 1
    assert response.json()["applied"] is True
    assert await _usage_row("codex:foreign") == before


async def test_codex_reconcile_foreign_difference_is_conflict(client):
    message = _codex_msg("codex:foreign")
    await _reconcile(client, [message], apply=True, instance="other")

    response = await _reconcile(
        client,
        [{**message, "input_tokens": 999}],
        apply=True,
        instance="pi",
    )

    assert response.json()["conflicts"] == 1
    assert response.json()["applied"] is False
    row = await _usage_row("codex:foreign")
    assert row is not None
    assert row["input_tokens"] == message["input_tokens"]


async def test_codex_reconcile_cross_harness_is_conflict(client):
    await _post(client, "claude", [_msg("shared-id")], instance="pi")

    response = await _reconcile(client, [_codex_msg("shared-id")], apply=True)

    assert response.json()["conflicts"] == 1
    assert response.json()["applied"] is False
    row = await _usage_row("shared-id")
    assert row is not None
    assert row["harness"] == "claude-code"


async def test_codex_reconcile_rejects_duplicate_ids_and_invalid_scope(client):
    message = _codex_msg("codex:duplicate")
    duplicate = await _reconcile(client, [message, message])
    too_many = await _reconcile(
        client, [_codex_msg(f"codex:{index}") for index in range(501)]
    )
    wrong_harness = await _reconcile(
        client, [{**message, "harness": "claude-code"}]
    )
    missing_instance = await client.post(
        "/api/usage/reconcile/codex", json={"messages": [message]}
    )

    assert duplicate.status_code == 422
    assert too_many.status_code == 422
    assert wrong_harness.status_code == 422
    assert missing_instance.status_code == 422


async def test_codex_reconcile_apply_rolls_back_whole_chunk(client):
    original_one = _codex_msg("codex:one", input_tokens=1)
    original_two = _codex_msg("codex:two", input_tokens=2)
    await _reconcile(client, [original_one, original_two], apply=True)
    db = await get_db()
    await db.execute(
        "CREATE TRIGGER fail_second_codex_update BEFORE UPDATE ON usage_messages "
        "WHEN OLD.message_id = 'codex:two' BEGIN SELECT RAISE(ABORT, 'stop'); END"
    )
    await db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        await _reconcile(
            client,
            [
                {**original_one, "input_tokens": 10},
                {**original_two, "input_tokens": 20},
            ],
            apply=True,
        )

    row_one = await _usage_row("codex:one")
    row_two = await _usage_row("codex:two")
    assert row_one is not None and row_two is not None
    assert row_one["input_tokens"] == 1
    assert row_two["input_tokens"] == 2


async def test_codex_reconcile_rolls_back_insert_when_update_fails(client):
    original = _codex_msg("codex:stored", input_tokens=1)
    await _reconcile(client, [original], apply=True)
    db = await get_db()
    await db.execute(
        "CREATE TRIGGER fail_codex_update BEFORE UPDATE ON usage_messages "
        "WHEN OLD.message_id = 'codex:stored' BEGIN SELECT RAISE(ABORT, 'stop'); END"
    )
    await db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        await _reconcile(
            client,
            [
                _codex_msg("codex:new", input_tokens=2),
                {**original, "input_tokens": 3},
            ],
            apply=True,
        )

    stored = await _usage_row("codex:stored")
    assert stored is not None
    assert stored["input_tokens"] == 1
    assert await _usage_row("codex:new") is None


async def test_codex_reconcile_route_has_usage_body_allowance(client):
    response = await client.post(
        "/api/usage/reconcile/codex",
        content=b'{"messages":[],"padding":"' + b"x" * (300 * 1024) + b'"}',
        headers={"X-Instance-Id": "pi", "Content-Type": "application/json"},
    )

    assert response.status_code == 200


async def test_ingest_and_summary(client):
    res = await _post(client, "s1", [_msg("m1"), _msg("m2")])
    assert res.status_code == 200
    assert res.json() == {"inserted": 2, "ignored": 0}

    res = await client.get("/api/usage/summary?group_by=day")
    body = res.json()
    assert [r["key"] for r in body["rows"]] == ["2026-08-09"]
    assert body["totals"]["messages"] == 2
    assert body["totals"]["output_tokens"] == 2000
    assert body["unpriced_models"] == []


async def test_replaying_a_batch_inserts_nothing(client):
    await _post(client, "s1", [_msg("m1"), _msg("m2")])
    res = await _post(client, "s1", [_msg("m1"), _msg("m2")])
    assert res.json() == {"inserted": 0, "ignored": 2}

    body = (await client.get("/api/usage/summary?group_by=day")).json()
    assert body["totals"]["messages"] == 2


async def test_same_message_under_a_second_session_is_ignored(client):
    """A resume or fork copies prior history into a new transcript file, so the
    same message.id legitimately arrives again under a different session_id.
    It must not be counted twice."""
    await _post(client, "s1", [_msg("m1")])
    res = await _post(client, "s2-resumed", [_msg("m1"), _msg("m2")])
    assert res.json() == {"inserted": 1, "ignored": 1}

    body = (await client.get("/api/usage/summary?group_by=day")).json()
    assert body["totals"]["messages"] == 2


async def test_row_inserts_without_a_matching_session(client):
    """usage_messages.session_id carries no FK on purpose: backfill imports
    transcripts for sessions the server has never seen."""
    res = await _post(client, "never-reported", [_msg("m1")])
    assert res.json()["inserted"] == 1


async def test_empty_batch(client):
    res = await _post(client, "s1", [])
    assert res.json() == {"inserted": 0, "ignored": 0}


async def test_known_unpriced_alias_is_noted_not_flagged(client):
    await _post(client, "s1", [_msg("m1", model="codex-auto-review")])
    body = (await client.get("/api/usage/summary?group_by=model")).json()

    assert body["unpriced_models"] == []
    assert body["known_unpriced_models"] == ["codex-auto-review"]
    assert body["totals"]["unpriced_messages"] == 0
    assert body["rows"][0]["unpriced_messages"] == 0
    assert body["totals"]["cost_usd"] == 0.0
    assert body["totals"]["output_tokens"] == 1000


async def test_unknown_model_is_unpriced_not_free(client):
    await _post(client, "s1", [_msg("m1", model="claude-from-the-future-9")])
    body = (await client.get("/api/usage/summary?group_by=model")).json()

    assert body["unpriced_models"] == ["claude-from-the-future-9"]
    assert body["totals"]["unpriced_messages"] == 1
    assert body["totals"]["cost_usd"] == 0.0
    # The tokens are still counted - only the money is unknown.
    assert body["totals"]["output_tokens"] == 1000


async def test_cost_is_priced_per_model_then_summed(client):
    await _post(client, "s1", [
        _msg("m1", model="claude-opus-5", input_tokens=1_000_000,
             output_tokens=0, cache_read_tokens=0, cache_write_1h_tokens=0),
        _msg("m2", model="claude-haiku-4-5-20251001", input_tokens=1_000_000,
             output_tokens=0, cache_read_tokens=0, cache_write_1h_tokens=0),
    ])
    body = (await client.get("/api/usage/summary?group_by=day")).json()
    # $5/MTok for opus-5 + $1/MTok for the dated haiku id = $6
    assert body["totals"]["cost_usd"] == pytest.approx(6.0)


async def test_cost_components_split_by_token_kind(client):
    """Token volume and cost have different shapes - cache reads dominate one and
    not the other - so the split is computed server-side from the same rates."""
    await _post(client, "s1", [_msg(
        "m1", model="claude-opus-5",
        input_tokens=0, output_tokens=1_000_000,
        cache_read_tokens=10_000_000,        # 10M * $5 * 0.1 = $5
        cache_write_5m_tokens=0, cache_write_1h_tokens=1_000_000,  # 1M * $5 * 2 = $10
    )])
    body = (await client.get("/api/usage/summary?group_by=day")).json()
    parts = body["totals"]["cost_components"]

    assert parts["output"] == pytest.approx(25.0)      # 1M * $25
    assert parts["cache_read"] == pytest.approx(5.0)
    assert parts["cache_write_1h"] == pytest.approx(10.0)
    assert parts["cache_write_5m"] == 0.0
    assert sum(parts.values()) == pytest.approx(body["totals"]["cost_usd"])


async def test_unpriced_model_contributes_no_components(client):
    await _post(client, "s1", [_msg("m1", model="claude-from-the-future-9")])
    body = (await client.get("/api/usage/summary?group_by=day")).json()
    assert body["totals"]["cost_components"] == {
        "input": 0.0, "output": 0.0, "cache_read": 0.0,
        "cache_write_5m": 0.0, "cache_write_1h": 0.0, "web_search": 0.0,
    }
    assert body["totals"]["unpriced_messages"] == 1


async def test_group_by_project_resolves_registered_paths(client):
    await client.post(
        "/api/projects",
        json={"slug": "hydra", "path": "/home/giosue/projects/hydra"},
        headers={"X-Instance-Id": "pi"},
    )
    await _post(client, "s1", [
        _msg("m1", cwd="/home/giosue/projects/hydra"),
        _msg("m2", cwd="/somewhere/unregistered"),
    ])

    body = (await client.get("/api/usage/summary?group_by=project")).json()
    keys = {r["key"] for r in body["rows"]}
    assert keys == {"hydra", "unregistered"}


async def test_project_registered_on_two_machines_does_not_double_count(client):
    """project_paths is keyed (slug, instance_id), so two machines sharing an
    absolute path would fan the join out if it weren't de-duplicated first."""
    for instance in ("pi", "laptop"):
        await client.post(
            "/api/projects",
            json={"slug": "hydra", "path": "/home/giosue/projects/hydra"},
            headers={"X-Instance-Id": instance},
        )
    await _post(client, "s1", [_msg("m1", cwd="/home/giosue/projects/hydra")])

    body = (await client.get("/api/usage/summary?group_by=project")).json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["messages"] == 1


async def test_project_group_uses_confirmed_ancestor(client):
    await client.post(
        "/api/projects",
        json={"slug": "hydra", "path": "/work/demo"},
        headers={"X-Instance-Id": "pi"},
    )
    await _post(client, "s1", [_msg("m1", cwd="/work/demo/server")])

    body = (await client.get("/api/usage/summary?group_by=project")).json()
    assert [(row["key"], row["messages"]) for row in body["rows"]] == [("hydra", 1)]


async def test_project_group_uses_longest_confirmed_ancestor(client):
    for slug, path in (
        ("outer", "/work/repo"),
        ("inner", "/work/repo/packages/app"),
    ):
        await client.post(
            "/api/projects",
            json={"slug": slug, "path": path},
            headers={"X-Instance-Id": slug},
        )
    await _post(client, "s1", [_msg("m1", cwd="/work/repo/packages/app/src")])

    body = (await client.get("/api/usage/summary?group_by=project")).json()
    assert [row["key"] for row in body["rows"]] == ["inner"]


async def test_project_group_exact_match_beats_confirmed_ancestor(client):
    auto = await client.post(
        "/api/projects/auto-register",
        json={"cwd": "/work/repo/sub"},
        headers={"X-Instance-Id": "auto"},
    )
    assert auto.json()["status"] == "created"
    await client.post(
        "/api/projects",
        json={"slug": "repo", "path": "/work/repo"},
        headers={"X-Instance-Id": "manual"},
    )
    await _post(client, "s1", [_msg("m1", cwd="/work/repo/sub")])

    body = (await client.get("/api/usage/summary?group_by=project")).json()
    assert [row["key"] for row in body["rows"]] == ["sub"]


async def test_unconfirmed_path_does_not_capture_usage(client):
    auto = await client.post(
        "/api/projects/auto-register",
        json={"cwd": "/work/junk"},
        headers={"X-Instance-Id": "auto"},
    )
    assert auto.json()["status"] == "created"
    await client.post(
        "/api/projects",
        json={"slug": "work", "path": "/work"},
        headers={"X-Instance-Id": "manual"},
    )
    await _post(client, "s1", [_msg("m1", cwd="/work/junk/src")])

    body = (await client.get("/api/usage/summary?group_by=project")).json()
    assert [row["key"] for row in body["rows"]] == ["work"]


async def test_project_prefix_does_not_treat_underscore_as_wildcard(client):
    await client.post(
        "/api/projects",
        json={"slug": "proj_a", "path": "/home/u/proj_a"},
        headers={"X-Instance-Id": "pi"},
    )
    await _post(client, "s1", [_msg("m1", cwd="/home/u/projXa/src")])

    body = (await client.get("/api/usage/summary?group_by=project")).json()
    assert [row["key"] for row in body["rows"]] == ["unregistered"]


async def test_unrelated_usage_stays_unregistered(client):
    await client.post(
        "/api/projects",
        json={"slug": "hydra", "path": "/work/hydra"},
        headers={"X-Instance-Id": "pi"},
    )
    await _post(client, "s1", [_msg("m1", cwd="/elsewhere/repo")])

    body = (await client.get("/api/usage/summary?group_by=project")).json()
    assert [row["key"] for row in body["rows"]] == ["unregistered"]


async def test_duplicate_prefixes_do_not_fan_out_usage(client):
    for slug, instance in (("alpha", "a"), ("beta", "b")):
        await client.post(
            "/api/projects",
            json={"slug": slug, "path": "/work/repo"},
            headers={"X-Instance-Id": instance},
        )
    await _post(client, "s1", [_msg("m1", cwd="/work/repo/src")])

    body = (await client.get("/api/usage/summary?group_by=project")).json()
    assert [(row["key"], row["messages"]) for row in body["rows"]] == [("alpha", 1)]
    assert body["totals"]["messages"] == 1


async def test_group_by_agent_and_instance(client):
    await _post(client, "s1", [_msg("m1")], instance="pi")
    await _post(client, "s2", [
        _msg("m2", is_subagent=True, agent_type="Explore"),
    ], instance="laptop")

    agents = (await client.get("/api/usage/summary?group_by=agent")).json()
    assert {r["key"] for r in agents["rows"]} == {"main", "Explore"}

    machines = (await client.get("/api/usage/summary?group_by=instance")).json()
    assert {r["key"] for r in machines["rows"]} == {"pi", "laptop"}


async def test_harness_defaults_groups_and_filters(client):
    await _post(client, "s1", [_msg("m1")], instance="pi")
    await _post(
        client,
        "s2",
        [_msg("m2", harness="codex-cli", model="gpt-5.6-sol")],
        instance="laptop",
    )

    harnesses = (await client.get("/api/usage/summary?group_by=harness")).json()
    assert {r["key"] for r in harnesses["rows"]} == {"claude-code", "codex-cli"}

    codex = (
        await client.get(
            "/api/usage/summary?group_by=instance&harness=codex-cli"
        )
    ).json()
    assert [(row["key"], row["messages"]) for row in codex["rows"]] == [
        ("laptop", 1)
    ]
    assert codex["harness"] == "codex-cli"

    claude = (
        await client.get(
            "/api/usage/summary?group_by=harness&instance=pi"
        )
    ).json()
    assert [(row["key"], row["messages"]) for row in claude["rows"]] == [
        ("claude-code", 1)
    ]


async def test_instance_filter(client):
    await _post(client, "s1", [_msg("m1"), _msg("m2")], instance="pi")
    await _post(client, "s2", [_msg("m3")], instance="laptop")

    both = (await client.get("/api/usage/summary?group_by=day")).json()
    assert both["totals"]["messages"] == 3
    assert both["instance"] is None

    pi = (await client.get("/api/usage/summary?group_by=day&instance=pi")).json()
    assert pi["totals"]["messages"] == 2
    assert pi["instance"] == "pi"

    # The filter composes with the time window rather than replacing it.
    scoped = await client.get(
        "/api/usage/summary?group_by=day&instance=laptop&since=2026-08-09"
    )
    assert scoped.json()["totals"]["messages"] == 1

    missing = (await client.get("/api/usage/summary?group_by=day&instance=nope")).json()
    assert missing["totals"]["messages"] == 0
    assert missing["rows"] == []


async def test_since_and_until_filter(client):
    await _post(client, "s1", [
        _msg("old", ts="2026-07-01T10:00:00.000Z"),
        _msg("new", ts="2026-08-09T10:00:00.000Z"),
    ])

    body = (await client.get("/api/usage/summary?group_by=day&since=2026-08-01")).json()
    assert body["totals"]["messages"] == 1
    assert [r["key"] for r in body["rows"]] == ["2026-08-09"]

    body = (await client.get("/api/usage/summary?group_by=day&until=2026-08-01")).json()
    assert body["totals"]["messages"] == 1
    assert [r["key"] for r in body["rows"]] == ["2026-07-01"]


async def test_days_sort_newest_first(client):
    await _post(client, "s1", [
        _msg("a", ts="2026-08-01T10:00:00.000Z"),
        _msg("b", ts="2026-08-09T10:00:00.000Z"),
        _msg("c", ts="2026-08-05T10:00:00.000Z"),
    ])
    body = (await client.get("/api/usage/summary?group_by=day")).json()
    assert [r["key"] for r in body["rows"]] == ["2026-08-09", "2026-08-05", "2026-08-01"]


async def test_fast_mode_prices_at_double_and_splits_the_group(client):
    await _post(client, "s1", [
        _msg("m1", model="gpt-6-astra", service_tier="default",
             input_tokens=1_000_000, output_tokens=0,
             cache_read_tokens=0, cache_write_1h_tokens=0),
        _msg("m2", model="gpt-6-astra", service_tier="priority",
             input_tokens=1_000_000, output_tokens=0,
             cache_read_tokens=0, cache_write_1h_tokens=0),
    ])

    body = (await client.get("/api/usage/summary?group_by=model")).json()
    row = next(r for r in body["rows"] if r["key"] == "gpt-6-astra")

    # $10 at base + $20 at fast mode. Summing the counters first would have
    # priced all 2M tokens at one rate and lost the difference.
    assert row["cost_usd"] == pytest.approx(30.0)


async def test_a_later_sweep_backfills_a_tier_it_did_not_know_before(client):
    await _post(client, "s1", [
        _msg("m1", model="gpt-6-astra", input_tokens=1_000_000, output_tokens=0,
             cache_read_tokens=0, cache_write_1h_tokens=0),
    ])
    before = (await client.get("/api/usage/summary")).json()["totals"]["cost_usd"]

    res = await _post(client, "s1", [
        _msg("m1", model="gpt-6-astra", service_tier="priority",
             input_tokens=1_000_000, output_tokens=0,
             cache_read_tokens=0, cache_write_1h_tokens=0),
    ])
    after = (await client.get("/api/usage/summary")).json()["totals"]["cost_usd"]

    assert res.json()["inserted"] == 0
    assert before == pytest.approx(10.0)
    assert after == pytest.approx(20.0)


async def test_backfill_never_revises_a_tier_already_recorded(client):
    await _post(client, "s1", [
        _msg("m1", model="gpt-6-astra", service_tier="priority",
             input_tokens=1_000_000, output_tokens=0,
             cache_read_tokens=0, cache_write_1h_tokens=0),
    ])
    await _post(client, "s1", [
        _msg("m1", model="gpt-6-astra", service_tier="default",
             input_tokens=1_000_000, output_tokens=0,
             cache_read_tokens=0, cache_write_1h_tokens=0),
    ])

    body = (await client.get("/api/usage/summary")).json()
    assert body["totals"]["cost_usd"] == pytest.approx(20.0)


async def test_backfill_leaves_token_counts_untouched(client):
    await _post(client, "s1", [
        _msg("m1", model="gpt-6-astra", input_tokens=1_000_000, output_tokens=0,
             cache_read_tokens=0, cache_write_1h_tokens=0),
    ])
    await _post(client, "s1", [
        _msg("m1", model="gpt-6-astra", service_tier="priority",
             input_tokens=999, output_tokens=999,
             cache_read_tokens=999, cache_write_1h_tokens=999),
    ])

    body = (await client.get("/api/usage/summary")).json()
    assert body["totals"]["input_tokens"] == 1_000_000
    assert body["totals"]["output_tokens"] == 0
