"""
Gold (XAUUSD) confirmation-candle alert bot -- SID 4, 7, 11
Run this every 15 minutes (via GitHub Actions cron). No orders are placed;
it only sends Telegram alerts.

IMPORTANT: this version uses the FREE gold-api.com current-price endpoint
(no API key needed at all). It builds its own 15-minute candle history over
time, one sample per run. Because of this:
  - It needs to run for a while (roughly a full day) before it has a
    complete "yesterday" candle to compute LDL/LDC/LDH levels from.
  - Each 15-min candle is built from a single price sample per run (or a
    few, if GitHub triggers extra runs), so it's an approximation of a
    true 15-min OHLC candle, not exact intrabar high/low.

Needs 2 environment variables (set as GitHub repo Secrets):
  TELEGRAM_TOKEN   - your Telegram bot token (from @BotFather)
  TELEGRAM_CHAT_ID - your Telegram chat id
"""

import os
import json
import requests
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '')

STATE_FILE = os.path.join(os.path.dirname(__file__), 'state.json')

# --- the 3 confirmed strategies (SID 4, 7, 11) ---
STRATEGIES = [
    {'sid': 4,  'level': 'LDL', 'last_day_color': 'Green', 'position': 'Above'},
    {'sid': 7,  'level': 'LDC', 'last_day_color': 'Red',   'position': 'Above'},
    {'sid': 11, 'level': 'LDC', 'last_day_color': 'Green', 'position': 'On'},
]

MAX_TRADES_PER_DAY = 3
MAX_CANDLES_KEPT = 1000  # ~10 days of 15-min candles


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        'candles': [],           # self-built 15-min candles: [{time, open, high, low, close}, ...]
        'pending': [],
        'last_bucket': None,     # the most recent (possibly still-forming) candle bucket
        'last_processed_bucket': None,  # last CLOSED candle bucket we've run logic on
        'day_counts': {},
        'daily_levels': None,
        'levels_date': None,
    }


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)


def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print('[WARN] Telegram not configured, message would be:', msg)
        return
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    requests.post(url, data={'chat_id': TELEGRAM_CHAT, 'text': msg}, timeout=10)


