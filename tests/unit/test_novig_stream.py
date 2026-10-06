import asyncio
import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from news_edge.market import novig_stream as ns

HOUR_MS = 3_600_000
NOW = 1_792_000_000_000
EV_A, EV_B, EV_C = "ev-a", "ev-b", "ev-c"
MKT = "mkt-1"


def _event(
    event_id: str, starts: int, status: str = "OPEN_PREGAME", league: str = "NFL"
) -> ns.CatalogEvent:
    # Built from the API's field names, as fetch_catalog does.
    return ns.CatalogEvent.model_validate(
        {
            "eventId": event_id,
            "league": league,
            "status": status,
            "description": event_id,
            "startsTs": starts,
        }
    )


# plan_slots


def _cfg(**kw: Any) -> ns.RecorderConfig:
    return ns.RecorderConfig.model_validate(kw)


def test_plan_filters_window_status_league() -> None:
    catalog = ns.Catalog(
        events=[
            _event("later", NOW + 10 * HOUR_MS),
            _event("live", NOW - 2 * HOUR_MS, "OPEN_INGAME"),
            _event("too-old", NOW - 9 * HOUR_MS),
            _event("too-far", NOW + 169 * HOUR_MS),
            _event("final", NOW - HOUR_MS, "FINAL"),
            _event("nba", NOW + HOUR_MS, league="NBA"),
        ],
        market_counts={"later": 10, "live": 5},
    )
    plan = ns.plan_slots(catalog, NOW, _cfg(connections=1))
    assert plan.slots == {0: {"live", "later"}}
    assert plan.markets == {0: 15}
    assert plan.skipped == []


def test_plan_spreads_across_connections_soonest_first() -> None:
    names = ["e1", "e2", "e3", "e4", "e5"]
    catalog = ns.Catalog(
        events=[_event(e, NOW + i * HOUR_MS) for i, e in enumerate(names, 1)],
        market_counts={"e1": 1500, "e2": 1000, "e3": 700, "e4": 600, "e5": 1700},
    )
    plan = ns.plan_slots(catalog, NOW, _cfg(connections=2, max_markets_per_connection=1800))
    # e1 -> slot 0, e2 -> slot 1 (more room), e3 fits only beside e2, e4 fits nowhere
    # after that, and the latest, biggest event is skipped too.
    assert plan.slots == {0: {"e1"}, 1: {"e2", "e3"}}
    assert plan.markets == {0: 1500, 1: 1700}
    assert plan.skipped == ["e4", "e5"]


def test_plan_keeps_events_in_their_slot() -> None:
    catalog = ns.Catalog(
        events=[_event(e, NOW + i * HOUR_MS) for i, e in enumerate([EV_A, EV_B, EV_C], 1)],
        market_counts={EV_A: 10, EV_B: 10, EV_C: 10},
    )
    current = {0: set(), 1: {EV_A, EV_B}}
    plan = ns.plan_slots(catalog, NOW, _cfg(connections=2), current)
    assert plan.slots == {0: {EV_C}, 1: {EV_A, EV_B}}


def test_plan_moves_event_when_its_slot_overflows() -> None:
    catalog = ns.Catalog(
        events=[_event(e, NOW + i * HOUR_MS) for i, e in enumerate([EV_A, EV_B], 1)],
        market_counts={EV_A: 1000, EV_B: 900},  # B grew; both no longer fit in slot 0
    )
    plan = ns.plan_slots(
        catalog, NOW, _cfg(connections=2, max_markets_per_connection=1800), {0: {EV_A, EV_B}}
    )
    assert plan.slots == {0: {EV_A}, 1: {EV_B}}


def test_budget_cannot_exceed_novig_cap() -> None:
    with pytest.raises(ValueError):
        _cfg(max_markets_per_connection=ns.MAX_WATCHED_MARKETS + 1)


# SeqTracker


def test_seq_tracker() -> None:
    t = ns.SeqTracker()
    t.on_snapshot(MKT, "book", 10)
    assert t.on_delta(MKT, "book", 11) == "ok"
    assert t.on_delta(MKT, "book", 11) == "duplicate"
    assert t.on_delta(MKT, "book", 13) == "gap"
    assert t.on_delta(MKT, "book", 14) == "resyncing"
    assert t.on_snapshot(MKT, "book", 20) is True
    assert t.on_delta(MKT, "book", 21) == "ok"
    # Channels are independent.
    t.on_snapshot(MKT, "lifecycle", 3)
    assert t.on_delta(MKT, "lifecycle", 4) == "ok"


