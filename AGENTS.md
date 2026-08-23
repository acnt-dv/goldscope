# Iran Gold Analysis Agent

This repository is an Iranian gold forecasting application. Apply the `iran-gold-swing-analyst` skill when changing or evaluating data feeds, forecasts, bubble analysis, support/resistance, or staged trade plans.

Project invariants:

- Keep the product scope limited to 18K and 24K gold unless the user explicitly changes it.
- Treat TGJU domestic 18K as the execution-price anchor and normalize rial to toman exactly once.
- Validate timestamp, source, age, units, and karat before forecasting. Never label cached or theoretical data as live.
- Merge the newest valid intraday quote into the edge of the daily dataset; preserve multi-year history and give recent samples more weight.
- Use chronological walk-forward tests and compare active forecasts with a no-change benchmark.
- Preserve the existing bubble, stance, five upside resistances, downside break, five lower resistances, staged entry/exit, stop, and interactive chart features.
- If data is stale or a model lacks statistical edge, display a visible warning and prefer a wait/insufficient-edge interpretation.
- Run `PYTHONPYCACHEPREFIX=/tmp/iran-gold-pycache python3 -m unittest -v` and `node --check static/app.js` after changes.

Analysis is decision support, not guaranteed profit or personalized financial advice.
