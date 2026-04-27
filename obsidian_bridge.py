"""
obsidian_bridge.py — интеграция screener.py с Obsidian vault.

Возможности:
  - Автоопределение vault
  - Сохранение отчётов скринера в vault (папка Trading/Отчёты/)
  - Сохранение кандидатов на памп (Trading/Пампы/)
  - Чтение заметок по конкретному токену
  - Чтение торгового журнала
  - Команда setup: выбор vault и настройка папок

CLI-использование:
  python3 obsidian_bridge.py setup          — мастер настройки
  python3 obsidian_bridge.py status         — показать текущую конфигурацию
  python3 obsidian_bridge.py read BTCUSDT   — показать заметки по токену
  python3 obsidian_bridge.py journal        — показать торговый журнал
  python3 obsidian_bridge.py list-notes     — показать все заметки в Trading/
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "obsidian_config.json"

DEFAULT_CONFIG = {
    "vault_path": None,
    "trading_folder": "крипта",          # папка внутри vault для трейдинга
    "reports_subfolder": "Отчёты",       # Trading/Отчёты/
    "pumps_subfolder": "Пампы",          # Trading/Пампы/
    "journal_file": "Сделки (дневник).md",
    "enabled": False,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # fill missing keys with defaults
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────
# Поиск vault
# ─────────────────────────────────────────────────────────────

_SEARCH_ROOTS = [
    Path.home() / "Documents",
    Path.home() / "Desktop",
    Path.home(),
    Path("/Users"),
]


def find_vaults(max_depth: int = 3) -> list[Path]:
    """Ищет папки с .obsidian внутри — это и есть vault'ы."""
    found = []
    for root in _SEARCH_ROOTS:
        if not root.exists():
            continue
        try:
            for item in root.rglob(".obsidian"):
                vault = item.parent
                if vault not in found:
                    found.append(vault)
                    if len(found) >= 10:
                        return found
        except PermissionError:
            continue
    return found


# ─────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────

def _trading_root(cfg: dict) -> Path:
    return Path(cfg["vault_path"]) / cfg["trading_folder"]


def _ensure_dirs(cfg: dict) -> None:
    root = _trading_root(cfg)
    (root / cfg["reports_subfolder"]).mkdir(parents=True, exist_ok=True)
    (root / cfg["pumps_subfolder"]).mkdir(parents=True, exist_ok=True)