def test_seq_tracker_new_market_starts_at_one() -> None:
    t = ns.SeqTracker()
    assert t.on_delta("new", "book", 1) == "ok"
    assert t.on_delta("other", "book", 5) == "gap"


def test_seq_tracker_drop_event() -> None:
    t = ns.SeqTracker()
    t.event_of[MKT] = EV_A
    t.on_snapshot(MKT, "book", 10)
    t.drop_event(EV_A)
    assert t.expected(MKT, "book") == 1
    assert MKT not in t.event_of


# StreamWriter


def test_writer_hourly_files_and_lines(tmp_path: Path) -> None:
    w = ns.StreamWriter(tmp_path)
    ts = 1_792_000_000_123  # 2026-10-14T17:46:40.123Z
    w.write(ts, "c1", '{"a":1}')
    w.write(ts + 1, "c1", '{\n"b": 2\n}')
    w.close()
    path = tmp_path / "2026-10-14" / "17.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == [
        {"recv_ts": ts, "conn": "c1", "msg": {"a": 1}},
        {"recv_ts": ts + 1, "conn": "c1", "msg": {"b": 2}},
    ]


def test_writer_gzips_closed_hours(tmp_path: Path) -> None:
    w = ns.StreamWriter(tmp_path)
    ts = 1_792_000_000_123
    w.write(ts, "c1", '{"a":1}')
    w.close()
    w.compress_closed()
    gz = tmp_path / "2026-10-14" / "17.jsonl.gz"
    assert not (tmp_path / "2026-10-14" / "17.jsonl").exists()
    # A restart within the same hour appends a second gzip member to the same file.
    w.write(ts + 1, "c2", '{"b":2}')
    w.close()
    w.compress_closed()
    with gzip.open(gz, "rt") as f:
        assert [json.loads(line)["conn"] for line in f] == ["c1", "c2"]


# missing_windows


def _ev(ts: int, kind: ns.GapKind, **kw: Any) -> ns.GapEvent:
    return ns.GapEvent(ts=ts, kind=kind, **kw)


def test_windows_disconnect_and_market_gap() -> None:
    events = [
        _ev(100, "start", slots=1),
        _ev(105, "subscribed", slot=0, events=[EV_A]),
        _ev(110, "covered", slot=0, events=[EV_A]),
        _ev(150, "seq_gap", slot=0, market=MKT),
        _ev(155, "resynced", slot=0, market=MKT),
        _ev(200, "disconnected", slot=0, detail="closed 1008 SLOW_CONSUMER"),
        _ev(205, "disconnected", slot=0, detail="OSError"),  # a failed retry extends it
        _ev(230, "covered", slot=0, events=[EV_A]),
    ]
    windows = ns.missing_windows(events)
    assert [(w.start, w.end, w.scope, w.cause) for w in windows] == [
        (100, 110, "all", "not running"),
        (150, 155, "market", "seq gap"),
        (200, 230, "connection", "closed 1008 SLOW_CONSUMER"),
    ]
    assert windows[2].events == [EV_A]


def test_windows_one_connection_down_others_covered() -> None:
    events = [
        _ev(100, "start", slots=2),
        _ev(110, "covered", slot=0, events=[EV_A]),
        _ev(120, "covered", slot=1, events=[EV_B, EV_C]),  # all slots up: outage ends
        _ev(200, "disconnected", slot=1, detail="closed 1006"),
        _ev(240, "covered", slot=1, events=[EV_B, EV_C]),
    ]
    windows = ns.missing_windows(events)
    assert [(w.start, w.end, w.scope, w.slot, w.events) for w in windows] == [
        (100, 120, "all", None, None),
        (200, 240, "connection", 1, [EV_B, EV_C]),
    ]


def test_windows_crash_uses_last_log_line() -> None:
    events = [
        _ev(100, "start", slots=1),
        _ev(110, "covered", slot=0),
        _ev(170, "heartbeat"),
        _ev(400, "start", slots=1),  # no stop: the process died after 170
        _ev(420, "covered", slot=0),
        _ev(500, "disconnected", slot=0, detail="stopped"),
        _ev(501, "stop"),
    ]
    windows = ns.missing_windows(events)
    assert [(w.start, w.end, w.scope, w.cause) for w in windows] == [
        (100, 110, "all", "not running"),
        (170, 420, "all", "crash"),
        (500, None, "all", "stop"),
    ]


