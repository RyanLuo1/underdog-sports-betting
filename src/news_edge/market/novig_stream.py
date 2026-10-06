"""Record Novig's order-book stream to disk, and log every gap in coverage.

One websocket, subscribed per event on the `book` channel, which reports each resting
order as it is added and each removal as a fill or a cancel. Every message received is
written verbatim, with its receive time, to hourly JSONL files:

    data/novig/stream/<env>/<YYYY-MM-DD>/<HH>.jsonl[.gz]     (UTC)

Coverage gaps go to data/novig/stream/<env>/connections.jsonl, one event per line:
process start and stop, connect, subscribe, disconnect, per-market seq gaps and their
resyncs, and a heartbeat every minute so a crash is bounded too. `covered` marks the
moment a connection has subscribed to the whole selection. `missing_windows`
turns that log into the exact windows with no data.

Protocol: https://docs.novig.com/api/streaming/connection.md
"""

import asyncio
import contextlib
import gzip
import json
import logging
import random
import shutil
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

log = logging.getLogger(__name__)

WS_PATH = "/v3/ws"
# Novig's `stream` throttle, and the cost of one subject on `book`.
STREAM_CAPACITY = 512
STREAM_REFILL_PER_S = 4.0
BOOK_WEIGHT = 16
# Novig caps one connection at 2048 watched markets; an event counts as its markets.
MAX_WATCHED_MARKETS = 2048
DONE_STATUSES = frozenset({"SETTLED", "FINAL", "CANCELED"})
HEARTBEAT_S = 60.0


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class RecorderConfig(BaseModel):
    """The `recorder` section of config/default.yaml."""

    leagues: list[str] = ["NFL"]
    # Keep events that started up to this long ago (in play), and pick up events this
    # far ahead, soonest first, until the market budget is spent.
    lookback_hours: float = 8.0
    horizon_hours: float = 168.0
    max_markets: int = Field(1800, le=MAX_WATCHED_MARKETS)
    refresh_seconds: float = 300.0
    subscribe_batch: int = 16


class CatalogEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    event_id: str = Field(alias="eventId")
    league: str
    status: str
    description: str
    starts_ts: int = Field(alias="startsTs")


@dataclass
class Catalog:
    events: list[CatalogEvent]
    market_counts: dict[str, int]


@dataclass
class Selection:
    event_ids: list[str]
    markets: int
    skipped: list[str] = field(default_factory=list)


def select_events(catalog: Catalog, now: int, cfg: RecorderConfig) -> Selection:
    """Live and upcoming events, soonest first, while their markets fit the budget."""
    earliest = now - int(cfg.lookback_hours * 3_600_000)
    latest = now + int(cfg.horizon_hours * 3_600_000)
    candidates = sorted(
        (
            e
            for e in catalog.events
            if e.league in cfg.leagues
            and e.status not in DONE_STATUSES
            and earliest <= e.starts_ts <= latest
        ),
        key=lambda e: (e.starts_ts, e.event_id),
    )
    chosen = Selection(event_ids=[], markets=0)
    for event in candidates:
        n = catalog.market_counts.get(event.event_id, 0)
        if chosen.markets + n > cfg.max_markets:
            chosen.skipped.append(event.event_id)
            continue
        chosen.event_ids.append(event.event_id)
        chosen.markets += n
    return chosen


async def fetch_catalog(client: httpx.AsyncClient, leagues: list[str]) -> Catalog:
    """Open events and per-event market counts from the public (unsigned) catalog."""
    league = ",".join(leagues)
    events = [
        CatalogEvent.model_validate(item)
        for item in await _pages(client, "/v3/public/catalog/events", {"league": league})
    ]
    counts: dict[str, int] = {}
    for market in await _pages(client, "/v3/public/catalog/markets", {"league": league}):
        counts[market["eventId"]] = counts.get(market["eventId"], 0) + 1
    return Catalog(events=events, market_counts=counts)


