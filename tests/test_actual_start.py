from datetime import datetime, timezone

import actual_start


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def espn_event(event_id="1", team1="Fever", team2="Aces"):
    return {
        "id": event_id,
        "competitions": [{"competitors": [
            {"team": {"displayName": team1}},
            {"team": {"displayName": team2}},
        ]}],
    }


def tennis_event(*, competition_id="177490", date="2026-07-10T15:20Z",
                 player1="Novak Djokovic", player2="Jannik Sinner",
                 grouping_slug="mens-singles", completed=True, time_valid=True):
    return {
        "groupings": [{
            "grouping": {"slug": grouping_slug},
            "competitions": [{
                "id": competition_id,
                "date": date,
                "timeValid": time_valid,
                "status": {"type": {"completed": completed}},
                "competitors": [
                    {"athlete": {"displayName": player1}},
                    {"athlete": {"displayName": player2}},
                ],
            }],
        }],
    }


def combat_bout(*, event_id="600059148", competition_id="401867788",
                fighter1="Max Holloway", fighter2="Conor McGregor", completed=True):
    return {
        "id": competition_id,
        "_event_id": event_id,
        "status": {"type": {"completed": completed, "name": "STATUS_FINAL" if completed else "STATUS_SCHEDULED"}},
        "competitors": [
            {"athlete": {"displayName": fighter1}},
            {"athlete": {"displayName": fighter2}},
        ],
    }


def round_start(wallclock="2026-07-12T03:39:33Z", period=1, text="Round Start"):
    return {
        "wallclock": wallclock,
        "period": {"number": period},
        "type": {"text": text},
    }


def test_first_play_wallclock_normalizes_offsets_and_uses_earliest():
    actual = actual_start.first_play_wallclock({"plays": [
        {"wallclock": "2026-07-11T00:06:10Z"},
        {"wallclock": "2026-07-10T19:05:40-05:00"},
    ]})
    assert actual == datetime(2026, 7, 11, 0, 5, 40, tzinfo=timezone.utc)


def test_first_play_wallclock_reads_soccer_keyevents():
    # Soccer summaries carry plays: [] and put the kickoff wallclock in
    # keyEvents. The earliest event (kickoff) must win even though later goals
    # appear first in the array.
    actual = actual_start.first_play_wallclock({
        "plays": [],
        "keyEvents": [
            {"type": {"type": "goal"}, "wallclock": "2026-07-14T19:38:00Z"},
            {"type": {"type": "kickoff"}, "wallclock": "2026-07-14T19:00:07Z"},
        ],
    })
    assert actual == datetime(2026, 7, 14, 19, 0, 7, tzinfo=timezone.utc)


def test_espn_team_match_uses_explicit_country_aliases():
    assert actual_start._team_matches("DR Congo", "Congo DR")
    assert actual_start._team_matches("Czech Republic", "Czechia")
    assert actual_start._team_matches("Caty McNally", "Catherine McNally")


def test_tennis_resolver_uses_completed_match_start(monkeypatch):
    monkeypatch.setattr(
        actual_start, "_load_espn_tennis_events",
        lambda *args: ([tennis_event()], ""),
    )
    result = actual_start.resolve_espn_tennis_actual_start({
        "sport": "tennis_atp_wimbledon", "game_date": "7/10/2026",
        "team1": "Novak Djokovic", "team2": "Jannik Sinner",
    })
    assert result.confident
    assert result.actual_start == datetime(2026, 7, 10, 15, 20, tzinfo=timezone.utc)
    assert result.event_id == "177490"
    assert result.source == "espn-tennis-competition-start"


def test_tennis_resolver_requires_completed_time_valid_match(monkeypatch):
    monkeypatch.setattr(
        actual_start, "_load_espn_tennis_events",
        lambda *args: ([tennis_event(completed=False)], ""),
    )
    result = actual_start.resolve_espn_tennis_actual_start({
        "sport": "tennis_atp_wimbledon", "game_date": "7/10/2026",
        "team1": "Novak Djokovic", "team2": "Jannik Sinner",
    })
    assert result.actual_start is None
    assert "match count 0" in result.error


