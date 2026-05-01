#!/usr/bin/env python3
"""
Model Yöneticisi
=================
- Eğitilmiş modeli .pkl olarak kaydet/yükle
- Haftalık otomatik yeniden eğitim
- Model performans geçmişi takibi

Kullanım:
    python model_manager.py                  # Modeli eğit ve kaydet
    python model_manager.py --retrain        # Zorla yeniden eğit
    python model_manager.py --info           # Mevcut model bilgisi
"""

import sqlite3
import pickle
import logging
import argparse
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score

from feature_builder import load_full_dataset, FEATURE_COLUMNS as IND_FEATURE_COLUMNS

log = logging.getLogger(__name__)

BT_DB_PATH   = "backtest_results.db"
MODEL_DIR    = "models"
MODEL_FILE   = "decision_model.pkl"
HISTORY_FILE = "model_history.json"

STRONG_WIN_PCT = 1.5
WEAK_WIN_PCT   = 0.0

TELEGRAM_FEATURES = [
    "hour", "weekday",
    "fear_greed", "fng_momentum_7d", "fng_momentum_14d",
    "btc_pct_1h", "btc_pct_24h",
    "trend_score", "trend_momentum",
    "pre_pct_1h", "pre_pct_4h",
    "pre_volatility_1h", "pre_volatility_4h",
    "pre_volume_rel_1h", "pre_volume_rel_4h",
    "pre_efficiency_1h",
    "pre_taker_buy_ratio_1h",
    "pump_before_4h", "pump_before_24h",
]

ALL_FEATURES = TELEGRAM_FEATURES + IND_FEATURE_COLUMNS


def define_target(df, horizon="1h"):
    pct_col = f"pct_{horizon}"
    targets = []
    for _, row in df.iterrows():
        pct = row[pct_col]
        direction = row.get("direction", "NEUTRAL")
        if pd.isna(pct):
            targets.append(None)
            continue
        is_long = direction in ("LONG", "LONG_IMPL")
        is_short = direction == "SHORT"
        if is_long:
            effective_pct = pct
        elif is_short:
            effective_pct = -pct
        else:
            effective_pct = abs(pct)
        if effective_pct >= STRONG_WIN_PCT:
            targets.append("STRONG_WIN")
        elif effective_pct >= WEAK_WIN_PCT:
            targets.append("WEAK_WIN")
        else:
            targets.append("LOSS")
    return pd.Series(targets, index=df.index, name=f"target_{horizon}")


def train_and_save(bt_db=BT_DB_PATH, model_dir=MODEL_DIR, horizon="1h"):
    """Model eğit, kaydet, performans geçmişine ekle."""
    Path(model_dir).mkdir(exist_ok=True)

    # Veri yükle
    df = load_full_dataset(bt_db)
    if df.empty:
        log.error("Veri seti boş!")
        return None

    target_col = f"target_{horizon}"
    df[target_col] = define_target(df, horizon)

    df_model = df[df["direction"].isin(["LONG", "SHORT", "LONG_IMPL"])].copy()
    df_model = df_model.dropna(subset=[target_col])

    available = [f for f in ALL_FEATURES if f in df_model.columns]
    X = df_model[available].copy()
    y = df_model[target_col].copy()

    for col in X.columns:
        if X[col].isna().any():
            med = X[col].median()
            X[col] = X[col].fillna(med if not pd.isna(med) else 0)

    if len(X) < 50:
        log.error(f"Yeterli veri yok: {len(X)}")
        return None

    # Eğit
    model = GradientBoostingClassifier(
        n_estimators=150, max_depth=5, learning_rate=0.1,
        min_samples_leaf=10, random_state=42,
    )

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    f1_scores = cross_val_score(model, X, y, cv=cv, scoring="f1_weighted")
    acc_scores = cross_val_score(model, X, y, cv=cv, scoring="accuracy")

    model.fit(X, y)

    f1 = f1_scores.mean()
    acc = acc_scores.mean()

    log.info(f"[model] F1={f1:.3f}±{f1_scores.std():.3f}  Acc={acc:.3f}")
    log.info(f"[model] Veri: {len(X)} satır × {len(available)} özellik")
    log.info(f"[model] Sınıflar: {sorted(y.unique().tolist())}")

    # Kaydet
    model_path = Path(model_dir) / MODEL_FILE
    model_data = {
        "model": model,
        "features": available,
        "classes": sorted(y.unique().tolist()),
        "f1": float(f1),
        "accuracy": float(acc),
        "n_samples": len(X),
        "n_features": len(available),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "horizon": horizon,
        "class_distribution": y.value_counts().to_dict(),
        "feature_medians": {col: float(X[col].median()) for col in available},
    }

    with open(model_path, "wb") as f:
        pickle.dump(model_data, f)

    log.info(f"[model] Kaydedildi: {model_path}")

    # Geçmiş
    _update_history(model_dir, model_data)

    return model_data


