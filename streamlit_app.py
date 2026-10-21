import streamlit as st
import ccxt
import pandas as pd
import numpy as np
import json
import os
import requests
from datetime import datetime

# ==============================================================================
# কনফিগারেশন ও পেজ সেটআপ
# ==============================================================================
st.set_page_config(
    page_title="Mobinul • Institutional Crypto Hub",
    page_icon="⚡",
    layout="wide"
)

DB_FILE = "trades_tracker.json"
MAX_DAILY_SETUPS = 5

exchange_binance = ccxt.binance({
    'options': {'defaultType': 'future'},
    'enableRateLimit': True
})

EXCLUDED_NON_CRYPTO = {
    'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'GOOG', 'META', 'TSLA', 'NVDA', 'BABA', 'UBER',
    'HD', 'V', 'BRKB', 'LLY', 'NVO', 'IBM', 'NOW', 'CRM', 'COST', 'EBAY', 'GME',
    'ADBE', 'SONY', 'TTWO', 'GS', 'KO', 'RDDT', 'VST', 'SHOP', 'MRNA', 'MRK', 'TEAM', 
    'BYD', 'CRCL', 'BIDU', 'NIO', 'PDD', 'ZM', 'PAYP', 'NAVER', 'TSLL', 'SPCX', 'APP',
    'SPY', 'QQQ', 'SQQQ', 'UVXY', 'TZA', 'TMF', 'GDX', 'XLE', 'COPPER', 'CL', 'NATGAS',
    'EWJ', 'HK0700', 'HK1810', 'TENCENT', 'MEITUAN', 'XAU', 'XAG'
}

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

def save_trades_db(db):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=4)

def check_btc_market_health():
    try:
        ohlcv = exchange_binance.fetch_ohlcv('BTC/USDT:USDT', timeframe='1d', limit=40)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['sma20'] = df['close'].rolling(20).mean()
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        btc_change_24h = ((curr['close'] - prev['close']) / prev['close']) * 100
        dist20 = ((curr['close'] - curr['sma20']) / curr['sma20']) * 100
        return {
            'status_text': "🟢 Bullish / Active Base",
            'sub_text': "বিটিসি বুলিশ জোনে আছে। ট্রেড নিরাপদ।",
            'price': round(float(curr['close']), 1),
            'change': round(float(btc_change_24h), 2)
        }
    except Exception:
        return {'status_text': 'SAFE', 'sub_text': 'মার্কেট স্বাভাবিক।', 'price': 0.0, 'change': 0.0}

# ==============================================================================
# UI ডিজাইন ও ড্যাশবোর্ড
# ==============================================================================
st.markdown("""
    <style>
    .main { background-color: #0f172a; color: #f8fafc; }
    .stMetric { background-color: #1e293b; padding: 15px; border-radius: 10px; border: 1px solid #334155; }
    </style>
""", unsafe_allow_html=True)

st.title("⚡ Institutional Pre-Pump Crypto Hub")
st.markdown("Owner & Lead Analyst: **Mobinul** | Streamlit Cloud Live Version")

btc_info = check_btc_market_health()
st.info(f"🌐 **BTC Market Status:** {btc_info['status_text']} | Price: **${btc_info['price']}** ({btc_info['change']}%)")

trades_db = load_trades_db()

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Total Setups", len(trades_db))
with col2:
    active_cnt = sum(1 for t in trades_db.values() if t.get('status') == 'ACTIVE')
    st.metric("Active Trades", active_cnt)
with col3:
    waiting_cnt = sum(1 for t in trades_db.values() if t.get('status') in ['WAITING_FOR_ENTRY', 'WATCHLIST'])
    st.metric("Waiting for Retrace", waiting_cnt)
with col4:
    total_pnl = round(sum(t.get('pnl_pct', 0.0) for t in trades_db.values()), 2)
    st.metric("Total PnL", f"{total_pnl}%")

st.markdown("---")
st.subheader("📊 Current Identified Trade Setups")

if not trades_db:
    st.warning("বর্তমানে কোনো ট্রেড ডাটা নেই। গিটহাব অ্যাকশন্স থেকে ডেইলি স্ক্যান রান হওয়ার পর এখানে ট্রেড শো করবে।")
else:
    for sym, t in trades_db.items():
        with st.expander(f"📌 {t.get('clean_symbol', sym)} — Status: {t.get('status')} | PnL: {t.get('pnl_pct', 0.0)}%"):
            col_a, col_b, col_c = st.columns(3)
            with col_a:
                st.write(f"**Entry Zone:** ${t.get('entry_zone_low')} — ${t.get('entry_zone_high')}")
                st.write(f"**Current Price:** ${t.get('current_price')}")
            with col_b:
                st.write(f"**Stop Loss:** ${t.get('stop_loss')}")
                st.write(f"**TP1:** ${t.get('tp1')}")
            with col_c:
                st.write(f"**TP2:** ${t.get('tp2')}")
                st.write(f"**TP3:** ${t.get('tp3')}")
            
            st.markdown(f"[📈 Open in TradingView]({f'https://www.tradingview.com/chart/?symbol=BINANCE:{t.get(\"base_coin\")}USDT.P'})")