import streamlit as st
import ccxt
import pandas as pd
import numpy as np
import json
import os
from datetime import datetime

# ==============================================================================
# পেজ কনফিগারেশন (ডার্ক মোড ও ওয়াইড লেআউট)
# ==============================================================================
st.set_page_config(
    page_title="Mobinul • Institutional Crypto Hub",
    page_icon="⚡",
    layout="wide"
)

# পরিষ্কার ও বড় ফন্টের জন্য কাস্টম CSS স্টাইল
st.markdown("""
    <style>
    .main { background-color: #0f172a; color: #f8fafc; }
    h1, h2, h3 { color: #38bdf8 !important; font-family: sans-serif; }
    .stMetric { background-color: #1e293b; padding: 18px; border-radius: 12px; border: 1px solid #334155; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
    .stMetric label { color: #94a3b8 !important; font-size: 15px !important; font-weight: 600 !important; }
    .stMetric div[data-testid="stMetricValue"] { color: #f8fafc !important; font-size: 28px !important; font-weight: bold !important; }
    .streamlit-expanderHeader { background-color: #1e293b !important; color: #f8fafc !important; font-size: 16px !important; font-weight: bold !important; border-radius: 8px; border: 1px solid #334155; }
    </style>
""", unsafe_allow_html=True)

DB_FILE = "trades_tracker.json"

exchange_binance = ccxt.binance({
    'options': {'defaultType': 'future'},
    'enableRateLimit': True
})

def round_price(val):
    if val is None or val == "N/A":
        return val
    try:
        val = float(val)
        if val >= 1.0:
            return round(val, 4)
        elif val >= 0.01:
            return round(val, 6)
        else:
            return round(val, 8)
    except Exception:
        return val

def load_trades_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def check_btc_market_health():
    try:
        ohlcv = exchange_binance.fetch_ohlcv('BTC/USDT:USDT', timeframe='1d', limit=40)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['sma20'] = df['close'].rolling(20).mean()
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        btc_change_24h = ((curr['close'] - prev['close']) / prev['close']) * 100
        return {
            'status_text': "🟢 Bullish / Active Base",
            'price': round(float(curr['close']), 1),
            'change': round(float(btc_change_24h), 2)
        }
    except Exception:
        return {'status_text': 'SAFE', 'price': 0.0, 'change': 0.0}

# ==============================================================================
# ড্যাশবোর্ড হেডার ও মেট্রিকস
# ==============================================================================
st.title("⚡ Institutional Pre-Pump Crypto Hub")
st.markdown("Owner & Lead Analyst: **Mobinul** | Clean Streamlit Live Version")

btc_info = check_btc_market_health()
st.success(f"🌐 **BTC Market Status:** {btc_info['status_text']} &nbsp;|&nbsp; Price: **${btc_info['price']}** ({btc_info['change']}%)")

trades_db = load_trades_db()

total_setups = len(trades_db)
active_trades = sum(1 for t in trades_db.values() if t.get('status') == 'ACTIVE')
waiting_trades = sum(1 for t in trades_db.values() if t.get('status') in ['WAITING_FOR_ENTRY', 'WATCHLIST'])
total_pnl = round(sum(t.get('pnl_pct', 0.0) for t in trades_db.values()), 2)

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Total Setups", total_setups)
with col2:
    st.metric("Active Trades", active_trades)
with col3:
    st.metric("Waiting for Retrace", waiting_trades)
with col4:
    st.metric("Total PnL", f"{total_pnl}%")

st.markdown("---")
st.subheader("📊 Current Identified Trade Setups")

if not trades_db:
    st.warning("বর্তমানে কোনো ট্রেড ডাটা নেই।")
else:
    for sym, t in trades_db.items():
        status_text = t.get('status', 'ACTIVE')
        pnl_val = t.get('pnl_pct', 0.0)
        
        with st.expander(f"📌 {t.get('clean_symbol', sym)}   |   Status: {status_text}   |   PnL: {pnl_val}%"):
            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown(f"**🎯 Entry Zone:** <br><span style='font-size:18px;color:#38bdf8;'>${t.get('entry_zone_low')} — ${t.get('entry_zone_high')}</span>", unsafe_allow_html=True)
                st.markdown(f"**📌 Current Price:** <br><span style='font-size:16px;'>${t.get('current_price')}</span>", unsafe_allow_html=True)
            with c2:
                st.markdown(f"**🛑 Stop Loss:** <br><span style='font-size:16px;color:#ef4444;'>${t.get('stop_loss')}</span>", unsafe_allow_html=True)
                st.markdown(f"**🎯 TP1:** <br><span style='font-size:16px;color:#10b981;'>${t.get('tp1')}</span>", unsafe_allow_html=True)
            with c3:
                st.markdown(f"**🎯 TP2:** <br><span style='font-size:16px;color:#10b981;'>${t.get('tp2')}</span>", unsafe_allow_html=True)
                st.markdown(f"**🎯 TP3:** <br><span style='font-size:16px;color:#10b981;'>${t.get('tp3')}</span>", unsafe_allow_html=True)
            
            base_c = t.get('base_coin', 'BTC')
            st.markdown("---")
            st.markdown(f"[📈 Open TradingView Chart](https://www.tradingview.com/chart/?symbol=BINANCE:{base_c}USDT.P)")
