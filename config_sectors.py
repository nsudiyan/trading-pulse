"""
config_sectors.py — Shared SECTOR_MAP for screener.py and telegram_alerts.py.

Single source of truth. Both files should import from here.
Last updated: 2026-06-11 (merged screener + telegram_alerts maps).
"""

SECTOR_MAP = {
    "L1":     ["SOLUSDT","AVAXUSDT","TONUSDT","NEARUSDT","APTUSDT","SUIUSDT","SEIUSDT",
               "MOVEUSDT","BERAAUSDT","MONADUSDT","ATOMUSDT","DOTUSDT"],
    "DeFi":   ["AAVEUSDT","CRVUSDT","MKRUSDT","UNIUSDT","SNXUSDT","COMPUSDT",
               "JUPUSDT","PENDLEUSDT","EIGENUSDT","LDOUSDT"],
    "AI":     ["FETUSDT","RENDERUSDT","WLDUSDT","AGIXUSDT","TAOBYBIT","TAOUSDT",
               "AIUSDT","VIRTUSDT","ACTUSDT","CHESHIREUSDT","OCEANUSDT"],
    "Meme":   ["DOGEUSDT","SHIBUSDT","PEPEUSDT","FLOKIUSDT","BONKUSDT",
               "1000PEPEUSDT","SHIB1000USDT","WIFUSDT","POPCATUSDT",
               "MOODENGUSDT","GOATUSDT","BRETTUSDT","NEIROCTOBYBIT","MEWUSDT"],
    "L2":     ["ARBUSDT","OPUSDT","MATICUSDT","STRKUSDT","SCROLLUSDT",
               "ZKUSDT","WUSDT","PYTHUSD","METISUSDT"],
    "RWA":    ["ONDOUSDT","CFGUSDT","POLIXUSDT","REALUSDT",
               "OPENUSDT","POLYXUSDT","POLUSDT"],
    "DePIN":  ["IOUSDT","HIVEUSDT","ALUSDT","XNETUSDT"],
    "Perp":   ["HYPEUSDT","DYDXUSDT","GMXUSDT","SNSUSDT"],
    "LST":    ["ENAUSDT","ETHFIUSDT","RETHUSDT","SFRXETHUSDT"],
    "GameFi": ["AXSUSDT","SANDUSDT","GALAUSDT","IMXUSDT","BEAMUSDT","RONUSDT","MANAUSDT"],
    "ETH":    ["ETHUSDT","STETHUSDT"],
    "BTC":    ["BTCUSDT"],
}
