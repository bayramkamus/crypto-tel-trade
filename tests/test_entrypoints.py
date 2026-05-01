import py_compile
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

RUNTIME_FILES = [
    "main.py",
    "run_collector.py",
    "collector.py",
    "config.py",
    "backtest_context.py",
    "backtest_signals.py",
    "backtest_trends.py",
    "generate_report.py",
    "ml_analysis.py",
    "ohlcv_collector.py",
    "indicator_engine.py",
    "feature_builder.py",
    "decision_model.py",
    "model_manager.py",
    "excel_styles.py",
    "exchanges.py",
    "live_report.py",
    "live_decision.py",
    "chart_pattern_live.py",
    "relay/client.py",
    "scraping/ai_classify.py",
    "scraping/coin_resolver.py",
    "scraping/collect.py",
    "scraping/db.py",
    "scraping/dedup.py",
    "scraping/source_resolver.py",
    "scraping/ticker_parser.py",
    "scraping/collectors/events.py",
    "scraping/collectors/news.py",
    "scraping/collectors/trends.py",
]


def test_runtime_files_compile():
    compiled_files = []
    try:
        for rel_path in RUNTIME_FILES:
            safe_name = rel_path.replace("/", "_").replace("\\", "_")
            cfile = ROOT / f".compile_{safe_name}.pyc"
            py_compile.compile(str(ROOT / rel_path), cfile=str(cfile), doraise=True)
            compiled_files.append(cfile)
    finally:
        for cfile in compiled_files:
            try:
                cfile.unlink()
            except FileNotFoundError:
                pass


def _run_help(script_name: str):
    return subprocess.run(
        [sys.executable, str(ROOT / script_name), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_main_help_smoke():
    result = _run_help("main.py")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "usage:" in result.stdout.lower()


def test_run_collector_help_smoke():
    result = _run_help("run_collector.py")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "usage:" in result.stdout.lower()


def test_live_report_includes_decision_block():
    from live_report import generate_report_html

    html = generate_report_html(
        ticker="BTC",
        channel="test_channel",
        message_text="BTC LONG",
        scraping_result={"resolved": True, "news": 1, "events": 2, "trends": 0},
        volume_data={"volume_ratio": 2.5, "is_anomaly": True},
        decision_data={
            "status": "ok",
            "action": "AL",
            "predicted": "STRONG_WIN",
            "model_decision": "EXECUTE",
            "confidence": 76.4,
            "probabilities": {"STRONG_WIN": 0.764, "WEAK_WIN": 0.12, "LOSS": 0.116},
            "reliability": {"label": "ORTA", "score": 66.2},
            "feature_coverage": {"available": 32, "total": 49, "percent": 65.3},
            "model": {"f1": 56.3, "accuracy": 57.2, "n_samples": 1134},
            "snapshots": {
                "5m": {
                    "rsi_14": 61.2,
                    "macd_histogram": 0.0012,
                    "macd_cross": 1,
                    "bb_pctb": 0.72,
                    "ema_alignment": 3,
                    "price_vs_ema200": 1.8,
                    "volume_ratio": 2.2,
                }
            },
            "snapshot_meta": {"5m": {"source": "binance_spot", "candles": 300}},
        },
    )

    assert "Karar Analizi" in html
    assert "AL" in html
    assert "Indikator Snapshotlari" in html


def test_live_report_includes_chart_pattern_block():
    from live_report import generate_report_html

    html = generate_report_html(
        ticker="BTC",
        channel="test_channel",
        message_text="BTC LONG",
        chart_pattern_data={
            "status": "ok",
            "action": "AL",
            "signal": "BUY",
            "signal_confidence": 0.81,
            "detected_pattern": "ascending_triangle",
            "pattern_confidence": 0.73,
            "signal_from_pattern_rule": "BUY",
            "heads_agree": True,
            "source": "binance_spot",
            "candles": 96,
            "image_cid": "chart_pattern_test",
        },
    )

    assert "15m Chart Pattern" in html
    assert "Ascending Triangle" in html
    assert "cid:chart_pattern_test" in html