def test_tennis_resolver_restricts_draw_and_refuses_ambiguity(monkeypatch):
    wrong_draw = tennis_event(grouping_slug="womens-singles")
    duplicate = tennis_event(competition_id="177491")
    monkeypatch.setattr(
        actual_start, "_load_espn_tennis_events",
        lambda *args: ([wrong_draw, tennis_event(), duplicate], ""),
    )
    result = actual_start.resolve_espn_tennis_actual_start({
        "sport": "tennis_atp_wimbledon", "game_date": "7/10/2026",
        "team1": "Novak Djokovic", "team2": "Jannik Sinner",
    })
    assert result.actual_start is None
    assert "match count 2" in result.error


def test_espn_resolver_returns_confident_soccer_kickoff(monkeypatch):
    responses = iter([
        FakeResponse({"events": [espn_event(team1="Spain", team2="France")]}),
        FakeResponse({"plays": [], "keyEvents": [
            {"type": {"type": "kickoff"}, "wallclock": "2026-07-14T19:00:07Z"},
        ]}),
    ])
    monkeypatch.setattr(actual_start.requests, "get", lambda *args, **kwargs: next(responses))
    result = actual_start.resolve_espn_actual_start({
        "sport": "soccer_fifa_world_cup", "game_date": "7/14/2026",
        "team1": "Spain", "team2": "France",
    })
    assert result.confident
    assert result.actual_start == datetime(2026, 7, 14, 19, 0, 7, tzinfo=timezone.utc)
    assert result.source == "espn-first-play"


def test_efl_cup_resolver_uses_espn_league_and_accepts_afc_suffix(monkeypatch):
    calls = []
    responses = iter([
        FakeResponse({"events": [espn_event(
            event_id="401881129", team1="Wrexham", team2="Middlesbrough",
        )]}),
        FakeResponse({"plays": [], "keyEvents": [
            {"type": {"type": "kickoff"}, "wallclock": "2026-08-07T19:00:08Z"},
        ]}),
    ])

    def fake_get(url, *args, **kwargs):
        calls.append((url, kwargs.get("params") or {}))
        return next(responses)

    monkeypatch.setattr(actual_start.requests, "get", fake_get)
    result = actual_start.resolve_actual_start({
        "sport": "soccer_england_efl_cup", "game_date": "8/7/2026",
        "team1": "Wrexham AFC", "team2": "Middlesbrough",
    })
    assert result.confident
    assert result.actual_start == datetime(2026, 8, 7, 19, 0, 8, tzinfo=timezone.utc)
    assert result.event_id == "401881129"
    assert "/soccer/eng.league_cup/scoreboard" in calls[0][0]


def test_espn_resolver_returns_confident_delayed_first_play(monkeypatch):
    responses = iter([
        FakeResponse({"events": [espn_event()]}),
        FakeResponse({"plays": [{"wallclock": "2026-07-11T00:12:00Z"}]}),
    ])
    monkeypatch.setattr(actual_start.requests, "get", lambda *args, **kwargs: next(responses))
    result = actual_start.resolve_espn_actual_start({
        "sport": "basketball_wnba", "game_date": "7/10/2026",
        "team1": "Indiana Fever", "team2": "Las Vegas Aces",
    })
    assert result.confident
    assert result.actual_start == datetime(2026, 7, 11, 0, 12, tzinfo=timezone.utc)
    assert result.source == "espn-first-play"


def test_espn_resolver_handles_nba_via_plays(monkeypatch):
    responses = iter([
        FakeResponse({"events": [espn_event(team1="Spurs", team2="Knicks")]}),
        FakeResponse({"plays": [{"wallclock": "2026-06-04T00:44:26Z"}], "keyEvents": []}),
    ])
    monkeypatch.setattr(actual_start.requests, "get", lambda *args, **kwargs: next(responses))
    result = actual_start.resolve_actual_start({
        "sport": "basketball_nba", "game_date": "6/3/2026",
        "team1": "San Antonio Spurs", "team2": "New York Knicks",
    })
    assert result.confident
    assert result.actual_start == datetime(2026, 6, 4, 0, 44, 26, tzinfo=timezone.utc)
    assert result.source == "espn-first-play"


def test_espn_resolver_refuses_ambiguous_doubleheader(monkeypatch):
    monkeypatch.setattr(actual_start.requests, "get", lambda *args, **kwargs: FakeResponse({
        "events": [espn_event("1"), espn_event("2")],
    }))
    result = actual_start.resolve_espn_actual_start({
        "sport": "basketball_wnba", "game_date": "7/10/2026",
        "team1": "Fever", "team2": "Aces",
    })
    assert result.actual_start is None
    assert "match count 2" in result.error