async def _pages(
    client: httpx.AsyncClient, path: str, params: dict[str, str]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    query: dict[str, str | int] = {**params, "limit": 5000}
    while True:
        resp = await client.get(path, params=query)
        resp.raise_for_status()
        page = resp.json()
        items.extend(page["items"])
        if not page.get("next"):
            return items
        query["after"] = page["next"]


SeqResult = Literal["ok", "duplicate", "gap", "resyncing"]


class SeqTracker:
    """Checks each market channel's seq. Seq is per market, per channel, per connection."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], int] = {}
        self._resyncing: set[str] = set()
        self.event_of: dict[str, str] = {}

    def on_snapshot(self, market: str, channel: str, seq: int) -> bool:
        """Record a snapshot's seq. Returns True if it ends a resync of this market."""
        self._last[(market, channel)] = seq
        if market in self._resyncing:
            self._resyncing.discard(market)
            return True
        return False

    def on_delta(self, market: str, channel: str, seq: int) -> SeqResult:
        if market in self._resyncing:
            return "resyncing"
        last = self._last.get((market, channel))
        # A market that opens under an event subscription has no snapshot; it starts at 1.
        expected = 1 if last is None else last + 1
        if seq < expected:
            return "duplicate"
        if seq > expected:
            self._resyncing.add(market)
            return "gap"
        self._last[(market, channel)] = seq
        return "ok"

    def expected(self, market: str, channel: str) -> int:
        return self._last.get((market, channel), 0) + 1

    def drop_event(self, event_id: str) -> None:
        markets = {m for m, e in self.event_of.items() if e == event_id}
        self._last = {k: v for k, v in self._last.items() if k[0] not in markets}
        self._resyncing -= markets
        for m in markets:
            del self.event_of[m]


class StreamWriter:
    """Appends raw messages to hourly UTC files and gzips each hour once it is closed."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._hour: str | None = None
        self._file: IO[bytes] | None = None
        self._compress_lock = threading.Lock()

    def path_for(self, ts_ms: int) -> Path:
        t = time.gmtime(ts_ms / 1000)
        return self.root / time.strftime("%Y-%m-%d", t) / f"{time.strftime('%H', t)}.jsonl"

    def write(self, recv_ts: int, conn: str, raw: str) -> None:
        path = self.path_for(recv_ts)
        f = self._file if self._file is not None and str(path) == self._hour else None
        if f is None:
            f = self._rotate(path)
        if "\n" in raw:
            raw = json.dumps(json.loads(raw), separators=(",", ":"))
        f.write(f'{{"recv_ts":{recv_ts},"conn":"{conn}","msg":{raw}}}\n'.encode())
        f.flush()

    def _rotate(self, path: Path) -> IO[bytes]:
        self.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        f = self._file = path.open("ab")
        self._hour = str(path)
        # Off the event loop: gzipping an hour takes seconds, and a reader stalled for
        # 15 s gets closed as SLOW_CONSUMER.
        threading.Thread(target=self.compress_closed, daemon=True).start()
        return f

    def compress_closed(self) -> None:
        """Gzip every .jsonl file except the one being written."""
        with self._compress_lock:
            for path in sorted(self.root.glob("*/*.jsonl")):
                if str(path) != self._hour:
                    gzip_file(path)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._hour = None


def gzip_file(path: Path) -> None:
    dest = path.with_name(path.name + ".gz")
    part = dest.with_name(dest.name + ".part")
    with path.open("rb") as src, gzip.open(part, "wb") as out:
        shutil.copyfileobj(src, out)
    # An hour that was gzipped, then reopened after a restart, gets a second member.
    if dest.exists():
        with dest.open("ab") as existing, part.open("rb") as extra:
            shutil.copyfileobj(extra, existing)
        part.unlink()
    else:
        part.rename(dest)
    path.unlink()


GapKind = Literal[
    "start",
    "stop",
    "heartbeat",
    "connecting",
    "connected",
    "subscribed",
    "covered",
    "unsubscribed",
    "disconnected",
    "seq_gap",
    "resynced",
    "error",
]


class GapEvent(BaseModel):
    ts: int
    kind: GapKind
    conn: str | None = None
    market: str | None = None
    channel: str | None = None
    expected: int | None = None
    got: int | None = None
    events: list[str] | None = None
    detail: str | None = None


class GapLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def record(self, kind: GapKind, **fields: Any) -> GapEvent:
        event = GapEvent(ts=now_ms(), kind=kind, **fields)
        with self.path.open("a") as f:
            f.write(event.model_dump_json(exclude_none=True) + "\n")
        level = logging.WARNING if kind in ("disconnected", "seq_gap", "error") else logging.INFO
        log.log(level, "%s", event.model_dump_json(exclude_none=True))
        return event


def read_gap_log(path: Path) -> list[GapEvent]:
    with path.open() as f:
        return [GapEvent.model_validate_json(line) for line in f if line.strip()]


class Window(BaseModel):
    start: int
    end: int | None
    scope: Literal["all", "market"]
    market: str | None = None
    cause: str


def missing_windows(events: Iterable[GapEvent]) -> list[Window]:
    """Windows with no data. A whole-stream window runs from the disconnect (or, after a
    crash, the last log line) until the next connection is `covered`. A market window runs from
    its seq gap to its resync. An open window has end=None."""
    windows: list[Window] = []
    covered = False
    outage: Window | None = None
    last_ts: int | None = None
    market_gaps: dict[str, Window] = {}
    for e in events:
        if e.kind == "start":
            if covered:  # the previous process died without logging a stop
                outage = Window(start=last_ts or e.ts, end=None, scope="all", cause="crash")
            elif outage is None:
                outage = Window(start=e.ts, end=None, scope="all", cause="not running")
            covered = False
            market_gaps.clear()
        elif e.kind in ("disconnected", "stop") and covered:
            cause = e.detail or e.kind
            outage = Window(start=e.ts, end=None, scope="all", cause=cause)
            covered = False
            market_gaps.clear()
        elif e.kind == "covered" and not covered:
            if outage is not None:
                outage.end = e.ts
                windows.append(outage)
                outage = None
            covered = True
        elif e.kind == "seq_gap" and covered and e.market and e.market not in market_gaps:
            market_gaps[e.market] = Window(
                start=e.ts, end=None, scope="market", market=e.market, cause="seq gap"
            )
        elif e.kind == "resynced" and e.market in market_gaps:
            gap = market_gaps.pop(e.market)
            gap.end = e.ts
            windows.append(gap)
        last_ts = e.ts
    if outage is not None:
        windows.append(outage)
    windows.extend(market_gaps.values())
    return sorted(windows, key=lambda w: w.start)


class TokenBucket:
    """A local model of Novig's `stream` throttle, so subscribes never hit a 429."""

    def __init__(self, capacity: float, refill_per_s: float) -> None:
        self.capacity = capacity
        self.refill = refill_per_s
        self._tokens = capacity
        self._at = time.monotonic()

    async def take(self, n: float) -> None:
        n = min(n, self.capacity)
        while True:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._at) * self.refill)
            self._at = now
            if self._tokens >= n:
                self._tokens -= n
                return
            await asyncio.sleep((n - self._tokens) / self.refill)


