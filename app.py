import ccxt
import pandas as pd
import numpy as np
import time
import json
import os
import webbrowser
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

# ==============================================================================
# ১. কনফিগারেশন ও মাল্টি-এক্সচেঞ্জ ইনিশিয়ালাইজেশন
# ==============================================================================
PORT = 5000
DB_FILE = "trades_tracker.json"
HTML_FILE = "scanner_dashboard.html"
AUTO_MONITOR_INTERVAL = 60
MAX_DAILY_SETUPS = 5  # সকালের স্ক্যানে সর্বোচ্চ ১ থেকে ৫টি সেরা হাই-কনভিকশন সেটআপ

exchange_binance = ccxt.binance({
    'options': {'defaultType': 'future'},
    'enableRateLimit': True
})

exchange_bybit = ccxt.bybit({
    'options': {'defaultType': 'future'},
    'enableRateLimit': True
})

exchange_okx = ccxt.okx({
    'options': {'defaultType': 'swap'},
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

is_scanning = False
scan_status_msg = "সিস্টেম প্রস্তুত। ফ্রেশ স্ক্যানের জন্য 'Start Full Market Scan' বাটনে ক্লিক করুন।"
tokenomics_cache = {}
last_alert_event = None

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

# ==============================================================================
# ৯০ দিনের মার্কেট স্ট্রাকচার সুইং হাই ও টেক-প্রফিট ক্যালকুলেটর
# ==============================================================================
def calculate_90d_structural_targets(df, cur_price, entry_high, stop_loss):
    df_90 = df.tail(90).copy().reset_index(drop=True)
    highs = df_90['high'].values
    n = len(df_90)
    macro_90d_high = float(highs.max())
    
    swing_highs = []
    for i in range(2, n - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            if highs[i] > cur_price * 1.03:
                swing_highs.append(float(highs[i]))
                
    last_row = df.iloc[-1]
    for sma_col in ['sma50', 'sma200']:
        if sma_col in last_row and not pd.isna(last_row[sma_col]):
            v = float(last_row[sma_col])
            if v > cur_price * 1.04:
                swing_highs.append(v)
                
    swing_highs = sorted(list(set(swing_highs)))
    risk = max(entry_high - stop_loss, entry_high * 0.08)
    
    if len(swing_highs) >= 3:
        tp1 = swing_highs[0]
        tp2 = swing_highs[len(swing_highs) // 2]
        tp3 = max(swing_highs[-1], macro_90d_high)
    elif len(swing_highs) == 2:
        tp1 = swing_highs[0]
        tp2 = swing_highs[1]
        tp3 = max(macro_90d_high, tp2 * 1.15)
    elif len(swing_highs) == 1:
        tp1 = swing_highs[0]
        tp2 = (tp1 + macro_90d_high) / 2 if macro_90d_high > tp1 else tp1 * 1.15
        tp3 = max(macro_90d_high, tp2 * 1.15)
    else:
        tp1 = cur_price + (risk * 1.5)
        tp2 = cur_price + (risk * 2.5)
        tp3 = max(macro_90d_high, cur_price + (risk * 3.8))
        
    if tp1 <= entry_high: tp1 = entry_high + (risk * 1.3)
    if tp2 <= tp1: tp2 = tp1 + (risk * 1.2)
    if tp3 <= tp2: tp3 = tp2 + (risk * 1.5)
    
    return round_price(tp1), round_price(tp2), round_price(tp3), round_price(macro_90d_high)

# ==============================================================================
# ২. মাল্টি-এক্সচেঞ্জ ডেটা ফেচার
# ==============================================================================
def fetch_aggregated_market_oi_and_funding(base_coin):
    clean = base_coin.upper()
    total_oi = 0.0
    funding_rates = []
    breakdown = []

    try:
        sym_bn = f"{clean}/USDT:USDT"
        oi_bn = exchange_binance.fetch_open_interest(sym_bn)
        amt_bn = float(oi_bn.get('openInterestAmount', 0))
        if amt_bn > 0:
            total_oi += amt_bn
            breakdown.append(f"BN: {amt_bn:,.0f}")
        f_bn = exchange_binance.fetch_funding_rate(sym_bn)
        if f_bn and 'fundingRate' in f_bn and f_bn['fundingRate'] is not None:
            funding_rates.append(f_bn['fundingRate'] * 100)
    except Exception:
        pass

    try:
        sym_by = f"{clean}/USDT:USDT"
        oi_by = exchange_bybit.fetch_open_interest(sym_by)
        amt_by = float(oi_by.get('openInterestAmount', 0))
        if amt_by > 0:
            total_oi += amt_by
            breakdown.append(f"BY: {amt_by:,.0f}")
        f_by = exchange_bybit.fetch_funding_rate(sym_by)
        if f_by and 'fundingRate' in f_by and f_by['fundingRate'] is not None:
            funding_rates.append(f_by['fundingRate'] * 100)
    except Exception:
        pass

    try:
        sym_okx = f"{clean}/USDT:USDT"
        oi_okx = exchange_okx.fetch_open_interest(sym_okx)
        amt_okx = float(oi_okx.get('openInterestAmount', 0))
        if amt_okx > 0:
            total_oi += amt_okx
            breakdown.append(f"OKX: {amt_okx:,.0f}")
        f_okx = exchange_okx.fetch_funding_rate(sym_okx)
        if f_okx and 'fundingRate' in f_okx and f_okx['fundingRate'] is not None:
            funding_rates.append(f_okx['fundingRate'] * 100)
    except Exception:
        pass

    oi_display = f"{total_oi:,.0f}" if total_oi > 0 else "N/A"
    oi_detail = " | ".join(breakdown) if breakdown else "Single Exchange"
    avg_funding = sum(funding_rates) / len(funding_rates) if funding_rates else 0.0
    funding_display = f"{avg_funding:+.4f}%"

    return oi_display, oi_detail, funding_display, avg_funding

def fetch_daily_oi_and_flow_history(base_coin):
    symbol_pair = f"{base_coin}USDT"
    oi_map = {}
    try:
        url_oi = f"https://fapi.binance.com/futures/data/openInterestHist?symbol={symbol_pair}&period=1d&limit=8"
        r = requests.get(url_oi, timeout=4)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                for i in range(len(data)):
                    item = data[i]
                    d_str = datetime.fromtimestamp(item['timestamp'] / 1000).strftime('%Y-%m-%d')
                    oi_amount = float(item['sumOpenInterest'])
                    chg_str = "0.0%"
                    raw_pct = 0.0
                    if i > 0:
                        prev_oi = float(data[i-1]['sumOpenInterest'])
                        if prev_oi > 0:
                            raw_pct = ((oi_amount - prev_oi) / prev_oi) * 100
                            chg_str = f"{raw_pct:+.1f}%"
                    oi_map[d_str] = {
                        'oi_display': f"{oi_amount:,.0f} ({chg_str})",
                        'oi_val': oi_amount,
                        'oi_chg_pct': raw_pct
                    }
    except Exception:
        pass
    return oi_map

def get_whale_taker_ratio(base_coin):
    try:
        url = f"https://fapi.binance.com/futures/data/takerlongshortRatio?symbol={base_coin}USDT&period=1d&limit=2"
        r = requests.get(url, timeout=3)
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0:
                ratio = round(float(data[-1]['buySellRatio']), 2)
                bias = "🟢 Aggressive Whale Buying" if ratio >= 1.20 else ("🟡 Neutral Float" if ratio >= 1.0 else "🔴 Selling Pressure")
                return f"{ratio}x", bias
    except Exception:
        pass
    return "1.00x", "🟡 Neutral Float"

def get_token_supply_info(base_coin):
    coin_clean = base_coin.upper()
    if coin_clean in tokenomics_cache:
        return tokenomics_cache[coin_clean]
    
    supply_data = {'circulating': 'N/A', 'total': 'N/A', 'circ_ratio': 'N/A', 'unlock_warning': 'Normal Float 🟢'}
    try:
        url = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&symbols={coin_clean.lower()}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=4)
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list):
                item = data[0]
                circ = item.get('circulating_supply')
                total = item.get('total_supply') or item.get('max_supply')
                if circ: supply_data['circulating'] = f"{circ:,.0f}"
                if total: supply_data['total'] = f"{total:,.0f}"
                if circ and total and total > 0:
                    ratio = (circ / total) * 100
                    supply_data['circ_ratio'] = f"{ratio:.1f}%"
                    if ratio < 35.0:
                        supply_data['unlock_warning'] = '⚠️ High Unlock / Dump Risk (Low Float)'
                    elif ratio > 75.0:
                        supply_data['unlock_warning'] = 'Safe Float / Low Dilution 🟢'
                    else:
                        supply_data['unlock_warning'] = 'Moderate Float 🟡'
    except Exception:
        pass
        
    tokenomics_cache[coin_clean] = supply_data
    return supply_data

# ==============================================================================
# ৩. ডাটাবেস হ্যান্ডলার
# ==============================================================================
def load_trades_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                cleaned = {}
                for k, v in data.items():
                    if k.split('/')[0].upper() in EXCLUDED_NON_CRYPTO:
                        continue
                    if 'triggered_at' not in v or not v['triggered_at']:
                        v['triggered_at'] = f"{v.get('entry_date', datetime.now().strftime('%Y-%m-%d'))} 00:00:00"
                    if v.get('sl_note') == "Breakeven Locked 🔒" and v.get('status') == 'ACTIVE':
                        v['status'] = 'TP1_HIT'
                    cleaned[k] = v
                return cleaned
        except Exception:
            return {}
    return {}

def save_trades_db(db):
    cleaned = {k: v for k, v in db.items() if k.split('/')[0].upper() not in EXCLUDED_NON_CRYPTO}
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(cleaned, f, indent=4)

# ==============================================================================
# ৪. বিটকয়েন ম্যাক্রো হেলথ ফিল্টার
# ==============================================================================
def check_btc_market_health():
    try:
        ohlcv = exchange_binance.fetch_ohlcv('BTC/USDT:USDT', timeframe='1d', limit=40)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['sma20'] = df['close'].rolling(20).mean()
        df['sma50'] = df['close'].rolling(50).mean()
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        
        btc_change_24h = ((curr['close'] - prev['close']) / prev['close']) * 100
        dist20 = ((curr['close'] - curr['sma20']) / curr['sma20']) * 100
        
        if dist20 >= 0 and curr['close'] >= curr['sma50']:
            zone = "🟢 Bullish (Above SMA20)"
            is_safe = True
            msg = "বিটিসি সম্পূর্ণ বুলিশ জোনে আছে। অল্টকয়েন ট্রেড নিরাপদ।"
        elif -3.5 <= dist20 < 0 and btc_change_24h > -2.2:
            zone = "🟡 Consolidation / Sideways Base"
            is_safe = True
            msg = "বিটিসি শান্ত সাইডওয়েজ জোনে অবস্থান করছে। এন্ট্রি জোন কার্যকর।"
        else:
            zone = "🔴 Bearish / Dump Zone"
            is_safe = False
            msg = "বিটিসি ডাম্প জোনে! ওয়াচলিস্ট মোড কার্যকর।"
            
        return {
            'is_safe': is_safe,
            'status_text': zone,
            'sub_text': msg,
            'price': round(float(curr['close']), 1),
            'change': round(float(btc_change_24h), 2),
            'dist20': round(float(dist20), 2)
        }
    except Exception:
        return {'is_safe': True, 'status_text': 'SAFE', 'sub_text': 'মার্কেট স্বাভাবিক রয়েছে।', 'price': 0.0, 'change': 0.0, 'dist20': 0.0}

# ==============================================================================
# ৫. টেকনিক্যাল ইন্ডিকেটর ও কড়া মাল্টি-টাইমফ্রেম (MTF) এন্ট্রি ভ্যালিডেটর
# ==============================================================================
def calculate_indicators(df):
    close, high, low = df['close'], df['high'], df['low']
    df['sma20']  = close.rolling(20).mean()
    df['sma50']  = close.rolling(50).mean()
    df['sma200'] = close.rolling(200).mean()
    
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = (100 - (100 / (1 + rs))).fillna(50)
    
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    df['avg_vol20'] = df['volume'].rolling(20).mean()
    df['rvol'] = df['volume'] / df['avg_vol20'].replace(0, np.nan)
    return df

def check_strict_retrace_entry(symbol, entry_low, entry_high, cur_price):
    if entry_low <= cur_price <= entry_high:
        return True, "Direct Retrace in Buy Zone (1D)"
        
    try:
        for tf in ['15m', '1h', '4h']:
            ohlcv = exchange_binance.fetch_ohlcv(symbol, timeframe=tf, limit=25)
            if len(ohlcv) < 20: continue
            df_tf = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_tf['sma20'] = df_tf['close'].rolling(20).mean()
            last_c = df_tf.iloc[-1]
            sma20_tf = float(last_c['sma20'])
            if not np.isnan(sma20_tf):
                tf_dist = ((cur_price - sma20_tf) / sma20_tf) * 100
                if -0.5 <= tf_dist <= 1.2 and cur_price >= float(last_c['open']):
                    return True, f"MTF Precision Retrace ({tf} SMA20 Rebound)"
    except Exception:
        pass

    return False, None

# ==============================================================================
# ৬. লাইভ পজিশন ও ট্রেইলিং ব্রেক-ইভেন ট্র্যাকার (স্ট্যাটাস লক সহ)
# ==============================================================================
def update_existing_trades(trades_db):
    global last_alert_event
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today_str = datetime.now().strftime('%Y-%m-%d')
    updated_count = 0
    for symbol, trade in list(trades_db.items()):
        current_status = trade.get('status', 'ACTIVE')
        if current_status in ['INVALIDATED']:
            continue
        try:
            ticker = exchange_binance.fetch_ticker(symbol)
            cur_price = float(ticker['last'])
            trade['current_price'] = cur_price
            
            entry_low = float(trade['entry_zone_low'])
            entry_high = float(trade['entry_zone_high'])
            sl = float(trade['stop_loss'])
            tp1 = float(trade['tp1'])
            tp2 = float(trade['tp2'])
            tp3 = float(trade['tp3'])
            actual_entry = trade.get('entry_price', trade['entry_zone_high'])
            
            if current_status in ['WAITING_FOR_ENTRY', 'WATCHLIST']:
                is_triggered, trigger_reason = check_strict_retrace_entry(symbol, entry_low, entry_high, cur_price)
                if is_triggered:
                    trade['status'] = 'ACTIVE'
                    trade['entry_price'] = round_price(cur_price)
                    trade['triggered_at'] = now_str
                    trade['triggered_date'] = today_str
                    trade['pnl_pct'] = 0.0
                    trade['entry_note'] = trigger_reason
                    last_alert_event = f"🎯 {trade['clean_symbol']} Activated via {trigger_reason} at ${cur_price}"
                elif cur_price < sl:
                    trade['status'] = 'INVALIDATED'
                    trade['exit_date'] = today_str
                continue

            if isinstance(actual_entry, (int, float)) and actual_entry > 0:
                trade['pnl_pct'] = round(((cur_price - actual_entry) / actual_entry) * 100, 2)
            
            # স্ট্যাটাস লক
            if current_status == 'TP3_HIT':
                pass
            elif current_status == 'TP2_HIT':
                if cur_price >= tp3:
                    trade['status'] = 'TP3_HIT'
                    trade['exit_date'] = today_str
                    last_alert_event = f"🚀 {trade['clean_symbol']} অল-টার্গেট TP3 স্পর্শ করেছে!"
                elif cur_price <= sl:
                    trade['status'] = 'SL_HIT'
                    trade['exit_date'] = today_str
            elif current_status == 'TP1_HIT':
                if cur_price >= tp3:
                    trade['status'] = 'TP3_HIT'
                    trade['exit_date'] = today_str
                elif cur_price >= tp2:
                    trade['status'] = 'TP2_HIT'
                    trade['stop_loss'] = round_price(tp1)
                    trade['sl_note'] = "Trailed to TP1 🔒"
                    trade['exit_date'] = today_str
                elif cur_price <= sl:
                    trade['status'] = 'SL_HIT'
                    trade['exit_date'] = today_str
            else:
                if cur_price >= tp3:
                    trade['status'] = 'TP3_HIT'
                    trade['exit_date'] = today_str
                    last_alert_event = f"🚀 {trade['clean_symbol']} TP3 Hit!"
                elif cur_price >= tp2:
                    trade['status'] = 'TP2_HIT'
                    trade['stop_loss'] = round_price(tp1)
                    trade['sl_note'] = "Trailed to TP1 🔒"
                    trade['exit_date'] = today_str
                elif cur_price >= tp1:
                    trade['status'] = 'TP1_HIT'
                    trade['stop_loss'] = round_price(actual_entry)
                    trade['sl_note'] = "Breakeven Locked 🔒"
                elif cur_price <= sl:
                    trade['status'] = 'SL_HIT'
                    trade['exit_date'] = today_str
                    last_alert_event = f"⚠️ {trade['clean_symbol']} SL Hit"
                else:
                    trade['status'] = 'ACTIVE'
                
            updated_count += 1
        except Exception:
            pass
            
    save_trades_db(trades_db)
    return updated_count

def background_auto_tracker():
    while True:
        time.sleep(AUTO_MONITOR_INTERVAL)
        try:
            if not is_scanning:
                t_db = load_trades_db()
                if t_db:
                    update_existing_trades(t_db)
        except Exception:
            pass

threading.Thread(target=background_auto_tracker, daemon=True).start()

# ==============================================================================
# ৭. T-1 ক্লোজড ক্যান্ডেল ক্লাসিফিকেশন লজিক
# ==============================================================================
def classify_setup(prev_closed, sma20_dist, sma50_dist, sma200_dist, max_rvol, funding_rate_num, has_fvg, btc_safe):
    rsi = prev_closed['rsi']
    upper_wick = prev_closed['high'] - max(prev_closed['open'], prev_closed['close'])
    total_range = prev_closed['high'] - prev_closed['low']
    
    if sma200_dist != "N/A" and float(sma200_dist.replace('%', '')) > 25.0:
        return "⚠️ Overextended Late-Cycle / Avoid", "#ef4444", "whale-trap", False, 0
        
    if (sma20_dist > 10.0 or rsi > 70) or (total_range > 0 and upper_wick / total_range > 0.45 and max_rvol > 2.5):
        return "⚠️ Whale Trap / Avoid", "#ef4444", "whale-trap", False, 0
        
    if funding_rate_num < -0.010 and max_rvol >= 2.5:
        return "🚨 Extreme Short Squeeze", "#f59e0b", "short-squeeze", True, 95
        
    is_prime_stage1 = (
        (sma200_dist == "N/A" or float(sma200_dist.replace('%', '')) <= 10.0) and
        (0.0 <= sma20_dist <= 7.0) and
        (max_rvol >= 2.0) and
        (48 <= rsi <= 66) and
        has_fvg
    )
    if is_prime_stage1:
        badge = "💎 Stage-1 Reversal (86%+ DNA)" if btc_safe else "👀 Stage-1 Watchlist (BTC Paused)"
        return badge, "#10b981", "a-plus", True, 90
        
    is_sma50_base = (abs(float(sma50_dist.replace('%', ''))) <= 3.5) and (max_rvol >= 1.8)
    if is_sma50_base:
        badge = "⚡ 50 SMA Mid-Base Rebound" if btc_safe else "👀 50 SMA Watchlist (BTC Paused)"
        return badge, "#38bdf8", "early-momentum", True, 85

    if (0.0 <= sma20_dist <= 8.0) and max_rvol >= 1.8:
        badge = "⚡ Early Momentum" if btc_safe else "👀 Early Momentum (Watchlist)"
        return badge, "#38bdf8", "early-momentum", True, 80

    return "Normal Setup", "#6b7280", "normal", False, 0

# ==============================================================================
# ৮. সম্পূর্ণ ফ্রেশ "Full Market Scan" ইঞ্জিন (সর্বোচ্চ ৫টি সেটআপ)
# ==============================================================================
def run_full_scan():
    global is_scanning, scan_status_msg
    is_scanning = True
    scan_status_msg = "ফুল মার্কেট স্ক্যানিং শুরু হচ্ছে... বিগত দিনের ক্লোজড ডেটা এনালাইসিস চলছে।"
    
    try:
        btc_health = check_btc_market_health()
        trades_db = {}
        
        markets = exchange_binance.load_markets()
        all_symbols = [
            s for s, m in markets.items()
            if '/USDT:USDT' in s and m.get('active', True) and m.get('linear', True)
            and s.split('/')[0].upper() not in EXCLUDED_NON_CRYPTO
        ]
        
        total = len(all_symbols)
        today_str = datetime.now().strftime('%Y-%m-%d')
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        qualified_candidates = []
        
        for idx, symbol in enumerate(all_symbols, start=1):
            clean = symbol.split('/')[0]
            scan_status_msg = f"ফুল মার্কেট স্ক্যান হচ্ছে [{idx}/{total}]: {clean}"
            
            try:
                ohlcv = exchange_binance.fetch_ohlcv(symbol, timeframe='1d', limit=250)
                if len(ohlcv) < 35: continue
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
                df = calculate_indicators(df)
                
                prev_closed = df.iloc[-2]
                before_prev = df.iloc[-3]
                curr_live = df.iloc[-1]
                
                past_7d_gain = ((prev_closed['close'] - df['close'].iloc[-8]) / df['close'].iloc[-8]) * 100
                past_30d_low = df['low'].tail(31).iloc[:-1].min()
                past_30d_gain = ((prev_closed['close'] - past_30d_low) / past_30d_low) * 100
                if past_7d_gain > 25.0 or past_30d_gain > 55.0:
                    continue
                
                sma20_val = prev_closed['sma20']
                if pd.isna(sma20_val): continue
                sma20_dist = ((prev_closed['close'] - sma20_val) / sma20_val) * 100
                if sma20_dist < -2.0 or sma20_dist > 8.5:
                    continue
                
                t1_rvol = float(prev_closed['rvol'])
                if t1_rvol < 1.8:
                    continue
                
                has_fvg = (prev_closed['low'] > before_prev['high']) or (prev_closed['close'] > before_prev['high'])
                
                oi_val_str, oi_detail_str, funding_rate_str, funding_rate_num = fetch_aggregated_market_oi_and_funding(clean)
                whale_taker_ratio, whale_flow_tag = get_whale_taker_ratio(clean)

                sma50_val = round_price(prev_closed['sma50']) if not pd.isna(prev_closed['sma50']) else "N/A"
                sma50_dist_str = f"{round(((prev_closed['close'] - prev_closed['sma50']) / prev_closed['sma50']) * 100, 2)}%" if not pd.isna(prev_closed['sma50']) else "N/A"
                
                sma200_val = round_price(prev_closed['sma200']) if not pd.isna(prev_closed['sma200']) else "New Coin (<200D)"
                sma200_dist_str = f"{round(((prev_closed['close'] - prev_closed['sma200']) / prev_closed['sma200']) * 100, 2)}%" if not pd.isna(prev_closed['sma200']) else "N/A"
                
                cat_label, cat_color, cat_slug, is_valid_trade, score = classify_setup(
                    prev_closed, sma20_dist, sma50_dist_str, sma200_dist_str, t1_rvol, funding_rate_num, has_fvg, btc_health['is_safe']
                )
                
                if not is_valid_trade:
                    continue
                
                cur_live_price = float(curr_live['close'])
                entry_zone_low = round_price(min(float(sma20_val), float(prev_closed['close'])))
                entry_zone_high = round_price(float(prev_closed['high']))
                stop_loss = round_price(entry_zone_low * 0.90)
                
                tp1, tp2, tp3, macro_90d_high = calculate_90d_structural_targets(df, cur_live_price, entry_zone_high, stop_loss)
                
                is_retrace_active, retrace_msg = check_strict_retrace_entry(symbol, entry_zone_low, entry_zone_high, cur_live_price)
                if is_retrace_active and btc_health['is_safe']:
                    initial_status = "ACTIVE"
                    triggered_at = now_str
                    entry_price_val = round_price(cur_live_price)
                else:
                    initial_status = "WAITING_FOR_ENTRY"
                    triggered_at = "Pending Retrace"
                    entry_price_val = "Pending"
                
                supply_info = get_token_supply_info(clean)
                daily_oi_map = fetch_daily_oi_and_flow_history(clean)
                
                past_7d = df.tail(8).iloc[:-1].copy()
                daily_breakdown = []
                for i in range(len(past_7d)):
                    d_row = past_7d.iloc[i]
                    d_str = d_row['datetime'].strftime('%Y-%m-%d')
                    d_dist20 = ((d_row['close'] - d_row['sma20']) / d_row['sma20']) * 100 if not pd.isna(d_row['sma20']) else 0.0
                    d_dist50 = ((d_row['close'] - d_row['sma50']) / d_row['sma50']) * 100 if not pd.isna(d_row['sma50']) else 0.0
                    day_oi_info = daily_oi_map.get(d_str, {'oi_display': 'N/A'})
                    is_bull = d_row['close'] >= d_row['open']
                    daily_breakdown.append({
                        'date': d_str,
                        'close': round_price(d_row['close']),
                        'sma20_dist': f"{round(d_dist20, 2)}%",
                        'sma50_dist': f"{round(d_dist50, 2)}%",
                        'rvol': f"{round(d_row['rvol'], 2)}x",
                        'daily_oi': day_oi_info['oi_display'],
                        'exchange_flow': "🟢 Outflow" if is_bull else "🔴 Inflow",
                        'type': '🟢 Bullish' if is_bull else '🔴 Bearish'
                    })
                
                candidate_data = {
                    'symbol': symbol,
                    'clean_symbol': clean + '/USDT',
                    'base_coin': clean,
                    'score': score + (t1_rvol * 2),
                    'category': cat_label,
                    'category_color': cat_color,
                    'category_slug': cat_slug,
                    'funding_rate': funding_rate_str,
                    'open_interest': oi_val_str,
                    'open_interest_detail': oi_detail_str,
                    'whale_taker_ratio': whale_taker_ratio,
                    'whale_flow_tag': whale_flow_tag,
                    'entry_date': today_str,
                    'triggered_at': triggered_at,
                    'exit_date': "RPT_RUNNING (Active)",
                    'entry_zone_low': entry_zone_low,
                    'entry_zone_high': entry_zone_high,
                    'entry_price': entry_price_val,
                    'current_price': round_price(cur_live_price),
                    'stop_loss': stop_loss,
                    'tp1': tp1,
                    'tp2': tp2,
                    'tp3': tp3,
                    'macro_90d_high': macro_90d_high,
                    'rsi': round(float(prev_closed['rsi']), 1),
                    'sma20_val': round_price(sma20_val),
                    'sma20_dist': round(sma20_dist, 2),
                    'sma50_val': sma50_val,
                    'sma50_dist': sma50_dist_str,
                    'sma200_val': sma200_val,
                    'sma200_dist': sma200_dist_str,
                    'circulating': supply_info['circulating'],
                    'total_supply': supply_info['total'],
                    'circ_ratio': supply_info['circ_ratio'],
                    'unlock_warning': supply_info['unlock_warning'],
                    'rvol': round(t1_rvol, 2),
                    'status': initial_status,
                    'pnl_pct': 0.0,
                    'daily_history': daily_breakdown
                }
                qualified_candidates.append(candidate_data)
            except Exception:
                continue
            time.sleep(0.02)
            
        qualified_candidates = sorted(qualified_candidates, key=lambda x: x['score'], reverse=True)
        top_candidates = qualified_candidates[:MAX_DAILY_SETUPS]
        
        fresh_db = {cand['symbol']: cand for cand in top_candidates}
        save_trades_db(fresh_db)
        
        top_names = ", ".join([c['base_coin'] for c in top_candidates]) if top_candidates else "কোনো কয়েন পাওয়া যায়নি"
        scan_status_msg = f"✅ ফুল মার্কেট স্ক্যান সম্পন্ন! আজকের ফ্রেশ সেরা সেটআপ ({len(top_candidates)}টি): {top_names}"
    except Exception as e:
        scan_status_msg = f"স্ক্যানিং এরর: {e}"
    finally:
        is_scanning = False

# ==============================================================================
# ৯. সিঙ্গল কয়েন ইন্সপেক্টর (লং ও শর্ট মোড)
# ==============================================================================
def inspect_single_coin(coin_input):
    base = coin_input.upper().replace('USDT', '').replace('/', '').strip()
    symbol = f"{base}/USDT:USDT"
    
    try:
        btc_health = check_btc_market_health()
        ohlcv = exchange_binance.fetch_ohlcv(symbol, timeframe='1d', limit=130)
        if len(ohlcv) < 35:
            return {'success': False, 'error': f'{base} কয়েনের পর্যাপ্ত হিস্ট্রি নেই।'}
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = calculate_indicators(df)
        
        df_90d = df.tail(90).copy()
        low_90d = round_price(float(df_90d['low'].min()))
        high_90d = round_price(float(df_90d['high'].max()))
        curr = df.iloc[-1]
        cur_price = float(curr['close'])
        
        gain_from_90d_low = round(((cur_price - low_90d) / low_90d) * 100, 2)
        dist_from_90d_high = round(((cur_price - high_90d) / high_90d) * 100, 2)
        
        sma20_val = float(curr['sma20']) if not pd.isna(curr['sma20']) else cur_price
        sma50_val = float(curr['sma50']) if not pd.isna(curr['sma50']) else cur_price
        sma200_val = float(curr['sma200']) if not pd.isna(curr['sma200']) else "N/A"
        
        sma20_dist = round(((cur_price - sma20_val) / sma20_val) * 100, 2)
        sma50_dist_str = f"{round(((cur_price - sma50_val) / sma50_val) * 100, 2)}%"
        sma200_dist_num = round(((cur_price - sma200_val) / sma200_val) * 100, 2) if sma200_val != "N/A" else 0.0
        sma200_dist_str = f"{sma200_dist_num}%" if sma200_val != "N/A" else "N/A"
        
        recent_max_rvol = df['rvol'].tail(3).max()
        has_fvg = (df['low'].iloc[-1] > df['high'].iloc[-3]) or (curr['close'] > df['high'].iloc[-2])
        
        oi_val_str, oi_detail_str, funding_rate_str, funding_rate_num = fetch_aggregated_market_oi_and_funding(base)
        whale_taker_ratio, whale_flow_tag = get_whale_taker_ratio(base)
        supply_info = get_token_supply_info(base)
        
        daily_oi_map = fetch_daily_oi_and_flow_history(base)
        past_7d = df.tail(7).copy()
        daily_breakdown = []
        for i in range(len(past_7d)):
            d_row = past_7d.iloc[i]
            d_str = d_row['datetime'].strftime('%Y-%m-%d')
            d_dist20 = ((d_row['close'] - d_row['sma20']) / d_row['sma20']) * 100 if not pd.isna(d_row['sma20']) else 0.0
            d_dist50 = ((d_row['close'] - d_row['sma50']) / d_row['sma50']) * 100 if not pd.isna(d_row['sma50']) else 0.0
            day_oi_info = daily_oi_map.get(d_str, {'oi_display': 'N/A', 'oi_chg_pct': 0.0})
            is_bull = d_row['close'] >= d_row['open']
            daily_breakdown.append({
                'date': d_str,
                'close': round_price(d_row['close']),
                'sma20_dist': f"{round(d_dist20, 2)}%",
                'sma50_dist': f"{round(d_dist50, 2)}%",
                'rvol': f"{round(d_row['rvol'], 2)}x",
                'daily_oi': day_oi_info['oi_display'],
                'exchange_flow': "🟢 Outflow" if is_bull else "🔴 Inflow",
                'type': '🟢 Bullish' if is_bull else '🔴 Bearish'
            })

        is_overextended = (sma200_dist_num > 25.0) or (sma20_dist > 14.0)
        has_sell_exhaustion = (curr['rsi'] >= 68) or (funding_rate_num > 0.015)
        is_short_setup = is_overextended and has_sell_exhaustion
        tv_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{base}USDT.P"
        
        if is_short_setup:
            setup_direction = "SHORT 🔻"
            cat_label = "🚨 Institutional Short Exhaustion (Sell Zone)"
            cat_color = "#ef4444"
            direction_color = "#ef4444"
            zone_low = round_price(cur_price)
            zone_high = round_price(float(curr['high']))
            stop_loss = round_price(float(curr['high']) * 1.06)
            tp1 = round_price(sma20_val if sma20_val < cur_price else cur_price * 0.90)
            tp2 = round_price(sma50_val if sma50_val < cur_price else cur_price * 0.80)
            tp3 = round_price(sma200_val if (sma200_val != "N/A" and sma200_val < cur_price) else cur_price * 0.65)
            trade_status = "READY FOR SHORT ENTRY"
            analysis_summary = "দাম মারাত্মক উপরে উঠে গেছে এবং সেল একজশন তৈরি হয়েছে।"
        else:
            setup_direction = "LONG 🟢"
            direction_color = "#10b981"
            cat_label, cat_color, cat_slug, is_valid, _ = classify_setup(
                curr, sma20_dist, sma50_dist_str, sma200_dist_str, recent_max_rvol, funding_rate_num, has_fvg, btc_health['is_safe']
            )
            zone_low = round_price(min(sma20_val, cur_price))
            zone_high = round_price(cur_price)
            stop_loss = round_price(min(sma20_val, cur_price) * 0.90)
            tp1, tp2, tp3, macro_90d_high = calculate_90d_structural_targets(df, cur_price, zone_high, stop_loss)
            is_retrace_active, _ = check_strict_retrace_entry(symbol, zone_low, zone_high, cur_price)
            trade_status = "ACTIVE" if (is_retrace_active and btc_health['is_safe']) else ("WAITING_FOR_ENTRY" if btc_health['is_safe'] else "WATCHLIST (BTC Paused)")
            analysis_summary = "কয়েনটি বটম সাপোর্ট বা মুভিং এভারেজ কম্প্রেশনে রয়েছে। ৯০ দিনের স্ট্রাকচারাল টার্গেট সক্রিয়।"

        return {
            'success': True,
            'clean_symbol': f"{base}/USDT",
            'cur_price': round_price(cur_price),
            'tv_link': tv_link,
            'setup_direction': setup_direction,
            'direction_color': direction_color,
            'category': cat_label,
            'category_color': cat_color,
            'status': trade_status,
            'analysis_summary': analysis_summary,
            'low_90d': low_90d,
            'high_90d': high_90d,
            'gain_from_90d_low': f"+{gain_from_90d_low}%",
            'dist_from_90d_high': f"{dist_from_90d_high}%",
            'entry_zone_low': zone_low,
            'entry_zone_high': zone_high,
            'stop_loss': stop_loss,
            'tp1': tp1,
            'tp2': tp2,
            'tp3': tp3,
            'open_interest': oi_val_str,
            'open_interest_detail': oi_detail_str,
            'whale_taker_ratio': whale_taker_ratio,
            'whale_flow_tag': whale_flow_tag,
            'funding_rate': funding_rate_str,
            'sma20_val': round_price(sma20_val),
            'sma20_dist': sma20_dist,
            'sma50_val': round_price(sma50_val) if sma50_val != "N/A" else "N/A",
            'sma50_dist': sma50_dist_str,
            'sma200_val': round_price(sma200_val) if sma200_val != "N/A" else "N/A",
            'sma200_dist': sma200_dist_str,
            'circulating': supply_info['circulating'],
            'total_supply': supply_info['total'],
            'circ_ratio': supply_info['circ_ratio'],
            'unlock_warning': supply_info['unlock_warning'],
            'daily_history': daily_breakdown
        }
    except Exception as e:
        return {'success': False, 'error': f'{base} অনুসন্ধান এরর: {e}'}

# ==============================================================================
# ১০. সম্পূর্ণ এইচটিএমএল ড্যাশবোর্ড জেনারেটর (অ্যাকর্ডিয়ন ড্রপডাউন সহ)
# ==============================================================================
def build_html_page():
    trades_db = load_trades_db()
    btc_health = check_btc_market_health()
    
    crypto_trades = {k: v for k, v in trades_db.items() if k.split('/')[0].upper() not in EXCLUDED_NON_CRYPTO}
    total_trades = len(crypto_trades)
    active_trades = sum(1 for t in crypto_trades.values() if t['status'] == 'ACTIVE')
    waiting_trades = sum(1 for t in crypto_trades.values() if t['status'] in ['WAITING_FOR_ENTRY', 'WATCHLIST'])
    tp1_hits = sum(1 for t in crypto_trades.values() if t['status'] == 'TP1_HIT')
    tp2_hits = sum(1 for t in crypto_trades.values() if t['status'] == 'TP2_HIT')
    tp3_hits = sum(1 for t in crypto_trades.values() if t['status'] == 'TP3_HIT')
    sl_hits = sum(1 for t in crypto_trades.values() if t['status'] == 'SL_HIT')
    
    count_all = total_trades
    count_waiting = waiting_trades
    count_active = active_trades
    count_stage1 = sum(1 for t in crypto_trades.values() if t.get('category_slug') == 'a-plus')
    count_short_squeeze = sum(1 for t in crypto_trades.values() if t.get('category_slug') == 'short-squeeze')
    count_early_momentum = sum(1 for t in crypto_trades.values() if t.get('category_slug') == 'early-momentum')
    count_whale_trap = sum(1 for t in crypto_trades.values() if t.get('category_slug') == 'whale-trap')

    total_pnl = round(sum(t.get('pnl_pct', 0.0) for t in crypto_trades.values() if t['status'] not in ['WAITING_FOR_ENTRY', 'WATCHLIST', 'INVALIDATED']), 2)
    pnl_color = "#10b981" if total_pnl >= 0 else "#ef4444"
    btc_banner_color = "#10b98120" if btc_health['is_safe'] else "#f59e0b20"
    btc_border_color = "#10b981" if btc_health['is_safe'] else "#f59e0b"

    cards_html = ""
    for idx, (sym, t) in enumerate(crypto_trades.items()):
        st = t['status']
        st_color = {
            "ACTIVE": "#10b981", 
            "WAITING_FOR_ENTRY": "#f59e0b", 
            "WATCHLIST": "#eab308", 
            "TP1_HIT": "#059669", 
            "TP2_HIT": "#047857", 
            "TP3_HIT": "#065f46", 
            "SL_HIT": "#ef4444", 
            "INVALIDATED": "#6b7280"
        }.get(st, "#6b7280")
        
        pnl_val = t.get('pnl_pct', 0.0)
        pnl_badge_color = "#10b981" if pnl_val >= 0 else "#ef4444"
        cat_badge = t.get('category', '⚡ Early Momentum')
        cat_color = t.get('category_color', '#38bdf8')
        cat_slug = t.get('category_slug', 'early-momentum')
        exit_date_display = t.get('exit_date', 'RPT_RUNNING (Active)')
        
        zone_low = round_price(t.get('entry_zone_low', t['entry_price']))
        zone_high = round_price(t.get('entry_zone_high', t['entry_price']))
        clean_base = t.get('base_coin', sym.split('/')[0]).upper()
        tv_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{clean_base}USDT.P"
        sl_note = f" <span style='font-size:11px;color:#38bdf8;'>({t.get('sl_note', '')})</span>" if t.get('sl_note') else ""
        
        activated_time = t.get('triggered_at', 'Pending Zone Retest')
        act_color = "#10b981" if st in ["ACTIVE", "TP1_HIT", "TP2_HIT", "TP3_HIT"] else "#f59e0b"
        status_filter_class = "status-active" if st in ["ACTIVE", "TP1_HIT", "TP2_HIT", "TP3_HIT"] else ("status-waiting" if st in ["WAITING_FOR_ENTRY", "WATCHLIST"] else "status-other")

        hist_rows = ""
        for h in t.get('daily_history', []):
            hist_rows += f"""
            <tr>
                <td>{h['date']}</td>
                <td><b>${round_price(h['close'])}</b></td>
                <td>{h['sma20_dist']}</td>
                <td>{h.get('sma50_dist', 'N/A')}</td>
                <td>{h['rvol']}</td>
                <td style="color:#38bdf8;font-weight:600;">{h.get('daily_oi', 'N/A')}</td>
                <td>{h.get('exchange_flow', 'Normal Flow')}</td>
                <td>{h['type']}</td>
            </tr>
            """

        details_id = f"details_content_{idx}"

        cards_html += f"""
        <div class="coin-card {status_filter_class}" data-category="{cat_slug}" data-status="{st}">
            <!-- সংক্ষেপিত হেডলাইন স্ট্রিপ: এখানে ক্লিক করলেই নিচের ড্রপডাউনটি খুলবে বা বন্ধ হবে -->
            <div class="card-summary-strip" onclick="toggleCardDetails('{details_id}', this)">
                <div class="strip-left">
                    <div class="strip-title-row">
                        <span class="coin-name">{t.get('clean_symbol', sym)}</span>
                        <span class="badge" style="background:{cat_color}">{cat_badge}</span>
                        <span class="badge" style="background:{st_color}">{st}</span>
                        <a href="{tv_link}" target="_blank" class="tv-btn" onclick="event.stopPropagation();">📈 TradingView</a>
                    </div>
                    <div class="strip-meta-row">
                        <span>🗓️ <b>Found:</b> {t['entry_date']}</span>
                        <span class="dot-sep">•</span>
                        <span>⚡ <b>Activated:</b> <b style="color:{act_color};">{activated_time}</b></span>
                        <span class="dot-sep">•</span>
                        <span>🎯 <b>Trigger Status:</b> <span style="color:#f59e0b">{exit_date_display}</span></span>
                        <span class="dot-sep">•</span>
                        <span>Avg Funding: <b>{t.get('funding_rate', '0.0%')}</b></span>
                    </div>
                </div>
                <div class="strip-right">
                    <div class="pnl-box" style="color: {pnl_badge_color}">
                        {'+' if pnl_val >= 0 else ''}{pnl_val}% PnL
                    </div>
                    <div class="expand-arrow">▼</div>
                </div>
            </div>

            <!-- ড্রপডাউন বডি: ক্লিক করলে এই পুরো বিস্তারিত অংশটি বের হবে -->
            <div class="card-expand-body" id="{details_id}">
                <div class="entry-zone-banner">
                    <span class="ez-label">🎯 RETRACE BUY ZONE:</span>
                    <span class="ez-val">${zone_low} &nbsp;—&nbsp; ${zone_high}</span>
                    <span class="ez-status">({'⏳ পুলব্যাকের অপেক্ষায়...' if st in ['WAITING_FOR_ENTRY', 'WATCHLIST'] else '✅ রিটেস্ট সফল ও ট্রেড অ্যাক্টিভ'})</span>
                </div>
                
                <div class="stats-grid">
                    <div class="stat-item">
                        <label>Triggered Entry Price</label>
                        <span style="color:#10b981;font-weight:bold;">${round_price(t.get('entry_price', 'Pending'))}</span>
                    </div>
                    <div class="stat-item">
                        <label>Current Live Price</label>
                        <span style="color:#38bdf8;">${round_price(t.get('current_price', t['entry_price']))}</span>
                    </div>
                    <div class="stat-item">
                        <label>Stop Loss</label>
                        <span style="color:#ef4444">${round_price(t['stop_loss'])}{sl_note}</span>
                    </div>
                    <div class="stat-item">
                        <label>TP1 (90D Swing Resistance)</label>
                        <span style="color:#10b981">${round_price(t['tp1'])}</span>
                    </div>
                    <div class="stat-item">
                        <label>TP2 (Major Structural Pivot)</label>
                        <span style="color:#059669">${round_price(t['tp2'])}</span>
                    </div>
                    <div class="stat-item">
                        <label>TP3 (90-Day Macro High)</label>
                        <span style="color:#047857">${round_price(t.get('tp3', 'N/A'))}</span>
                    </div>
                </div>

                <div class="orderflow-grid">
                    <div class="of-box">
                        <span class="of-title">Aggregated Market OI (All-Exchanges)</span>
                        <span class="of-val">{t.get('open_interest', 'N/A')}</span>
                        <span class="of-sub" style="font-size:10px;color:#94a3b8;">{t.get('open_interest_detail', 'Aggregated')}</span>
                    </div>
                    <div class="of-box">
                        <span class="of-title">Whale Taker Ratio</span>
                        <span class="of-val" style="color:#38bdf8;">{t.get('whale_taker_ratio', '1.0x')}</span>
                        <span class="of-sub">{t.get('whale_flow_tag', 'Normal Flow')}</span>
                    </div>
                    <div class="of-box">
                        <span class="of-title">Average Market Funding Bias</span>
                        <span class="of-val">{t.get('funding_rate', '0.0%')}</span>
                        <span class="of-sub">Binance + Bybit + OKX</span>
                    </div>
                </div>

                <div class="sma-structure-grid">
                    <div class="sma-box">
                        <span class="sma-title">SMA 20 (Launch Base)</span>
                        <span class="sma-val">${round_price(t.get('sma20_val', 'N/A'))}</span>
                        <span class="sma-sub">Diff: +{t.get('sma20_dist', 0)}%</span>
                    </div>
                    <div class="sma-box">
                        <span class="sma-title">SMA 50 (Mid Support)</span>
                        <span class="sma-val">${round_price(t.get('sma50_val', 'N/A'))}</span>
                        <span class="sma-sub">Diff: {t.get('sma50_dist', 'N/A')}</span>
                    </div>
                    <div class="sma-box">
                        <span class="sma-title">SMA 200 (Ceiling)</span>
                        <span class="sma-val">${round_price(t.get('sma200_val', 'N/A'))}</span>
                        <span class="sma-sub" style="color:#38bdf8">Diff: {t.get('sma200_dist', 'N/A')}</span>
                    </div>
                </div>

                <div class="tokenomics-grid">
                    <div class="token-box">
                        <span class="t-title">Circulating</span>
                        <span class="t-val">{t.get('circulating', 'N/A')}</span>
                    </div>
                    <div class="token-box">
                        <span class="t-title">Total Supply</span>
                        <span class="t-val">{t.get('total_supply', 'N/A')}</span>
                    </div>
                    <div class="token-box">
                        <span class="t-title">Unlocked %</span>
                        <span class="t-val">{t.get('circ_ratio', 'N/A')}</span>
                    </div>
                    <div class="token-box" style="grid-column: span 1;">
                        <span class="t-title">Unlock Risk</span>
                        <span class="t-val" style="font-size:12px;color:#f59e0b;">{t.get('unlock_warning', 'Normal Float 🟢')}</span>
                    </div>
                </div>

                <details>
                    <summary>📊 বিগত ৭ দিনের বিস্তারিত হিস্টোরি (Daily OI ও Exchange Net Flow সহ)</summary>
                    <div class="table-container">
                        <table class="sub-table">
                            <thead>
                                <tr>
                                    <th>Date</th>
                                    <th>Close</th>
                                    <th>SMA20 Dist%</th>
                                    <th>SMA50 Dist%</th>
                                    <th>RVOL</th>
                                    <th style="color:#38bdf8;">Daily OI (Change%)</th>
                                    <th>Exchange Flow (In/Out)</th>
                                    <th>Candle Type</th>
                                </tr>
                            </thead>
                            <tbody>{hist_rows}</tbody>
                        </table>
                    </div>
                </details>
            </div>
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <title>Mobinul • Institutional Crypto Hub (Clean Accordion Engine)</title>
        <style>
            :root {{
                --bg: #0f172a;
                --card-bg: #1e293b;
                --border: #334155;
                --text: #f8fafc;
                --subtext: #94a3b8;
                --accent: #38bdf8;
            }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background-color: var(--bg);
                color: var(--text);
                margin: 0;
                padding: 24px;
            }}
            .container {{
                max-width: 1280px;
                margin: 0 auto;
            }}
            .header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid var(--border);
                padding-bottom: 20px;
                margin-bottom: 20px;
                flex-wrap: wrap;
                gap: 16px;
            }}
            .header-brand {{
                display: flex;
                align-items: center;
                gap: 16px;
            }}
            .brand-logo {{
                width: 58px;
                height: 58px;
                border-radius: 50%;
                border: 2px solid var(--accent);
                object-fit: cover;
                background: #ffffff;
                box-shadow: 0 4px 14px rgba(56, 189, 248, 0.25);
            }}
            .header h1 {{
                margin: 0;
                font-size: 24px;
                color: var(--accent);
            }}
            .control-panel {{
                display: flex;
                gap: 12px;
                align-items: center;
                background: #1e293b;
                padding: 14px 20px;
                border-radius: 12px;
                border: 1px solid var(--border);
                margin-bottom: 20px;
                justify-content: space-between;
                flex-wrap: wrap;
            }}
            .btn {{
                padding: 10px 18px;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                border: none;
                cursor: pointer;
                transition: 0.2s all;
            }}
            .btn-start {{
                background: #10b981;
                color: white;
            }}
            .btn-start:hover {{
                background: #059669;
            }}
            .btn-inspect {{
                background: #8b5cf6;
                color: white;
            }}
            .btn-inspect:hover {{
                background: #7c3aed;
            }}
            .btn-pnl {{
                background: #38bdf8;
                color: #0f172a;
            }}
            .btn-pnl:hover {{
                background: #0284c7;
                color: white;
            }}
            .btn-reset {{
                background: #ef4444;
                color: white;
            }}
            .btn-reset:hover {{
                background: #dc2626;
            }}
            .btn:disabled {{
                opacity: 0.5;
                cursor: not-allowed;
            }}
            .tv-btn {{
                background: #1e3a8a;
                color: #60a5fa;
                padding: 4px 10px;
                border-radius: 6px;
                text-decoration: none;
                font-size: 11px;
                font-weight: bold;
                border: 1px solid #2563eb;
                transition: 0.2s;
            }}
            .tv-btn:hover {{
                background: #2563eb;
                color: #ffffff;
            }}
            .btc-sentinel {{
                background: {btc_banner_color};
                border: 1px solid {btc_border_color};
                border-radius: 12px;
                padding: 14px 20px;
                margin-bottom: 24px;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }}
            .btc-sentinel h3 {{
                margin: 0;
                font-size: 16px;
            }}
            .btc-sentinel p {{
                margin: 4px 0 0;
                font-size: 13px;
                color: var(--subtext);
            }}
            .tab-nav {{
                display: flex;
                gap: 10px;
                margin-bottom: 24px;
                flex-wrap: wrap;
            }}
            .tab-btn {{
                background: #1e293b;
                color: var(--subtext);
                border: 1px solid var(--border);
                padding: 10px 18px;
                border-radius: 20px;
                cursor: pointer;
                font-size: 13px;
                font-weight: 600;
                display: flex;
                align-items: center;
                gap: 6px;
            }}
            .tab-btn.active {{
                background: var(--accent);
                color: #0f172a;
                border-color: var(--accent);
            }}
            .tab-count {{
                background: #0f172a;
                color: var(--accent);
                font-size: 11px;
                padding: 2px 7px;
                border-radius: 10px;
                font-weight: bold;
            }}
            .tab-btn.active .tab-count {{
                background: #0f172a;
                color: #ffffff;
            }}
            .metrics-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
                gap: 14px;
                margin-bottom: 30px;
            }}
            .metric-card {{
                background: var(--card-bg);
                border: 1px solid var(--border);
                border-radius: 12px;
                padding: 16px;
                text-align: center;
            }}
            .metric-card label {{
                font-size: 12px;
                color: var(--subtext);
                display: block;
                margin-bottom: 6px;
            }}
            .metric-card .val {{
                font-size: 20px;
                font-weight: bold;
            }}

            /* --- নতুন কমপ্যাক্ট ড্রপডাউন অ্যাকর্ডিয়ন কার্ড ডিজাইন --- */
            .coin-card {{
                background: var(--card-bg);
                border: 1px solid var(--border);
                border-radius: 12px;
                margin-bottom: 14px;
                overflow: hidden;
                transition: 0.2s border-color, 0.2s box-shadow;
            }}
            .coin-card:hover {{
                border-color: #475569;
                box-shadow: 0 4px 18px rgba(0, 0, 0, 0.3);
            }}
            .card-summary-strip {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 16px 20px;
                cursor: pointer;
                background: #1e293b;
                user-select: none;
                transition: background 0.2s;
            }}
            .card-summary-strip:hover {{
                background: #24334a;
            }}
            .strip-left {{
                display: flex;
                flex-direction: column;
                gap: 8px;
            }}
            .strip-title-row {{
                display: flex;
                align-items: center;
                gap: 10px;
                flex-wrap: wrap;
            }}
            .coin-name {{
                font-size: 19px;
                font-weight: bold;
                color: #f8fafc;
            }}
            .strip-meta-row {{
                font-size: 12px;
                color: var(--subtext);
                display: flex;
                align-items: center;
                gap: 8px;
                flex-wrap: wrap;
            }}
            .dot-sep {{
                color: #475569;
            }}
            .strip-right {{
                display: flex;
                align-items: center;
                gap: 16px;
            }}
            .pnl-box {{
                font-size: 20px;
                font-weight: bold;
            }}
            .expand-arrow {{
                font-size: 14px;
                color: var(--accent);
                transition: transform 0.3s ease;
                background: #0f172a;
                width: 30px;
                height: 30px;
                display: flex;
                align-items: center;
                justify-content: center;
                border-radius: 50%;
                border: 1px solid var(--border);
            }}
            .card-summary-strip.open .expand-arrow {{
                transform: rotate(180deg);
                background: var(--accent);
                color: #0f172a;
            }}

            /* ড্রপডাউন বডি (শুরুতে বন্ধ থাকবে) */
            .card-expand-body {{
                display: none;
                padding: 0 20px 20px 20px;
                border-top: 1px solid #33415550;
                background: #172132;
            }}

            .entry-zone-banner {{
                background: #0284c725;
                border: 1px solid #0284c7;
                border-radius: 8px;
                padding: 10px 14px;
                margin: 16px 0 12px 0;
                display: flex;
                align-items: center;
                gap: 10px;
                flex-wrap: wrap;
            }}
            .ez-label {{
                font-size: 11px;
                font-weight: bold;
                color: #38bdf8;
            }}
            .ez-val {{
                font-size: 15px;
                font-weight: bold;
                color: #ffffff;
            }}
            .ez-status {{
                font-size: 12px;
                color: var(--subtext);
            }}
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
                gap: 12px;
                background: #0f172a80;
                padding: 14px;
                border-radius: 8px;
                margin-bottom: 12px;
            }}
            .stat-item label {{
                font-size: 11px;
                color: var(--subtext);
                display: block;
            }}
            .stat-item span {{
                font-size: 15px;
                font-weight: 600;
            }}
            .orderflow-grid, .sma-structure-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                gap: 10px;
                background: #0d1b2a;
                padding: 12px 16px;
                border-radius: 8px;
                border: 1px solid #1e3a8a;
                margin-bottom: 10px;
            }}
            .of-box, .sma-box {{
                display: flex;
                flex-direction: column;
            }}
            .of-title, .sma-title {{
                font-size: 10px;
                color: #60a5fa;
                font-weight: 600;
                text-transform: uppercase;
            }}
            .of-val, .sma-val {{
                font-size: 15px;
                font-weight: bold;
                color: #f8fafc;
                margin: 2px 0;
            }}
            .of-sub, .sma-sub {{
                font-size: 11px;
                color: var(--subtext);
            }}
            .tokenomics-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
                gap: 10px;
                background: #091220;
                padding: 10px 16px;
                border-radius: 8px;
                border: 1px dashed #334155;
                margin-bottom: 14px;
            }}
            .token-box {{
                display: flex;
                flex-direction: column;
            }}
            .t-title {{
                font-size: 10px;
                color: #94a3b8;
                text-transform: uppercase;
            }}
            .t-val {{
                font-size: 13px;
                font-weight: 600;
                color: #e2e8f0;
                margin-top: 2px;
            }}
            details summary {{
                font-size: 14px;
                color: var(--accent);
                cursor: pointer;
                padding: 8px 0;
                font-weight: 600;
            }}
            .table-container {{
                margin-top: 10px;
                overflow-x: auto;
            }}
            .sub-table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 13px;
                text-align: left;
            }}
            .sub-table th, .sub-table td {{
                padding: 10px 12px;
                border-bottom: 1px solid var(--border);
            }}
            .sub-table th {{
                color: var(--subtext);
                background: #0f172a;
                text-transform: uppercase;
                font-size: 11px;
            }}
            .badge {{
                font-size: 11px;
                padding: 4px 10px;
                border-radius: 20px;
                color: #fff;
                font-weight: 600;
            }}
            .modal-overlay {{
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.75);
                backdrop-filter: blur(4px);
                z-index: 1000;
                justify-content: center;
                align-items: center;
                padding: 20px;
            }}
            .modal-content {{
                background: #1e293b;
                border: 1px solid #475569;
                border-radius: 16px;
                width: 100%;
                max-width: 950px;
                max-height: 90vh;
                overflow-y: auto;
                padding: 24px;
            }}
            .modal-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid #334155;
                padding-bottom: 14px;
                margin-bottom: 20px;
            }}
            .modal-header h2 {{
                margin: 0;
                font-size: 20px;
                color: #38bdf8;
            }}
            .close-btn {{
                background: transparent;
                border: none;
                color: #94a3b8;
                font-size: 24px;
                cursor: pointer;
            }}
            .close-btn:hover {{
                color: #ffffff;
            }}
            .search-bar-row {{
                display: flex;
                gap: 12px;
                margin-bottom: 20px;
            }}
            .search-input {{
                flex: 1;
                background: #0f172a;
                border: 1px solid #334155;
                color: #f8fafc;
                padding: 12px 16px;
                border-radius: 8px;
                font-size: 15px;
                outline: none;
            }}
            .search-input:focus {{
                border-color: #38bdf8;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="header-brand">
                    <img src="/logo.png" alt="Mobinul Logo" class="brand-logo" onerror="this.src='/icn copy.png'; this.onerror=null;">
                    <div>
                        <h1>Institutional Pre-Pump Crypto Hub</h1>
                        <p style="color:var(--subtext);margin:4px 0 0;font-size:13px;">
                            Owner & Lead Analyst: <b style="color:#38bdf8;font-size:14px;">Mobinul</b> &nbsp;|&nbsp; 
                            <span>Clean Accordion Engine • Full Market Scan (Max 5 Setups)</span>
                        </p>
                    </div>
                </div>
                <div style="font-size: 13px; color: var(--subtext); text-align:right;">
                    <span id="autoIndicator" style="color:#10b981;font-weight:bold;">● Auto-Monitor Active (60s)</span><br>
                    <span style="font-size:11px;">Last Update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</span>
                </div>
            </div>

            <div class="control-panel">
                <div style="display:flex;gap:10px;flex-wrap:wrap;">
                    <button class="btn btn-inspect" onclick="openInspectModal()">🔍 Search & Inspect Coin</button>
                    <button class="btn btn-start" id="startBtn" onclick="triggerScan()">🚀 Start Full Market Scan</button>
                    <button class="btn btn-pnl" id="pnlBtn" onclick="triggerQuickPnL()">⚡ Force Update PnL & Zones</button>
                    <button class="btn btn-reset" onclick="triggerReset()">🔄 Reset Database</button>
                </div>
                <div id="statusText" style="font-size:13px;color:var(--accent);">স্ট্যাটাস: {scan_status_msg}</div>
            </div>

            <div class="btc-sentinel">
                <div>
                    <h3>🌐 BTC Market Status: {btc_health['status_text']}</h3>
                    <p>{btc_health['sub_text']}</p>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:18px;font-weight:bold;">${btc_health['price']}</div>
                    <div style="font-size:13px;color:{'#10b981' if btc_health['change'] >= 0 else '#ef4444'};">{'+' if btc_health['change'] >= 0 else ''}{btc_health['change']}% (24h) | SMA20: {btc_health['dist20']:+.2f}%</div>
                </div>
            </div>

            <div class="metrics-grid">
                <div class="metric-card"><label>Total Identified</label><div class="val">{total_trades}</div></div>
                <div class="metric-card"><label>Waiting for Retrace</label><div class="val" style="color:#f59e0b">{waiting_trades}</div></div>
                <div class="metric-card"><label>Active Triggered</label><div class="val" style="color:#10b981">{active_trades}</div></div>
                <div class="metric-card"><label>TP1 Hits</label><div class="val" style="color:#059669">{tp1_hits}</div></div>
                <div class="metric-card"><label>TP2 Hits</label><div class="val" style="color:#047857">{tp2_hits}</div></div>
                <div class="metric-card"><label>TP3 Hits</label><div class="val" style="color:#065f46">{tp3_hits}</div></div>
                <div class="metric-card"><label>SL Hits</label><div class="val" style="color:#ef4444">{sl_hits}</div></div>
                <div class="metric-card"><label>Total PnL (Active)</label><div class="val" style="color:{pnl_color}">{'+' if total_pnl >= 0 else ''}{total_pnl}%</div></div>
            </div>

            <div class="tab-nav">
                <button class="tab-btn active" onclick="filterTab('all', this)">🌐 All Setups <span class="tab-count">{count_all}</span></button>
                <button class="tab-btn" onclick="filterTab('waiting', this)">⏳ Waiting For Retrace <span class="tab-count">{count_waiting}</span></button>
                <button class="tab-btn" onclick="filterTab('active', this)">⚡ Live Active Trades <span class="tab-count">{count_active}</span></button>
                <button class="tab-btn" onclick="filterTab('a-plus', this)">💎 Stage-1 Reversal <span class="tab-count">{count_stage1}</span></button>
                <button class="tab-btn" onclick="filterTab('short-squeeze', this)">🚨 Extreme Short Squeeze <span class="tab-count">{count_short_squeeze}</span></button>
                <button class="tab-btn" onclick="filterTab('early-momentum', this)">⚡ Early Momentum <span class="tab-count">{count_early_momentum}</span></button>
                <button class="tab-btn" onclick="filterTab('whale-trap', this)">⚠️ Avoid / Traps <span class="tab-count">{count_whale_trap}</span></button>
            </div>

            <div class="trades-container" id="tradesContainer">
                {cards_html if cards_html else '<p style="text-align:center;color:#94a3b8;padding:40px;">বর্তমানে কোনো ট্রেড ডাটা নেই। ফ্রেশ স্ক্যানের জন্য <b>"Start Full Market Scan"</b> বাটনে ক্লিক করুন।</p>'}
            </div>
        </div>

        <div class="modal-overlay" id="inspectModal">
            <div class="modal-content">
                <div class="modal-header">
                    <h2>🔍 Single Coin Institutional Inspector (Long & Short Engine)</h2>
                    <button class="close-btn" onclick="closeInspectModal()">&times;</button>
                </div>
                <div class="search-bar-row">
                    <input type="text" id="coinSearchInput" class="search-input" placeholder="কয়েনের নাম লিখুন (যেমন: BTC, ETH, SOL, NEAR, AXL, ALT)..." onkeypress="if(event.key==='Enter') submitInspect();">
                    <button class="btn btn-inspect" onclick="submitInspect()">Inspect Coin</button>
                </div>
                <div id="inspectLoading" style="display:none;text-align:center;padding:30px;color:#38bdf8;">
                    ⏳ Binance, Bybit এবং OKX থেকে অল-মার্কেট ওপেন ইন্টারেস্ট এবং হিস্ট্রি সংগ্রহ হচ্ছে...
                </div>
                <div id="inspectResult"></div>
            </div>
        </div>

        <script>
            function playBeep(freq = 600, duration = 200) {{
                try {{
                    const ctx = new (window.AudioContext || window.webkitAudioContext)();
                    const osc = ctx.createOscillator();
                    osc.type = 'sine';
                    osc.frequency.setValueAtTime(freq, ctx.currentTime);
                    osc.connect(ctx.destination);
                    osc.start();
                    setTimeout(() => osc.stop(), duration);
                }} catch (e) {{}}
            }}

            // কার্ডে ক্লিক করলে ড্রপডাউন ওপেন ও ক্লোজ করার ইন্টারেক্টিভ ফাংশন
            function toggleCardDetails(detailsId, stripElement) {{
                const detailsBox = document.getElementById(detailsId);
                if (detailsBox.style.display === 'block') {{
                    detailsBox.style.display = 'none';
                    stripElement.classList.remove('open');
                }} else {{
                    detailsBox.style.display = 'block';
                    stripElement.classList.add('open');
                }}
            }}

            function openInspectModal() {{ document.getElementById('inspectModal').style.display = 'flex'; document.getElementById('coinSearchInput').focus(); }}
            function closeInspectModal() {{ document.getElementById('inspectModal').style.display = 'none'; }}
            function submitInspect() {{
                const input = document.getElementById('coinSearchInput').value.trim();
                if (!input) return;
                document.getElementById('inspectLoading').style.display = 'block';
                document.getElementById('inspectResult').innerHTML = '';
                fetch('/api/inspect?symbol=' + encodeURIComponent(input))
                .then(r => r.json())
                .then(d => {{
                    document.getElementById('inspectLoading').style.display = 'none';
                    if (!d.success) {{
                        document.getElementById('inspectResult').innerHTML = '<div style="color:#ef4444;padding:20px;text-align:center;">' + d.error + '</div>';
                        return;
                    }}
                    renderInspectResult(d);
                }})
                .catch(err => {{
                    document.getElementById('inspectLoading').style.display = 'none';
                    document.getElementById('inspectResult').innerHTML = '<div style="color:#ef4444;padding:20px;text-align:center;">সার্ভার কানেকশন এরর!</div>';
                }});
            }}

            function renderInspectResult(d) {{
                let histHtml = '';
                d.daily_history.forEach(h => {{
                    histHtml += `
                        <tr>
                            <td>${{h.date}}</td>
                            <td><b>$${{h.close}}</b></td>
                            <td>${{h.sma20_dist}}</td>
                            <td>${{h.sma50_dist}}</td>
                            <td>${{h.rvol}}</td>
                            <td style="color:#38bdf8;font-weight:600;">${{h.daily_oi}}</td>
                            <td>${{h.exchange_flow}}</td>
                            <td>${{h.type}}</td>
                        </tr>
                    `;
                }});

                const isShort = d.setup_direction.includes('SHORT');
                const bannerBorderColor = isShort ? '#ef4444' : '#10b981';
                const bannerBgColor = isShort ? '#ef444425' : '#10b98125';
                const zoneTitle = isShort ? '🎯 INSTITUTIONAL SELL / SHORT ZONE:' : '🎯 INSTITUTIONAL BUY ZONE:';
                const slLabel = isShort ? 'Stop Loss (Above High +6%)' : 'Stop Loss (Zone -10%)';

                const html = `
                    <div class="coin-card" style="margin-top:10px;background:#0f172a;border-color:${{d.direction_color}};">
                        <div style="padding:16px 20px;">
                            <div style="display:flex;justify-content:space-between;align-items:center;">
                                <div>
                                    <h2 style="margin:0;font-size:20px;display:flex;align-items:center;gap:8px;">
                                        ${{d.clean_symbol}} 
                                        <span class="badge" style="background:${{d.direction_color}};font-size:13px;padding:5px 12px;">${{d.setup_direction}}</span>
                                        <span class="badge" style="background:${{d.category_color}}">${{d.category}}</span>
                                        <span class="badge" style="background:#3b82f6">${{d.status}}</span>
                                        <a href="${{d.tv_link}}" target="_blank" class="tv-btn">📈 TradingView</a>
                                    </h2>
                                    <span style="font-size:12px;color:var(--subtext);margin-top:6px;display:block;">
                                        🌐 <b>Live Price:</b> $${{d.cur_price}} &nbsp;|&nbsp; 
                                        Avg Market Funding: <b>${{d.funding_rate}}</b>
                                    </span>
                                </div>
                            </div>

                            <div style="background:${{bannerBgColor}};border:1px solid ${{bannerBorderColor}};padding:12px 16px;border-radius:8px;margin:14px 0 12px 0;font-size:13px;">
                                <b>💡 প্রাতিষ্ঠানিক ডায়াগনসিস:</b> ${{d.analysis_summary}}
                            </div>

                            <div style="background:#1e293b;padding:12px 16px;border-radius:8px;border-left:4px solid #8b5cf6;margin-bottom:12px;font-size:13px;">
                                <b>🗓️ বিগত ৯০ দিনের সাইকেল রিপোর্ট:</b> 
                                90D Low: <b style="color:#10b981">$${{d.low_90d}}</b> (${{d.gain_from_90d_low}} from bottom) &nbsp;|&nbsp; 
                                90D High: <b style="color:#ef4444">$${{d.high_90d}}</b> (${{d.dist_from_90d_high}} from peak)
                            </div>

                            <div class="entry-zone-banner" style="background:${{bannerBgColor}};border-color:${{bannerBorderColor}};">
                                <span class="ez-label" style="color:${{bannerBorderColor}}">${{zoneTitle}}</span>
                                <span class="ez-val">$${{d.entry_zone_low}} &nbsp;—&nbsp; $${{d.entry_zone_high}}</span>
                                <span class="ez-status">(SL: $${{d.stop_loss}} | TP1: $${{d.tp1}} | TP2: $${{d.tp2}} | TP3: $${{d.tp3}})</span>
                            </div>

                            <div class="stats-grid">
                                <div class="stat-item"><label>Current Live Price</label><span style="color:#38bdf8;">$${{d.cur_price}}</span></div>
                                <div class="stat-item"><label>${{slLabel}}</label><span style="color:#ef4444">$${{d.stop_loss}}</span></div>
                                <div class="stat-item"><label>TP1 (90D Structural Swing)</label><span style="color:#10b981">$${{d.tp1}}</span></div>
                                <div class="stat-item"><label>TP2 (Major Structural Pivot)</label><span style="color:#059669">$${{d.tp2}}</span></div>
                                <div class="stat-item"><label>TP3 (90-Day Macro High)</label><span style="color:#047857">$${{d.tp3}}</span></div>
                            </div>

                            <div class="orderflow-grid">
                                <div class="of-box">
                                    <span class="of-title">Aggregated Market OI (All-Exchanges)</span>
                                    <span class="of-val">${{d.open_interest}}</span>
                                    <span class="of-sub" style="font-size:10px;color:#94a3b8;">${{d.open_interest_detail}}</span>
                                </div>
                                <div class="of-box">
                                    <span class="of-title">Whale Taker Ratio</span>
                                    <span class="of-val" style="color:#38bdf8;">${{d.whale_taker_ratio}}</span>
                                    <span class="of-sub">${{d.whale_flow_tag}}</span>
                                </div>
                                <div class="of-box">
                                    <span class="of-title">Average Market Funding Bias</span>
                                    <span class="of-val">${{d.funding_rate}}</span>
                                    <span class="of-sub">Binance + Bybit + OKX</span>
                                </div>
                            </div>

                            <div class="sma-structure-grid">
                                <div class="sma-box">
                                    <span class="sma-title">SMA 20 (Launch Base)</span>
                                    <span class="sma-val">$${{d.sma20_val}}</span>
                                    <span class="sma-sub">Diff: +${{d.sma20_dist}}%</span>
                                </div>
                                <div class="sma-box">
                                    <span class="sma-title">SMA 50 (Mid Support)</span>
                                    <span class="sma-val">$${{d.sma50_val}}</span>
                                    <span class="sma-sub">Diff: ${{d.sma50_dist}}</span>
                                </div>
                                <div class="sma-box">
                                    <span class="sma-title">SMA 200 (Macro Ceiling)</span>
                                    <span class="sma-val">$${{d.sma200_val}}</span>
                                    <span class="sma-sub" style="color:#38bdf8">Diff: ${{d.sma200_dist}}</span>
                                </div>
                            </div>

                            <div class="tokenomics-grid">
                                <div class="token-box"><span class="t-title">Circulating</span><span class="t-val">${{d.circulating}}</span></div>
                                <div class="token-box"><span class="t-title">Total Supply</span><span class="t-val">${{d.total_supply}}</span></div>
                                <div class="token-box"><span class="t-title">Unlocked %</span><span class="t-val">${{d.circ_ratio}}</span></div>
                                <div class="token-box"><span class="t-title">Unlock Risk</span><span class="t-val" style="color:#f59e0b;">${{d.unlock_warning}}</span></div>
                            </div>

                            <h4 style="margin:16px 0 8px;color:#38bdf8;">📊 বিগত ৭ দিনের বিস্তারিত হিস্টোরি (Daily OI ও Exchange Net Flow সহ):</h4>
                            <div class="table-container">
                                <table class="sub-table">
                                    <thead>
                                        <tr>
                                            <th>Date</th>
                                            <th>Close</th>
                                            <th>SMA20 Dist%</th>
                                            <th>SMA50 Dist%</th>
                                            <th>RVOL</th>
                                            <th style="color:#38bdf8;">Daily OI (Change%)</th>
                                            <th>Exchange Flow (In/Out)</th>
                                            <th>Candle Type</th>
                                        </tr>
                                    </thead>
                                    <tbody>${{histHtml}}</tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                `;
                document.getElementById('inspectResult').innerHTML = html;
            }}

            function filterTab(mode, btn) {{
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                
                const cards = document.querySelectorAll('.coin-card');
                cards.forEach(card => {{
                    const cat = card.getAttribute('data-category');
                    const status = card.getAttribute('data-status');
                    
                    if (mode === 'all') {{
                        card.style.display = 'block';
                    }} else if (mode === 'waiting') {{
                        card.style.display = (status === 'WAITING_FOR_ENTRY' || status === 'WATCHLIST') ? 'block' : 'none';
                    }} else if (mode === 'active') {{
                        card.style.display = (status === 'ACTIVE' || status.includes('HIT')) ? 'block' : 'none';
                    }} else {{
                        card.style.display = (cat === mode) ? 'block' : 'none';
                    }}
                }});
            }}

            function triggerScan() {{
                const btn = document.getElementById('startBtn');
                btn.disabled = true;
                document.getElementById('statusText').innerText = "সম্পূর্ণ ফ্রেশ ফুল মার্কেট স্ক্যানিং শুরু হচ্ছে... অনুগ্রহ করে অপেক্ষা করুন।";
                fetch('/api/scan', {{ method: 'POST' }}).then(r => r.json()).then(d => {{ pollStatus(); }});
            }}

            function triggerQuickPnL() {{
                const btn = document.getElementById('pnlBtn');
                btn.disabled = true;
                document.getElementById('statusText').innerText = "⚡ লাইভ প্রাইস ও এন্ট্রি জোন চেক হচ্ছে...";
                fetch('/api/update_pnl', {{ method: 'POST' }})
                .then(r => r.json())
                .then(d => {{
                    document.getElementById('statusText').innerText = "✅ " + d.count + "টি ট্রেডের লাইভ আপডেট সফল!";
                    setTimeout(() => location.reload(), 800);
                }})
                .catch(err => {{ btn.disabled = false; }});
            }}

            function triggerReset() {{
                if (confirm("আপনি কি নিশ্চিত যে সম্পূর্ণ ডাটাবেস মুছে ফেলতে চান?")) {{
                    fetch('/api/reset', {{ method: 'POST' }}).then(r => r.json()).then(d => {{ location.reload(); }});
                }}
            }}

            function pollStatus() {{
                const timer = setInterval(() => {{
                    fetch('/api/status').then(r => r.json()).then(data => {{
                        document.getElementById('statusText').innerText = "স্ট্যাটাস: " + data.message;
                        if (!data.scanning) {{
                            clearInterval(timer);
                            document.getElementById('startBtn').disabled = false;
                            setTimeout(() => location.reload(), 1500);
                        }}
                    }});
                }}, 2000);
            }}

            setInterval(() => {{
                fetch('/api/status').then(r => r.json()).then(d => {{ if (!d.scanning) location.reload(); }});
            }}, 60000);
        </script>
    </body>
    </html>
    """
    return html

# ==============================================================================
# ১১. লোকাল ওয়েব সার্ভার ও রুটস
# ==============================================================================
class WebServerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ['/logo.png', '/icn copy.png']:
            img_file = 'logo.png' if os.path.exists('logo.png') else ('icn copy.png' if os.path.exists('icn copy.png') else None)
            if img_file:
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.end_headers()
                with open(img_file, 'rb') as f: self.wfile.write(f.read())
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        if parsed.path == '/' or parsed.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(build_html_page().encode('utf-8'))
        elif parsed.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'scanning': is_scanning, 'message': scan_status_msg, 'alert': last_alert_event}).encode('utf-8'))
        elif parsed.path == '/api/inspect':
            qs = parse_qs(parsed.query)
            sym = qs.get('symbol', [''])[0]
            result = inspect_single_coin(sym)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global is_scanning, scan_status_msg
        parsed = urlparse(self.path)
        if parsed.path == '/api/scan':
            if not is_scanning:
                threading.Thread(target=run_full_scan, daemon=True).start()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'started'}).encode('utf-8'))
        elif parsed.path == 'api/update_pnl':
            trades_db = load_trades_db()
            count = update_existing_trades(trades_db)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'pnl_updated', 'count': count}).encode('utf-8'))
        elif parsed.path == '/api/reset':
            save_trades_db({})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'reset_done'}).encode('utf-8'))

    def log_message(self, format, *args): pass

def start_server():
    server_address = ('', PORT)
    httpd = HTTPServer(server_address, WebServerHandler)
    url = f"http://localhost:{PORT}"
    print(f"\n=======================================================")
    print(f"🚀 Mobinul's Institutional Crypto Hub Live: {url}")
    print(f"👉 ব্রাউজারে ড্যাশবোর্ড ওপেন হচ্ছে... বন্ধ করতে Ctrl+C চাপুন।")
    print(f"=======================================================\n")
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nসার্ভার বন্ধ করা হয়েছে।")

if __name__ == "__main__":
    start_server()