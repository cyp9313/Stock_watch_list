from kline_indicators import default_indicator_settings
import multiuser_store
from multiuser_store import config_to_api_groups, normalize_config
from short_term_watchlist import default_short_term_watchlist


def test_normalize_config_adds_empty_portfolio_pages_for_legacy_configs():
    config = normalize_config({
        "stocks_pages": [{"name": "Stocks", "groups": {"Core": ["AAPL"]}}],
        "broad_pages": [{"name": "Macro", "groups": {"Index": ["^GSPC"]}}],
    })

    page = config["portfolio_pages"][0]
    assert page["name"] == "Portfolio"
    assert page["holdings"] == []
    assert page["id"].startswith("pf_")
    assert page["analysis_settings"]["base_currency"] == "EUR"
    assert page["analysis_settings"]["benchmark"] == "^GSPC"
    assert config["kline_indicator_settings"] == default_indicator_settings()
    assert config["short_term_watchlist"] == default_short_term_watchlist()


def test_normalize_config_preserves_portfolio_id_and_settings():
    config = normalize_config({
        "portfolio_pages": [{
            "id": "pf_custom",
            "name": "Growth",
            "analysis_settings": {
                "base_currency": "USD",
                "benchmark": "SXR8.DE",
                "risk_profile": "growth",
                "max_focus_holdings": 3,
            },
            "holdings": [],
        }]
    })

    page = config["portfolio_pages"][0]
    assert page["id"] == "pf_custom"
    assert page["analysis_settings"]["base_currency"] == "USD"
    assert page["analysis_settings"]["benchmark"] == "SXR8.DE"
    assert page["analysis_settings"]["risk_profile"] == "growth"
    assert page["analysis_settings"]["max_focus_holdings"] == 3


def test_normalize_config_keeps_valid_saved_kline_indicator_settings():
    saved = default_indicator_settings()
    saved["moving_averages"][0] = {"period": 8, "type": "EMA"}
    saved["rsi"] = {"period": 21}
    saved["fibonacci"]["retracement"] = {"enabled": True, "deviation": 4.5, "depth": 18}
    saved["fibonacci"]["extension"] = {"enabled": True, "depth": 24}

    config = normalize_config({"kline_indicator_settings": saved})

    assert config["kline_indicator_settings"] == saved


def test_kline_indicator_settings_persist_per_account(tmp_path, monkeypatch):
    monkeypatch.setattr(multiuser_store, "USER_DB_PATH", str(tmp_path / "users.db"))
    multiuser_store.create_user("indicator_a", "password-a")
    multiuser_store.create_user("indicator_b", "password-b")

    user_a = multiuser_store.authenticate("indicator_a", "password-a")
    user_b = multiuser_store.authenticate("indicator_b", "password-b")
    config_a = multiuser_store.get_user_config(user_a["id"])
    saved = default_indicator_settings()
    saved["moving_averages"][1] = {"period": 13, "type": "EMA"}
    saved["fibonacci"]["retracement"] = {"enabled": True, "deviation": 2.5, "depth": 12}
    saved["fibonacci"]["extension"] = {"enabled": True, "depth": 16}
    config_a["kline_indicator_settings"] = saved
    multiuser_store.save_user_config(user_a["id"], config_a)

    assert multiuser_store.get_user_config(user_a["id"])["kline_indicator_settings"] == saved
    assert multiuser_store.get_user_config(user_b["id"])["kline_indicator_settings"] == default_indicator_settings()


def test_short_term_watchlist_settings_persist_per_account(tmp_path, monkeypatch):
    monkeypatch.setattr(multiuser_store, "USER_DB_PATH", str(tmp_path / "users.db"))
    multiuser_store.create_user("short_term_a", "password-a")
    multiuser_store.create_user("short_term_b", "password-b")
    user_a = multiuser_store.authenticate("short_term_a", "password-a")
    user_b = multiuser_store.authenticate("short_term_b", "password-b")

    config_a = multiuser_store.get_user_config(user_a["id"])
    short_term = default_short_term_watchlist()
    short_term["groups"] = {"Momentum": ["AAPL", "MSFT"]}
    short_term["settings"]["ma_1"] = {"period": 5, "type": "SMA"}
    short_term["refresh"] = {"enabled": True, "interval_seconds": 20}
    short_term["alerts"] = {
        "enabled": True,
        "intervals": {"5m": True, "15m": False},
        "near_enabled": True,
        "confirmed_enabled": False,
        "duration_seconds": 30,
        "signals": {
            "macd": {"enabled": True, "threshold": 5.0},
            "ema": {"enabled": True, "threshold": 3.0},
            "bollinger": {"enabled": False, "threshold": 10.0},
            "vwap": {"enabled": False, "threshold": 5.0},
            "rsi": {"enabled": False, "threshold": 2.0},
        },
        "ticker_enabled": {"AAPL": False},
    }
    config_a["short_term_watchlist"] = short_term
    multiuser_store.save_user_config(user_a["id"], config_a)

    assert multiuser_store.get_user_config(user_a["id"])["short_term_watchlist"] == short_term
    assert multiuser_store.get_user_config(user_b["id"])["short_term_watchlist"] == default_short_term_watchlist()


def test_portfolio_tickers_are_included_in_stock_data_api_groups():
    config = normalize_config({
        "stocks_pages": [{"name": "Stocks", "groups": {"Core": ["AAPL"]}}],
        "broad_pages": [{"name": "Macro", "groups": {"Index": ["^GSPC"]}}],
        "portfolio_pages": [{
            "name": "My Portfolio",
            "holdings": [
                {
                    "group": "Longs",
                    "ticker": "TSM",
                    "buy_price": 100,
                    "shares": 2,
                    "buy_currency": "USD",
                }
            ],
        }],
    })

    groups = config_to_api_groups(config)

    page_id = config["portfolio_pages"][0]["id"]
    assert groups[f"P:{page_id}:My Portfolio:Longs"] == ["TSM"]


def test_short_term_tickers_are_included_for_shared_daily_metrics():
    config = normalize_config({
        "short_term_watchlist": {"groups": {"Momentum": ["AAPL", "BTC-USD"]}},
    })

    assert config_to_api_groups(config)["ST:Momentum"] == ["AAPL", "BTC-USD"]