def fetch_price():
    """gold-api.com current gold price -- free, no API key needed."""
    url = 'https://api.gold-api.com/price/XAU'
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    price = data['price']
    # updatedAt looks like "2026-07-03T16:08:54Z"
    dt = datetime.strptime(data['updatedAt'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    return price, dt.timestamp()


def bucket_for(ts):
    """Round a unix timestamp down to the start of its 15-minute bucket (UTC)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    minute = (dt.minute // 15) * 15
    dt = dt.replace(minute=minute, second=0, microsecond=0)
    return dt.isoformat()


def update_candles(state, price, ts):
    bucket = bucket_for(ts)
    candles = state['candles']

    if candles and candles[-1]['time'] == bucket:
        c = candles[-1]
        c['high'] = max(c['high'], price)
        c['low'] = min(c['low'], price)
        c['close'] = price
    else:
        candles.append({
            'time': bucket,
            'open': price,
            'high': price,
            'low': price,
            'close': price,
        })
        if len(candles) > MAX_CANDLES_KEPT:
            state['candles'] = candles[-MAX_CANDLES_KEPT:]

    return bucket


def recompute_daily_levels(state, today_str):
    """Build LDL/LDC/LDH from the most recent fully-completed UTC day in our
    self-built candle history."""
    by_day = {}
    for c in state['candles']:
        day = c['time'][:10]
        if day == today_str:
            continue  # today isn't finished yet
        d = by_day.setdefault(day, {'open': c['open'], 'high': c['high'], 'low': c['low'], 'close': c['close']})
        d['high'] = max(d['high'], c['high'])
        d['low'] = min(d['low'], c['low'])
        d['close'] = c['close']  # candles are appended in time order, so last write wins

    if not by_day:
        return None

    last_day = sorted(by_day.keys())[-1]
    y = by_day[last_day]
    color = 'Green' if y['close'] >= y['open'] else 'Red'
    return {'LDL': y['low'], 'LDC': y['close'], 'LDH': y['high'], 'color': color}, last_day


def position_ok(candle_low, candle_high, level, position):
    if position == 'Above':
        return candle_low > level
    if position == 'Below':
        return candle_high < level
    return candle_low <= level <= candle_high  # 'On'


def check_once():
    state = load_state()

    try:
        price, ts = fetch_price()
    except Exception as e:
        print(f'[ERROR] Could not fetch price: {e}')
        return

    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    bucket = update_candles(state, price, ts)

    # recompute daily levels once we've moved into a new day, or if we don't have any yet
    if state.get('levels_date') != today_str:
        result = recompute_daily_levels(state, today_str)
        if result:
            state['daily_levels'], levels_day = result
            state['levels_date'] = today_str
            state['day_counts'] = {}
            print(f'[INFO] Daily levels from {levels_day}: {state["daily_levels"]}')
        else:
            print('[INFO] Not enough history yet to compute daily levels. Still collecting data.')
            save_state(state)
            return

    lvl = state['daily_levels']
    if lvl is None:
        print('[INFO] No daily levels available yet. Still collecting data.')
        save_state(state)
        return

    # has a NEW bucket started since we last processed one? if so, the
    # previous bucket is now closed and ready for confirmation/breakout logic
    if state.get('last_bucket') == bucket:
        # still the same forming candle, nothing closed yet
        state['last_bucket'] = bucket
        save_state(state)
        print('[INFO] No new closed candle yet.')
        return

    prev_bucket = state.get('last_bucket')
    state['last_bucket'] = bucket

    if prev_bucket is None or state.get('last_processed_bucket') == prev_bucket:
        save_state(state)
        return

    # find the closed candle
    closed = next((c for c in state['candles'] if c['time'] == prev_bucket), None)
    if closed is None:
        save_state(state)
        return

    state['last_processed_bucket'] = prev_bucket
    latest = closed
    color = 'Green' if latest['close'] >= latest['open'] else 'Red'
    latest_day = latest['time'][:10]

    # expire any pending confirmation candles from a PREVIOUS day
    state['pending'] = [p for p in state['pending'] if p.get('conf_day') == latest_day]

    # 1) check if this closed candle triggers a BREAKOUT for any pending setup
    still_pending = []
    for p in state['pending']:
        sid = p['sid']
        if latest['high'] >= p['conf_high']:
            day_count = state['day_counts'].get(str(sid), 0)
            if day_count < MAX_TRADES_PER_DAY:
                entry = p['conf_high']
                risk = entry - p['sl']
                t1 = entry + risk * 1.6
                t2 = entry + risk * 3.33
                send_telegram(
                    f"ENTRY SIGNAL -- Strategy {sid}\n"
                    f"BUY breakout of {entry:.2f}\n"
                    f"SL: {p['sl']:.2f}\n"
                    f"Part1(70%) target: {t1:.2f}\nPart2(30%) target: {t2:.2f}\n"
                    f"Move Part2 SL to breakeven ({entry:.2f}) once Part1 target hits."
                )
                state['day_counts'][str(sid)] = day_count + 1
            else:
                still_pending.append(p)
        else:
            still_pending.append(p)
    state['pending'] = still_pending

    # 2) check if this closed candle IS a new confirmation candle for any strategy
    for s in STRATEGIES:
        sid = s['sid']
        if state['day_counts'].get(str(sid), 0) >= MAX_TRADES_PER_DAY:
            continue
        if color != 'Green':
            continue
        if lvl['color'] != s['last_day_color']:
            continue
        level_val = lvl[s['level']]
        if position_ok(latest['low'], latest['high'], level_val, s['position']):
            already = any(
                p['sid'] == sid and p['conf_time'] == latest['time']
                for p in state['pending']
            )
            if not already:
                state['pending'].append({
                    'sid': sid,
                    'conf_time': latest['time'],
                    'conf_day': latest_day,
                    'conf_high': latest['high'],
                    'sl': latest['low'] - 5,
                })
                send_telegram(
                    f"CONFIRMATION CANDLE -- Strategy {sid} "
                    f"({s['level']}, {s['last_day_color']})\n"
                    f"Candle High: {latest['high']:.2f}  Low: {latest['low']:.2f}\n"
                    f"Watching for breakout above {latest['high']:.2f}..."
                )

    save_state(state)


if __name__ == '__main__':
    check_once()