def _slug(text: str) -> str:
    """Безопасное имя файла."""
    text = re.sub(r'[\\/:*?"<>|]', "_", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────
# Экспорт отчёта скринера
# ─────────────────────────────────────────────────────────────

def export_report(
    results: list,
    filtered: list,
    session_info: Optional[dict] = None,
    fg_value: Optional[int] = None,
    fg_label: Optional[str] = None,
    cfg: Optional[dict] = None,
) -> Optional[Path]:
    """
    Сохраняет полный отчёт скринера в vault.
    Возвращает путь к созданному файлу или None при ошибке.
    """
    if cfg is None:
        cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("vault_path"):
        return None

    _ensure_dirs(cfg)
    now = datetime.now()
    filename = now.strftime("%Y-%m-%d_%H-%M") + ".md"
    filepath = _trading_root(cfg) / cfg["reports_subfolder"] / filename

    lines = []

    # YAML frontmatter
    lines += [
        "---",
        f'date: "{now.strftime("%Y-%m-%d %H:%M")}"',
        f'session: "{session_info.get("session", "?") if session_info else "?"}"',
        f'fear_greed: {fg_value if fg_value is not None else "null"}',
        f'fear_greed_label: "{fg_label or ""}"',
        f'symbols_scanned: {len(results)}',
        f'signals_found: {len(filtered)}',
        "tags: [screener, trading, crypto]",
        "---",
        "",
    ]

    # Заголовок
    lines += [
        f"# Отчёт скринера {now.strftime('%d.%m.%Y %H:%M')}",
        "",
    ]

    # Макро-блок
    if session_info or fg_value is not None:
        lines.append("## Макро-контекст")
        if session_info:
            sess = session_info.get("session", "—")
            best = session_info.get("best_for", "—")
            lines.append(f"- **Сессия:** {sess}")
            lines.append(f"- **Лучше для:** {best}")
        if fg_value is not None:
            lines.append(f"- **Fear & Greed:** {fg_value} ({fg_label})")
        lines.append("")

    # Таблица сигналов
    if filtered:
        lines += [
            "## Сигналы скринера",
            "",
            "| # | Символ | Цена | Изм 24h | Сетап | Оценка | Грейд |",
            "|---|--------|------|---------|-------|--------|-------|",
        ]
        for i, r in enumerate(filtered[:30], 1):
            sym    = r.get("symbol", "?")
            price  = r.get("price", 0)
            chg    = r.get("change_24h", 0)
            setup  = r.get("setup", "—")
            score  = r.get("score", 0)
            grade  = r.get("grade", "—")
            chg_s  = f"+{chg:.1f}%" if chg >= 0 else f"{chg:.1f}%"
            lines.append(f"| {i} | [[{sym}]] | {price:.4f} | {chg_s} | {setup} | {score} | {grade} |")
        lines.append("")
    else:
        lines += ["## Сигналы скринера", "", "_Сигналов не найдено._", ""]

    # Детали по каждому символу
    if filtered:
        lines.append("## Детальный разбор")
        lines.append("")
        for r in filtered[:15]:
            sym   = r.get("symbol", "?")
            price = r.get("price", 0)
            setup = r.get("setup", "—")
            score = r.get("score", 0)
            grade = r.get("grade", "—")

            lines.append(f"### {sym}")
            lines.append(f"- **Цена:** {price:.6g}  |  **Сетап:** {setup}  |  **Оценка:** {score}  |  **Грейд:** {grade}")

            # Базовые метрики
            chg   = r.get("change_24h", 0)
            vol   = r.get("turnover24h", 0) or r.get("volume_24h_usd", 0)
            oi    = r.get("oi24h_%", 0)   or r.get("oi_change_pct", 0)
            fund  = r.get("fund_%", 0)    or r.get("funding_rate", 0)
            lines.append(f"- Изм 24h: {'+'if chg>=0 else ''}{chg:.1f}%  |  Объём 24h: ${vol:,.0f}  |  OI изм: {oi:.1f}%  |  Funding: {fund*100:.4f}%")

            # RSI
            rsi = r.get("rsi_1h")
            if rsi is not None:
                rsi_div = r.get("rsi_div_1h", {})
                div_txt = []
                if rsi_div.get("bull_div"):    div_txt.append("бычье расх.")
                if rsi_div.get("hidden_bull"): div_txt.append("скрытое бычье")
                if rsi_div.get("bear_div"):    div_txt.append("медвежье расх.")
                if rsi_div.get("hidden_bear"): div_txt.append("скрытое медвежье")
                div_s = ", ".join(div_txt) if div_txt else "нет"
                lines.append(f"- RSI 1H: {rsi:.1f}  |  Дивергенция: {div_s}")

            # VWAP
            vwap     = r.get("vwap")
            vwap_dev = r.get("vwap_dev")
            if vwap is not None:
                lines.append(f"- VWAP: {vwap:.6g}  |  Откл: {vwap_dev:+.1f}%")

            # EMA
            ema1h = r.get("ema_1h", {})
            if ema1h:
                order = "бычий" if ema1h.get("ema_bull") else ("медвежий" if ema1h.get("ema_bear") else "нейтр.")
                cross = ""
                if ema1h.get("golden_cross"): cross = " | GOLDEN CROSS"
                if ema1h.get("death_cross"):  cross = " | DEATH CROSS"
                lines.append(f"- EMA 1H: порядок={order}{cross}  |  Наклон EMA20: {ema1h.get('slope_ema20', 0):+.2f}%")

            # CHoCH
            choch1h = r.get("choch_1h", {})
            if choch1h.get("bull_choch"): lines.append("- **CHoCH 1H:** бычий разворот структуры")
            if choch1h.get("bear_choch"): lines.append("- **CHoCH 1H:** медвежий разворот структуры")

            # MTF
            bull_mtf = r.get("bull_mtf_ext", r.get("bull_mtf", 0))
            bear_mtf = r.get("bear_mtf_ext", r.get("bear_mtf", 0))
            lines.append(f"- MTF bull: {bull_mtf}  |  MTF bear: {bear_mtf}")

            lines.append("")

    # Все токены (компактно)
    if results:
        lines += [
            "## Все просканированные токены",
            "",
            "| Символ | Цена | Изм 24h | Сетап | Оценка |",
            "|--------|------|---------|-------|--------|",
        ]
        for r in results:
            sym   = r.get("symbol", "?")
            price = r.get("price", 0)
            chg   = r.get("change_24h", 0)
            setup = r.get("setup", "—")
            score = r.get("score", 0)
            chg_s = f"+{chg:.1f}%" if chg >= 0 else f"{chg:.1f}%"
            lines.append(f"| {sym} | {price:.4f} | {chg_s} | {setup} | {score} |")
        lines.append("")

    # Футер
    lines += [
        "---",
        f"*Создано автоматически screener.py • {now.strftime('%d.%m.%Y %H:%M')}*",
    ]

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath


# ─────────────────────────────────────────────────────────────
# Экспорт кандидатов на памп
# ─────────────────────────────────────────────────────────────

def export_pump_candidates(
    candidates: list,
    cfg: Optional[dict] = None,
) -> Optional[Path]:
    """
    Сохраняет список кандидатов на памп.
    candidates — список dict из build_pump_narrative.
    """
    if cfg is None:
        cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("vault_path"):
        return None

    _ensure_dirs(cfg)
    now = datetime.now()
    filename = now.strftime("%Y-%m-%d_%H-%M") + "_пампы.md"
    filepath = _trading_root(cfg) / cfg["pumps_subfolder"] / filename

    lines = [
        "---",
        f'date: "{now.strftime("%Y-%m-%d %H:%M")}"',
        "tags: [pump, watchlist, crypto]",
        "---",
        "",
        f"# Кандидаты на памп {now.strftime('%d.%m.%Y %H:%M')}",
        "",
    ]

    if not candidates:
        lines.append("_Кандидатов не найдено._")
    else:
        for c in candidates:
            sym        = c.get("symbol", "?")
            conviction = c.get("conviction", "?")
            stars      = c.get("stars", "")
            pump_score = c.get("pump_score", 0)
            price      = c.get("price", 0)
            trigger    = c.get("trigger_hint", "")
            signals    = c.get("signals", [])

            lines += [
                f"## {sym}  {stars}",
                f"- **Убеждённость:** {conviction}  |  **Pump Score:** {pump_score}  |  **Цена:** {price:.6g}",
            ]
            if trigger:
                lines.append(f"- **Триггер:** {trigger}")
            if signals:
                lines.append("- **Сигналы:**")
                for s in signals:
                    lines.append(f"  - {s}")
            lines.append("")

    lines += [
        "---",
        f"*screener.py • {now.strftime('%d.%m.%Y %H:%M')}*",
    ]

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath


# ─────────────────────────────────────────────────────────────
# Чтение заметок из vault
# ─────────────────────────────────────────────────────────────

def read_token_notes(symbol: str, cfg: Optional[dict] = None) -> Optional[str]:
    """
    Ищет заметку по токену в vault.
    Проверяет: Trading/BTCUSDT.md, Trading/BTC.md, vault root.
    """
    if cfg is None:
        cfg = load_config()
    if not cfg.get("vault_path"):
        return None

    vault    = Path(cfg["vault_path"])
    trading  = vault / cfg["trading_folder"]
    base     = symbol.replace("USDT", "").replace("usdt", "")

    candidates = [
        trading / f"{symbol}.md",
        trading / f"{base}.md",
        vault / f"{symbol}.md",
        vault / f"{base}.md",
    ]

    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")

    # поиск по всему vault (поверхностно)
    for md_file in vault.rglob("*.md"):
        if md_file.name.lower() in (f"{symbol.lower()}.md", f"{base.lower()}.md"):
            return md_file.read_text(encoding="utf-8")

    return None


def read_journal(cfg: Optional[dict] = None) -> Optional[str]:
    """Читает торговый журнал."""
    if cfg is None:
        cfg = load_config()
    if not cfg.get("vault_path"):
        return None

    path = _trading_root(cfg) / cfg["journal_file"]
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def read_all_trading_notes(cfg: Optional[dict] = None) -> dict[str, str]:
    """
    Читает все .md файлы из папки Trading/ vault.
    Возвращает dict {filename: content}.
    """
    if cfg is None:
        cfg = load_config()
    if not cfg.get("vault_path"):
        return {}

    root = _trading_root(cfg)
    if not root.exists():
        return {}

    notes = {}
    for md_file in root.rglob("*.md"):
        try:
            notes[str(md_file.relative_to(root))] = md_file.read_text(encoding="utf-8")
        except Exception:
            pass
    return notes


# ─────────────────────────────────────────────────────────────
# Мастер настройки
# ─────────────────────────────────────────────────────────────

def setup_wizard() -> dict:
    """Интерактивный мастер настройки Obsidian-интеграции."""
    print("\n" + "="*60)
    print("  OBSIDIAN INTEGRATION SETUP")
    print("="*60)

    cfg = load_config()

    # 1. Ищем vault'ы
    print("\nПоиск Obsidian vault'ов...")
    vaults = find_vaults()

    if not vaults:
        print("  Vault'ы не найдены автоматически.")
        manual = input("  Введите путь к vault вручную (или Enter для отмены): ").strip()
        if not manual:
            print("Настройка отменена.")
            return cfg
        vault_path = Path(manual)
    else:
        print(f"\n  Найдено vault'ов: {len(vaults)}")
        for i, v in enumerate(vaults, 1):
            marker = " ← текущий" if str(v) == cfg.get("vault_path") else ""
            print(f"  [{i}] {v}{marker}")
        print(f"  [0] Ввести путь вручную")

        while True:
            choice = input("\n  Выберите vault (номер): ").strip()
            if choice == "0":
                manual = input("  Путь к vault: ").strip()
                vault_path = Path(manual)
                break
            elif choice.isdigit() and 1 <= int(choice) <= len(vaults):
                vault_path = vaults[int(choice) - 1]
                break
            else:
                print("  Неверный выбор, попробуйте снова.")

    if not vault_path.exists():
        print(f"  Путь не существует: {vault_path}")
        return cfg

    cfg["vault_path"] = str(vault_path)
    print(f"\n  Vault: {vault_path}")

    # 2. Папка для трейдинга внутри vault
    default_folder = cfg.get("trading_folder", "крипта")
    existing = [d.name for d in vault_path.iterdir() if d.is_dir() and not d.name.startswith(".")]
    print(f"\n  Папки в vault: {', '.join(existing) or 'нет'}")
    folder = input(f"  Папка для торговли [{default_folder}]: ").strip()
    if folder:
        cfg["trading_folder"] = folder
    else:
        cfg["trading_folder"] = default_folder

    # 3. Подпапки
    def ask_subfolder(key, label, default):
        val = input(f"  Подпапка для {label} [{default}]: ").strip()
        cfg[key] = val if val else default

    print("\n  Подпапки (внутри торговой папки):")
    ask_subfolder("reports_subfolder",  "отчётов",          cfg.get("reports_subfolder", "Отчёты"))
    ask_subfolder("pumps_subfolder",    "кандидатов пампа", cfg.get("pumps_subfolder",   "Пампы"))

    # 4. Файл журнала
    journal_default = cfg.get("journal_file", "Сделки (дневник).md")
    journal = input(f"\n  Файл торгового журнала [{journal_default}]: ").strip()
    cfg["journal_file"] = journal if journal else journal_default

    # 5. Включить интеграцию
    cfg["enabled"] = True

    # 6. Создать папки
    try:
        _ensure_dirs(cfg)
        print(f"\n  Папки созданы в: {cfg['vault_path']}/{cfg['trading_folder']}/")
    except Exception as e:
        print(f"  Предупреждение: не удалось создать папки — {e}")

    # 7. Сохранить
    save_config(cfg)
    print("\n  Конфигурация сохранена.")
    print("="*60)
    _print_status(cfg)
    return cfg


# ─────────────────────────────────────────────────────────────
# Статус
# ─────────────────────────────────────────────────────────────

def _print_status(cfg: dict) -> None:
    print("\n  OBSIDIAN CONFIG")
    print(f"  Enabled:  {cfg.get('enabled', False)}")
    print(f"  Vault:    {cfg.get('vault_path') or '(не настроен)'}")
    print(f"  Папка:    {cfg.get('trading_folder')}")
    print(f"  Отчёты:   .../{cfg.get('trading_folder')}/{cfg.get('reports_subfolder')}")
    print(f"  Пампы:    .../{cfg.get('trading_folder')}/{cfg.get('pumps_subfolder')}")
    print(f"  Журнал:   {cfg.get('journal_file')}")
    if cfg.get("vault_path"):
        n = len(read_all_trading_notes(cfg))
        print(f"  Заметок в торговой папке: {n}")
    print()


def enable(cfg: Optional[dict] = None) -> dict:
    if cfg is None:
        cfg = load_config()
    if not cfg.get("vault_path"):
        print("[Obsidian] Vault не настроен. Запустите: python3 obsidian_bridge.py setup")
        return cfg
    cfg["enabled"] = True
    save_config(cfg)
    return cfg


def disable(cfg: Optional[dict] = None) -> dict:
    if cfg is None:
        cfg = load_config()
    cfg["enabled"] = False
    save_config(cfg)
    return cfg


# ─────────────────────────────────────────────────────────────
# Coin note refresh (автоматическое обновление Монеты/*.md)
# ─────────────────────────────────────────────────────────────

_OUTCOME_ICON = {"TP1": "✅TP1", "WIN": "🟢WIN", "FLAT": "⚪FLAT",
                 "LOSS": "🔴LOSS", "STOP": "❌STOP"}
_SETUP_SHORT  = {"squeeze": "SQZ", "bos_fvg": "BOS/FVG", "range_sweep": "SWEEP",
                 "breakout": "PUMP", "short_dist": "DIST"}
_DIR_ICON     = {"ЛОНГ": "🟢ЛОНГ", "ШОРТ": "🔴ШОРТ"}


def refresh_coin_note(symbol: str, cfg: Optional[dict] = None) -> Optional[Path]:
    """
    Пересоздаёт заметку Монеты/{symbol}.md из resolved.csv + pending.json.
    Вызывается из outcome_tracker после каждого сохранения/резолва.
    Возвращает путь к файлу или None если Obsidian не настроен.
    """
    import csv as _csv

    if cfg is None:
        cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("vault_path"):
        return None

    vault     = Path(cfg["vault_path"])
    coins_dir = _trading_root(cfg) / "Результаты сделок" / "Монеты"
    coins_dir.mkdir(parents=True, exist_ok=True)
    note_path = coins_dir / f"{symbol}.md"

    base_dir      = Path(__file__).parent
    resolved_csv  = base_dir / "outcomes" / "resolved.csv"
    pending_json  = base_dir / "outcomes" / "pending.json"

    # Читаем resolved строки для этого символа
    resolved = []
    if resolved_csv.exists():
        with open(resolved_csv, "r", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                if row.get("symbol") == symbol:
                    resolved.append(row)

    # Читаем pending записи для этого символа
    pending = []
    if pending_json.exists():
        try:
            data = json.loads(pending_json.read_text(encoding="utf-8"))
            pending = [e for e in data if e.get("symbol") == symbol]
        except Exception:
            pass

    if not resolved and not pending:
        return None

    # ── Статистика (по outcome_24h, fallback outcome_4h) ──
    tp1_n = win_n = flat_n = loss_n = stop_n = 0
    changes = []
    for r in resolved:
        out = r.get("outcome_24h") or r.get("outcome_4h") or ""
        if out == "TP1":        tp1_n  += 1
        elif out == "WIN":      win_n  += 1
        elif out == "FLAT":     flat_n += 1
        elif out == "LOSS":     loss_n += 1
        elif out == "STOP":     stop_n += 1
        try:
            chg = float(r.get("change_24h_pct") or r.get("change_4h_pct") or "0")
            changes.append(chg)
        except (ValueError, TypeError):
            pass

    total_res = tp1_n + win_n + flat_n + loss_n + stop_n
    wr = round((tp1_n + win_n) / total_res * 100) if total_res else 0
    best  = f"+{max(changes):.1f}%" if changes else "—"
    worst = f"{min(changes):.1f}%"  if changes else "—"
    total_sig = total_res + len(pending)
    base_sym  = symbol.replace("USDT", "").lower()

    # ── Frontmatter + заголовок ──
    lines = [
        "---",
        f'symbol: "{symbol}"',
        f"signals: {total_sig}",
        f"win_rate_24h: {wr}",
        f"tags: [trading, results, {base_sym}]",
        "---",
        "",
        f"# {symbol}",
        "",
        f"**Сигналов:** {total_sig}  |  **Win Rate (24h):** {wr}%  |  **Лучшее:** {best}  |  **Худшее:** {worst}",
        "",
        "| ✅TP1 | 🟢WIN | ⚪FLAT | 🔴LOSS | ❌STOP |",
        "|------|------|------|------|------|",
        f"| {tp1_n} | {win_n} | {flat_n} | {loss_n} | {stop_n} |",
        "",
        "## История сигналов",
        "",
        "| Дата | Напр. | Сетап | Score | Вход | Стоп | TP1 | Рез. 4h | Рез. 24h | Изм% 24h |",
        "|------|-------|-------|-------|------|------|-----|---------|----------|----------|",
    ]

    def _fmt_price(v):
        try:
            return f"`{float(v):.4g}`" if v else "—"
        except (ValueError, TypeError):
            return "—"

    def _fmt_chg(v):
        try:
            f = float(v)
            return f"+{f:.1f}%" if f >= 0 else f"{f:.1f}%"
        except (ValueError, TypeError):
            return "—"

    def _out_icon(v):
        return _OUTCOME_ICON.get(v, "—")

    def _dir_icon(v):
        return _DIR_ICON.get(v, v or "—")

    for r in resolved:
        ts      = (r.get("run_ts") or "")[:16].replace("T", " ")
        dirn    = _dir_icon(r.get("direction", ""))
        setup   = _SETUP_SHORT.get(r.get("setup", ""), r.get("setup", "—"))
        score   = r.get("score", "—")
        entry   = _fmt_price(r.get("price_entry"))
        stop    = _fmt_price(r.get("stop"))
        tp1_p   = _fmt_price(r.get("tp1"))
        out4    = _out_icon(r.get("outcome_4h", ""))
        out24   = _out_icon(r.get("outcome_24h", ""))
        chg     = _fmt_chg(r.get("change_24h_pct") or r.get("change_4h_pct"))
        lines.append(f"| {ts} | {dirn} | {setup} | {score} | {entry} | {stop} | {tp1_p} | {out4} | {out24} | {chg} |")

    # ── Pending ──
    if pending:
        lines += ["", "## ⏳ Ожидают резолва", ""]
        for e in pending:
            ts   = (e.get("run_ts") or "")[:16].replace("T", " ")
            dirn = _dir_icon(e.get("direction", ""))
            sc   = e.get("score", "—")
            ep   = e.get("price_entry", "—")
            lines.append(f"- {ts}  {dirn}  score={sc}  entry=`{ep}`")

    note_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return note_path


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# Devlog: запись системных изменений из git commit
# ─────────────────────────────────────────────────────────────

def export_devlog(
    commit_hash: Optional[str] = None,
    cfg: Optional[dict] = None,
) -> Optional[Path]:
    """
    Читает последний git commit и пишет devlog-ноту в Obsidian.
    Папка: крипта/Системные изменения/
    Формат файла: YYYY-MM-DD — <тема>.md
    """
    import subprocess

    if cfg is None:
        cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("vault_path"):
        return None

    repo_root = Path(__file__).parent

    def _git(*git_args) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(repo_root), *git_args],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            return ""

    ref = commit_hash or "HEAD"

    hash_short  = _git("log", "-1", "--format=%h",   ref)
    author      = _git("log", "-1", "--format=%an",  ref)
    date_str    = _git("log", "-1", "--format=%ci",  ref)[:10]
    full_msg    = _git("log", "-1", "--format=%B",   ref).strip()
    first_line  = full_msg.splitlines()[0] if full_msg else hash_short
    body_lines  = full_msg.splitlines()[2:] if len(full_msg.splitlines()) > 2 else []
    body        = "\n".join(body_lines).strip()

    changed_raw = _git("diff-tree", "--no-commit-id", "-r", "--name-only", ref)
    changed     = [f for f in changed_raw.splitlines() if f]

    aveva_match = re.search(r"AVEVA-(\d+)", first_line, re.IGNORECASE)
    aveva_tag   = f"AVEVA-{aveva_match.group(1)}" if aveva_match else ""
    aveva_link  = f"\n**Ticket:** #{aveva_match.group(1)}" if aveva_match else ""

    topic = re.sub(r"^(feat|fix|chore|refactor|docs|test)\([^)]+\):\s*", "", first_line)
    topic = re.sub(r"AVEVA-\d+\s*[:\-–]?\s*", "", topic, flags=re.IGNORECASE).strip()
    if not topic:
        topic = first_line[:60]

    tags = ["devlog", "screener"]
    if aveva_tag:
        tags.append(aveva_tag.lower())
    for f in changed:
        base = Path(f).stem
        if base not in tags and len(base) < 30:
            tags.append(base)

    files_md = "\n".join(f"- `{f}`" for f in changed) if changed else "_нет_"
    body_md  = f"\n### Описание\n```\n{body}\n```\n" if body else ""

    note = f"""---
date: "{date_str}"
tags: [{", ".join(tags)}]
---

# {date_str} — {topic}

| | |
|---|---|
| **Коммит** | `{hash_short}` |
| **Автор** | {author} |{aveva_link} |

## Изменения

```
{first_line}
```
{body_md}
## Изменённые файлы

{files_md}

---
*Авто-сгенерировано obsidian_bridge.py из git commit*
"""

    root = _trading_root(cfg)
    devlog_dir = root / "Системные изменения"
    devlog_dir.mkdir(parents=True, exist_ok=True)

    safe_topic = _slug(topic)[:80]
    filename   = f"{date_str} — {safe_topic}.md"
    dest       = devlog_dir / filename

    if dest.exists():
        existing = dest.read_text(encoding="utf-8")
        append_block = f"\n---\n\n## {hash_short}: {first_line}\n\n{files_md}\n"
        dest.write_text(existing + append_block, encoding="utf-8")
    else:
        dest.write_text(note, encoding="utf-8")

    return dest


def main():
    args = sys.argv[1:]
    cmd  = args[0] if args else "status"

    if cmd == "setup":
        setup_wizard()

    elif cmd == "status":
        cfg = load_config()
        _print_status(cfg)

    elif cmd == "enable":
        cfg = enable()
        print(f"[Obsidian] Интеграция включена. Vault: {cfg['vault_path']}")

    elif cmd == "disable":
        cfg = disable()
        print("[Obsidian] Интеграция отключена.")

    elif cmd == "read":
        if len(args) < 2:
            print("Использование: python3 obsidian_bridge.py read <SYMBOL>")
            sys.exit(1)
        symbol = args[1].upper()
        cfg    = load_config()
        text   = read_token_notes(symbol, cfg)
        if text:
            print(f"\n=== Заметки по {symbol} ===\n")
            print(text)
        else:
            print(f"Заметок по {symbol} не найдено в vault.")

    elif cmd == "journal":
        cfg  = load_config()
        text = read_journal(cfg)
        if text:
            print("\n=== Торговый журнал ===\n")
            print(text)
        else:
            print("Журнал не найден.")

    elif cmd == "list-notes":
        cfg   = load_config()
        notes = read_all_trading_notes(cfg)
        if notes:
            print(f"\n=== Заметки в Trading/ ({len(notes)} шт.) ===\n")
            for name in sorted(notes.keys()):
                size = len(notes[name])
                print(f"  {name}  ({size} chars)")
        else:
            print("Заметок не найдено.")

    elif cmd == "test-export":
        # тест экспорта с фиктивными данными
        cfg = load_config()
        if not cfg.get("enabled"):
            cfg = enable(cfg)
        fake_results = [
            {"symbol": "BTCUSDT", "price": 84000, "change_24h": 2.1,
             "setup": "Setup 4", "score": 72, "grade": "A",
             "volume_24h_usd": 1_200_000_000, "oi_change_pct": 3.5,
             "funding_rate": 0.0001, "bull_mtf_ext": 4, "bear_mtf_ext": 1,
             "rsi_1h": 58.2, "rsi_div_1h": {"bull_div": False, "hidden_bull": False,
                                              "bear_div": False, "hidden_bear": False},
             "vwap": 83500, "vwap_dev": 0.6,
             "ema_1h": {"ema_bull": True, "ema_bear": False,
                        "golden_cross": False, "death_cross": False,
                        "slope_ema20": 0.12},
             "choch_1h": {"bull_choch": True, "bear_choch": False}},
        ]
        path = export_report(
            results=fake_results,
            filtered=fake_results,
            session_info={"session": "НьюЙорк", "best_for": "скальп"},
            fg_value=45,
            fg_label="Fear",
            cfg=cfg,
        )
        if path:
            print(f"[Obsidian] Тестовый отчёт сохранён: {path}")
        else:
            print("[Obsidian] Экспорт не выполнен (проверь конфиг).")

    elif cmd == "devlog":
        # Вызывается из git post-commit хука: python3 obsidian_bridge.py devlog
        import subprocess
        cfg = load_config()
        if not cfg.get("enabled") or not cfg.get("vault_path"):
            sys.exit(0)  # молча выходим если vault не настроен
        path = export_devlog(cfg=cfg)
        if path:
            print(f"[Obsidian] Devlog записан: {path}")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