CatalogFetcher = Callable[[], Awaitable[Catalog]]
HeaderFactory = Callable[[], dict[str, str]]


class Recorder:
    def __init__(
        self,
        ws_url: str,
        headers: HeaderFactory,
        catalog: CatalogFetcher,
        root: Path,
        cfg: RecorderConfig,
        max_backoff_s: float = 60.0,
    ) -> None:
        self.ws_url = ws_url
        self.headers = headers
        self.catalog = catalog
        self.cfg = cfg
        self.writer = StreamWriter(root)
        self.gaps = GapLog(root / "connections.jsonl")
        self.max_backoff_s = max_backoff_s
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        """Connect, record, and reconnect with backoff until stop() is called."""
        self.gaps.record("start")
        self.writer.compress_closed()
        heartbeat = asyncio.create_task(self._heartbeat())
        backoff = 1.0
        try:
            while not self.stopping.is_set():
                session = _Session(self)
                retry_after = await session.run()
                if session.covered:
                    backoff = 1.0
                if self.stopping.is_set():
                    break
                delay = retry_after or backoff * random.uniform(0.5, 1.0)  # noqa: S311
                backoff = min(self.max_backoff_s, backoff * 2)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), delay)
        finally:
            heartbeat.cancel()
            self.writer.close()
            self.gaps.record("stop")

    def stop(self) -> None:
        self.stopping.set()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            self.gaps.record("heartbeat")


