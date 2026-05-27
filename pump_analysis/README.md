# pump_analysis — Как запустить

Пайплайн состоит из 4 этапов (ST1 → ST2 → ST3b → ST3). Каждый этап зависит от предыдущего. Запускать строго по порядку из корня репозитория.

---

## Зависимости

```bash
pip install pandas scipy numpy requests tqdm matplotlib
```

Python 3.11+. Все скрипты запускаются из корня репо (`/Users/nikitasudian/Desktop/трейдинг/`).

---

## ST1 — Загрузка исторических данных

Скачивает klines (5m, 15m, 1h), funding, open interest, ликвидации и каталистические данные за 2024-05-01 → 2026-05-27.

```bash
# Основная загрузка klines + funding + OI
python pump_analysis/fetch_historical.py

# Catalyst-данные (Fear&Greed, stablecoin flows, sweep clusters, news signals)
python pump_analysis/fetch_catalyst_data.py

# Дозагрузка пропущенных символов (если fetch_historical упал на части)
python pump_analysis/fetch_remaining.py

# Финализация манифеста данных
python pump_analysis/finalize_manifest.py
```

**Выходные файлы:**
- `pump_analysis/klines_1h/*.csv.gz` — OHLCV per symbol (28 символов)
- `pump_analysis/klines_5m/*.csv.gz`, `klines_15m/*.csv.gz`
- `pump_analysis/funding.csv` — funding rates (8h)
- `pump_analysis/open_interest.csv` — OI (1h, Bybit)
- `pump_analysis/liquidations_summary.csv` — ликвидации Apr-May 2026
- `pump_analysis/catalyst_data/` — macro_events.csv, stablecoin_flows.csv, sweep_clusters.csv, news_signals.csv, token_unlocks.csv
- `pump_analysis/data_manifest.json` — полный манифест с row counts

**Время**: ~30–60 мин (rate limits Binance/Bybit).

---

## ST2 — Разметка пампов

Обнаруживает памп-события в klines_1h, генерирует контрольную выборку.

```bash
python pump_analysis/pump_labeler.py
```

**Выходные файлы:**
- `pump_analysis/pump_events.csv` — 677 пампов (pump_id, symbol, start_ts, end_ts, pct_chg, vol_mult, pump_type, split)
- `pump_analysis/control_windows.csv` — 3385 контрольных окон (K=5 на каждый памп)
- `pump_analysis/label_stats.json` — статистика разметки

**Параметры** (в начале файла):
- `PUMP_PCT = 15` — порог роста цены (%)
- `FAST_PCT = 10` — порог быстрого пампа (1h)
- `VOL_MULT = 3.0` — порог объёма (× медианы 30 дней)
- `SPLIT_DT = "2025-11-01"` — граница train/test

---

## ST3b — Каузальная атрибуция

Для каждого пампа определяет катализатор из catalyst_data/ через event-study (Fisher's exact test).

```bash
python pump_analysis/causal_attributor.py
```

**Требует**: pump_events.csv, control_windows.csv, все файлы catalyst_data/

**Выходные файлы:**
- `pump_analysis/causal_attribution.csv` — 677 строк: какой катализатор у каждого пампа
- `pump_analysis/catalyst_event_study.csv` — lift + p-value по 5 категориям катализаторов
- `pump_analysis/catalyst_stats.json` — агрегированная статистика
- `pump_analysis/plots/catalyst_distribution.png`

**Ключевые результаты** (из реальных данных):
- 51.3% пампов — причина не установлена
- Stablecoin inflow: lift=2.72 (HIGH confidence, p<0.001)
- Screener signal: lift=2.56 (MEDIUM, Apr 2026+ coverage only)
- Fear&Greed extreme: lift=1.001 (не подтверждено, p=0.91)

---

## ST3 — Feature Engineering

Вычисляет 16 предсказывающих признаков для каждого пампа и контрольного окна. Признаки берутся строго из окна [-4h, 0) — без заглядывания в будущее.

```bash
python pump_analysis/feature_engineer.py
```

**Требует**: klines_1h/, funding.csv, open_interest.csv, pump_events.csv, control_windows.csv, causal_attribution.csv

**Выходные файлы:**
- `pump_analysis/feature_matrix.csv` — 4062 строки × 28 колонок (16 признаков + метаданные)
- `pump_analysis/feature_stats.csv` — lift, p-value, медианы для каждого признака
- `pump_analysis/combo_stats.csv` — precision/recall/F1 для 2-признаковых комбинаций
- `pump_analysis/walkforward_results.csv` — 19 периодов walk-forward валидации
- `pump_analysis/sensitivity_sweep.csv` — чувствительность по типу пампа
- `pump_analysis/plots/feature_lift_bar.png`
- `pump_analysis/plots/walkforward_precision.png`

**Топ признак**: `vol_spike_count` — lift=2.055 (train), lift=2.232 (test), p<0.001.

**Лучшая комбинация**: `vol_spike_count > 0 AND vol_ratio_1h > 0.94` → precision=39%, recall=70%, lift=2.14.

---

## ST4 — Отчёт

Финальный отчёт с числами из реальных данных:

```
PUMP_PATTERNS_ANALYSIS.md  — в корне репо
```

Отчёт сгенерирован вручную по данным из всех предыдущих этапов. Для пересоздания — перезапустить ST1→ST3, затем обновить числа в разделах 3–7.

---

## Структура директории

```
pump_analysis/
├── README.md                       # этот файл
├── fetch_historical.py             # ST1: klines + funding + OI
├── fetch_catalyst_data.py          # ST1: catalyst data
├── fetch_remaining.py              # ST1: дозагрузка
├── finalize_manifest.py            # ST1: манифест
├── pump_labeler.py                 # ST2: разметка
├── causal_attributor.py            # ST3b: атрибуция
├── feature_engineer.py             # ST3: фичи
├── data_manifest.json              # манифест данных
├── pump_events.csv                 # 677 пампов
├── control_windows.csv             # 3385 контрольных окон
├── label_stats.json
├── causal_attribution.csv
├── catalyst_event_study.csv
├── catalyst_stats.json
├── feature_matrix.csv
├── feature_stats.csv
├── combo_stats.csv
├── walkforward_results.csv
├── sensitivity_sweep.csv
├── klines_1h/                      # OHLCV 1h per symbol
├── klines_5m/
├── klines_15m/
├── catalyst_data/
│   ├── macro_events.csv
│   ├── stablecoin_flows.csv
│   ├── sweep_clusters.csv
│   ├── news_signals.csv
│   └── token_unlocks.csv
└── plots/
    ├── catalyst_distribution.png
    ├── feature_lift_bar.png
    └── walkforward_precision.png
```

---

*Период данных: 2024-05-01 → 2026-05-27 | 28 символов USDT-perp | Binance Futures + Bybit V5*