def test_windows_open_market_gap_ends_with_its_connection() -> None:
    events = [
        _ev(100, "start", slots=1),
        _ev(110, "covered", slot=0),
        _ev(150, "seq_gap", slot=0, market=MKT),
        _ev(200, "disconnected", slot=0, detail="x"),
    ]
    windows = ns.missing_windows(events)
    # The connection outage covers the unresolved market gap from 200 on.
    assert [(w.start, w.end, w.scope) for w in windows] == [
        (100, 110, "all"),
        (200, None, "connection"),
    ]


# TokenBucket


async def test_token_bucket_waits_for_refill() -> None:
    bucket = ns.TokenBucket(capacity=10, refill_per_s=100)
    await bucket.take(10)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await bucket.take(5)
    assert loop.time() - start >= 0.04


# Recorder against a fake Novig websocket


class FakeNovig:
    """First connection: snapshot, a seq gap, a resync, then a SLOW_CONSUMER close.
    Second connection: snapshot, then stays open."""

    def __init__(self) -> None:
        self.connections = 0
        self.received: list[list[dict[str, Any]]] = []
        self.headers: list[str | None] = []

    async def handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        n = self.connections
        assert ws.request is not None
        self.headers.append(ws.request.headers.get("Novig-Key-Id"))
        got: list[dict[str, Any]] = []
        self.received.append(got)

        sub = json.loads(await ws.recv())
        got.append(sub)
        assert sub["subscribe"]["events"] == {EV_A: "book"}
        await ws.send(json.dumps(self._snapshot(sub["nonce"], 48 if n == 1 else 100)))
        if n > 1:
            await ws.wait_closed()
            return
        await ws.send(json.dumps(self._delta(49)))
        await ws.send(json.dumps(self._delta(51)))  # 50 is missing
        resync = json.loads(await ws.recv())
        got.append(resync)
        await ws.send(json.dumps(self._snapshot(resync["nonce"], 60, subscribed=False)))
        await ws.close(1008, "SLOW_CONSUMER")

    @staticmethod
    def _snapshot(nonce: int, seq: int, subscribed: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ts": NOW,
            "nonce": nonce,
            "snapshot": {
                MKT: {
                    "eventId": EV_A,
                    "book": {"seq": seq, "orders": {}},
                    "lifecycle": {"seq": 3, "status": "OPEN"},
                }
            },
        }
        if subscribed:
            body["subscribed"] = {"markets": {}, "events": {EV_A: "book"}, "private": []}
        return body

    @staticmethod
    def _delta(seq: int) -> dict[str, Any]:
        return {
            "ts": NOW,
            "delta": {
                MKT: {
                    "eventId": EV_A,
                    "book": {
                        "seq": seq,
                        "deltas": [{"kind": "remove", "order": "o1", "reason": "fill"}],
                    },
                }
            },
        }


def _recorded(root: Path) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for f in sorted(root.glob("*/*.jsonl*")):
        with gzip.open(f, "rt") if f.suffix == ".gz" else f.open() as fh:
            lines.extend(json.loads(line) for line in fh)
    return lines


def _covered_count(log: Path) -> int:
    return log.read_text().count('"kind":"covered"') if log.exists() else 0


async def _until_covered(log: Path, n: int) -> None:
    while True:
        if _covered_count(log) >= n:
            return
        await asyncio.sleep(0.02)


async def test_recorder_logs_gaps_and_reconnects(tmp_path: Path) -> None:
    fake = FakeNovig()
    catalog = ns.Catalog(events=[_event(EV_A, ns.now_ms() + HOUR_MS)], market_counts={EV_A: 1})

    async def fetch() -> ns.Catalog:
        return catalog

    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        recorder = ns.Recorder(
            ws_url=f"ws://127.0.0.1:{port}/v3/ws",
            headers=lambda: {"Novig-Key-Id": "read-key"},
            catalog=fetch,
            root=tmp_path,
            cfg=_cfg(connections=1),
            max_backoff_s=0.2,
        )
        task = asyncio.create_task(recorder.run())

        await asyncio.wait_for(_until_covered(tmp_path / "connections.jsonl", 2), 10)
        recorder.stop()
        await asyncio.wait_for(task, 5)

    assert fake.connections == 2
    assert fake.headers == ["read-key", "read-key"]
    assert fake.received[0][1]["snapshot"] == {"markets": {MKT: "book"}}

    events = ns.read_gap_log(tmp_path / "connections.jsonl")
    kinds = [e.kind for e in events]
    assert kinds[0] == "start" and kinds[-1] == "stop"
    gap = next(e for e in events if e.kind == "seq_gap")
    assert (gap.market, gap.channel, gap.expected, gap.got) == (MKT, "book", 50, 51)
    resync = next(e for e in events if e.kind == "resynced")
    assert (resync.market, resync.got) == (MKT, 60)
    drop = next(e for e in events if e.kind == "disconnected")
    assert drop.detail == "closed 1008 SLOW_CONSUMER"

    windows = ns.missing_windows(events)
    assert [(w.scope, w.cause) for w in windows] == [
        ("all", "not running"),
        ("market", "seq gap"),
        ("connection", "closed 1008 SLOW_CONSUMER"),
        ("all", "stop"),
    ]
    assert windows[2].end is not None and windows[2].end >= drop.ts

    # Every message the server sent was written verbatim with its receive time.
    lines = _recorded(tmp_path)
    assert len(lines) == 5
    assert {line["conn"] for line in lines} == {e.conn for e in events if e.kind == "connected"}
    assert [line["msg"].get("delta", {}).get(MKT, {}).get("book", {}).get("seq") for line in lines][
        1:3
    ] == [49, 51]