class _Session:
    """One websocket connection: subscribe, keep the selection fresh, record, check seq."""

    def __init__(self, recorder: Recorder) -> None:
        self.r = recorder
        self.conn = uuid.uuid4().hex[:12]
        self.seq = SeqTracker()
        self.bucket = TokenBucket(STREAM_CAPACITY, STREAM_REFILL_PER_S)
        self.nonce = 0
        self.subscribed: set[str] = set()
        self.pending: dict[int, list[str]] = {}
        self.covered = False
        self.refreshing = False
        self.ws: ClientConnection | None = None

    async def run(self) -> float | None:
        """Returns a server-requested retry delay, if any."""
        gaps = self.r.gaps
        gaps.record("connecting", conn=self.conn)
        try:
            async with connect(
                self.r.ws_url,
                additional_headers=self.r.headers(),
                max_size=None,
                open_timeout=15,
                ping_interval=20,
                ping_timeout=20,
                compression=None,
            ) as ws:
                self.ws = ws
                gaps.record("connected", conn=self.conn)
                await self._serve(ws)
        except InvalidStatus as exc:
            status = exc.response.status_code
            body = exc.response.body.decode(errors="replace")[:200] if exc.response.body else ""
            gaps.record("disconnected", conn=self.conn, detail=f"handshake {status} {body}")
            retry = exc.response.headers.get("Retry-After")
            return float(retry) if retry and retry.isdigit() else None
        except ConnectionClosed as exc:
            detail = f"closed {exc.rcvd.code} {exc.rcvd.reason}" if exc.rcvd else "closed 1006"
            gaps.record("disconnected", conn=self.conn, detail=detail)
        except (OSError, TimeoutError, InvalidHandshake) as exc:
            gaps.record("disconnected", conn=self.conn, detail=f"{type(exc).__name__}: {exc}")
        except asyncio.CancelledError:
            gaps.record("disconnected", conn=self.conn, detail="stopped")
            raise
        else:
            gaps.record("disconnected", conn=self.conn, detail="stopped")
        return None

    async def _serve(self, ws: ClientConnection) -> None:
        reader = asyncio.create_task(self._read(ws))
        refresher = asyncio.create_task(self._refresh_loop())
        stopper = asyncio.create_task(self.r.stopping.wait())
        done, pending = await asyncio.wait(
            {reader, refresher, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            if task is not stopper:
                task.result()  # re-raise ConnectionClosed and friends

    async def _read(self, ws: ClientConnection) -> None:
        async for raw in ws:
            text = raw if isinstance(raw, str) else raw.decode()
            self.r.writer.write(now_ms(), self.conn, text)
            try:
                msg = json.loads(text)
            except ValueError:
                self.r.gaps.record("error", conn=self.conn, detail="unparseable message")
                continue
            await self._handle(msg)

    async def _handle(self, msg: dict[str, Any]) -> None:
        nonce = msg.get("nonce")
        if "code" in msg and "message" in msg:
            batch = self.pending.pop(nonce, None) if isinstance(nonce, int) else None
            self.r.gaps.record(
                "error",
                conn=self.conn,
                events=batch,
                detail=f"{msg['code']}: {msg['message']}",
            )
            self._check_covered()
            return
        if "snapshot" in msg:
            for market, body in msg["snapshot"].items():
                self._note_event(market, body)
                for channel, state in _channels(body):
                    if self.seq.on_snapshot(market, channel, state["seq"]):
                        self.r.gaps.record(
                            "resynced",
                            conn=self.conn,
                            market=market,
                            channel=channel,
                            got=state["seq"],
                        )
        if isinstance(nonce, int) and nonce in self.pending:
            events = self.pending.pop(nonce)
            self.subscribed.update(events)
            self.r.gaps.record("subscribed", conn=self.conn, events=events)
            self._check_covered()
        if "delta" in msg:
            for market, body in msg["delta"].items():
                self._note_event(market, body)
                for channel, state in _channels(body):
                    expected = self.seq.expected(market, channel)
                    if self.seq.on_delta(market, channel, state["seq"]) == "gap":
                        self.r.gaps.record(
                            "seq_gap",
                            conn=self.conn,
                            market=market,
                            channel=channel,
                            expected=expected,
                            got=state["seq"],
                        )
                        await self._send({"snapshot": {"markets": {market: "book"}}}, BOOK_WEIGHT)

    def _note_event(self, market: str, body: dict[str, Any]) -> None:
        if isinstance(body.get("eventId"), str):
            self.seq.event_of[market] = body["eventId"]

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await self._refresh()
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                # Keep recording what we have; try the catalog again next round.
                self.r.gaps.record("error", conn=self.conn, detail=f"catalog: {exc!r}")
            await asyncio.sleep(self.r.cfg.refresh_seconds)

    async def _refresh(self) -> None:
        self.refreshing = True
        try:
            await self._update_subscriptions()
        finally:
            self.refreshing = False
        self._check_covered()

    async def _update_subscriptions(self) -> None:
        selection = select_events(await self.r.catalog(), now_ms(), self.r.cfg)
        if selection.skipped:
            log.warning(
                "market budget %d full; not recording %d events: %s",
                self.r.cfg.max_markets,
                len(selection.skipped),
                selection.skipped,
            )
        want = set(selection.event_ids)
        in_flight = {e for batch in self.pending.values() for e in batch}
        drop = sorted(self.subscribed - want)
        if drop:
            await self._send({"unsubscribe": [f"event:{e}" for e in drop]}, len(drop))
            for e in drop:
                self.subscribed.discard(e)
                self.seq.drop_event(e)
            self.r.gaps.record("unsubscribed", conn=self.conn, events=drop)
        add = [e for e in selection.event_ids if e not in self.subscribed | in_flight]
        size = self.r.cfg.subscribe_batch
        for i in range(0, len(add), size):
            batch = add[i : i + size]
            nonce = await self._send(
                {"subscribe": {"events": dict.fromkeys(batch, "book")}}, BOOK_WEIGHT * len(batch)
            )
            self.pending[nonce] = batch

    def _check_covered(self) -> None:
        """Once the first selection is fully acked, this connection is recording."""
        if not self.covered and not self.pending and not self.refreshing:
            self.covered = True
            self.r.gaps.record("covered", conn=self.conn, events=sorted(self.subscribed))

    async def _send(self, body: dict[str, Any], cost: float) -> int:
        if self.ws is None:
            raise RuntimeError("not connected")
        await self.bucket.take(cost)
        self.nonce += 1
        await self.ws.send(json.dumps({"nonce": self.nonce, **body}))
        return self.nonce


def _channels(body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The (channel, state) pairs in one market's entry that carry a seq."""
    return [
        (name, state)
        for name, state in body.items()
        if isinstance(state, dict) and isinstance(state.get("seq"), int)
    ]
