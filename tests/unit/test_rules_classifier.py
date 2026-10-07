"""The rules classifier on real @UnderdogNFL posts from the Aug-Oct 2026 backfill."""

import pytest

from news_edge.classify.rules import classify

# (post text, status, template, players, team, injuries, source, week, preseason)
CASES = [
    (
        "Chig Okonkwo (hamstring) ruled out for Week 2.",
        "out", "ruled_out", ["Chig Okonkwo"], None, ["hamstring"], None, 2, False,
    ),
    (
        "Puka Nacua (hip) inactive for Week 2.",
        "inactive", "inactive", ["Puka Nacua"], None, ["hip"], None, 2, False,
    ),
    (
        "Rap: De'Von Achane suffered torn ACL.",
        "out", "season_ending_injury", ["De'Von Achane"], None, [None], "Rap", None, False,
    ),
    (
        "Schefter: Calvin Austin diagnosed with torn ACL. https://t.co/d4TEIeBFTn",
        "out", "season_ending_injury", ["Calvin Austin"], None, [None], "Schefter", None, False,
    ),
    (
        "Darius Slayton healthy inactive for Week 3.",
        "inactive", "healthy_inactive", ["Darius Slayton"], None, [None], None, 3, False,
    ),
    (
        "DT Ed Oliver (hip) inactive for Week 2 after suffering injury during warm-ups.",
        "inactive", "inactive", ["Ed Oliver"], None, ["hip"], None, 2, False,
    ),
    (
        "Fowler: Adonai Mitchell (finger) expected to be inactive in Week 3.",
        "inactive", "expected_inactive", ["Adonai Mitchell"], None, ["finger"], "Fowler", 3,
        False,
    ),
    (
        "Status alert: Zay Flowers (hamstring) won't return Sunday.",
        "in_game_injury", "in_game_8", ["Zay Flowers"], None, ["hamstring"], None, None, False,
    ),
    (
        "Status alert: Saquon Barkley (leg) limped off field Sunday.",
        "in_game_injury", "in_game_7", ["Saquon Barkley"], None, ["leg"], None, None, False,
    ),
    (
        "Status alert: Caleb Williams suffered a hamstring injury according to the Bears.",
        "in_game_injury", "in_game_9", ["Caleb Williams"], None, [None], None, None, False,
    ),
    (
        "CB Nate Wiggins carted off field with apparent lower-body injury, per @jonas_shaffer.",
        "in_game_injury", "in_game_any_0", ["Nate Wiggins"], None, [None], None, None, False,
    ),
    (
        "Zay Flowers (hamstring) officially doesn't practice again Thursday.",
        "practice_dnp", "no_practice", ["Zay Flowers"], None, ["hamstring"], None, None, False,
    ),
    (
        "Brock Bowers (knee) officially limited in practice Wednesday.",
        "practice_limited", "limited", ["Brock Bowers"], None, ["knee"], None, None, False,
    ),
    (
        "Michael Penix Jr. (knee), Cooper Rush (back) fully practice again Thursday.",
        "practice_full", "fully_practices", ["Michael Penix Jr.", "Cooper Rush"], None,
        ["knee", "back"], None, None, False,
    ),
    (
        "Sam Darnold, Jadarian Price won't play preseason Week 1.\n\n"
        'Price is "on the mend," per Mike Macdonald.',
        "out", "wont_play", ["Sam Darnold", "Jadarian Price"], None, [None, None], None, 1, True,
    ),
    (
        "Bowles: Baker Mayfield (right thumb) expected to miss at least 3 weeks.",
        "out", "will_miss", ["Baker Mayfield"], None, ["right thumb"], "Bowles", None, False,
    ),
    (
        "Puka Nacua (hip, groin) doubtful for Week 3, per @sarahbarshop.",
        "doubtful", "doubtful", ["Puka Nacua"], None, ["hip, groin"], None, 3, False,
    ),
    (
        "Rap: Puka Nacua (groin, hip) unlikely to play Week 3.",
        "doubtful", "unlikely", ["Puka Nacua"], None, ["groin, hip"], "Rap", 3, False,
    ),
    (
        "DJ Moore (shoulder), Keon Coleman (ankle) questionable for Week 3.",
        "questionable", "questionable", ["DJ Moore", "Keon Coleman"], None,
        ["shoulder", "ankle"], None, 3, False,
    ),
    (
        "Michael Pittman Jr. (foot) listed questionable for Week 2.",
        "questionable", "questionable", ["Michael Pittman Jr."], None, ["foot"], None, 2, False,
    ),
    (
        'Garafolo: Zay Flowers (hamstring) remains a game-time decision for Week 3.',
        "questionable", "game_time_decision", ["Zay Flowers"], None, ["hamstring"],
        "Garafolo", 3, False,
    ),
    (
        'TreVeyon Henderson (ankle) should be "good to go" for Week 1, per @cpriceglobe.',
        "probable", "good_to_go", ["TreVeyon Henderson"], None, ["ankle"], None, 1, False,
    ),
    (
        "Chuba Hubbard (hamstring) not listed on injury report for Week 1.",
        "probable", "not_on_report", ["Chuba Hubbard"], None, ["hamstring"], None, 1, False,
    ),
    (
        "Tyson Bagent (concussion) officially active for Week 3.\n\nExpected to back up Case Keenum.",
        "active", "active", ["Tyson Bagent"], None, ["concussion"], None, 3, False,
    ),
    (
        "Terrance Ferguson (ankle) placed on IR; will miss at least 4 games.",
        "ir", "placed_on_ir", ["Terrance Ferguson"], None, ["ankle"], None, None, False,
    ),
    (
        "Bud Clark (ankle) placed on season-ending IR.",
        "ir", "placed_on_ir", ["Bud Clark"], None, ["ankle"], None, None, False,
    ),
    (
        "Status alert: Omar Cooper Jr. (ankle) carted to locker room Sunday.\n\n"
        "Questionable to return, per @AKinkhabwala.",
        "in_game_injury", "in_game_0", ["Omar Cooper Jr."], None, ["ankle"], None, None, False,
    ),
    (
        "Status alert: Rashee Rice (hamstring) questionable to return Sunday.",
        "in_game_injury", "in_game_2", ["Rashee Rice"], None, ["hamstring"], None, None, False,
    ),
    (
        "Tua Tagovailoa (oblique) won't practice Wednesday.",
        "practice_dnp", "no_practice", ["Tua Tagovailoa"], None, ["oblique"], None, None, False,
    ),
    (
        "49ers DT Gracen Halton (ankle), RG Dominick Puni (concussion) didn't practice Sunday.",
        "practice_dnp", "no_practice", ["Gracen Halton", "Dominick Puni"], "49ers",
        ["ankle", "concussion"], None, None, False,
    ),
    (
        "RJ Harvey (hamstring) limited in practice Thursday.",
        "practice_limited", "limited", ["RJ Harvey"], None, ["hamstring"], None, None, False,
    ),
    (
        "Patrick Mahomes (knee) officially fully practices Thursday.",
        "practice_full", "fully_practices", ["Patrick Mahomes"], None, ["knee"], None, None,
        False,
    ),
    (
        "Falcons LB Jalon Walker (left leg) carted off practice field on Tuesday.",
        "practice_injury", "left_practice", ["Jalon Walker"], "Falcons", ["left leg"], None,
        None, False,
    ),
    (
        "Schefter: Josh Johnson to be released by the Bengals.",
        "transaction", "transaction_1", ["Josh Johnson"], "Bengals", [None], "Schefter", None,
        False,
    ),
    (
        "Rap: DT Keeanu Benton to sign a 4-year, $72M extension with the Steelers.",
        "transaction", "transaction_0", ["Keeanu Benton"], "Steelers", [None], "Rap", None, False,
    ),
    (
        "Garafolo: Sterling Shepard and Zay Jones to sign with the Texans.",
        "transaction", "transaction_0", ["Sterling Shepard", "Zay Jones"], "Texans",
        [None, None], "Garafolo", None, False,
    ),
    (
        "Commanders EDGE Dorance Armstrong suspended 1 game for violating Personal Conduct "
        "Policy.",
        "suspended", "suspended", ["Dorance Armstrong"], "Commanders", [None], None, None, False,
    ),
]  # fmt: skip


