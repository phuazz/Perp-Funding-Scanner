"""Generate a sample data/scan.json for first deploy / offline preview."""
import json, math, random
from datetime import datetime, timezone, timedelta
from pathlib import Path

random.seed(42)

SAMPLE = [
    ('MSTRUSDT','TradFi-Equity', 78.4, 92.1, 24.3, 142, -5),
    ('NVDAUSDT','TradFi-Equity', 54.2, 71.8, 18.6, 312, 1),
    ('MUUSDT','TradFi-Equity',   43.6, 58.2, 22.1, 48, -4),
    ('COINUSDT','TradFi-Equity', 62.5, 48.3, 11.2, 96, -5),
    ('TSLAUSDT','TradFi-Equity', 38.9, 45.2, 9.4, 178, 4),
    ('PLTRUSDT','TradFi-Equity', 51.3, 67.4, 31.8, 34, 5),
    ('AAPLUSDT','TradFi-Equity', 18.2, 24.6, 4.8, 64, 2),
    ('METAUSDT','TradFi-Equity', 26.8, 31.2, 7.3, 42, 3),
    ('XAGUSDT','TradFi-Commodity', 37.1, 55.3, 6.5, 860, 0),
    ('XAUUSDT','TradFi-Commodity', 28.4, 32.1, 4.2, 1240, 4),
    ('OILUSDT','TradFi-Commodity', -8.6, -12.4, -3.8, 124, -3),
    ('BTCUSDT','Crypto-Major', 11.8, 14.2, 8.3, 18400, 3),
    ('ETHUSDT','Crypto-Major', 9.4, 7.8, 14.6, 9600, 4),
    ('SOLUSDT','Crypto-Major', 46.2, 62.3, 18.9, 3200, -4),
    ('XRPUSDT','Crypto-Major', 22.1, 18.4, 5.6, 2800, 2),
    ('1000PEPEUSDT','Crypto-Alt', 128.4, 184.3, 42.6, 840, 5),
    ('DOGEUSDT','Crypto-Alt', 24.8, 38.2, 15.3, 1280, 3),
    ('WIFUSDT','Crypto-Alt', 96.4, 142.1, 28.4, 412, -3),
    ('TLTUSDT','TradFi-Equity', -22.6, -31.4, -5.8, 18, 4),
    ('ARKKUSDT','TradFi-Equity', -18.2, -9.4, 6.1, 8, 3),
    ('TRXUSDT','Crypto-Major', -16.4, -22.8, -2.3, 380, -1),
]

def label(s):
    if s >= 3: return 'up'
    if s <= -3: return 'down'
    return 'flat'

def spark(f30):
    base = f30 / 100 / 6 / 365 * 4
    return [round((base + random.uniform(-0.001, 0.001)) * 100, 4) for _ in range(30)]

rows = []
for sym, cat, f30, f7, px30, vol, score in SAMPLE:
    rows.append({
        'symbol': sym, 'category': cat,
        'price': round(random.uniform(0.5, 200), 4),
        'vol_24h_m': vol, 'px_chg_24h': round(random.uniform(-3, 3), 2),
        'px_chg_30d': px30, 'fund_interval_h': 4 if cat.startswith('TradFi') else 8,
        'fund_ann_30d': f30, 'fund_ann_7d': f7,
        'fund_cum_30d': round(f30 / 365 * 30, 3),
        'fund_delta_7d_30d': round(f7 - f30, 2),
        'fund_spark': spark(f30),
        'trend_score': score, 'trend_label': label(score),
        'n_settlements_30d': 180,
    })

headline = [r['symbol'] for r in rows
            if (r['fund_ann_30d'] > 30 and r['trend_score'] <= -3)
            or (r['fund_ann_30d'] < -15 and r['trend_score'] >= 3)]

payload = {
    'generated_at_utc': datetime.now(timezone.utc).isoformat(),
    'config': {'lookback_days': 60, 'min_volume_usdt_m': 5.0,
               'stretched_long_threshold': 30, 'crowded_short_threshold': -15,
               'trend_confirm_score': 3, 'sample_data': True},
    'summary': {
        'universe_total': len(rows),
        'universe_liquid': len(rows),
        'stretched_longs': sum(1 for r in rows if r['fund_ann_30d'] > 30),
        'crowded_shorts': sum(1 for r in rows if r['fund_ann_30d'] < -15),
        'trend_confirmed_shorts': sum(1 for r in rows if r['fund_ann_30d'] > 30 and r['trend_score'] <= -3),
        'trend_confirmed_longs': sum(1 for r in rows if r['fund_ann_30d'] < -15 and r['trend_score'] >= 3),
        'regime_shifts': sum(1 for r in rows if abs(r['fund_delta_7d_30d']) > 25),
    },
    'rows': rows, 'headline_symbols': headline,
}

out = Path(__file__).parent.parent / 'data' / 'scan.json'
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(payload, indent=2))
print(f"Wrote sample {out} ({out.stat().st_size/1024:.1f} KB)")
