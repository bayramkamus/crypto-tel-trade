#!/usr/bin/env python3
"""
Telegram Sinyal Analiz Pipeline
=================================
Tek komutla tum pipeline'i calistirir:

  1. Gunluk rapor  → pump_research_daily.xlsx
  2. Backtest       → Binance fiyat verisi (inkremental)  → backtest_signals.xlsx
  3. Trend          → Google Trends zenginlestirme (inkremental, pytrends gerekli)
  4. Context        → Fear & Greed Index zenginlestirme
  5. ML Analizi     → market cap + karar agaci            → ml_analysis.xlsx
  6. OHLCV Toplama  → Tarihsel mum verisi (inkremental)   → ohlcv_data.db
  7. İndikatör      → RSI, MACD, BB, EMA, OBV snapshot    → backtest_results.db
  8. Özellik Birleştirme → Confluence score + MTF agreement
  9. Karar Modeli   → RF/GBT model eğitimi + rapor        → decision_analysis.xlsx
 10. Serving Model  → GBT model yeniden eğitip pkl kaydet → models/decision_model.pkl

Kullanim:
    python main.py                         # her seyi calistir
    python main.py --skip-report           # rapor atlat
    python main.py --skip-backtest         # backtest atlat
    python main.py --skip-trends           # Google Trends adimini atla
    python main.py --skip-context          # Fear & Greed adimini atla
    python main.py --skip-ml              # Eski ML analizini atla
    python main.py --skip-ohlcv           # OHLCV toplama atla
    python main.py --skip-indicators      # İndikatör hesaplama atla
    python main.py --skip-features        # Özellik birleştirme atla
    python main.py --skip-decision        # Karar modeli atla
    python main.py --skip-serving-model   # Serving model yenilemeyi atla
    python main.py --force                 # backtest onbellegi sifirla
    python main.py --force-trends          # trend onbellegi sifirla
    python main.py --force-context         # FnG onbellegi sifirla
    python main.py --force-ohlcv          # OHLCV verisi yeniden topla
    python main.py --force-indicators     # İndikatör snapshot sıfırla
    python main.py --force-features       # Özellik hesaplama sıfırla
    python main.py --force-all             # tum onbellekleri sifirla
    python main.py --no-marketcap          # CoinGecko market cap verisi atla
    python main.py --target win_4h         # karar agaci hedefi degistir
    python main.py --ohlcv-timeframes 1h 4h 1d  # Sadece belirli TF'ler
"""

import sys
import time
import logging
import argparse
from pathlib import Path

# ─────────────────────────────────────────────────────────────────
# VARSAYILAN YOLLAR
# ─────────────────────────────────────────────────────────────────

DB_PATH       = "pump_research.db"
BT_DB_PATH    = "backtest_results.db"
OHLCV_DB_PATH = "ohlcv_data.db"
REPORT_OUT    = "pump_research_daily.xlsx"
BACKTEST_OUT  = "backtest_signals.xlsx"
ML_OUT        = "ml_analysis.xlsx"
DECISION_OUT  = "decision_analysis.xlsx"

TOTAL_STEPS = 10


