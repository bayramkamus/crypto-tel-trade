"""
Anlık Sinyal Raporu + Email Bildirimi
=======================================
Her canlı sinyal sonrası:
  1. Scraping sonuçlarını toplar
  2. Volume anomali bilgisini ekler
  3. HTML rapor üretir
  4. Email ile gönderir (SMTP)

Config (.env):
    REPORT_EMAIL_TO=bayramkamus@gmail.com
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=bayramkamus@gmail.com
    SMTP_PASS=xxxx-xxxx-xxxx-xxxx     # Gmail App Password
    SMTP_USE_TLS=true

Kullanım:
    from live_report import send_signal_report

    send_signal_report(
        ticker="PEPE",
        channel="binancekillers",
        message_text="#PEPE/USDT LONG Entry: 0.000012",
        scraping_result={...},
        volume_data={...},
    )
"""

import os
import logging
import smtplib
from html import escape
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# SMTP AYARLARI (.env'den)
# ─────────────────────────────────────────────────────────────────

def _get_smtp_config() -> dict | None:
    """SMTP ayarlarını .env'den okur."""
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pwd  = os.environ.get("SMTP_PASS")
    to   = os.environ.get("REPORT_EMAIL_TO")

    if not all([host, user, pwd, to]):
        return None

    return {
        "host":    host,
        "port":    int(os.environ.get("SMTP_PORT", "587")),
        "user":    user,
        "password": pwd,
        "to":      to,
        "use_tls": os.environ.get("SMTP_USE_TLS", "true").lower() == "true",
    }


# ─────────────────────────────────────────────────────────────────
# HTML RAPOR ÜRETİCİ
# ─────────────────────────────────────────────────────────────────

def _fmt(value, suffix: str = "", digits: int = 2) -> str:
    """HTML rapor icin guvenli kisa sayi formati."""
    if value is None:
        return "-"
    try:
        if isinstance(value, float):
            return f"{value:.{digits}f}{suffix}"
        return f"{value}{suffix}"
    except Exception:
        return "-"


def _action_color(action: str) -> str:
    return {
        "AL": "#22aa44",
        "SAT": "#ff8844",
        "TUT": "#ffaa00",
        "UYGULANABİLİR": "#22aa44",
        "ZAYIF KARAR": "#ffaa00",
        "ATLA": "#ff5555",
    }.get(action, "#aaaaaa")


def _probability_text(probabilities: dict | None) -> str:
    if not probabilities:
        return "-"
    labels = []
    for key in ["STRONG_WIN", "WEAK_WIN", "LOSS"]:
        if key in probabilities:
            labels.append(f"{key}: {float(probabilities[key]) * 100:.1f}%")
    return " | ".join(labels) if labels else "-"


def _indicator_rows(snapshots: dict | None, snapshot_meta: dict | None) -> str:
    if not snapshots:
        return """
        <tr>
            <td colspan="8" style="padding:8px;color:#aaa;text-align:center;">
                Indikator snapshot hesaplanamadi.
            </td>
        </tr>
        """

    rows = []
    for tf in ["5m", "15m", "1h"]:
        snap = snapshots.get(tf)
        if not snap:
            meta = (snapshot_meta or {}).get(tf, {})
            rows.append(f"""
            <tr>
                <td style="padding:6px;color:white;font-weight:bold;">{tf}</td>
                <td colspan="7" style="padding:6px;color:#aaa;text-align:center;">
                    Veri yok ({meta.get("source", "none")}, {meta.get("candles", 0)} mum)
                </td>
            </tr>
            """)
            continue
        rows.append(f"""
        <tr>
            <td style="padding:6px;color:white;font-weight:bold;">{tf}</td>
            <td style="padding:6px;text-align:right;">{_fmt(snap.get("rsi_14"), digits=1)}</td>
            <td style="padding:6px;text-align:right;">{_fmt(snap.get("macd_histogram"), digits=5)}</td>
            <td style="padding:6px;text-align:right;">{_fmt(snap.get("macd_cross"), digits=0)}</td>
            <td style="padding:6px;text-align:right;">{_fmt(snap.get("bb_pctb"), digits=2)}</td>
            <td style="padding:6px;text-align:right;">{_fmt(snap.get("ema_alignment"), digits=0)}</td>
            <td style="padding:6px;text-align:right;">{_fmt(snap.get("price_vs_ema200"), "%", digits=2)}</td>
            <td style="padding:6px;text-align:right;">{_fmt(snap.get("volume_ratio"), "x", digits=2)}</td>
        </tr>
        """)
    return "\n".join(rows)