@pytest.mark.parametrize(
    ("text", "status", "template", "players", "team", "injuries", "source", "week", "pre"),
    CASES,
    ids=[c[0][:40] for c in CASES],
)
def test_real_posts(
    text: str,
    status: str,
    template: str,
    players: list[str],
    team: str | None,
    injuries: list[str | None],
    source: str | None,
    week: int | None,
    pre: bool,
) -> None:
    c = classify(text)
    assert (c.status, c.template, c.players, c.team) == (status, template, players, team)
    assert (c.injuries, c.source, c.week, c.preseason) == (injuries, source, week, pre)


# Real posts the parser must not classify: hedged reports, stat lines, quotes, analysis.
UNKNOWN = [
    "Taylor: Ja'Marr Chase remains in concussion protocol.",
    "Schefter: Kyle Monangai (knee) believed to be “fine;” will undergo MRI.",
    "Pelissero: There's optimism Micah Parsons (knee) can return Week 6 against the Cowboys "
    'and "potentially" even Week 5 against the Bears.',
    'Taylor: Ja\'Marr Chase (knee), Tee Higgins (heel) will be "good" by end of week.',
    "Schefter: Giants LT Andrew Thomas (groin) has chance to play Week 3.",
    "Schefter: George Kittle (Achilles) recovery is ahead of schedule; could play Week 1.",
    "McVay: Rams hopeful Puka Nacua (hip) plays Week 3.",
    "Deshaun Watson today:\n\n238 passing yards\n2 TDs, 0 INT\n121.9 passer rating",
    "Alec Pierce (heel) leaving the stadium in a boot and using crutches.",
    "Brock Bowers (knee) seen participating in practice Friday.",
    "Welcome to the Hall of Fame, Luke Kuechly. https://t.co/5heieDv3qp",
    "Sean McVay on if Aaron Donald will play Week 2, via @TaylorBisciotti:\n\n"
    '"Haven\'t decided yet."',
    "",
]


@pytest.mark.parametrize("text", UNKNOWN, ids=[t[:40] or "empty" for t in UNKNOWN])
def test_never_guesses(text: str) -> None:
    c = classify(text)
    assert c.status == "unknown"
    assert c.players == [] and c.team is None and c.template is None
