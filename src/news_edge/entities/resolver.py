"""Resolve classified players to Novig games and markets from the schedule. Fails closed.

Everything used is known at the post's time t0:

1. The player's team is the team the post names. If it names none, it comes from his
   prop history before t0 (a market's UUIDv7 ID encodes when it was created): start
   with the two teams of his most recent prop game and keep only the teams each
   earlier game shares, newest first, stopping at one team or at the first game with
   no team in common (a trade). Two teams left (one game): a roster team that is one
   of the two breaks the tie; otherwise ambiguous. A post's team that contradicts the
   prop history's teams is ambiguous too.
2. The game is that team's next game on the schedule: the first NFL game ("A @ B")
   starting no more than GAME_LENGTH before t0 (a game in progress) and no more than
   8 days after it. Whether the game has props for the player does not choose it.
3. Markets are the player's props in that game created before t0, if any (status
   matched), and the game's lines. Without such props the status is team_only.

`tier1` is true if Novig listed props for the player in any game before t0.

Player names come from prop descriptions ("Zay Flowers 2.5 RECEPTIONS").
"""

import re
import unicodedata
import uuid
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from news_edge.entities.teams import game_teams

GAME_LINE_TYPES = frozenset({"MONEY", "SPREAD", "TOTAL", "TEAM_TOTAL"})
_PERIOD = re.compile(r"_(?:1H|2H|1Q|2Q|3Q|4Q)$")
_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}

# A game is "in progress" this long after kickoff (placeholder; overtime can run longer).
GAME_LENGTH = timedelta(hours=3, minutes=30)
AFTER = timedelta(days=8)

Status = Literal["matched", "team_only", "ambiguous", "unmatched"]


def is_game_line(market_type: str) -> bool:
    return _PERIOD.sub("", market_type) in GAME_LINE_TYPES


def prop_player(description: str, market_type: str) -> str | None:
    """The player in a prop description ("Mark Andrews 89.5 RECEIVING_YARDS")."""
    if is_game_line(market_type) or not description.endswith(f" {market_type}"):
        return None
    name = description.removesuffix(f" {market_type}")
    name = re.sub(r" -?\d+(?:\.\d+)?$", "", name).strip()
    return name or None


def normalize(name: str) -> str:
    """Case, accents, punctuation, and Jr./Sr./II-style suffixes do not matter."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9 ]", "", text.lower().replace("-", " ")).split()
    return " ".join(w for w in words if w not in _SUFFIX)


def created_at(market_id: str) -> datetime:
    """When a market was created, from its UUIDv7 ID."""
    return datetime.fromtimestamp((uuid.UUID(market_id).int >> 80) / 1000, tz=UTC)


@dataclass(frozen=True)
class Event:
    event_id: str
    starts_at: datetime
    description: str

    @property
    def teams(self) -> tuple[str, str]:
        teams = game_teams(self.description)
        if teams is None:
            raise ValueError(f"not a game: {self.description}")
        return teams


@dataclass(frozen=True)
class Market:
    market_id: str
    event_id: str
    market_type: str
    description: str


@dataclass(frozen=True)
class Prop:
    market_id: str
    event_id: str
    created: datetime


@dataclass
class Index:
    events: dict[str, Event]
    schedule: dict[str, list[Event]] = field(default_factory=dict)  # team -> games by start
    props: dict[str, list[Prop]] = field(default_factory=dict)  # player -> props by creation
    game_lines: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, events: list[Event], markets: list[Market]) -> "Index":
        games = [e for e in events if game_teams(e.description) is not None]
        idx = cls(events={e.event_id: e for e in games})
        for e in games:
            for team in e.teams:
                idx.schedule.setdefault(team, []).append(e)
        for m in markets:
            if m.event_id not in idx.events:
                continue  # futures and awards are not games
            if is_game_line(m.market_type):
                idx.game_lines.setdefault(m.event_id, []).append(m.market_id)
                continue
            name = prop_player(m.description, m.market_type)
            if name is not None:
                prop = Prop(m.market_id, m.event_id, created_at(m.market_id))
                idx.props.setdefault(normalize(name), []).append(prop)
        for games_ in idx.schedule.values():
            games_.sort(key=lambda e: (e.starts_at, e.event_id))
        for props in idx.props.values():
            props.sort(key=lambda p: (p.created, p.market_id))
        return idx

    def prior_games(self, player: str, t0: datetime) -> list[Event]:
        """Games in which the player had props created before t0, oldest first."""
        seen: dict[str, Event] = {}
        for p in self.props.get(normalize(player), []):
            if p.created >= t0:
                break
            seen.setdefault(p.event_id, self.events[p.event_id])
        return sorted(seen.values(), key=lambda e: (e.starts_at, e.event_id))

    def next_game(self, team: str, t0: datetime) -> Event | None:
        games = self.schedule.get(team, [])
        i = bisect_left([e.starts_at for e in games], t0 - GAME_LENGTH)
        if i < len(games) and games[i].starts_at <= t0 + AFTER:
            return games[i]
        return None


def recent_team(prior: list[Event]) -> set[str]:
    """Teams consistent with the player's most recent prop games, newest first: one team
    once two games share only it; stops at the first game sharing neither (a trade)."""
    teams = set(prior[-1].teams)
    for e in reversed(prior[:-1]):
        if len(teams) == 1:
            break
        common = teams & set(e.teams)
        if not common:
            break
        teams = common
    return teams


@dataclass(frozen=True)
class Resolution:
    subject: str
    status: Status
    reason: str | None = None
    event_id: str | None = None
    team: str | None = None
    tier1: bool = False
    market_ids: list[str] = field(default_factory=list)


def resolve(
    idx: Index,
    t0: datetime,
    players: list[str],
    post_team: str | None,
    roster: Mapping[str, set[str]] | None = None,
) -> list[Resolution]:
    """`roster` (normalized name -> team nicknames) only breaks a tie between two teams."""
    out = []
    for player in players:
        prior = idx.prior_games(player, t0)
        tier1 = bool(prior)
        history = recent_team(prior) if prior else None
        if post_team is not None:
            if history is not None and post_team not in history:
                out.append(
                    Resolution(
                        player,
                        "ambiguous",
                        f"post says {post_team}; props before t0 were in games of "
                        f"{sorted(history) or 'different teams'}",
                        tier1=tier1,
                    )
                )
                continue
            team = post_team
            why = "team from post"
        elif history is None:
            out.append(
                Resolution(player, "unmatched", "no team: post names none, no props before t0")
            )
            continue
        elif len(history) == 1:
            team = next(iter(history))
            why = "team from prop history"
        else:
            known = (roster or {}).get(normalize(player), set()) & history
            if len(known) != 1:
                out.append(
                    Resolution(
                        player, "ambiguous", f"team unclear among {sorted(history)}", tier1=tier1
                    )
                )
                continue
            team = next(iter(known))
            why = "team from roster tie-break"
        game = idx.next_game(team, t0)
        if game is None:
            out.append(
                Resolution(
                    player,
                    "unmatched",
                    f"no {team} game in progress or within 8 days",
                    None,
                    team,
                    tier1,
                )
            )
            continue
        props = [
            p.market_id
            for p in idx.props.get(normalize(player), [])
            if p.event_id == game.event_id and p.created < t0
        ]
        lines = idx.game_lines.get(game.event_id, [])
        out.append(
            Resolution(
                player,
                "matched" if props else "team_only",
                why if props else f"{why}; no props for player in this game",
                game.event_id,
                team,
                tier1,
                props + lines,
            )
        )
    return out
