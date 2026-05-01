# Troubleshooting

## Telegram login fails

Check `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in `.env`.

## No channels are resolved

Check `configs/channels.yml` and make sure the account can access the channels.

## Email is not sent

Check SMTP settings. Gmail requires an App Password, not your normal password.

## Model is missing

Train a model or place a compatible file at `models/decision_model.pkl`.

