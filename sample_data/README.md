# Sample Data

This folder contains sanitized demo databases from the author's research setup.

Included sample artifacts:

- `pump_research.sample.db.gz`
- `backtest_results.sample.db.gz`

`pump_research.sample.db.gz` keeps public channel names and message text but
removes personal sender IDs and reply references. `backtest_results.sample.db.gz`
contains derived backtest, context, indicator and feature tables.

Extract them into the project root before running report/backtest/model commands:

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

Do not publish:

- Telegram `.session` files
- `.env`
- private sender IDs or private invite URLs
- unsanitized personal data