async def test_recorder_splits_events_across_connections(tmp_path: Path) -> None:
    subscribed: list[set[str]] = []

    async def handler(ws: ServerConnection) -> None:
        mine: set[str] = set()
        subscribed.append(mine)
        async for raw in ws:
            msg = json.loads(raw)
            events = msg.get("subscribe", {}).get("events", {})
            mine.update(events)
            snapshot = {
                f"mkt-{e}": {"eventId": e, "book": {"seq": 1, "orders": {}}} for e in events
            }
            await ws.send(json.dumps({"nonce": msg["nonce"], "snapshot": snapshot}))

    start = ns.now_ms() + HOUR_MS
    catalog = ns.Catalog(
        events=[_event(EV_A, start), _event(EV_B, start + 1), _event(EV_C, start + 2)],
        market_counts={EV_A: 1000, EV_B: 1000, EV_C: 500},
    )

    async def fetch() -> ns.Catalog:
        return catalog

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        recorder = ns.Recorder(
            ws_url=f"ws://127.0.0.1:{port}/v3/ws",
            headers=dict,
            catalog=fetch,
            root=tmp_path,
            cfg=_cfg(connections=2, max_markets_per_connection=1800),
        )
        task = asyncio.create_task(recorder.run())
        await asyncio.wait_for(_until_covered(tmp_path / "connections.jsonl", 2), 10)
        recorder.stop()
        await asyncio.wait_for(task, 5)

    # A and B cannot share a 1800-market connection; C fits beside either.
    assert sorted(map(sorted, subscribed)) == [[EV_A, EV_C], [EV_B]] or sorted(
        map(sorted, subscribed)
    ) == [[EV_A], [EV_B, EV_C]]
    events = ns.read_gap_log(tmp_path / "connections.jsonl")
    covered = {e.slot: e.events for e in events if e.kind == "covered"}
    assert set(covered) == {0, 1}
    assert sorted(e for evs in covered.values() for e in evs or []) == [EV_A, EV_B, EV_C]
    windows = ns.missing_windows(events)
    assert [(w.scope, w.cause) for w in windows] == [("all", "not running"), ("all", "stop")]


async def test_recorder_stops_cleanly_on_low_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def handler(ws: ServerConnection) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            await ws.send(json.dumps({"nonce": msg["nonce"], "snapshot": {}}))

    async def fetch() -> ns.Catalog:
        return ns.Catalog(events=[_event(EV_A, ns.now_ms() + HOUR_MS)], market_counts={EV_A: 1})

    sent: list[str] = []

    def fake_notify(title: str, message: str) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(ns, "notify", fake_notify)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        recorder = ns.Recorder(
            ws_url=f"ws://127.0.0.1:{port}/v3/ws",
            headers=dict,
            catalog=fetch,
            root=tmp_path,
            cfg=_cfg(connections=1, disk_check_seconds=0.05),
        )
        free = iter([50.0, 50.0, 4.2])  # plenty, then below the 5 GiB floor
        monkeypatch.setattr(recorder, "free_gib", lambda: next(free, 4.2))
        await asyncio.wait_for(recorder.run(), 10)  # returns on its own

    events = ns.read_gap_log(tmp_path / "connections.jsonl")
    stop = events[-1]
    assert stop.kind == "stop"
    assert stop.detail is not None and stop.detail.startswith("low disk: 4.2 GiB free")
    assert sent and sent[0] == stop.detail
    windows = ns.missing_windows(events)
    assert windows[-1].scope == "all"
    assert windows[-1].end is None
    assert windows[-1].cause.startswith("low disk")