def test_espn_resolver_leaves_missing_first_play_unresolved(monkeypatch):
    responses = iter([FakeResponse({"events": [espn_event()]}), FakeResponse({"plays": []})])
    monkeypatch.setattr(actual_start.requests, "get", lambda *args, **kwargs: next(responses))
    result = actual_start.resolve_espn_actual_start({
        "sport": "basketball_wnba", "game_date": "7/10/2026",
        "team1": "Fever", "team2": "Aces",
    })
    assert result.actual_start is None
    assert "unavailable" in result.error


def test_combat_resolver_uses_round_one_start_not_card_time(monkeypatch):
    monkeypatch.setattr(actual_start.espn_fights, "_fetch_scoreboard", lambda *_: [combat_bout()])
    monkeypatch.setattr(
        actual_start.espn_fights, "_fetch_bout_plays",
        lambda *args: [
            {"wallclock": "2026-07-12T03:23:54Z", "type": {"text": "Fight Open"}, "period": {"number": 0}},
            round_start(),
            round_start("2026-07-12T03:44:33Z", period=2),
        ],
    )
    result = actual_start.resolve_actual_start({
        "sport": "mma_mixed_martial_arts", "game_date": "7/11/2026",
        "team1": "Max Holloway", "team2": "Conor McGregor",
    })
    assert result.confident
    assert result.actual_start == datetime(2026, 7, 12, 3, 39, 33, tzinfo=timezone.utc)
    assert result.source == "espn-ufc-round-one-start"
    assert result.event_id == "401867788"


def test_combat_resolver_requires_unique_round_one_start(monkeypatch):
    monkeypatch.setattr(actual_start.espn_fights, "_fetch_scoreboard", lambda *_: [combat_bout()])
    monkeypatch.setattr(
        actual_start.espn_fights, "_fetch_bout_plays",
        lambda *args: [round_start(), round_start("2026-07-12T03:39:34Z")],
    )
    result = actual_start.resolve_espn_combat_actual_start({
        "sport": "mma_mixed_martial_arts", "game_date": "7/11/2026",
        "team1": "Max Holloway", "team2": "Conor McGregor",
    })
    assert result.actual_start is None
    assert "start count 2" in result.error


def test_boxing_has_no_combat_start_route():
    result = actual_start.resolve_actual_start({
        "sport": "boxing_boxing", "game_date": "6/20/2026",
        "team1": "Michael Magnesi", "team2": "Ryan Garner",
    })
    assert result.actual_start is None
    assert "no actual-start resolver" in result.error


def test_mlb_resolver_prefers_hydrated_first_pitch(monkeypatch):
    game = {"gamePk": 42, "gameInfo": {"firstPitch": "2026-07-11T00:06:00Z"}}
    monkeypatch.setattr(actual_start.mlb_statsapi, "get_schedule_games", lambda *_: [game])
    monkeypatch.setattr(actual_start.mlb_statsapi, "find_game", lambda *args: game)
    monkeypatch.setattr(actual_start.mlb_statsapi, "get_game_feed_live", lambda *_: (_ for _ in ()).throw(
        AssertionError("hydrate should avoid feed/live")
    ))
    result = actual_start.resolve_mlb_actual_start({
        "game_date": "7/10/2026", "team1": "Cubs", "team2": "Mets",
    })
    assert result.confident
    assert result.event_id == "42"
    assert result.actual_start == datetime(2026, 7, 11, 0, 6, tzinfo=timezone.utc)


def test_mlb_postponement_without_first_pitch_stays_unresolved(monkeypatch):
    game = {"gamePk": 43, "status": {"detailedState": "Postponed"}}
    monkeypatch.setattr(actual_start.mlb_statsapi, "get_schedule_games", lambda *_: [game])
    monkeypatch.setattr(actual_start.mlb_statsapi, "find_game", lambda *args: game)
    monkeypatch.setattr(actual_start.mlb_statsapi, "get_game_feed_live", lambda *_: {})
    result = actual_start.resolve_mlb_actual_start({
        "game_date": "7/10/2026", "team1": "Cubs", "team2": "Mets",
    })
    assert result.actual_start is None
    assert result.confidence == "UNRESOLVED"
