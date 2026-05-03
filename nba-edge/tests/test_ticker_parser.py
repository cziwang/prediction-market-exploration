from nba_edge.data.ticker_parser import parse_ticker, is_game_winner


def test_game_winner_ticker():
    p = parse_ticker("KXNBAGAME-26APR20MINDEN-MIN")
    assert p.series == "GAME"
    assert p.game_date == "2026-04-20"
    assert p.away_team == "MIN"
    assert p.home_team == "DEN"
    assert p.selection == "MIN"
    assert p.threshold is None


def test_spread_ticker():
    p = parse_ticker("KXNBASPREAD-26APR20MINDEN-DEN7")
    assert p.series == "SPREAD"
    assert p.game_date == "2026-04-20"
    assert p.away_team == "MIN"
    assert p.home_team == "DEN"
    assert p.selection == "DEN"
    assert p.threshold == 7


def test_total_ticker():
    p = parse_ticker("KXNBATOTAL-26APR20MINDEN-239")
    assert p.series == "TOTAL"
    assert p.game_date == "2026-04-20"
    assert p.away_team == "MIN"
    assert p.home_team == "DEN"
    assert p.threshold == 239


def test_player_prop_ticker():
    p = parse_ticker("KXNBAPTS-26APR25OKCPHX-PHXDBROOKS3-25")
    assert p.series == "PTS"
    assert p.game_date == "2026-04-25"
    assert p.away_team == "OKC"
    assert p.home_team == "PHX"
    assert p.selection == "PHXDBROOKS3"
    assert p.threshold == 25


def test_game_winner_other_team():
    p = parse_ticker("KXNBAGAME-26APR18ATLNYK-ATL")
    assert p.series == "GAME"
    assert p.game_date == "2026-04-18"
    assert p.away_team == "ATL"
    assert p.home_team == "NYK"
    assert p.selection == "ATL"


def test_is_game_winner():
    assert is_game_winner("KXNBAGAME-26APR20MINDEN-MIN")
    assert not is_game_winner("KXNBASPREAD-26APR20MINDEN-DEN7")
    assert not is_game_winner("KXNBAPTS-26APR25OKCPHX-PHXDBROOKS3-25")


def test_unknown_ticker():
    p = parse_ticker("SOMETHINGELSE-FOO-BAR")
    assert p.series == "UNKNOWN"