def banner():
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║         TELEGRAM SİNYAL ANALİZ PİPELINE v2.0               ║")
    print("║   Rapor → Backtest → Trend → Context → ML                  ║")
    print("║   → OHLCV → İndikatör → Özellik → Karar → Serving          ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Telegram sinyal analiz pipeline — tek komutla calistir")

    # Yollar
    parser.add_argument("--db",     default=DB_PATH,      help="Kaynak SQLite DB")
    parser.add_argument("--btdb",   default=BT_DB_PATH,   help="Backtest sonuc DB")
    parser.add_argument("--ohlcv-db", default=OHLCV_DB_PATH, help="OHLCV veri DB")
    parser.add_argument("--report-out",   default=REPORT_OUT)
    parser.add_argument("--backtest-out", default=BACKTEST_OUT)
    parser.add_argument("--ml-out",       default=ML_OUT)
    parser.add_argument("--decision-out", default=DECISION_OUT)

    # Skip kontrolleri
    parser.add_argument("--skip-report",     action="store_true",
                        help="Gunluk rapor uretmeyi atla")
    parser.add_argument("--skip-backtest",   action="store_true",
                        help="Backtest API cagrilarini atla")
    parser.add_argument("--skip-trends",     action="store_true",
                        help="Google Trends zenginlestirme adimini atla")
    parser.add_argument("--skip-context",    action="store_true",
                        help="Fear & Greed Index zenginlestirme adimini atla")
    parser.add_argument("--skip-ml",         action="store_true",
                        help="Eski ML analizini atla")
    parser.add_argument("--skip-ohlcv",      action="store_true",
                        help="OHLCV veri toplama adimini atla")
    parser.add_argument("--skip-indicators", action="store_true",
                        help="İndikatör hesaplama adimini atla")
    parser.add_argument("--skip-features",   action="store_true",
                        help="Özellik birleştirme adimini atla")
    parser.add_argument("--skip-decision",   action="store_true",
                        help="Karar modeli eğitimini atla")
    parser.add_argument("--skip-serving-model", action="store_true",
                        help="Serving model yenilemeyi atla")

    # Force kontrolleri
    parser.add_argument("--force",           action="store_true",
                        help="Backtest onbellegi sifirla")
    parser.add_argument("--force-trends",    action="store_true",
                        help="Trend onbellegi sifirla")
    parser.add_argument("--force-context",   action="store_true",
                        help="FnG onbellegi sifirla")
    parser.add_argument("--force-ohlcv",     action="store_true",
                        help="OHLCV verisi yeniden topla")
    parser.add_argument("--force-indicators", action="store_true",
                        help="İndikatör snapshot sıfırla")
    parser.add_argument("--force-features",  action="store_true",
                        help="Özellik hesaplama sıfırla")
    parser.add_argument("--force-all",       action="store_true",
                        help="Tum onbellekleri sifirla")

    # Diğer seçenekler
    parser.add_argument("--no-marketcap",   action="store_true",
                        help="CoinGecko market cap verisini atla")
    parser.add_argument("--target",         default="win_1h",
                        choices=["win_1h", "win_4h", "win_1d"],
                        help="Karar agaci hedef degiskeni")
    parser.add_argument("--ohlcv-timeframes", nargs="+",
                        default=["1m", "5m", "15m", "1h", "4h", "1d"],
                        help="OHLCV toplanacak timeframe'ler")
    parser.add_argument("--indicator-timeframes", nargs="+",
                        default=["5m", "15m", "1h", "4h", "1d"],
                        help="İndikatör hesaplanacak timeframe'ler")

    args = parser.parse_args()

    # Logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    banner()
    t0 = time.time()

    # ─── Force flag çözümlemesi ────────────────────────────────
    force_bt   = args.force or args.force_all
    force_tr   = args.force_trends or args.force_all
    force_ctx  = args.force_context or args.force_all
    force_ohlcv = args.force_ohlcv or args.force_all
    force_ind  = args.force_indicators or args.force_all
    force_feat = args.force_features or args.force_all

    # ─── Yol dönüşümleri ────────────────────────────────────────
    db_path    = str(Path(args.db).resolve())
    btdb_path  = str(Path(args.btdb).resolve())
    ohlcv_path = str(Path(args.ohlcv_db).resolve())

    # DB dosyasi var mi?
    if not Path(db_path).exists():
        print(f"  Veritabani bulunamadi: {db_path}")
        print("   Once collector.py ile Telegram mesajlarini cek.")
        sys.exit(1)

    steps_done = 0

    # ════════════════════════════════════════════════════════════
    # ADIM 1/9: GÜNLÜK RAPOR
    # ════════════════════════════════════════════════════════════
    if not args.skip_report:
        print("━" * 60)
        print(f"  ADIM 1/{TOTAL_STEPS} — GUNLUK RAPOR")
        print("━" * 60)
        try:
            import generate_report
            generate_report.run(db=db_path, out=args.report_out)
            steps_done += 1
        except Exception as e:
            print(f"  Rapor olusturulamadi: {e}")
    else:
        print(f"  >>  ADIM 1/{TOTAL_STEPS} — Gunluk rapor atlandi (--skip-report)")

    # ════════════════════════════════════════════════════════════
    # ADIM 2/9: BACKTEST
    # ════════════════════════════════════════════════════════════
    if not args.skip_backtest:
        print()
        print("━" * 60)
        print(f"  ADIM 2/{TOTAL_STEPS} — BACKTEST (Binance API, inkremental)")
        print("━" * 60)
        try:
            import backtest_signals
            backtest_signals.run(
                db=db_path, btdb=btdb_path,
                out=args.backtest_out, force=force_bt,
            )
            steps_done += 1
        except Exception as e:
            print(f"  Backtest basarisiz: {e}")
            import traceback; traceback.print_exc()
    else:
        print(f"  >>  ADIM 2/{TOTAL_STEPS} — Backtest atlandi (--skip-backtest)")

    # ════════════════════════════════════════════════════════════
    # ADIM 3/9: TREND ZENGİNLEŞTİRME
    # ════════════════════════════════════════════════════════════
    if not args.skip_trends:
        print()
        print("━" * 60)
        print(f"  ADIM 3/{TOTAL_STEPS} — TREND ZENGİNLEŞTİRME (Google Trends)")
        print("━" * 60)
        if not Path(btdb_path).exists():
            print(f"  Backtest DB bulunamadi: {btdb_path} — trend adimi atlaniyor")
        else:
            try:
                import backtest_trends
                if not backtest_trends.PYTRENDS_OK:
                    print("  pytrends yuklu degil — trend adimi atlaniyor")
                    print("     Kurmak icin: pip install pytrends")
                else:
                    backtest_trends.run(bt_db=btdb_path, force=force_tr)
                    steps_done += 1
            except Exception as e:
                print(f"  Trend zenginlestirme basarisiz: {e}")
                import traceback; traceback.print_exc()
    else:
        print(f"  >>  ADIM 3/{TOTAL_STEPS} — Trend adimi atlandi (--skip-trends)")

    # ════════════════════════════════════════════════════════════
    # ADIM 4/9: CONTEXT (Fear & Greed)
    # ════════════════════════════════════════════════════════════
    if not args.skip_context:
        print()
        print("━" * 60)
        print(f"  ADIM 4/{TOTAL_STEPS} — PİYASA BAĞLAMI (Fear & Greed Index)")
        print("━" * 60)
        if not Path(btdb_path).exists():
            print(f"  Backtest DB bulunamadi: {btdb_path} — context adimi atlaniyor")
        else:
            try:
                import backtest_context
                backtest_context.run(bt_db=btdb_path, force=force_ctx)
                steps_done += 1
            except Exception as e:
                print(f"  Context zenginlestirme basarisiz: {e}")
                import traceback; traceback.print_exc()
    else:
        print(f"  >>  ADIM 4/{TOTAL_STEPS} — Context adimi atlandi (--skip-context)")

    # ════════════════════════════════════════════════════════════
    # ADIM 5/9: ESKİ ML ANALİZ
    # ════════════════════════════════════════════════════════════
    if not args.skip_ml:
        print()
        print("━" * 60)
        print(f"  ADIM 5/{TOTAL_STEPS} — ML ANALİZ (CoinGecko + scikit-learn)")
        print("━" * 60)
        if not Path(btdb_path).exists():
            print(f"  Backtest DB bulunamadi: {btdb_path}")
        else:
            try:
                import ml_analysis
                ml_analysis.run(
                    btdb=btdb_path, out=args.ml_out,
                    no_volume=args.no_marketcap, target=args.target,
                )
                steps_done += 1
            except Exception as e:
                print(f"  ML analizi basarisiz: {e}")
                import traceback; traceback.print_exc()
    else:
        print(f"  >>  ADIM 5/{TOTAL_STEPS} — ML analizi atlandi (--skip-ml)")

    # ════════════════════════════════════════════════════════════
    # ADIM 6/9: OHLCV TOPLAMA (YENİ)
    # ════════════════════════════════════════════════════════════
    if not args.skip_ohlcv:
        print()
        print("━" * 60)
        print(f"  ADIM 6/{TOTAL_STEPS} — OHLCV VERİ TOPLAMA (Binance, inkremental)")
        print("━" * 60)
        if not Path(btdb_path).exists():
            print(f"  Backtest DB bulunamadi: {btdb_path} — OHLCV adimi atlaniyor")
        else:
            try:
                import ohlcv_collector
                ohlcv_collector.collect_all(
                    timeframes=args.ohlcv_timeframes,
                    bt_db=btdb_path,
                    ohlcv_db=ohlcv_path,
                    force=force_ohlcv,
                )
                steps_done += 1
            except Exception as e:
                print(f"  OHLCV toplama basarisiz: {e}")
                import traceback; traceback.print_exc()
    else:
        print(f"  >>  ADIM 6/{TOTAL_STEPS} — OHLCV toplama atlandi (--skip-ohlcv)")

    # ════════════════════════════════════════════════════════════
    # ADIM 7/9: İNDİKATÖR HESAPLAMA (YENİ)
    # ════════════════════════════════════════════════════════════
    if not args.skip_indicators:
        print()
        print("━" * 60)
        print(f"  ADIM 7/{TOTAL_STEPS} — TEKNİK İNDİKATÖR HESAPLAMA")
        print("━" * 60)
        if not Path(ohlcv_path).exists():
            print(f"  OHLCV DB bulunamadi: {ohlcv_path} — indikatör adimi atlaniyor")
        else:
            try:
                import indicator_engine
                indicator_engine.compute_all_indicators(
                    timeframes=args.indicator_timeframes,
                    bt_db=btdb_path,
                    ohlcv_db=ohlcv_path,
                    force=force_ind,
                )
                steps_done += 1
            except Exception as e:
                print(f"  İndikatör hesaplama basarisiz: {e}")
                import traceback; traceback.print_exc()
    else:
        print(f"  >>  ADIM 7/{TOTAL_STEPS} — İndikatör hesaplama atlandi (--skip-indicators)")

    # ════════════════════════════════════════════════════════════
    # ADIM 8/9: ÖZELLİK BİRLEŞTİRME (YENİ)
    # ════════════════════════════════════════════════════════════
    if not args.skip_features:
        print()
        print("━" * 60)
        print(f"  ADIM 8/{TOTAL_STEPS} — ÖZELLİK BİRLEŞTİRME + CONFLUENCE SCORE")
        print("━" * 60)
        try:
            import feature_builder
            feature_builder.build_all_features(
                bt_db=btdb_path,
                force=force_feat,
            )
            steps_done += 1
        except Exception as e:
            print(f"  Özellik birleştirme basarisiz: {e}")
            import traceback; traceback.print_exc()
    else:
        print(f"  >>  ADIM 8/{TOTAL_STEPS} — Özellik birleştirme atlandi (--skip-features)")

    # ════════════════════════════════════════════════════════════
    # ADIM 9/9: KARAR MODELİ EĞİTİMİ (YENİ)
    # ════════════════════════════════════════════════════════════
    if not args.skip_decision:
        print()
        print("━" * 60)
        print(f"  ADIM 9/{TOTAL_STEPS} — KARAR MODELİ EĞİTİMİ")
        print("━" * 60)
        try:
            import decision_model
            # target formatı: "win_1h" → "1h"
            target_horizon = args.target.replace("win_", "")
            decision_model.run_decision_analysis(
                bt_db=btdb_path,
                target_horizon=target_horizon,
                out_path=args.decision_out,
            )
            steps_done += 1
        except Exception as e:
            print(f"  Karar modeli eğitimi basarisiz: {e}")
            import traceback; traceback.print_exc()
    else:
        print(f"  >>  ADIM 9/{TOTAL_STEPS} — Karar modeli atlandi (--skip-decision)")

    # ════════════════════════════════════════════════════════════
    # ADIM 10/10: SERVING MODEL YENİLEME
    # ════════════════════════════════════════════════════════════
    if not args.skip_serving_model:
        print()
        print("━" * 60)
        print(f"  ADIM 10/{TOTAL_STEPS} — SERVİNG MODEL YENİLEME")
        print("━" * 60)
        if not Path(btdb_path).exists():
            print(f"  Backtest DB bulunamadı: {btdb_path} — model yenileme atlaniyor")
        else:
            try:
                import model_manager
                target_horizon = args.target.replace("win_", "")
                model_data = model_manager.train_and_save(
                    bt_db=btdb_path,
                    model_dir="models",
                    horizon=target_horizon,
                )
                if model_data:
                    print(f"  Model güncellendi — F1={model_data['f1']:.3f}, "
                          f"Acc={model_data['accuracy']:.3f}, "
                          f"n={model_data['n_samples']}")
                    steps_done += 1
                else:
                    print("  Model eğitimi başarısız (yetersiz veri?)")
            except Exception as e:
                print(f"  Serving model yenileme basarisiz: {e}")
                import traceback; traceback.print_exc()
    else:
        print(f"  >>  ADIM 10/{TOTAL_STEPS} — Serving model yenileme atlandi (--skip-serving-model)")

    # ════════════════════════════════════════════════════════════
    # SONUÇ ÖZETİ
    # ════════════════════════════════════════════════════════════
    elapsed = time.time() - t0
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║                        TAMAMLANDI                           ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"  Sure: {elapsed:.1f} saniye  |  {steps_done}/{TOTAL_STEPS} adim basariyla tamamlandi")
    print()

    if not args.skip_report:
        print(f"  Gunluk Rapor:        {args.report_out}")
    if not args.skip_backtest:
        print(f"  Backtest Excel:      {args.backtest_out}")
    if not args.skip_trends:
        print(f"  Trend Verisi:        backtest_results.db (trend_score, trend_momentum)")
    if not args.skip_context:
        print(f"  Context Verisi:      backtest_results.db (fear_greed)")
    if not args.skip_ml:
        print(f"  ML Analizi:          {args.ml_out}")
    if not args.skip_ohlcv:
        print(f"  OHLCV Verisi:        {args.ohlcv_db}")
    if not args.skip_indicators:
        print(f"  İndikatör Snapshot:  backtest_results.db (indicator_snapshots)")
    if not args.skip_features:
        print(f"  Özellik Verisi:      backtest_results.db (signal_features)")
    if not args.skip_decision:
        print(f"  Karar Raporu:        {args.decision_out}")
    if not args.skip_serving_model:
        print(f"  Serving Model:       models/decision_model.pkl")
    print()


if __name__ == "__main__":
    main()
