"""NFL teams: canonical nicknames, the abbreviations Novig and Sleeper use, and aliases."""

ABBRS: dict[str, frozenset[str]] = {
    nick: frozenset(abbrs.split())
    for nick, abbrs in {
        "Cardinals": "ARI",
        "Falcons": "ATL",
        "Ravens": "BAL",
        "Bills": "BUF",
        "Panthers": "CAR",
        "Bears": "CHI",
        "Bengals": "CIN",
        "Browns": "CLE",
        "Cowboys": "DAL",
        "Broncos": "DEN",
        "Lions": "DET",
        "Packers": "GB",
        "Texans": "HOU",
        "Colts": "IND",
        "Jaguars": "JAX JAC",
        "Chiefs": "KC",
        "Chargers": "LAC",
        "Rams": "LAR LA",
        "Raiders": "LV OAK",
        "Dolphins": "MIA",
        "Vikings": "MIN",
        "Patriots": "NE",
        "Saints": "NO",
        "Giants": "NYG",
        "Jets": "NYJ",
        "Eagles": "PHI",
        "Steelers": "PIT",
        "Seahawks": "SEA",
        "49ers": "SF",
        "Buccaneers": "TB",
        "Titans": "TEN",
        "Commanders": "WAS WSH",
    }.items()
}
NICKNAMES = frozenset(ABBRS)
ALIASES = {"Bucs": "Buccaneers", "Niners": "49ers"}
NICKNAME_OF_ABBR = {a: nick for nick, abbrs in ABBRS.items() for a in abbrs}


def canonical(name: str | None) -> str | None:
    """A canonical nickname from a nickname or alias ("Bucs" -> "Buccaneers"), else None."""
    if name is None:
        return None
    name = ALIASES.get(name, name)
    return name if name in NICKNAMES else None


def game_teams(description: str) -> tuple[str, str] | None:
    """(away, home) nicknames from "Carolina Panthers @ Arizona Cardinals", or None for
    anything that is not a game between two NFL teams (futures, awards)."""
    sides = [s.strip().split()[-1] for s in description.split("@") if s.strip()]
    if len(sides) != 2 or any(s not in NICKNAMES for s in sides):
        return None
    return sides[0], sides[1]
