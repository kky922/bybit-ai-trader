from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import config
from infra.event_log import get_recent_events
from infra.state import PNL_FILE, POSITIONS_FILE, SNAPSHOT_FILE

load_dotenv(config.ROOT_DIR / ".env")


def _check_password() -> bool:
    if not config.DASHBOARD_PASSWORD:
        st.error("DASHBOARD_PASSWORD_COIN 환경변수를 설정하세요.")
        return False
    if "coin_dash_authed" not in st.session_state:
        st.session_state.coin_dash_authed = False
    if st.session_state.coin_dash_authed:
        return True
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Login")
    if ok and pw == config.DASHBOARD_PASSWORD:
        st.session_state.coin_dash_authed = True
        st.rerun()
    return False


def _load(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


st.set_page_config(page_title="Coin Direction Bot", layout="wide")
if not _check_password():
    st.stop()

st.title("Coin Direction Bot Dashboard")
st.caption(f"UTC {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
st_autorefresh = st.empty()
st_autorefresh.caption("Auto refresh: 30s")

positions = _load(POSITIONS_FILE, [])
pnl = _load(PNL_FILE, [])
snaps = _load(SNAPSHOT_FILE, [])

tabs = st.tabs(["현재 포지션", "수익", "섹터/내러티브", "이벤트 로그", "설정"])

with tabs[0]:
    st.dataframe(pd.DataFrame(positions), use_container_width=True)

with tabs[1]:
    df = pd.DataFrame(pnl)
    if not df.empty:
        df["date"] = pd.to_datetime(df["ts"]).dt.date
        daily = df.groupby("date")["pnl"].sum().reset_index()
        st.line_chart(daily.set_index("date"))
        st.metric("누적 실현 PnL", f"{df['pnl'].sum():.2f} USDT")
    else:
        st.info("No pnl history")

with tabs[2]:
    st.json(snaps[-1] if snaps else {})

with tabs[3]:
    st.dataframe(pd.DataFrame(get_recent_events(50)), use_container_width=True)

with tabs[4]:
    st.json(config.config_summary())
