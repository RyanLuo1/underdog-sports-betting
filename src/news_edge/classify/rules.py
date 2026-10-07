"""Rules-based classifier for Underdog's templated news posts.

A post is classified only when its first line matches a known template end to end:

    [Source: ][Team POS ]Player[ (injury)][, Player2 (injury)...] <status phrase>[ for Week N].

Anything else, including hedged reports ("could return", "hopeful", "optimism"), gets
status "unknown". The parser never guesses a player, team, or status.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from news_edge.entities.teams import canonical

MODEL = "rules"
VERSION = "2026-10-06.4"

Status = Literal[
    "out",
    "inactive",
    "doubtful",
    "questionable",
    "probable",
    "active",
    "ir",
    "in_game_injury",
    "practice_dnp",
    "practice_limited",
    "practice_full",
    "practice_injury",
    "transaction",
    "suspended",
    "unknown",
]

TEAMS = [
    "49ers",
    "Bears",
    "Bengals",
    "Bills",
    "Broncos",
    "Browns",
    "Buccaneers",
    "Bucs",
    "Cardinals",
    "Chargers",
    "Chiefs",
    "Colts",
    "Commanders",
    "Cowboys",
    "Dolphins",
    "Eagles",
    "Falcons",
    "Giants",
    "Jaguars",
    "Jets",
    "Lions",
    "Packers",
    "Panthers",
    "Patriots",
    "Raiders",
    "Rams",
    "Ravens",
    "Saints",
    "Seahawks",
    "Steelers",
    "Texans",
    "Titans",
    "Vikings",
]
_TEAM = "|".join(TEAMS)
_POS = r"QB|RB|FB|WR|TE|OT|LT|RT|OG|LG|RG|G|T|C|OL|DL|DT|NT|DE|EDGE|LB|ILB|OLB|CB|S|FS|SS|DB|K|P|LS"

# A name: capitalized words (Ja'Marr, TreVeyon, AJ, St. Brown, Smith-Njigba), optional suffix.
_WORD = r"(?:[A-Z][a-z]*\.?|[A-Z][a-zA-Z]*(?:['’-][A-Za-z]+)*)"
_NAME = rf"(?!The |Week |(?:{_POS}) ){_WORD}(?: {_WORD}){{1,3}}(?:,? (?:Jr\.|Sr\.|II|III|IV|V))?"


def _player(n: int) -> str:
    """Player n, with an optional parenthesized injury: `Chig Okonkwo (hamstring)`."""
    return rf"(?:(?:{_POS}) )?(?P<p{n}>{_NAME})(?: \((?P<inj{n}>[^()]{{1,40}})\))?"


_SUBJECT = (
    rf"(?:(?P<team>{_TEAM}) )?"
    + _player(1)
    + rf"(?:, {_player(2)})?(?:,? and {_player(3)}|, {_player(4)})?"
)

_SOURCE = r"(?:(?P<source>Status alert|[A-Z][a-zA-Z.'-]+(?: [A-Z][a-zA-Z.'-]+)?): )?"
_WEEK = r"(?: (?:(?:for|in|during) )?(?P<pre>preseason )?Week (?P<week>\d{1,2}))"
_WHEN = r"(?: (?:on )?(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|today|tonight))"
_REASON = r"(?: after (?:suffering|sustaining) [^.]+)"
_TAIL = rf"{_WEEK}?{_WHEN}?(?: (?:vs\.|against) (?:the )?(?:{_TEAM}))?{_REASON}?\.?"

# (status, template name, predicate regex). The first full match wins.
_RULES: list[tuple[Status, str, str]] = [
    (
        "ir",
        "placed_on_ir",
        r"(?:placed on|to|headed to) (?:season-ending )?(?:injured reserve|IR)(?:; .*)?",
    ),
    ("out", "ruled_out", r"(?:has been |officially |been )?ruled out"),
    ("inactive", "inactive", r"(?:is |will be )?(?:officially )?inactive"),
    ("inactive", "healthy_inactive", r"(?:is |will be )?(?:a )?healthy (?:inactive|scratch)"),
    ("inactive", "expected_inactive", r"expected to be inactive"),
    ("out", "out", r"(?:is |officially |will be )?out"),
    ("out", "wont_play", r"(?:won't|will not|won’t|not expected to) play"),
    ("out", "will_miss", r"(?:will|expected to) miss(?: at least \d+ (?:weeks|games))?"),
    (
        "out",
        "season_ending_injury",
        r"(?:suffered|suffers|diagnosed with|has|sustained) (?:an? )?(?:torn|ruptured) "
        r"(?:ACL|Achilles)(?: tendon)?",
    ),
    ("doubtful", "doubtful", r"(?:listed |is |officially )?doubtful"),
    ("doubtful", "unlikely", r"(?:is )?unlikely to play"),
    ("questionable", "questionable", r"(?:listed |is |officially )?questionable"),
    (
        "questionable",
        "game_time_decision",
        r"(?:is |remains |considered )?(?:a )?(?:\"true game-time decision\"|game-time decision)",
    ),
    ("probable", "expected_to_play", r"(?:is )?(?:expected|on track) to play"),
    (
        "probable",
        "good_to_go",
        r"(?:should be |is |will be )?(?:\"good to go\"|good to go|a full go)",
    ),
    ("probable", "not_on_report", rf"not listed on (?:the |(?:{_TEAM}) )?injury report"),
    ("active", "active", r"(?:is )?(?:officially )?active"),
    ("active", "will_play", r"will play"),
    ("active", "will_start", r"(?:will|expected to) start"),
    (
        "practice_dnp",
        "no_practice",
        r"(?:officially )?(?:won't|will not|won’t|doesn't|does not|didn't|did not) practice"
        r"(?: again)?",
    ),
    ("practice_dnp", "dnp", r"(?:was )?(?:a )?DNP"),
    (
        "practice_limited",
        "limited",
        r"(?:was |officially )?limited(?: (?:in|at) practice)?(?: again)?",
    ),
    ("practice_full", "will_practice", r"will practice(?: again)?"),
    (
        "practice_full",
        "fully_practices",
        r"(?:officially )?(?:fully practices?|practices? fully)(?: again)?",
    ),
    (
        "practice_full",
        "full_practice",
        r"(?:was )?(?:a )?full(?: participant)?(?: (?:in|at) practice)?",
    ),
    ("practice_full", "return_to_practice", r"will return to practice"),
    ("suspended", "suspended", r"suspended \d+ games?(?: for .*)?"),
    (
        "practice_injury",
        "left_practice",
        r"(?:carted|walked|limped) off (?:the )?practice field(?: with (?:trainers|a trainer))?"
        r"|left practice(?: early| with (?:trainers|a trainer))?",
    ),
]

_IN_GAME = [
    r"carted (?:off|to (?:the )?locker room)",
    r"headed to (?:the )?medical tent",
    r"(?:is )?questionable to return",
    r"receiving X-rays",
    r"being evaluated for a (?:concussion|head injury)",
    r"(?:is )?out for the (?:game|rest of the game)",
    r"(?:went|goes|headed) to (?:the )?locker room",
    r"(?:limping|limped|helped) off(?: the)?(?: field)?",
    r"(?:won't|will not|won’t) return",
    r"suffered an? [a-z ]+ injury(?: according to the (?:{t}))?",
]

# In-game injuries recognizable without a "Status alert:" prefix.
_IN_GAME_ANY = [
    r"carted off(?: the)? field(?: with (?:an )?(?:apparent )?[a-z -]+ injury)?",
]

_TRANSACTION = [
    r"(?:to sign|signs|signed|agrees to terms)(?: a .+?)? with (?:the )?(?P<tteam>{t})"
    r"(?: practice squad)?",
    r"(?:to be |been |was |has been )?(?:released|waived|cut) by the (?P<tteam>{t})",
    r"(?:traded|being traded|to be traded) to the (?P<tteam>{t})",
]

_LINK = re.compile(r"\s*https?://\S+")
_ATTRIBUTION = re.compile(r",? (?:per|via) (?:@\w+|the broadcast|[A-Z][\w.' ]+?)(?=[.:]?$)")


@dataclass(frozen=True)
class Classification:
    status: Status
    template: str | None = None
    players: list[str] = field(default_factory=list)
    team: str | None = None
    injuries: list[str | None] = field(default_factory=list)
    source: str | None = None
    week: int | None = None
    preseason: bool = False

    @property
    def detail(self) -> dict[str, object]:
        return {
            "injuries": self.injuries,
            "source": self.source,
            "week": self.week,
            "preseason": self.preseason,
        }


UNKNOWN = Classification(status="unknown")


def _first_line(text: str) -> str:
    line = text.strip().split("\n", 1)[0]
    line = _LINK.sub("", line).strip()
    return _ATTRIBUTION.sub("", line).strip()


def classify(text: str) -> Classification:
    """Classify one post. Returns UNKNOWN unless a template matches the whole first line."""
    line = _first_line(text)
    if not line:
        return UNKNOWN

    in_game = line.startswith("Status alert:")
    rules: list[tuple[Status, str, str]] = (
        [("in_game_injury", f"in_game_{i}", r.format(t=_TEAM)) for i, r in enumerate(_IN_GAME)]
        if in_game
        else [
            *_RULES,
            *(("in_game_injury", f"in_game_any_{i}", r) for i, r in enumerate(_IN_GAME_ANY)),
            *(
                ("transaction", f"transaction_{i}", r.format(t=_TEAM))
                for i, r in enumerate(_TRANSACTION)
            ),
        ]
    )
    for status, template, predicate in rules:
        pattern = rf"{_SOURCE}{_SUBJECT} (?:{predicate}){_TAIL}"
        m = re.fullmatch(pattern, line)
        if m is None:
            continue
        groups = m.groupdict()
        players = [groups[f"p{n}"] for n in (1, 2, 3, 4) if groups.get(f"p{n}")]
        injuries = [groups.get(f"inj{n}") for n in (1, 2, 3, 4) if groups.get(f"p{n}")]
        team = canonical(groups.get("team") or groups.get("tteam"))
        source = groups.get("source")
        return Classification(
            status=status,
            template=template,
            players=players,
            team=team,
            injuries=injuries,
            source=None if source == "Status alert" else source,
            week=int(groups["week"]) if groups.get("week") else None,
            preseason=bool(groups.get("pre")),
        )
    return UNKNOWN
