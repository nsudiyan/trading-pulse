# dump_analysis — Как запустить

Пайплайн состоит из 4 этапов (ST1 → ST2 → ST3 → ST4). Каждый этап зависит от предыдущего. Запускать строго по порядку из корня репозитория (`/Users/nikitasudian/Desktop/трейдинг/`).

---

## Зависимости

```bash
pip install pandas scipy numpy requests tqdm matplotlib
```

Python 3.11+.

---

## ST1 — Данные (переиспользованы из pump_analysis/)

Dump analysis **не требует отдельной загрузки данных** — klines_1h, funding, OI и ликвидации берутся из `pump_analysis/`. Запустите `pump_analysis/README.md` ST1 если данных ещё нет.

**Источники:**
- klines 1h: `pump_analysis/klines_1h/` (29 символов Bybit perp, 2024-05-01 → 2026-05-27)
- Ликвидации: `pump_analysis/liquidations_summary.csv`
- BTCUSDT для btc_cascade: `pump_analysis/klines_1h/BTCUSDT*.csv.gz`

---

## ST2 — Разметка дампов (AVEC-59)

Обнаруживает dump-события в klines_1h, генерирует контрольную выборку (K=5 контролей на каждый дамп).

```bash
python dump_analysis/dump_labeler.py
```

**Параметры разметки (в начале файла):**
- `DUMP_PCT = 15` — порог падения цены (%)
- `FAST_PCT = 10` — порог быстрого дампа (≤1h)
- `VOL_MULT = 2.0` — порог объёма (× медианы 30 дней)
- `SPLIT_DT = "2025-11-01"` — граница train/test

**Выходные файлы:**
- `dump_events.csv` — 593 дампа (dump_id, symbol, start_ts, end_ts, pct_chg, vol_mult, dump_type, split)
- `control_windows.csv` — 2965 контрольных окон
- `label_stats.json` — статистика: 386 standard, 207 fast; train=272, test=321
- `sensitivity_sweep_dump.csv` — sweep по порогам [10–20%] × окнам [1–24h]

---

## ST3 — Каузальная атрибуция (AVEC-63)

Для каждого дампа определяет катализатор через event-study с контрольными окнами. 6 категорий: btc_cascade, liq_cascade, macro_event, stablecoin_outflow, news_negative, token_unlock.

```bash
python dump_analysis/dump_causal_attributor.py
```

**Требует:** `dump_events.csv`, `control_windows.csv`, `pump_analysis/klines_1h/BTCUSDT*.csv.gz`, `pump_analysis/liquidations_summary.csv`

**Выходные файлы:**
- `causal_attribution.csv` — 593 строки: primary_cause для каждого дампа
- `catalyst_event_study.csv` — lift + p-value по категориям
- `catalyst_stats.json` — агрегированная статистика

**Ключевые результаты (из catalyst_stats.json):**
- `btc_cascade`: lift=4.755, **HIGH confidence**, полное покрытие (97/593 дампов)
- `liq_cascade`: lift=7.953, MEDIUM confidence, только 2026-04-14+ (15/149 покрытых)
- `macro_event` (Fear&Greed ≤25): lift=0.824 — статистически **не подтверждено**
- `stablecoin_outflow`: lift=0.500 — **не подтверждено**
- **50.3%** дампов (298/593) — причина не установлена (`unknown`)

---

## ST4 — Feature Engineering (AVEC-60)

Вычисляет 12 предсказывающих признаков для каждого дампа и контрольного окна. Признаки берутся строго из окна [-4h, 0) — без lookahead.

```bash
python dump_analysis/dump_feature_engineer.py
```

**Требует:** `pump_analysis/klines_1h/`, `dump_events.csv`, `control_windows.csv`

**Выходные файлы:**
- `feature_matrix.csv` — строк × 12+ колонок (признаки + метаданные)
- `feature_stats.csv` — lift, p-value, медианы на train split (n_dump=272, n_ctrl=1360)
- `combo_stats.csv` — precision/recall/F1 для 2-признаковых комбинаций
- `walkforward_results.csv` — 19 периодов walk-forward валидации (rolling monthly)
- `walkforward_oos.csv` — полный OOS по месяцам (2024-06 → 2026-05)
- `regime_analysis.csv` — разбивка по BTC-режиму (bear/sideways/bull)

**Топ признак:** `vol_spike_count` — lift=1.684 (train), p<0.001, 13/19 OOS-периодов с lift>1.

**Лучшая комбинация:** `vol_spike_count + vol_ratio_1h` → precision=28.8%, recall=44.7%, lift=1.696 (train).

**АНТИСИГНАЛЫ (значимые, lift<1):**
- `support_break`: lift=0.667, p=0.022 — слом поддержки до дампа НЕ является leading-сигналом
- `btc_regime=+1` (bull): lift=0.808, p<0.001 — bull BTC снижает вероятность дампа

### Валидация ST4

```bash
python dump_analysis/dump_st4_validation.py
```

Cross-check числовых результатов (базовые ставки, drift по периодам).

---

## ST5 — Отчёт (AVEVA-68)

Финальный отчёт с числами из реальных данных:

```
DUMP_PATTERNS_ANALYSIS.md  — в корне репо
```

Для пересоздания — перезапустить ST2→ST4, затем обновить числа в разделах 2–9.

---

## Полный пересчёт

```bash
python dump_analysis/dump_labeler.py && \
python dump_analysis/dump_causal_attributor.py && \
python dump_analysis/dump_feature_engineer.py && \
python dump_analysis/dump_st4_validation.py
```

Время: ~5–15 мин (зависит от числа символов в klines_1h/).

---

## Структура директории

```
dump_analysis/
├── README.md                       # этот файл
├── dump_labeler.py                 # ST2: разметка
├── dump_causal_attributor.py       # ST3: атрибуция
├── dump_feature_engineer.py        # ST4: фичи
├── dump_st4_validation.py          # ST4: валидация
├── dump_events.csv                 # 593 дампа
├── control_windows.csv             # 2965 контрольных окон
├── label_stats.json                # статистика разметки
├── causal_attribution.csv          # причина каждого дампа
├── catalyst_event_study.csv        # lift + p-value по категориям
├── catalyst_stats.json             # агрегат по атрибуции
├── feature_matrix.csv              # матрица признаков
├── feature_stats.csv               # lift/p-value признаков
├── combo_stats.csv                 # 2-признаковые комбинации
├── walkforward_results.csv         # 19 периодов walk-forward
├── walkforward_oos.csv             # OOS по месяцам
├── regime_analysis.csv             # разбивка по BTC-режиму
├── sensitivity_sweep_dump.csv      # sensitivity по порогам
├── sensitivity_sweep.csv           # альтернативный sweep
└── plots/
    ├── regime_breakdown.png
    └── sensitivity_sweep_dump.png
```

---

*Период данных: 2024-05-01 → 2026-05-27 | 28 символов с dump events | Bybit V5 klines (переиспользованы из pump_analysis/)*