def _build_decision_html(decision_data: dict | None) -> str:
    if not decision_data:
        return ""

    status = decision_data.get("status")
    model_decision = decision_data.get("model_decision")
    action = {
        "EXECUTE": "UYGULANABİLİR",
        "CAUTION": "ZAYIF KARAR",
        "SKIP": "ATLA",
    }.get(model_decision, decision_data.get("action", "TUT"))
    color = _action_color(action)
    reliability = decision_data.get("reliability", {})
    snapshots = decision_data.get("snapshots", {})
    snapshot_meta = decision_data.get("snapshot_meta", {})
    notes = decision_data.get("notes", [])

    if status != "ok":
        reason = decision_data.get("reason", "Karar analizi kullanilamadi.")
        return f"""
        <div style="background:#211a1a;border-radius:8px;padding:16px;margin:12px 0;
                    border-left:4px solid #ffaa00;">
            <h3 style="color:#e0e0e0;margin:0 0 12px 0;">Karar Analizi</h3>
            <p style="margin:0;color:#ffaa00;font-weight:bold;">Aksiyon: TUT</p>
            <p style="margin:8px 0 0 0;color:#ccc;font-size:13px;">{reason}</p>
        </div>
        """

    model = decision_data.get("model", {})
    coverage = decision_data.get("feature_coverage", {})
    probs = _probability_text(decision_data.get("probabilities"))
    note_html = ""
    if notes:
        note_items = "".join(f"<li>{note}</li>" for note in notes)
        note_html = f"""
        <ul style="margin:10px 0 0 18px;color:#aaa;font-size:12px;line-height:1.5;">
            {note_items}
        </ul>
        """

    return f"""
    <div style="background:#102018;border-radius:8px;padding:16px;margin:12px 0;
                border-left:4px solid {color};">
        <h3 style="color:#e0e0e0;margin:0 0 12px 0;">Karar Analizi</h3>
        <table style="width:100%;color:#ccc;font-size:14px;">
            <tr>
                <td style="padding:4px 0;">Karar</td>
                <td style="text-align:right;font-weight:bold;font-size:22px;color:{color};">
                    {action}
                </td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Model sinifi</td>
                <td style="text-align:right;color:white;">
                    {decision_data.get("predicted", "-")} / {decision_data.get("model_decision", "-")}
                </td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Model guveni</td>
                <td style="text-align:right;color:white;">{_fmt(decision_data.get("confidence"), "%", 1)}</td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Analiz guvenilirligi</td>
                <td style="text-align:right;color:white;">
                    {reliability.get("label", "-")} ({_fmt(reliability.get("score"), "/100", 1)})
                </td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Olasiliklar</td>
                <td style="text-align:right;color:#aaa;">{probs}</td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Feature kapsami</td>
                <td style="text-align:right;color:#aaa;">
                    {coverage.get("available", 0)}/{coverage.get("total", 0)}
                    ({_fmt(coverage.get("percent"), "%", 1)})
                </td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Model kalitesi</td>
                <td style="text-align:right;color:#aaa;">
                    F1 {_fmt(model.get("f1"), "%", 1)} / Acc {_fmt(model.get("accuracy"), "%", 1)}
                    / n={model.get("n_samples", 0)}
                </td>
            </tr>
        </table>
        {note_html}
        <p style="margin:10px 0 0 0;color:#777;font-size:11px;">
            Otomatik model ciktisidir; tek basina yatirim tavsiyesi degildir.
        </p>
    </div>

    <div style="background:#161b22;border-radius:8px;padding:16px;margin:12px 0;">
        <h3 style="color:#e0e0e0;margin:0 0 12px 0;">Indikator Snapshotlari</h3>
        <table style="width:100%;color:#ccc;font-size:12px;border-collapse:collapse;">
            <tr style="color:#888;border-bottom:1px solid #333;">
                <th style="text-align:left;padding:6px;">TF</th>
                <th style="text-align:right;padding:6px;">RSI</th>
                <th style="text-align:right;padding:6px;">MACD Hist</th>
                <th style="text-align:right;padding:6px;">Cross</th>
                <th style="text-align:right;padding:6px;">BB %B</th>
                <th style="text-align:right;padding:6px;">EMA Align</th>
                <th style="text-align:right;padding:6px;">Px/EMA200</th>
                <th style="text-align:right;padding:6px;">Vol</th>
            </tr>
            {_indicator_rows(snapshots, snapshot_meta)}
        </table>
    </div>
    """


