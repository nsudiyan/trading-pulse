#!/usr/bin/env python3
"""
tv_trade_plan.py — продакшен-обёртка для разметки торгового плана на TradingView.

Схема (согласована): ОДИН выделенный foreground-график TradingView Desktop.
На сигнал: символ -> 15m -> детерминированный расчёт (свинги/Fib/EMA) -> разметка
(вход/стоп/тейк/вертикаль входа/уровни) -> скрин PNG. БЕЗ обращений к Claude API.

Использование (ручная команда):
    python3 tv_trade_plan.py ETH
    python3 tv_trade_plan.py BTC --dir SHORT --entry 67140 --sl 68050 --tp 65450 --note "ретест .236"

Из бота:
    from tv_trade_plan import make_plan_image
    png = make_plan_image("ETH", direction="SHORT", entry=1904.3, sl=1935.6, tp=1852.0)
    if png:  # graceful: None если TV закрыт/недоступен — алерт уходит без картинки
        tg_send_photo(token, chat_id, png, caption=...)
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
MCP_DIR = Path(os.environ.get("TV_MCP_DIR", str(Path.home() / "tradingview-mcp")))
RENDERER = HERE / "tv_plan_render.js"
CHARTS_DIR = HERE / "tv_charts"
CDP_HOST = os.environ.get("TV_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("TV_CDP_PORT", "9222"))
NODE = os.environ.get("TV_NODE") or shutil.which("node") or "/opt/homebrew/bin/node"


def _log(msg: str) -> None:
    print(f"[TVPlan] {msg}", flush=True)


def activate_tradingview() -> None:
    """Вывести окно TradingView на передний план (macOS): фоновая вкладка не
    перерисовывает canvas, поэтому для корректного скрина TV должен быть видим.
    Best-effort — молча игнорируем ошибки."""
    try:
        subprocess.run(["osascript", "-e", 'tell application "TradingView" to activate'],
                       capture_output=True, timeout=5)
        time.sleep(1.0)
    except Exception as e:
        _log(f"activate: {e}")


def open_new_tab() -> bool:
    """Открыть НОВУЮ вкладку TradingView через меню (System Events, нужен Accessibility).
    Вкладка открывается пустой («Новая вкладка»); рендерер потом навигирует её на монету.
    Прошлые монеты остаются в своих вкладках. True если команда отправлена без ошибки."""
    script = (
        'tell application "TradingView" to activate\n'
        'delay 0.5\n'
        'tell application "System Events" to tell process "TradingView"\n'
        '  click menu item "Новая вкладка" of menu 1 of menu bar item "TradingView" of menu bar 1\n'
        'end tell'
    )
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
        time.sleep(1.8)  # дать вкладке открыться
        if p.returncode != 0:
            _log(f"open_new_tab: {p.stderr.strip()[:120]}")
            return False
        return True
    except Exception as e:
        _log(f"open_new_tab: {e}")
        return False


def tv_healthy(timeout: float = 3.0) -> bool:
    """TradingView Desktop поднят с CDP и есть хотя бы один chart-таргет?"""
    try:
        with urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json/list", timeout=timeout) as r:
            targets = json.loads(r.read().decode("utf-8"))
        return any(t.get("type") == "page" and "tradingview.com/chart" in (t.get("url") or "")
                   for t in targets)
    except Exception as e:
        _log(f"health: TV недоступен ({e})")
        return False


def make_plan_image(symbol: str,
                    direction: Optional[str] = None,
                    entry: Optional[float] = None,
                    sl: Optional[float] = None,
                    tp: Optional[float] = None,
                    note: Optional[str] = None,
                    out: Optional[str] = None,
                    activate: bool = True,
                    new_tab: bool = False,
                    rich: bool = False,
                    levels: Optional[list] = None,
                    zones: Optional[list] = None,
                    simple: bool = False,
                    timeout: float = 55.0) -> Optional[str]:
    """Разметить план и вернуть путь к PNG. None при любой проблеме (graceful skip)."""
    try:
        if not RENDERER.exists():
            _log(f"нет рендерера {RENDERER}")
            return None
        if not tv_healthy():
            return None  # graceful: алерт уйдёт без картинки
        if activate:
            activate_tradingview()  # вывести TV вперёд, иначе скрин фоновой вкладки подмёрзнет
        opened_tab = False
        if new_tab:
            opened_tab = open_new_tab()  # открыть пустую вкладку → рендерер навигирует её на монету
        base = symbol.upper().replace("BYBIT:", "").replace(".P", "").replace("USDT", "") or symbol
        if out is None:
            CHARTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = str(CHARTS_DIR / f"{base}_{ts}.png")
        spec = {"symbol": symbol, "direction": direction, "entry": entry,
                "sl": sl, "tp": tp, "note": note, "out": out, "tf": "15",
                "navTab": bool(opened_tab),
                "rich": bool(rich), "levels": levels or [], "zones": zones or [],
                "simple": bool(simple)}
        env = dict(os.environ, TV_MCP_DIR=str(MCP_DIR), TV_CDP_HOST=CDP_HOST, TV_CDP_PORT=str(CDP_PORT))
        proc = subprocess.run([NODE, str(RENDERER), json.dumps(spec)],
                              capture_output=True, text=True, timeout=timeout, env=env)
        line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else ""
        try:
            res = json.loads(line)
        except Exception:
            _log(f"рендерер не вернул JSON (rc={proc.returncode}); stderr: {(proc.stderr or '')[-200:]}")
            return None
        if res.get("ok") and res.get("path") and os.path.exists(res["path"]):
            p = res["plan"]
            _log(f"OK {p['dir']} {res.get('verifySym')} entry={p['entry']} sl={p['sl']} tp={p['tp']} R:R={p['rr']} -> {res['path']}")
            for w in res.get("warnings", []):
                _log(f"warn: {w}")
            return res["path"]
        _log(f"рендер не удался: {res.get('error') or res}")
        return None
    except subprocess.TimeoutExpired:
        _log(f"таймаут рендера ({timeout}s)")
        return None
    except Exception as e:
        _log(f"исключение: {e}")
        return None


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Разметить торговый план монеты на TradingView (15m).")
    ap.add_argument("symbol", help="монета: ETH / BTC / SOLUSDT / BYBIT:ETHUSDT.P")
    ap.add_argument("--dir", dest="direction", choices=["SHORT", "LONG"], help="направление (иначе считается по структуре)")
    ap.add_argument("--entry", type=float)
    ap.add_argument("--sl", type=float)
    ap.add_argument("--tp", type=float)
    ap.add_argument("--note")
    ap.add_argument("--out")
    ap.add_argument("--new-tab", dest="new_tab", action="store_true",
                    help="попытаться открыть НОВУЮ вкладку под монету (best-effort: создание вкладки на macOS нестабильно)")
    a = ap.parse_args()
    png = make_plan_image(a.symbol, direction=a.direction, entry=a.entry, sl=a.sl,
                          tp=a.tp, note=a.note, out=a.out, new_tab=a.new_tab)
    if png:
        print(png)
        return 0
    print("FAILED (см. логи выше)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
