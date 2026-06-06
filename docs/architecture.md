# Architecture

The application separates market data, analysis, execution, and persistence:

- `data/`: market candles, news, and symbol universe
- `analysis/`: narrative analysis and deterministic entry/exit signals
- `trading/`: exchange adapter, sizing, risk controls, and order execution
- `infra/`: runtime policy, state files, event log, and notifications
- `dashboard/`: read-only operational view

AI output creates candidates only. Deterministic market, technical, sizing, and risk checks decide
whether a candidate can reach the exchange adapter. The default exchange path is dry-run.
