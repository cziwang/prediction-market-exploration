"""Tests for pm.kalshi.ticker."""

from pm.kalshi.ticker import parse_nba_ticker


class TestGameTickers:
    def test_home_side(self) -> None:
        p = parse_nba_ticker("KXNBAGAME-26APR18ATLNYK-NYK")
        assert p is not None
        assert p.series == "KXNBAGAME"
        assert p.event_code == "26APR18ATLNYK"
        assert p.away == "ATL"
        assert p.home == "NYK"
        assert p.yes_is_home is True

    def test_away_side(self) -> None:
        p = parse_nba_ticker("KXNBAGAME-26APR18ATLNYK-ATL")
        assert p is not None
        assert p.yes_is_home is False

    def test_yes_side_not_in_matchup_rejected(self) -> None:
        assert parse_nba_ticker("KXNBAGAME-26APR18ATLNYK-BOS") is None


class TestOtherSeries:
    def test_total(self) -> None:
        p = parse_nba_ticker("KXNBATOTAL-26MAY02PHIBOS-205")
        assert p is not None
        assert p.series == "KXNBATOTAL"
        assert p.event_code == "26MAY02PHIBOS"
        assert p.yes_is_home is None  # model prob not applicable

    def test_spread(self) -> None:
        p = parse_nba_ticker("KXNBASPREAD-26APR18ATLNYK-NYK15")
        assert p is not None
        assert p.yes_is_home is None


class TestGarbage:
    def test_non_nba(self) -> None:
        assert parse_nba_ticker("KXBTC-26APR18-50000") is None

    def test_empty(self) -> None:
        assert parse_nba_ticker("") is None