def _chart_confidence(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _pattern_label(value) -> str:
    if not value:
        return "-"
    return escape(str(value).replace("_", " ").title())


def _chart_image_html(chart_pattern_data: dict | None) -> str:
    if not chart_pattern_data:
        return ""
    cid = chart_pattern_data.get("image_cid")
    if not cid:
        return ""
    alt = escape(f"{chart_pattern_data.get('symbol', '')} 15m chart")
    return f"""
        <img src="cid:{escape(str(cid))}" alt="{alt}"
             style="width:100%;max-width:560px;border-radius:8px;
                    display:block;margin:12px auto 0 auto;border:1px solid #30363d;">
    """


def _build_chart_pattern_html(chart_pattern_data: dict | None) -> str:
    if not chart_pattern_data:
        return ""

    status = chart_pattern_data.get("status")
    action = chart_pattern_data.get("action", "TUT")
    color = _action_color(action)
    image_html = _chart_image_html(chart_pattern_data)

    if status != "ok":
        reason = escape(chart_pattern_data.get("reason", "Chart pattern analizi kullanilamadi."))
        return f"""
        <div style="background:#211a1a;border-radius:8px;padding:16px;margin:12px 0;
                    border-left:4px solid #ffaa00;">
            <h3 style="color:#e0e0e0;margin:0 0 12px 0;">15m Chart Pattern</h3>
            <p style="margin:0;color:#ffaa00;font-weight:bold;">Aksiyon: TUT</p>
            <p style="margin:8px 0 0 0;color:#ccc;font-size:13px;">{reason}</p>
            {image_html}
        </div>
        """

    heads_agree = chart_pattern_data.get("heads_agree")
    agree_text = "Evet" if heads_agree is True else "Hayir" if heads_agree is False else "-"
    pattern = _pattern_label(chart_pattern_data.get("detected_pattern"))
    signal = escape(str(chart_pattern_data.get("signal", "-")))
    rule_signal = escape(str(chart_pattern_data.get("signal_from_pattern_rule", "-")))
    source = escape(str(chart_pattern_data.get("source", "-")))

    return f"""
    <div style="background:#101d22;border-radius:8px;padding:16px;margin:12px 0;
                border-left:4px solid {color};">
        <h3 style="color:#e0e0e0;margin:0 0 12px 0;">15m Chart Pattern</h3>
        <table style="width:100%;color:#ccc;font-size:14px;">
            <tr>
                <td style="padding:4px 0;">Al/Sat/Tut</td>
                <td style="text-align:right;font-weight:bold;font-size:22px;color:{color};">
                    {action}
                </td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Pattern</td>
                <td style="text-align:right;color:white;">{pattern}</td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Pattern guveni</td>
                <td style="text-align:right;color:white;">
                    {_chart_confidence(chart_pattern_data.get("pattern_confidence"))}
                </td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Model sinyali</td>
                <td style="text-align:right;color:white;">
                    {signal} ({_chart_confidence(chart_pattern_data.get("signal_confidence"))})
                </td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Pattern kural sinyali</td>
                <td style="text-align:right;color:#aaa;">
                    {rule_signal} / uyum: {agree_text}
                </td>
            </tr>
            <tr>
                <td style="padding:4px 0;">Mum kaynagi</td>
                <td style="text-align:right;color:#aaa;">
                    {source}, {chart_pattern_data.get("candles", 0)} mum
                </td>
            </tr>
        </table>
        {image_html}
        <p style="margin:10px 0 0 0;color:#777;font-size:11px;">
            Chart pattern modeli 15m mum goruntusu uzerinden uretilen otomatik ciktidir.
        </p>
    </div>
    """


def _build_html_report(
    ticker: str,
    channel: str,
    message_text: str,
    scraping_result: dict | None,
    volume_data: dict | None,
    timestamp: str | None = None,
    decision_data: dict | None = None,
    chart_pattern_data: dict | None = None,
) -> str:
    """Sinyal için HTML rapor üretir."""

    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Volume bilgisi
    vol_html = ""
    if volume_data:
        ratio = volume_data.get("volume_ratio", 0)
        is_anomaly = volume_data.get("is_anomaly", False)
        current_vol = volume_data.get("current_vol", 0)
        avg_vol = volume_data.get("avg_vol_1h", 0)
        price = volume_data.get("current_price")
        exchange = volume_data.get("exchange", "?")
        market = volume_data.get("market", "?")

        anomaly_badge = ""
        if is_anomaly:
            anomaly_badge = (
                '<span style="background:#ff4444;color:white;padding:2px 8px;'
                'border-radius:4px;font-weight:bold;font-size:12px;">'
                'VOLUME ANOMALİ</span>'
            )

        ratio_color = "#ff4444" if ratio > 3 else "#ff8800" if ratio > 2 else "#22aa44"

        vol_html = f"""
        <div style="background:#1a1a2e;border-radius:8px;padding:16px;margin:12px 0;">
            <h3 style="color:#e0e0e0;margin:0 0 12px 0;">
                📊 Volume Analizi {anomaly_badge}
            </h3>
            <table style="width:100%;color:#ccc;font-size:14px;">
                <tr>
                    <td style="padding:4px 0;">Borsa</td>
                    <td style="text-align:right;font-weight:bold;color:white;">
                        {exchange.upper()} ({market})
                    </td>
                </tr>
                <tr>
                    <td style="padding:4px 0;">Anlık Fiyat</td>
                    <td style="text-align:right;color:#4fc3f7;">
                        {f"${price:.8g}" if price else "—"}
                    </td>
                </tr>
                <tr>
                    <td style="padding:4px 0;">Şu Anki Hacim (1m)</td>
                    <td style="text-align:right;color:white;">{current_vol:,.0f}</td>
                </tr>
                <tr>
                    <td style="padding:4px 0;">Ortalama Hacim (1sa)</td>
                    <td style="text-align:right;color:#aaa;">{avg_vol:,.0f}</td>
                </tr>
                <tr>
                    <td style="padding:4px 0;font-weight:bold;">Volume Ratio</td>
                    <td style="text-align:right;font-weight:bold;font-size:18px;
                               color:{ratio_color};">
                        {ratio}x
                    </td>
                </tr>
            </table>
        </div>
        """

  
    # Mesaj metni (kısa)
    msg_display = message_text[:500].replace("\n", "<br>") if message_text else "—"
    decision_html = _build_decision_html(decision_data)
    chart_pattern_html = _build_chart_pattern_html(chart_pattern_data)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #0d1117;
                color: #e0e0e0;
                margin: 0;
                padding: 20px;
            }}
        </style>
    </head>
    <body>
        <div style="max-width:600px;margin:0 auto;">
            <!-- Header -->
            <div style="background:linear-gradient(135deg,#1e3a5f,#2d1b69);
                        border-radius:12px;padding:20px;text-align:center;">
                <h1 style="color:white;margin:0;font-size:24px;">
                    🚀 {ticker}
                </h1>
                <p style="color:#aaa;margin:8px 0 0 0;font-size:14px;">
                    @{channel} · {ts}
                </p>
            </div>

            <!-- Sinyal Mesajı -->
            <div style="background:#161b22;border-radius:8px;padding:16px;margin:12px 0;
                        border-left:4px solid #4fc3f7;">
                <p style="color:#ccc;margin:0;font-size:13px;line-height:1.6;">
                    {msg_display}
                </p>
            </div>

            {decision_html}

            {chart_pattern_html}

            {vol_html}

            

            <!-- Footer -->
            <div style="text-align:center;padding:16px;color:#666;font-size:11px;">
                Telegram Signal Pipeline · Otomatik Rapor
            </div>
        </div>
    </body>
    </html>
    """
    return html


# ─────────────────────────────────────────────────────────────────
# EMAIL GÖNDERME
# ─────────────────────────────────────────────────────────────────

def _send_email(
    smtp_cfg: dict,
    subject: str,
    html_body: str,
    inline_images: list[dict] | None = None,
) -> bool:
    """SMTP ile email gönderir."""
    try:
        if inline_images:
            msg = MIMEMultipart("related")
            alternative = MIMEMultipart("alternative")
            msg.attach(alternative)
        else:
            msg = MIMEMultipart("alternative")
            alternative = msg
        msg["Subject"] = subject
        msg["From"]    = smtp_cfg["user"]
        msg["To"]      = smtp_cfg["to"]

        # HTML part
        html_part = MIMEText(html_body, "html", "utf-8")
        alternative.attach(html_part)

        for image_info in inline_images or []:
            path = Path(image_info.get("path", ""))
            cid = image_info.get("cid")
            if not cid or not path.exists():
                continue
            try:
                with path.open("rb") as fh:
                    image_part = MIMEImage(fh.read())
                image_part.add_header("Content-ID", f"<{cid}>")
                image_part.add_header(
                    "Content-Disposition",
                    "inline",
                    filename=image_info.get("name") or path.name,
                )
                msg.attach(image_part)
            except Exception as exc:
                log.warning(f"[email] Inline image eklenemedi ({path}): {exc}")

        # SMTP bağlantısı
        if smtp_cfg["use_tls"]:
            server = smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=15)
            server.ehlo()
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(smtp_cfg["host"], smtp_cfg["port"], timeout=15)

        server.login(smtp_cfg["user"], smtp_cfg["password"])
        server.send_message(msg)
        server.quit()

        log.info(f"[email] ✅ Rapor gönderildi: {smtp_cfg['to']}")
        return True

    except Exception as e:
        log.error(f"[email] ❌ Gönderim hatası: {e}")
        return False


# ─────────────────────────────────────────────────────────────────
# ANA FONKSİYON
# ─────────────────────────────────────────────────────────────────

def _inline_images(chart_pattern_data: dict | None) -> list[dict]:
    if not chart_pattern_data:
        return []
    image_path = chart_pattern_data.get("image_path")
    image_cid = chart_pattern_data.get("image_cid")
    if not image_path or not image_cid:
        return []
    path = Path(image_path)
    if not path.exists():
        return []
    return [{"path": str(path), "cid": image_cid, "name": path.name}]


def send_signal_report(
    ticker: str,
    channel: str,
    message_text: str,
    scraping_result: dict | None = None,
    volume_data: dict | None = None,
    timestamp: str | None = None,
    decision_data: dict | None = None,
    chart_pattern_data: dict | None = None,
) -> bool:
    """
    Anlık sinyal raporu üretir ve email ile gönderir.

    Dönüş: True (email gönderildi) / False (gönderilemedi veya config yok)
    """
    smtp_cfg = _get_smtp_config()
    if not smtp_cfg:
        log.warning(
            "[report] SMTP ayarları eksik. .env'ye SMTP_HOST, SMTP_USER, "
            "SMTP_PASS, REPORT_EMAIL_TO ekleyin."
        )
        return False

    # HTML rapor üret
    html = _build_html_report(
        ticker=ticker,
        channel=channel,
        message_text=message_text,
        scraping_result=scraping_result,
        volume_data=volume_data,
        timestamp=timestamp,
        decision_data=decision_data,
        chart_pattern_data=chart_pattern_data,
    )

    # Volume anomali varsa subject'e ekle
    anomaly_tag = ""
    if volume_data and volume_data.get("is_anomaly"):
        ratio = volume_data.get("volume_ratio", 0)
        anomaly_tag = f" ⚠️ VOL {ratio}x"

    subject = f"🚀 {ticker} | @{channel}{anomaly_tag}"

    return _send_email(smtp_cfg, subject, html, _inline_images(chart_pattern_data))


def generate_report_html(
    ticker: str,
    channel: str,
    message_text: str,
    scraping_result: dict | None = None,
    volume_data: dict | None = None,
    timestamp: str | None = None,
    decision_data: dict | None = None,
    chart_pattern_data: dict | None = None,
) -> str:
    """
    Sadece HTML rapor üretir (email göndermeden).
    Dashboard veya dosya kaydetme için kullanılabilir.
    """
    return _build_html_report(
        ticker=ticker,
        channel=channel,
        message_text=message_text,
        scraping_result=scraping_result,
        volume_data=volume_data,
        timestamp=timestamp,
        decision_data=decision_data,
        chart_pattern_data=chart_pattern_data,
    )
