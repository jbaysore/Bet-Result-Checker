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


def test_first_play_wallclock_normalizes_offsets_and_uses_earliest():
    actual = actual_start.first_play_wallclock({"plays": [
        {"wallclock": "2026-07-11T00:06:10Z"},
        {"wallclock": "2026-07-10T19:05:40-05:00"},
    ]})
    assert actual == datetime(2026, 7, 11, 0, 5, 40, tzinfo=timezone.utc)


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
