from scraping.ticker_parser import extract_ticker


def test_extract_ticker_from_symbol_direction_format():
    assert extract_ticker("Diamond ALGO - LONG Entry Point: 0.1106") == "ALGO"


def test_extract_ticker_from_markdown_coin_hash_usdt_format():
    text = "**Coin : ****#SOLV****/USDT SHORT Entry: 0.004835**"
    assert extract_ticker(text) == "SOLV"


def test_extract_ticker_from_plain_hash_research_format():
    assert extract_ticker("#STORJ has found support at the lower border") == "STORJ"


def test_extract_ticker_keeps_noise_words_filtered():
    assert extract_ticker("#AI Signal is trending") is None