def load_model(model_dir=MODEL_DIR):
    """Kaydedilmiş modeli yükle."""
    model_path = Path(model_dir) / MODEL_FILE
    if not model_path.exists():
        log.warning(f"[model] Model bulunamadı: {model_path}")
        return None

    with open(model_path, "rb") as f:
        model_data = pickle.load(f)

    log.info(f"[model] Yüklendi: F1={model_data['f1']:.3f}, {model_data['n_samples']} örnek, {model_data['trained_at'][:10]}")
    return model_data


def predict_signal(model_data, feature_dict):
    """
    Tek bir sinyal için tahmin yap.

    Args:
        model_data: load_model() çıktısı
        feature_dict: {feature_name: value} dict

    Returns:
        {"decision", "predicted", "confidence", "probabilities"}
    """
    model = model_data["model"]
    features = model_data["features"]
    medians = model_data.get("feature_medians", {})

    # Feature vector oluştur
    values = []
    for f in features:
        val = feature_dict.get(f)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            val = medians.get(f, 0)
        values.append(val)

    X = pd.DataFrame([values], columns=features)
    pred = model.predict(X)[0]
    proba = model.predict_proba(X)[0]
    classes = model.classes_
    proba_dict = dict(zip(classes, [float(p) for p in proba]))
    max_proba = float(max(proba))

    loss_prob = proba_dict.get("LOSS", 0)

    if pred == "STRONG_WIN" and max_proba > 0.5:
        decision = "EXECUTE"
    elif pred != "LOSS" and loss_prob < 0.4:
        decision = "CAUTION"
    else:
        decision = "SKIP"

    return {
        "decision": decision,
        "predicted": pred,
        "confidence": round(max_proba * 100, 1),
        "probabilities": {k: round(v, 3) for k, v in proba_dict.items()},
    }


def needs_retrain(model_dir=MODEL_DIR, interval_days=7):
    """Model yeniden eğitim gerekiyor mu?"""
    model_path = Path(model_dir) / MODEL_FILE
    if not model_path.exists():
        return True

    model_data = load_model(model_dir)
    if model_data is None:
        return True

    trained_at = datetime.fromisoformat(model_data["trained_at"])
    age = datetime.now(timezone.utc) - trained_at

    if age > timedelta(days=interval_days):
        log.info(f"[model] Son eğitim {age.days} gün önce — yeniden eğitim gerekli")
        return True

    return False


def _update_history(model_dir, model_data):
    """Model performans geçmişini güncelle."""
    history_path = Path(model_dir) / HISTORY_FILE

    history = []
    if history_path.exists():
        with open(history_path, "r") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []

    entry = {
        "trained_at": model_data["trained_at"],
        "f1": model_data["f1"],
        "accuracy": model_data["accuracy"],
        "n_samples": model_data["n_samples"],
        "n_features": model_data["n_features"],
        "class_distribution": {k: int(v) for k, v in model_data["class_distribution"].items()},
    }
    history.append(entry)

    with open(history_path, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    log.info(f"[model] Geçmiş güncellendi: {len(history)} kayıt")


def show_info(model_dir=MODEL_DIR):
    """Mevcut model bilgisini göster."""
    model_data = load_model(model_dir)
    if model_data is None:
        print("Model bulunamadı!")
        return

    print(f"Model Bilgisi:")
    print(f"  Eğitim tarihi: {model_data['trained_at'][:19]}")
    print(f"  F1 Score:      {model_data['f1']:.3f}")
    print(f"  Accuracy:      {model_data['accuracy']:.3f}")
    print(f"  Veri sayısı:   {model_data['n_samples']}")
    print(f"  Feature sayısı:{model_data['n_features']}")
    print(f"  Sınıflar:      {model_data['classes']}")
    print(f"  Dağılım:       {model_data['class_distribution']}")

    history_path = Path(model_dir) / HISTORY_FILE
    if history_path.exists():
        with open(history_path, "r") as f:
            history = json.load(f)
        print(f"\nEğitim Geçmişi ({len(history)} kayıt):")
        for h in history[-5:]:
            print(f"  {h['trained_at'][:10]}: F1={h['f1']:.3f}, n={h['n_samples']}")


def main():
    parser = argparse.ArgumentParser(description="Model Yöneticisi")
    parser.add_argument("--bt-db", default=BT_DB_PATH)
    parser.add_argument("--model-dir", default=MODEL_DIR)
    parser.add_argument("--retrain", action="store_true", help="Zorla yeniden eğit")
    parser.add_argument("--info", action="store_true", help="Model bilgisi göster")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")

    if args.info:
        show_info(args.model_dir)
    elif args.retrain or needs_retrain(args.model_dir):
        train_and_save(args.bt_db, args.model_dir)
    else:
        log.info("[model] Yeniden eğitime gerek yok — model güncel")
        show_info(args.model_dir)


if __name__ == "__main__":
    main()
