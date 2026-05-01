# Telegram Signal Analyzer

Telegram crypto signal channels collector, backtester, model trainer and live
email reporter.

This public version is designed so every user can run the system with their own
Telegram API credentials, channel list and optional API keys.

## What This Project Does

- Collects messages from Telegram channels or groups.
- Extracts ticker and direction from signal-like messages.
- Stores raw Telegram messages in SQLite.
- Backtests historical signals.
- Builds technical features and trains a decision model.
- Runs a live collector that can send email reports for new live signals.
- Optionally runs a chart pattern model and attaches the chart result to email.

This project is for research and automation. It is not financial advice.

## Security First

Never commit or share these files:

- `.env`
- `*.session`
- real API keys or tokens
- email app passwords
- private SQLite databases

If a token, app password or session file was ever published, rotate it from the
provider immediately.

## Requirements

- Python 3.10+
- Telegram account
- Telegram API ID and API HASH from https://my.telegram.org
- Optional SMTP account for email reports
- Optional CryptoPanic token for news
- Optional OpenAI key for AI classification

## Installation

```bash
git clone https://github.com/bayramkamus/crypto-tel-trade.git
cd telegram-signal-analyzer

python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

For the optional chart pattern model:

```bash
pip install -r requirements-ml.txt
```

## Configuration

Create local config files from the examples:

```bash
cp .env.example .env
cp configs/channels.example.yml configs/channels.yml
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
Copy-Item configs\channels.example.yml configs\channels.yml
```

Edit `.env` with your own values.

### Telegram API ID and HASH

1. Go to https://my.telegram.org
2. Log in with your Telegram phone number.
3. Open "API development tools".
4. Create an app.
5. Copy `api_id` and `api_hash` into `.env`.

### Telegram Channels

Edit `configs/channels.yml`:

```yaml
channels:
  - binancekillers
  - BitcoinBullets
  - your_channel_name

backfill_days: 30
```

Do not include private invite links in a public repo.

### Bundled Research Channel List

This repository also includes an example channel list matching the bundled
sample databases:

```text
configs/channels.research.example.yml
```

The sample research list contains these public Telegram channel usernames:

- `wallstreetqueenofficial`
- `cryptoninjas_trading_ann`
- `binancekillers`
- `EveningTrader`
- `Bitcoin_BulletsSignals`
- `Crypto_Whales_Pumps_Guide`
- `Classic_Coincodecap`
- `BitcoinBullets`
- `Wall_Street_Queen_Official0`
- `Wolfx_Signals9`
- `crypto_pumps_p`
- `FedRussianInsiders`
- `ThomasSign`
- `Fed_Russian_InsidersOfficial`

You can use this list as a starting point:

```bash
cp configs/channels.research.example.yml configs/channels.yml
```

Then add your own channels under `channels:`. The collector will continue from
the existing database and add new messages for the new channels.

### Gmail SMTP

For Gmail:

1. Enable 2-Step Verification on your Google account.
2. Create an App Password.
3. Use that app password as `SMTP_PASS`.

Do not use your normal email password.

## Common Commands

Collect Telegram messages:

```bash
python run_collector.py
```

Collect without historical backfill:

```bash
python run_collector.py --no-backfill
```

Run the full research pipeline:

```bash
python main.py
```

Run only backtest:

```bash
python backtest_signals.py
```

Train and save serving model:

```bash
python model_manager.py --retrain
```

## Live Email Reports

The live collector can send email reports when a new live Telegram signal is
detected. Configure SMTP in `.env` first.

The saved decision model is loaded from:

```text
models/decision_model.pkl
```

If no trained decision model exists, run the research pipeline or train a model
from your own `backtest_results.db`.

## Optional Chart Pattern Model

The optional chart pattern package is kept in:

```text
models/chart_pattern_model_1777159444/
```

Install ML dependencies first:

```bash
pip install -r requirements-ml.txt
```

Then set:

```env
ENABLE_CHART_PATTERN=true
```

If the model or dependencies are missing, the live email pipeline should still
continue without chart pattern output.

## Sample Data

This public package can include sanitized sample databases:

- `pump_research.sample.db.gz`
- `backtest_results.sample.db.gz`

These files are stored under `sample_data/`. They let new users explore the
reports, backtest tables and model training flow before collecting their own
Telegram data.

Extract sample data into the project root:

```bash
python - <<'PY'
import gzip, shutil
for name in ["pump_research", "backtest_results"]:
    src = f"sample_data/{name}.sample.db.gz"
    dst = f"{name}.db"
    with gzip.open(src, "rb") as f_in, open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
PY
```

Windows PowerShell:

```powershell
python -c "import gzip, shutil; [shutil.copyfileobj(gzip.open(f'sample_data/{n}.sample.db.gz','rb'), open(f'{n}.db','wb')) for n in ['pump_research','backtest_results']]"
```

After extraction, users can run:

```bash
python generate_report.py
python backtest_signals.py --excel-only
python model_manager.py --retrain
```

To build on top of the sample data:

1. Copy `configs/channels.research.example.yml` to `configs/channels.yml`.
2. Add new Telegram channels to the same `channels:` list.
3. Run `python run_collector.py`.
4. Run `python main.py` or the individual backtest/model commands.

Do not publish private session files or unsanitized personal data.

## Troubleshooting

- `Telegram API_ID/API_HASH missing`: fill `.env`.
- `No channels resolved`: check `configs/channels.yml` and channel usernames.
- `SMTP settings missing`: fill SMTP variables in `.env`.
- `Saved model not found`: train the model or place it in `models/decision_model.pkl`.
- `Exchange/API request failed`: check internet access and provider availability.
