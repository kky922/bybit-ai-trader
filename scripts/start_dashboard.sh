#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
streamlit run dashboard/coin_dashboard.py --server.port 8502
