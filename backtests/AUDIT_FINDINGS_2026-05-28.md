# Валидационный аудит модуля знаний (25-мес анализ + AVEA-81) — 2026-05-28

**Метод:** состязательный аудит, 10 независимых агентов, каждая находка перепроверена скептиком против реальных файлов. Все цифры сверены с CSV/JSON. Источник: workflow `validate-pump-knowledge` (`wf_8acd0419-779`).

> ⚠️ **Этот документ ОТМЕНЯЕТ вывод `V1_VALIDATED_REPORT.md` (AVEA-81).** Тот отчёт нельзя использовать как основание активации `USE_VALIDATED_V1`. См. §3.

---

## 1. Что РЕАЛЬНО и чисто (фундамент держится)

- **Объёмное ядро `vol_spike_count`, `vol_ratio_1h/4h` — чистое и OOS-валидное.** Считается строго из pre-t klines (`start_ms-1`), без lookahead. `feature_stats.csv`: vol_ratio_1h lift 1.599, vol_ratio_4h 1.605, vol_spike_count 1.959 (train); walk-forward median lift 3.03, **18/19 периодов lift>1**. Подтверждено сверкой с `walkforward_results.csv`, `pump_events.csv` (ровно 1664 пампа), `label_stats.json` (base rate 16.67%).
- **Метки без lookahead** (`_fwd_rolling` берёт строго будущие бары, исключая свечу события). Контрольные окна **не пересекаются** с пампами (0/8320). Train/test сплит цел (train ≤2025-10-31, test ≥2025-11-01).
- **PUMP/DUMP отчёты — честные:** почти все числа воспроизводятся точно. macro_extreme разоблачён верно (train 6.0 → event-study 1.0). btc_cascade для дампов 5.115 HIGH. support_break-ретракция подтверждена (p=0.578).
- **Источник данных размечен верно:** klines/funding=Binance, OI/liq=Bybit.

## 2. Дефекты ФУНДАМЕНТА (чинить, но ядро не трогают)

| ID | Severity | Находка | Файл |
|---|---|---|---|
| LA1/LA2 | HIGH | **OI-lookahead:** `oi_features` читает OI В свече события t (нет `-1`). `oi_chg_1h`/`oi_oi_btc_corr` включают внутрисобытийный OI. Эффект мал (oi_chg_1h lift 0.93, незначим), но баг реален. Есть и в дамп-пайплайне. | `feature_engineer.py:211`, `dump_feature_engineer.py:235` |
| LEAK1 | HIGH | **Утечка метки:** `catalyst_hit_*` заданы 0 для ВСЕХ контролей по построению → механический lift ровно 6.0 и фейковый p=0 в `feature_stats.csv`. Это частично сама метка, не предиктор. | `feature_engineer.py:361-365` |
| LEAK1b | LOW | Смягчение есть: `catalyst_hit_*` исключены из combos/walkforward (`feature_engineer.py:669-673`), поэтому headline-lifts НЕ отравлены. Отравлена только таблица `feature_stats.csv`. | — |
| LA3 | MED | Окна детекции катализаторов liq_sweep(+1h)/news(+2h)/unlock(+1d) лезут ЗА t → эти «катализаторы» ретродиктивны, не предиктивны. macro_extreme и stablecoin — чисты ([t-48h, t)). | `causal_attributor.py:130-188` |
| BUG1 | MED | `matched_pump_id` в `control_windows.csv` побит ремапом (8180/8320 указывают на чужой символ). Целостность трассировки, на результаты НЕ влияет. | `pump_labeler.py:236-264,358` |

## 3. AVEA-81 НЕВАЛИДЕН (все находки CRITICAL/HIGH подтверждены)

- **SCOPE (CRITICAL):** бэктест валидирует `calibration/signal_weights.json` (13 СТАРЫХ флагов скринера: choch/oi/rsi/funding/ema/mtf), а live-путь `USE_VALIDATED_V1` считает ДРУГОЕ (vol_spike/vol_ratio/btc_trend/price_vs_high/btc_cascade). **Детекторы вообще не читают `signal_weights.json`.**
- **PROV/CIRC (HIGH):** веса нафитены `calibration/train_model.py` логрегом на том же `resolved.csv` (StratifiedKFold shuffle, без временного сплита). Ре-скоринг того же файла → **in-sample/циркулярно**. «13/13 OOS-подтверждено» — тавтология.
- **OOS (HIGH):** заявленный период «2025-11-01→2026-05-27» ложен — реально `resolved.csv` = 2026-04-12→2026-05-27; фильтр OOS отсекает 0 строк.
- Сам харнесс leakage-free (флаги из entry-time колонок), но это не спасает: он меряет не тот артефакт.

## 4. Live-код `USE_VALIDATED_V1` — НЕ реализует валидированное (чинить перед активацией)

| ID | Severity | Находка |
|---|---|---|
| LA3/LA4 | HIGH | **БАЗА НЕ ТА (killer):** анализ мерил vol_ratio/vol_spike против **30-дн rolling-median 1h-баров** (памп) / 20-ч медианы (дамп). Live считает против **~50-мин среднего 10×5m-свечей**. Это РАЗНАЯ статистика → валидированные lifts 1.96/1.60 **не переносятся** на live-расчёт. |
| LA2 | HIGH | Самореферентный ratio: `vol_avg10` пересекается с окном спайка. |
| UNW1 | HIGH | `stablecoin_inflow` (сильнейший, event-study 2.31) **закомментирован**. |
| SIGN1-3 | OK | Знаки антисигналов и буста — верные. |
| PROXY1 | MED | `btc_4h>0` как прокси `btc_trend_1h` — разные горизонты. |
| PROXY3/4 | MED | Синхронные Bybit REST-вызовы (7d-хай, BTC klines) на горячем пути — латентность/сбой. |
| CLAMP2 | MED | **Re-gating gap:** V1-блок после `MIN_SIGNAL_SCORE`-гейта, второго гейта нет → сигнал с отрицательной V1-поправкой всё равно эмитится. |
| EXCH1 | MED | Валидировано на Binance, live читает Bybit. |
| VOL4H/SPIKE_RANGE | MED | `vol_ratio_4h` формула (sum vs mean) и шкала `vol_spike_count` (5m vs 1h-бары) отличаются от анализа. |

## 5. ИСПРАВЛЕНИЕ моего же A1 (правило no-fab)

В первом прогоне `v1_real_factors_oos.py` я показал `stablecoin_inflow==1 → lift 6.0, P=100%` и назвал «worth wiring». **Это АРТЕФАКТ УТЕЧКИ** (LEAK1): в `feature_matrix.csv` `catalyst_hit_stablecoin_inflow`=0 у всех контролей по построению, поэтому P(памп|флаг)=100% тавтологично. **Реальная (чистая) ценность stablecoin = event-study lift 2.31** (`catalyst_event_study.csv`, окно [t-48h,t)), а не 6.0. Объёмные факторы (vol_spike/vol_ratio) в A1 — чисты и валидны; stablecoin-строку из A1 убрать/пометить как leakage.

## 6. Воспроизводимость

- A1: `python3 backtests/v1_real_factors_oos.py` (поправить: исключить stablecoin как leakage)
- B1: `python3 backtests/exit_ladder_sim.py`
- Аудит: workflow `validate-pump-knowledge`, полный лог в task `w2ljs7lw6`.
