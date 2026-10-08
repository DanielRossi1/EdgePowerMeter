# Translations (i18n)

The app's UI text is written in English and wrapped in `tr()`:

```python
from app.i18n import tr, N_
button.setText(tr("Start"))
status.setText(tr("No data received for {seconds}s", seconds=5))
COLUMNS = [N_("Voltage"), N_("Current")]  # marked now, translated later with tr(col)
```

At runtime `app/i18n` only reads `app/i18n/locales/<code>.json`. Those catalogs
are generated **offline, on your machine**, with Argos Translate models run
through CTranslate2 (no cloud service, no torch).

## Workflow

```bash
# one-time setup (separate venv, gitignored)
python3 -m venv tools/i18n/.venv
tools/i18n/.venv/bin/pip install -r tools/i18n/requirements.txt

# after changing UI strings
tools/i18n/.venv/bin/python tools/i18n/translate.py extract              # -> tools/i18n/strings.json
tools/i18n/.venv/bin/python tools/i18n/translate.py translate --prune    # fills locales/*.json
tools/i18n/.venv/bin/python tools/i18n/translate.py check                # missing/stale report
```

The first `translate` downloads the en→it/es/fr/de models (~85–100 MB each)
into `~/.cache/edgepowermeter-i18n`. After that it runs fully offline.

Only strings missing from a catalog are machine-translated, so existing
entries (including hand edits) are kept. `--prune` drops entries whose
English source no longer exists.

## Fixing bad translations

Machine translation handles sentences well but often gets short UI labels
and electrical terms wrong ("Current" → "Actuellement", "Shunt" → "shock").
Put corrections in `app/i18n/overrides/<code>.json` (`{english: fixed}`).
Overrides are merged on every `translate` run and always win over the
machine output.

## Protected text

`{placeholders}`, bracketed units (`[V]`, `(mA)`), units after a value
(`5 s`, `{value} V`) and technical names (INA226, CSV, PDF, Hz, mW, ...) are
swapped for tokens before translation and restored afterwards. If the model
mangles a placeholder, the string is left in English and a warning is printed.
