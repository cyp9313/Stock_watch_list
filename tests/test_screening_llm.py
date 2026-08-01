import screening_llm


def _strategy():
    return {
        "label": "趋势质量", "description": "test",
        "candidates": [
            {"Ticker": "AAPL", "Score": 90, "Factor Scores": {}, "Risk Tags": [], "Metrics": {}},
            {"Ticker": "MSFT", "Score": 88, "Factor Scores": {}, "Risk Tags": [], "Metrics": {}},
        ],
    }


def test_rerank_accepts_only_a_reordering_of_original_candidates(monkeypatch):
    monkeypatch.setattr(screening_llm, "_call_model", lambda *_: '{"ranked_tickers":["MSFT","AAPL"],"items":[{"ticker":"MSFT","reason":"动量更强","risk":"高位波动"},{"ticker":"AAPL","reason":"趋势稳定","risk":"估值风险"}]}')
    result = screening_llm.rerank_candidates(_strategy())
    assert result["ok"] is True
    assert result["ranking"] == ["MSFT", "AAPL"]


def test_rerank_rejects_model_added_ticker(monkeypatch):
    monkeypatch.setattr(screening_llm, "_call_model", lambda *_: '{"ranked_tickers":["AAPL","NVDA"],"items":[{"ticker":"AAPL","reason":"x","risk":"x"},{"ticker":"NVDA","reason":"x","risk":"x"}]}')
    result = screening_llm.rerank_candidates(_strategy())
    assert result["ok"] is False
    assert "原始候选" in result["error"]
