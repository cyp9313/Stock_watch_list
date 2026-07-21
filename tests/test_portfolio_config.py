from multiuser_store import config_to_api_groups, normalize_config


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
