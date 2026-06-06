# LIVE-CODE AUDIT — 2026-05-30

Многоагентный аудit (Workflow): 67 агентов, ~3M токенов, 21 мин. 12 модулей live-пути × 5 классов багов → состязательная верификация каждой находки → синтез.

**Подтверждено: 45 багов (после дедупа синтезатора ~38). Бьют по LIVE: 34. Critical: 0.**

Верифицировано вручную (Claude Code): DistGate-0/BreakoutFloor/RSI-блок мертвы (ключ `direction` не существует); cooldown/daily-limit пампа не персистятся. Остальное — доверие к верификатору с указанной confidence.

---
## ОТЧЁТ ПРЕДСЕДАТЕЛЯ

Все баги уже верифицированы — мне нужно лишь синтезировать их в отчёт по заданной структуре. Данные полные, дополнительных вызовов кода не требуется.

## Сводка

Всего подтверждённых багов: **38**. Бьют по LIVE (`affects_live=true`): **27**. Анализ-онли (искажают офлайн-метрики/выводы): **11**.

По тяжести среди LIVE: **High — 4**, **Medium — 11**, **Low — 12**. Critical нет.

Доминирующий класс — `fail-open-stale` (тихий отказ источника / устаревшие данные молча проходят как валидные) и `look-ahead` (живая незакрытая свеча втекает в решение). Самый болезненный кластер — четыре мёртвых гейта в `screener.py`, завязанные на несуществующий ключ `r['direction']`, и нестабильность счётчиков в `pump_detector.py` при рестартах.

---

## 🔴 Critical/High — бьют по живым сигналам/деньгам (affects_live=true)

Ранжировано по тяжести и денежному риску.

**1. Cooldown и дневной лимит только в памяти — рестарт обнуляет (HIGH)**
- screener-pump · `pump_detector.py:79, :137`
- При `launchctl kickstart` (частые рестарты) `_sent_recently` и `_daily_alerts` обнуляются: дубль-алерт на тот же символ внутри 4ч cooldown и превышение `MAX_DAILY_ALERTS=5` посреди дня. Прямой денежный риск (повторные входы).
- Фикс: персистить оба dict в atomic JSON при обновлении, грузить при старте `watch()`, отбрасывая записи старше cooldown / не за сегодня.
- confidence 0.90

**2. DistGate-0 (short_dist/LONG блок) мёртв — `r['direction']` не существует (HIGH)**
- screener-gates · `screener.py:6358-6363`
- `.get("direction","")` всегда `""` → условие всегда False → самое убыточное ведро (short_dist/LONG: WR 30.1%, totR −24.3) проходит в Telegram без помех. Двойной промах: даже при наличии ключа значения `'лонг'/'long'` не совпали бы с реальной схемой `'ЛОНГ'`.
- Фикс: определять направление по `setup` (как в `build_trade_plan`/pending `_sdir`); убрать молчаливый дефолт `""`, логировать отсутствие направления.
- confidence 0.95

**3. BreakoutFloor (LONG-флор score<160) мёртв — тот же ключ `direction` (HIGH)**
- screener-gates · `screener.py:7045-7059`
- Флор для breakout-лонгов (WR 36.3%) не срабатывает: `r.get("direction")=="ЛОНГ"` всегда False, предикат всегда пропускает. Сегмент 120-159 без CHoCH/RSI-подтверждения течёт в алерты.
- Фикс: убрать условие на `direction` — ветка и так только для `setup=='breakout'` (breakout = лонг по построению), оставить `score<160 or (choch!=bull_choch and rsi>=40)`.
- confidence 0.96

**4. outcome_4h/24h классификация TP-first — кормит LIVE Claude WR-контекст (HIGH)**
- outcome-tracker · `outcome_tracker.py:449-450, 457-458, 522-523, 529-530`
- При касании и TP, и стопа внутри окна метка всегда `TP1` (стоп-первым игнорируется) → завышенный WR. Этот лейбл читает `_fmt_historical_wr` и инжектит в контекст, который видит Claude при GO/SKIP → толкает живого привратника к APPROVE.
- Фикс: заменить inline TP-first тернарник на уже существующий order-aware `outcome_model.outcome(...)` (пессимистичен при ничьей/неизвестном порядке).
- confidence 0.90

**5. rs_btc как ratio меняет знак при падающем BTC (MEDIUM)** *(дубль-находка: «screener-scoring» и «xcut-sign-unit»)*
- screener-scoring · `screener.py:2717` (исп. 2998-3004, 3138-3144, 3326-3334, 3805-3813)
- `rs_btc = pair_chg/btc_chg_24h` без same-sign проверки: BTC −3%, альт −4.5% → rs=+1.5, и падающий аутсайдер набирает бычьи баллы как «лидер от дна»; пороги `>1.3`/`0<rs<2` калибровались под BTC-up. `rs_weak` (<0) этот both-down кейс НЕ ловит.
- Фикс: гейтить ratio-бонусы за `rs_btc_pp>0` (sign-stable, уже считается) либо требовать одинаковый знак ног.
- confidence 0.82

**6. Conviction-гейт (<65) полностью обойдён в `_promote_wait_watchlist` (MEDIUM)**
- pump-gates-send · `pump_detector.py:1945-1961` (против 2034-2049)
- Кандидат из Claude WAIT-watchlist при повторном GO уходит в TG с сырым score 35-64, минуя `CONVICTION_MIN_SCORE=65`, DEX-adj и bad-hours гейт. Комментарий: GO при score<70 дал 0 WIN.
- Фикс: вынести `_passes_quality_gates(c, utc_h)` и звать в обоих путях; перед `send_pump_alert` проверять `final_score < CONVICTION_MIN_SCORE: continue`.
- confidence 0.85

**7. Momentum-алерт использует устаревший funding — FundGate на данных времени детекта (MEDIUM)**
- pump-gates-send · `pump_detector.py:1891-1895, :1558`
- При momentum обновляется только `price`; FundGate (`funding<=-0.5%` блок) и тело алерта читают funding, снятый 4-8ч назад → ложный блок исправившегося сетапа или пропуск заведомо проигрышного pump-LONG.
- Фикс: дёрнуть свежий ticker `fundingRate` и обновить `c['funding']` перед `send_pump_alert`.
- confidence 0.88

**8. Verdict-кэш RT-фильтра без направления в ключе — GO/TP/SL переиспользуются для ПРОТИВОПОЛОЖНОЙ стороны (MEDIUM)**
- rt-filter · `claude_realtime_filter.py:788, 840-843, 900-901`
- Ключ `(symbol, setup)` без direction; при флипе bos_fvg/short_dist внутри `CACHE_TTL_SEC=1800` возвращается чужой вердикт с TP/SL не той стороны, минуя unlock LONG-veto. Подтверждён реальный флип ETHUSDT за ~8.5 мин.
- Фикс: ключ `(symbol, setup, direction)`; не кэшировать GO через смену направления.
- confidence 0.82

**9. OI-data outage → `oi_regime='stable'` молча обходит DistGate-A/C (MEDIUM)**
- screener-gates · `screener.py:6159-6162, 2336-2348, 6366/6381`
- Тихий отказ OI-фида → пустой `oi_hist` → `oi_change=0.0` → `'stable'` проходит Gate A и не даёт OI-confirm в Gate C; защита «не шортить, когда входят лонги» теряется именно при отсутствии данных.
- Фикс: при пустом/коротком `oi_hist` ставить `oi_regime='unknown'`/флаг `oi_stale`, трактовать консервативно для шорта + лог.
- confidence 0.90

**10. Тихий fail-open `_fetch_symbol_data_parallel` — занулённые OI/CVD/funding, обход OI-трапа (MEDIUM)**
- screener-features · `screener.py:6148-6163, 2602-2611`
- При сбое источника воркер молча продолжает на нейтралях; особо опасно: `oi_change=0.0` при `oi_hist=[]` тихо проходит трап `if oi_change>30: return None` — взрывной OI больше не блокирует FOMO-ловушку.
- Фикс: различать «нет данных» и «нейтрально»; при пустом `oi_hist` не выдавать high-confidence сигнал / skip / явный data_quality-флаг.
- confidence 0.82

**11. `detect_fvg` сканирует до n включительно — FVG на живой свече даёт баллы и уровни плана (MEDIUM)**
- screener-features · `screener.py:915-938` (план 3926-3927)
- Цикл `range(start, n)` достигает живой свечи: гэп может появиться/исчезнуть к закрытию часа, а `bfvg_top/bot` задают вход/стоп на дрейфующем интрабар-экстремуме.
- Фикс: `range(start, n-1)` чтобы 3-я свеча гэпа была закрытой; `price=closes[-1]` оставить.
- confidence 0.85

**12. `detect_order_blocks` сканирует до n включительно — BOS/OB по close живой свечи (MEDIUM)**
- screener-features · `screener.py:956-985` (score 2944-2946, 3098, 3573)
- BOS подтверждается только закрытием; на `i=n-1` `closes[i]` меняется каждую секунду → OB то появляется, то исчезает, давая живые баллы и зоны плана.
- Фикс: `range(start, n-1)`, согласовать `start = max(6,(n-1)-lookback)`.
- confidence 0.90

**13. `fetch_btc_4h_ema_position` считает EMA/цену по живой 4h-свече — BTC-регимный хард-гейт мигает (MEDIUM)**
- screener-features · `screener.py:1858-1875` (гейты 3739-3747, 3829)
- `price=cl[-1]` и обе EMA включают незакрытый 4h-бар → один и тот же шорт то блокируется (`return None`/−30/DistGate), то проходит в зависимости от минуты скана. Не воспроизводимо.
- Фикс: считать по `cl[:-1]`, `price=cl[-2]` (закрытый 4h-close); прогревать EMA SMA-seed.
- confidence 0.86

**14. CVD fail-open — `_calc_cvd_5min` молча возвращает 0.0, проходит гейты обоих паттернов (MEDIUM)** *(дубль: «pump-scoring»)*
- pump-scoring · `pump_detector.py:868` (гейты 990, 1066)
- При сбое fetch сделок `0.0,'Error'`, но метка `'Error'` нигде в скоринге не проверяется; 0.0 проходит `>=-20` (squeeze) и `<=10` (rug) — сигнал выходит на занулённой CVD-фиче.
- Фикс: при `cvd_source=='Error'` — skip символа или fail-closed для CVD-гейтов (`if cvd_source!='Error' and ...`).
- confidence 0.85

**15. CVD partial-window fallback — при <5 сделок берутся ВСЕ до 500 (часы истории) под видом 5-мин (MEDIUM)** *(дубль: «pump-scoring» и «xcut-sign-unit», low)*
- pump-scoring · `pump_detector.py:855-866`
- На тонком символе `recent=trades` (до 7.6ч горизонта) подаётся в те же 5-мин гейты/усилители; метка `'CVDAll'` только в тексте.
- Фикс: не применять CVD-гейты при `source=='CVDAll'` / отбраковывать fallback, если старейший трейд старше ~15 мин.
- confidence 0.85

**16. RSI>80 хард-блок ЛОНГов мёртв для swing-лонга — тот же ключ `direction` (MEDIUM)**
- screener-gates · `screener.py:7092-7097`
- `r.get("direction")=="ЛОНГ"` всегда False. Squeeze/breakout/bos_fvg спасает соседняя ветка по `setup` (7099-7105), но **swing-лонг** с RSI>80 не блокируется ничем (range_sweep/short_dist в живом пути не лонги — уже).
- Фикс: лонговость по `setup`: `rsi>80 and _is_long(r)`, `_is_long` из setup; убрать дефолт `""`.
- confidence 0.90

**17. TLDB circularity — правила добыты и применены на одной популяции без holdout (MEDIUM)**
- calibration-loop · `trade_learnings_db.py:316-475`, `screener.py:4137-4201`
- Паттерны с `delta_pp<=-12` отобраны in-sample и применяются как живые гейты (`-25`/до `-30` к score) на той же пересекающейся выборке; нет walk-forward, эффект самоподкрепляется. 26 correction + 14 prohibited + 39 filters активны live.
- Фикс: point-in-time split — добывать до cutoff, валидировать/применять после; промоутить правило только если delta_pp выживает OOS.
- confidence 0.85

**18. `detect_htf_trend` включает живую свечу в свинг-пивоты и SMA-fallback (LOW)** *(дубль: «screener-scoring» и «xcut-sign-unit»)*
- screener-scoring · `screener.py:869-901` (вызовы 2639-2640)
- Единственный структурный детектор без обрезки `[-1]`: пивот n-3 квалифицируется против живой свечи `highs[n-1]`, SMA-fallback по `closes[-1]` → trend-лейбл мигает внутри бара, втекает в гейты/скоры.
- Фикс: `highs, lows, closes = highs[:-1], lows[:-1], closes[:-1]` в начале функции, как у сиблингов.
- confidence 0.90

**19. liq_long/short_usd молча обнуляются при отсутствии liq_stats (LOW)**
- screener-scoring · `screener.py:2796-2799` (s1/s4/s5)
- При сбое Coinalyze фича уходит в нейтраль 0$ без флага деградации; реальный liq-бонус подавляется (fail-toward-neutral).
- Фикс: флаг `liq_available`; при недоступности — лог + явно не применять liq-ветки.
- confidence 0.82

**20. `detect_oi_divergence` — цена `closes[-2]`, OI `oi_hist[-1]` (незакрытый час) → off-by-one рассинхрон (LOW)**
- screener-features · `screener.py:517-526` (score 2900-2902)
- Два ряда на сдвинутых окнах + последний OI ещё формируется → знак дивергенции переворачивается на границе; влияет на score и `pos_tp_mult`.
- Фикс: выровнять — `oi_end=oi_hist[-2]`, `oi_start=oi_hist[-(lookback+2)]`.
- confidence 0.85

**21. choch_conviction даёт −30 к min-score для short_dist на основании БЫЧЬЕГО choch (LOW)**
- screener-gates · `screener.py:4063, 6423-6424`
- `choch_conviction=(choch_1h=='bull_choch')` — бычий сигнал, но скидка планки применяется без setup-guard и к short_dist (шорт): слабый шорт (60-84) проходит туда, куда не должен.
- Фикс: применять скидку только к лонг-сетапам; для short_dist — флаг по `bear_choch`.
- confidence 0.83

**22. ATR_COIL включает незакрытую свечу в recent_atr — заниженный TR, паттерн срабатывает легче (LOW)**
- pump-scoring · `pump_detector.py:1192-1193`
- `highs[-7:]/lows[-7:]` тянут `[-1]` (forming), `hist_atr` — только закрытые → `atr_ratio` смещён вниз, `<0.55` срабатывает чаще (Monte-Carlo: +51% ложных триггеров).
- Фикс: `recent_atr=_atr_simple(highs[-8:-1], lows[-8:-1], closes[-9:-2])`.
- confidence 0.85

**23. Bad-hours score-гейт (>=90) обходится momentum-алертами (LOW)**
- pump-gates-send · `pump_detector.py:1988` против 2046
- Гейт времени применяется к моменту обнаружения, а не отправки: кандидат из хорошего часа срабатывает по momentum в плохой UTC-час без проверки `score>=90`.
- Фикс: в `_check_momentum` повторять bad-hours гейт по текущему `_utc_h`.
- confidence 0.85

**24. Macro-контекст без staleness-гейта — устаревшее макро молча инжектится в Claude (LOW)**
- rt-filter · `claude_realtime_filter.py:242-245, 560-564`
- `ts` пишется, но никогда не читается; при тотальном отказе `get_macro()` последние строки висят бесконечно, подаются как ≤5-мин-свежие.
- Фикс: в `_get_macro_lines` читать `ts`, при возрасте >15-30 мин дропать/помечать «⚠ MACRO STALE».
- confidence 0.78

**25. `_fmt_channel_mentions` помечает «(6-8ч)» но не фильтрует по времени — стейл-конфлюенс как свежий (MEDIUM)**
- rt-filter · `claude_realtime_filter.py:358-404, 542`
- Считает ВСЕ кэшированные упоминания, эскалирует «⚡ HIGH CONFLUENCE» при ≥3; при простое channelreader инжектит выдуманную конфлюенс в живой вердикт. Сиблинги (`enrich_with_channel_signals`) фильтруют по `ts`.
- Фикс: читать `data.get('ts')`, при `age_h > LOOKBACK_HOURS` возвращать «кэш устарел».
- confidence 0.90

**26. `_fmt_historical_wr` исключает FLAT из знаменателя и игнорирует direction (MEDIUM)**
- rt-filter · `claude_realtime_filter.py:434-457, 633`
- WR = wins/(wins+losses) выбрасывает ~30% FLAT → завышение до +18пп; смешение LONG/SHORT для одного setup → WR не специфичен направлению (short_dist: SHORT 47.8% vs LONG 32.8%). Толкает Claude к GO.
- Фикс: включить FLAT в знаменатель (или переименовать «WR среди решённых») + фильтр по direction кандидата.
- confidence 0.83

**27. `_cached` отдаёт устаревший кэш как свежий при отказе источника (LOW)**
- data-failopen · `free_data.py:61-74` (стр.71)
- TTL-проверка только на свежем пути; при ошибке fetcher возвращается `entry['data']` любого возраста без флага. Втекает в binance enrichment → голоса bull/bear в interpret_signals (топ-3 кандидата).
- Фикс: потолок возраста на stale-пути (`< ttl*STALE_FACTOR`), иначе None; отдавать `{'data','stale','age'}`.
- confidence 0.80

**28. Макро-блэкаут (FOMC/CPI) молча открывается при отказе ForexFactory (MEDIUM)**
- data-failopen · `free_data.py:182-198` → `telegram_alerts.py:1317-1366`
- `next_macro_window()` возвращает None и при «нет события», и при «календарь не загрузился»; потребитель не различает → `in_macro_blackout=False` → сигналы уходят перед релизом. Реальный триггер — cold-cache + outage.
- Фикс: трёхзначное состояние `in_window/clear/unknown`; при unknown — fail-CLOSED для рискованных секций + age-граница 24-48ч.
- confidence 0.84

**29. `_cached` кэширует успешный None на полный TTL — блокирует ретраи (LOW)**
- data-failopen · `free_data.py:61-74` (стр.72)
- Фетчеры глотают ошибку и возвращают None → негативный кэш на полный TTL (opt 30 мин, global 15 мин), повторные вызовы не ретраят; фича (btc_dominance, enrichment) молча отсутствует дольше нужного. Эффект мягкий (отсутствие, не искажение).
- Фикс: не кэшировать None или ставить короткий negative-TTL (~ttl/10).
- confidence 0.85

**30. TLDB gate direction-blind — short-loss правила штрафуют LONG-кандидатов (LOW)**
- calibration-loop · `trade_learnings_db.py:480-591`, `screener.py:4137-4201`
- Featurize без direction-токена; правило применяется по token-membership без проверки стороны. (Верификатор: флагманский инверсный кейс на данных не подтвердился — direction-consistent; реальный узкий дефект — `rsi_oversold` PEC-020 штрафует выигрышные oversold-шорты.)
- Фикс: добавить `direction:{...}` токен в featurize; гейтить prohibited/correction по направлению результата.
- confidence 0.82

**31. `_score_priority` считает обрабатываемую запись против себя — self-inclusion off-by-one (LOW)**
- calibration-loop · `learning_generator.py:240-261`
- `r is rca` тривиально проходит → +1 к счётчику, может поднять правило LOW→MEDIUM (или к HIGH) → больший живой штраф через `_TLDB_PENALTY_MAP`. Измеренный эффект ничтожен (флипает 1 правило, +6-8 пт, латентно).
- Фикс: `n = sum(... if r is not rca ...)`.
- confidence 0.85

---

## 🟡 Анализ-онли (искажают метрики/выводы, не live)

`affects_live=false` — реальные дефекты, но потребляются только офлайн-аналитикой / human-in-the-loop отчётами, в автогейты/скоринг не втекают.

| # | Модуль · file:line | Что не так | Severity | conf |
|---|---|---|---|---|
| 1 | outcome-tracker · `outcome_tracker.py:403-411` (вызовы 469-472, 540-543) | `_resolve_exit` всегда выбирает TP над стопом без учёта порядка касания → look-ahead `r_multiple` (4h/24h). Order-aware `outcome_model.outcome` уже существует, но не используется. Питает `check_score_gate` (личка владельцу) и калибровку — не автогейт. | medium | 0.82 |
| 2 | outcome-tracker · `outcome_tracker.py:452-461, 524-532` | MFE/MAE по экстремумам всего окна без обрезки по стопу → завышенная excursion для застопленных трейдов. Читается только офлайн-калибровкой (`joined_dataset` Spearman) и ручным отчётом. | low | 0.85 |
| 3 | outcome-tracker · `outcome_tracker.py:157-161` | `close_end=bars[0][4]` опирается на позиционный desc-порядок Bybit; при развороте порядка молча возьмёт старейший close. Сейчас корректно, латентный hardening-дефект. | low | 0.82 |
| 4 | reject-tracker · `reject_tracker.py:134-152` | `_fetch_price_at_horizon` берёт `bars[0]` из неразвёрнутого newest-first → резолв-цена ~15-30 мин ПОЗЖЕ горизонта (forward time-shift). Смещает офлайн-датасет оценки гейтов. | medium | 0.90 |
| 5 | reject-tracker · `reject_tracker.py:243-259, 191-192` | «signal correct» = любое направленное движение (`>0`/`<0`) без порога fee/магнитуды → завышает `pct_signal_correct`, смещает вердикты к `too_strict`. Только CLI `stats`. | low | 0.90 |
| 6 | pump-gates-send · `pump_detector.py:1833-1872` | Momentum burst (5m) и 1H-trend гейты молча fail-open при сбое kline. Перекрыто downstream Claude RT-фильтром + `FAIL_CLOSED_PUMP=True` → live значимо не пробивает (верификатор понизил до affects_live=false). | low | 0.80 |
| 7 | calibration-loop · `trade_learnings_db.py:581-589` | `utc_session` из `datetime.utcnow().hour` (scan-time), а не времени сигнала. В live совпадает посекундно; риск только при офлайн-реплее (которого сейчас нет). + deprecated `utcnow()`. | low | 0.85 |
| 8 | xcut-sign-unit · `pump_detector.py:1702-1710` | SHORT SL отображается с минусом `(−{_sl}%)`, хотя стоп шорта бьётся на росте (+%). Цена SL и R:R корректны — чисто label в TG. Автоторговли в проекте нет. | low | 0.90 |
| 9 | xcut-pit-survivorship · `fetch_historical.py:90-123` + `symbols_new.json` | Вся историческая вселенная выбрана по ТЕКУЩЕЙ ликвидности (снапшот 2026-05-27) при 24-мес истории → survivorship + look-ahead universe selection. `USE_VALIDATED_V1=False`, в live не втекает. | medium | 0.90 |
| 10 | xcut-pit-survivorship · `joined_dataset.py:98-102` + `shorts_joined.py:128-132` | Включают только сигналы с локальным klines-файлом (= та же survivorship-вселенная) → двойная фильтрация, ~30% сигналов (970 строк) молча выброшены, у дропнутых медианный объём 2.8x ниже. | medium | 0.92 |
| 11 | xcut-pit-survivorship · `outcome_tracker.py:136-175, 433/506` | Сигналы по делистнутым/нерезолвимым символам навечно в pending → resolved.csv не содержит худших delisting-кейсов (мягкий survivorship в исходах). Сейчас 0 застрявших >48ч. | low | 0.82 |

---

## Паттерны (повторяющиеся классы багов через модули)

1. **Мёртвый ключ `r['direction']` — четыре гейта подряд.** DistGate-0 (6358), BreakoutFloor (7050), RSI>80-блок (7095) и читается в `check_score_gate.py:45`. Ключ НИКОГДА не пишется в row-словарь `score_symbol` (направление выводится позже из `setup`/`verdict`). Один корневой дефект → 3+ молча отключённых живых гейта. **Самый дешёвый высокоотдачный фикс: завести `direction` в row один раз.**

2. **Живая (незакрытая) свеча `[-1]` в детекторах.** `detect_htf_trend`, `detect_fvg`, `detect_order_blocks`, `fetch_btc_4h_ema_position`, `detect_oi_divergence` (off-by-one), ATR_COIL — все тянут forming-свечу, тогда как ~12 сиблингов сознательно режут `[:-1]` (документированная конвенция `fetch_klines:304`). Класс look-ahead/repaint: решение «сейчас» зависит от ещё не финальных экстремумов бара.

3. **Тихий fail-open / fail-toward-neutral при отказе источника.** Самый массовый класс (≥14 находок): OI/CVD/funding/liq/binance-enrichment/macro/channel/ForexFactory — все молча уходят в нейтраль или устаревший кэш без флага деградации. Особо: обход OI-трапа `>30`, обход макро-блэкаута FOMC/CPI, выдуманная channel-конфлюенс. Оператор не отличает «данных нет» от «рынок тихий».

4. **In-memory состояние теряется при рестарте.** Cooldown + дневной лимит (`pump_detector`) — единственный, но дорогой случай; демоны рестартятся часто (`KeepAlive`).

5. **Order-blind / TP-first резолв исходов.** `outcome_tracker` всюду выбирает TP над стопом без порядка касания (label + r_multiple + MFE/MAE), хотя order-aware `outcome_model.outcome` уже написан и используется в pump-пути. Завышает WR — в т.ч. тот, что видит Claude.

6. **Survivorship / point-in-time в офлайн-вселенной.** Вселенная по сегодняшней ликвидности + двойная фильтрация по наличию klines + делисты застревают в pending. Train/test-сплит это НЕ лечит — смещён сам набор символов. Совпадает с памятью пользователя (V1 validation audit: циркулярность, нужен joined/holdout).

7. **sign-unit инверсии.** `rs_btc` ratio при BTC-down, choch_conviction для short_dist — направление/знак перепутаны на границе режима.

---

## Рекомендованный порядок починки

Принцип: сначала то, что молча выпускает заведомо убыточные сигналы или дубли в TG (прямой денежный риск), при минимальной стоимости фикса; офлайн-метрики — в конце.

1. **Завести `direction` в row-словарь `score_symbol` (один раз).** Чинит сразу DistGate-0, BreakoutFloor, RSI>80-блок и `check_score_gate`. Один источник правды по направлению. Максимальная отдача / минимальная стоимость, бьёт по самому убыточному ведру (short_dist/LONG, WR 30%). **Первым.**

2. **Персистить cooldown + дневной лимит на диск.** Прямой денежный риск (дубль-входы, превышение лимита посреди дня), частые рестарты — реальны. Тривиальный atomic-JSON по образцу `pump_pending.json`.

3. **Conviction-гейт в `_promote_wait_watchlist` + verdict-кэш RT с direction в ключе.** Два узких, но точно денежных fail-open в pump/RT-пути: проигрышные score<65 и сигналы не той стороны с чужими TP/SL. Вынести `_passes_quality_gates`, расширить cache-key до 3-tuple.

4. **fail-open источников: OI-трап + макро-блэкаут + OI-regime DistGate.** Различать «нет данных» vs «нейтрально»: при пустом `oi_hist` не давать high-confidence/`oi_regime='unknown'`; макро-календарь недоступен → fail-CLOSED. Снимает класс «сигнал на занулённой критичной фиче перед релизом / при взрывном OI».

5. **Обрезка живой свечи `[:-1]` в пяти детекторах** (`detect_htf_trend`, `detect_fvg`, `detect_order_blocks`, `fetch_btc_4h_ema`, `detect_oi_divergence`). Один стилистически единый фикс по всему модулю → воспроизводимость гейтов и чистые уровни плана. Делать пакетом.

6. **order-aware outcome (`outcome_model.outcome`) в `outcome_tracker`** для label + r_multiple, затем recompute `resolved.csv`. Чинит и LIVE WR-контекст Claude (#4 High), и офлайн-калибровку разом. После — фиксы MFE/MAE и forward time-shift в `reject_tracker`.

7. **Остальные LOW live-нуджи** (rs_btc sign, choch_conviction, ATR_COIL, CVD-fallback/Error, stale-cache потолки, channel/historical-WR контекст) — пакетом, по мере касания соответствующих файлов.

8. **Офлайн-аналитика последней:** survivorship-вселенная, joined-фильтрация, pending-делисты, reject `pct_signal_correct` порог. Не трогают живые деньги, но критичны для корректных go/no-go перед масштабированием шортов / включением `USE_VALIDATED_V1` — чинить ПЕРЕД любым решением масштабировать по этим цифрам.

**Не делать:** включать `USE_VALIDATED_V1` или масштабировать шорт-эдж по текущим офлайн-метрикам, пока не починены п.8 (survivorship + circularity) — цифры lift/precision систематически завышены.

---
## ПОЛНАЯ ТАБЛИЦА (live сначала, по тяжести)


### 🔴 LIVE — HIGH
- **BreakoutFloor (LONG-флор score<160 + CHoCH/RSI) мёртв: тот же несуществующий ключ direction**
  - `screener-gates` · /Users/nikitasudian/Desktop/трейдинг/screener.py:7045-7059 · тип=fail-open-stale · conf=0.96
  - фикс: Использовать направление по setup (breakout → long) вместо r['direction']: т.к. ветка применяется только к setup=='breakout', а breakout по построению лонг, условие `r.get('direction')=="ЛОНГ"` просто лишнее — удалить его, оставив `r.get('setup')=='breakout' and (score<160 or (choch!=bull_choch and rsi>=40))`. Тогда флор заработает.
- **DistGate-0 (short_dist/LONG блок) мёртв: читает несуществующий ключ r['direction'] → fail-open**
  - `screener-gates` · /Users/nikitasudian/Desktop/трейдинг/screener.py:6358-6363 (чтение); схема строки 3970-4150 (ключ direction отсутствует) · тип=fail-open-stale · conf=0.95
  - фикс: Определять направление по setup, как это делается в build_trade_plan (5202-5218) и при сохранении в pending (7304: `_sdir = "short" if setup=="short_dist" else "long"`). Для DistGate-0: short_dist по построению — шорт; 'short_dist/LONG' возникает только если verdict перекрывает setup. Заменить на: вычислить _, verdict, _, _ = interpret_signals(r) (или прочитать r-плановый side) и блокировать, когда фактическое направление short_dist-строки == long. Либо — если бизнес-логика в том, что short_dist всегда шорт — убрать DistGate-0 как недостижимый. В любом случае убрать молчаливый дефолт "": при отсутствии направления логировать и НЕ пропускать молча.
- **Cooldown и дневной лимит хранятся только в памяти → сброс при рестарте демона, повторные алерты и превышение лимита**
  - `pump-gates-send` · pump_detector.py:79 (_sent_recently={}), 137 (_daily_alerts={}) · тип=fail-open-stale · conf=0.9
  - фикс: Персистить _sent_recently и _daily_alerts на диск (atomic json) при каждом обновлении и загружать при старте watch(), как делается с pump_pending.json. При загрузке отбрасывать записи старше COOLDOWN_SEC и daily-ключи не за сегодня.
- **outcome_4h / outcome_24h classification also TP-first (drives the LIVE Claude WR context)**
  - `outcome-tracker` · /Users/nikitasudian/Desktop/трейдинг/outcome_tracker.py:449-450 and 457-458 (4h); 522-523 and 529-530 (24h) · тип=look-ahead · conf=0.9
  - фикс: Replace the inline 'TP1 if hit_tp1 else STOP if hit_stop ...' branches with a call to outcome_model.outcome(hit_tp1, hit_stop, tp_touch_h, stop_touch_h) (mapping WIN→TP1/STOP→STOP, then the pct>0.5 WIN/LOSS/FLAT fallback for the no-touch case). This unifies screener outcomes with the pump path and removes the look-ahead from the label that the live filter reads.

### 🔴 LIVE — MEDIUM
- **RSI>80 хард-блок ЛОНГов мёртв для short_dist/swing/range_sweep: тот же direction-ключ**
  - `screener-gates` · /Users/nikitasudian/Desktop/трейдинг/screener.py:7092-7097 · тип=fail-open-stale · conf=0.9
  - фикс: Убрать зависимость от r['direction']. Лонговость определять по setup/verdict: для безусловного RSI>80-блока достаточно `(r.get('rsi_1h') or 50) > 80 and _is_long(r)`, где _is_long вычисляется из setup (squeeze/breakout/bos_fvg/swing-long/range_sweep-long = long, short_dist = short). Либо, поскольку RSI>80 перекупленность вредна и шортам тоже не как лонг-фильтр — пересмотреть намерение. Главное — устранить молчаливый дефолт "".
- **OI-data outage → oi_regime='stable' молча обходит DistGate-A/C для short_dist (fail-open)**
  - `screener-gates` · /Users/nikitasudian/Desktop/трейдинг/screener.py:6159-6162 (глушение исключения), 6149/6179 (oi default []), 2602-2606 (oi_change=0.0), 2336-2343/2348 (classify_oi_regime), 6366/6381 (DistGate-A/C) · тип=fail-open-stale · conf=0.9
  - фикс: Различать 'нет данных' и 'реально стабильно'. Если oi_hist пуст/слишком короткий — помечать oi_regime как 'unknown' (или прокидывать флаг oi_stale=True) и в DistGate трактовать отсутствие OI-данных консервативно для шорта (не засчитывать как 'не distribution', т.е. требовать confirm из надёжного источника либо блокировать с логом DistGate-DataMissing). Не маскировать отказ под 'stable'.
- **detect_order_blocks сканирует до n включительно → BOS и OB определяются по close живой свечи**
  - `screener-features` · /Users/nikitasudian/Desktop/трейдинг/screener.py:956-985 (loop 956, BOS-проверки 959 и 974); вызов 2650; score 2944-2946, 3098, 3573 · тип=look-ahead · conf=0.9
  - фикс: Ограничить цикл завершёнными свечами: for i in range(start, n-1) (и согласовать start = max(6,(n-1)-lookback)). price=closes[-1] для in_zone/dist оставить.
- **_fmt_channel_mentions labels output '(6-8ч)' but never filters by time — stale channel confluence shown as fresh**
  - `rt-filter` · /Users/nikitasudian/Desktop/трейdинг/claude_realtime_filter.py:358-404, 542 · тип=fail-open-stale · conf=0.9
  - фикс: Gate on the cache age: read data.get('ts'), and if now - cache_ts exceeds the claimed window (or a max-age), return 'Каналы: данные устарели (Nч)' instead of counting. Better, store per-item timestamps in the cache and filter items to the actual 6-8h window before counting. At minimum stop asserting '6-8ч' when no time filter is applied.
- **Momentum-алерт использует устаревший funding/фичи: обновляется только price, FundGate и тело алерта читают funding времени обнаружения**
  - `pump-gates-send` · pump_detector.py:1891-1895 (обновление c) + 1558 (FundGate в send_pump_alert) · тип=fail-open-stale · conf=0.88
  - фикс: При срабатывании momentum дёрнуть свежий ticker (fundingRate) для символа и обновить c['funding']/c['fund_prev'] перед send_pump_alert, либо пересчитать FundGate на свежем funding. Минимум — рефреш funding в _check_momentum рядом с обновлением price.
- **fetch_btc_4h_ema_position: цена и EMA20/EMA50 считаются по живой (незакрытой) 4h-свече → BTC-регимный хард-гейт мигает внутри 4h-бара**
  - `screener-features` · /Users/nikitasudian/Desktop/трейдинг/screener.py:1858-1875; вызов 6517; гейты 3739-3740, 3746-3747, 3829-3832; также fetch_btc_4h_change 1849 · тип=off-by-one · conf=0.86
  - фикс: Считать EMA и сравнение по завершённым 4h-свечам: использовать cl[:-1] и price=cl[-2] (последний закрытый 4h-close), либо сравнивать текущую mark-цену с EMA, посчитанной по закрытым барам. Для btc_chg_4h аналогично рассмотреть закрытый бар. EMA желательно прогревать (seed=SMA первых n) для устойчивости.
- **detect_fvg сканирует до n включительно → FVG на живой (незакрытой) свече попадает в зоны, добавляет score и задаёт уровни план-сделки**
  - `screener-features` · /Users/nikitasudian/Desktop/трейдинг/screener.py:915-938 (loop 915); вызов 2646; score 2935-2944, 3091-3098, 3566-3573; план 3926-3927 · тип=look-ahead · conf=0.85
  - фикс: Менять верхнюю границу цикла на завершённые свечи: for i in range(start, n-1) (или range(max(2,(n-1)-lookback), n-1)), чтобы i-я свеча (третья в гэпе) была закрытой. price=closes[-1] для in_zone/dist оставить (это легитимная текущая цена).
- **CVD fail-open: при отказе fetch сделок _calc_cvd_5min молча возвращает 0.0, которое проходит CVD-гейты обоих основных паттернов**
  - `pump-scoring` · /Users/nikitasudian/Desktop/трейдинг/pump_detector.py:868 (источник), используется 953, гейты 990 и 1066 · тип=fail-open-stale · conf=0.85
  - фикс: Разделить «честный 0» и «ошибку». Возвращать sentinel (например cvd_pct=None или cvd_source='Error') и в _analyze_symbol при cvd_source=='Error' либо fail-closed (return None / не давать паттернам, использующим CVD как гейт, активироваться), либо как минимум применять штраф к score и не засчитывать CVD-усилители. Сейчас гейты `cvd_pct >= -20` / `cvd_pct <= 10` нужно делать недостижимыми при ошибке: `if cvd_source != 'Error' and cvd_pct >= -20 ...`.
- **CVD partial-window fallback: при <5 сделок в 5-мин окне берутся ВСЕ сделки (до 500, произвольный горизонт), но cvd_pct используется в тех же 5-мин гейтах/усилителях**
  - `pump-scoring` · /Users/nikitasudian/Desktop/трейдинг/pump_detector.py:855-858 · тип=fail-open-stale · conf=0.85
  - фикс: Либо не использовать CVD-гейты/усилители при source=='CVDAll' (или применять заниженный вес), либо ограничивать fallback по реальному временно́му охвату (отбраковывать, если самый старый трейд старше, скажем, 15 мин). Минимум — пробрасывать факт деградации в скоринг, а не только в строку алерта.
- **Conviction-гейт (<65) полностью обойдён в пути _promote_wait_watchlist → сигналы ниже порога уходят в TG**
  - `pump-gates-send` · pump_detector.py:1945-1961 (_promote_wait_watchlist) против 2034-2049 (watch loop) · тип=fail-open-stale · conf=0.85
  - фикс: В _promote_wait_watchlist перед send_pump_alert применить ту же логику, что в watch loop: посчитать dex_adj и final_score, и `if final_score < CONVICTION_MIN_SCORE: continue` (+ bad-hours гейт). Либо вынести единую функцию _passes_quality_gates(c, utc_h) и звать её в обоих путях.
- **Circularity: TLDB prohibited/correction/filter rules mined over all resolved outcomes are applied as live gates on the same overlapping population — no train/test holdout**
  - `calibration-loop` · trade_learnings_db.py / screener.py:trade_learnings_db.py:316-475 (build_from_engines), 378-446 (auto-derive); screener.py:159-168 (load TLDB), 4137-4201 (_apply_tldb_gate) · тип=leakage-circular · conf=0.85
  - фикс: Introduce a point-in-time / walk-forward split: mine patterns only on trades resolved strictly before a cutoff and validate/apply on trades after it; require the delta_pp to hold on a held-out fold before a pattern is promoted to a live prohibited/correction rule. At minimum, log in-sample vs out-of-sample WR for each auto-derived rule and gate promotion on OOS delta surviving.
- **Макро-блэкаут (не входить за 30 мин до FOMC/CPI) молча ОТКРЫВАЕТСЯ при отказе ForexFactory: None трактуется как 'окна нет'**
  - `data-failopen` · /Users/nikitasudian/Desktop/трейдинг/free_data.py:free_data.py:182-198 (next_macro_window) → потребитель telegram_alerts.py:1317-1366 · тип=fail-open-stale · conf=0.84
  - фикс: Сделать состояние трёхзначным: 'in_window' / 'clear' / 'unknown'. next_macro_window должна сигнализировать, что календарь недоступен/пуст (отдельный признак, либо отдельная функция macro_data_available()). В telegram_alerts.py при unknown применять fail-CLOSED для рискованных секций (трактовать как блэкаут или хотя бы помечать алерт '⚠ макро-календарь недоступен, осторожно перед релизами'). Также добавить age-границу: если кэш макро старше, например, 24-48ч — считать данные невалидными, а не молча использовать.
- **_fmt_historical_wr excludes FLAT from denominator and ignores direction → inflated/mis-attributed WR shown live to Claude**
  - `rt-filter` · /Users/nikitasudian/Desktop/трейдинг/claude_realtime_filter.py:434-457, 633 · тип=leakage-circular · conf=0.83
  - фикс: Decide on a consistent denominator: either include FLAT as a non-win in the denominator (sh_t += 1 for FLAT too) so WR reflects P(win | signal), or relabel the metric as 'WR среди решённых (excl FLAT)' and also report the FLAT share. Add a direction filter (row.get('direction') matching the candidate direction, mapping ЛОНГ/ШОРТ↔LONG/SHORT) so the WR is point-in-time relevant to the trade being judged.
- **rs_btc как отношение pair_chg/btc_chg_24h меняет знак при падающем BTC — бонусы '0<rs_btc<2 (clean setup)' молча не срабатывают в режиме BTC-down, а сильно опережающий альт получает отрицательный ratio**
  - `screener-scoring` · /Users/nikitasudian/Desktop/трейдинг/screener.py:2717 (определение); использования 2998-3004, 3138-3144, 3326-3334, 3805-3813, 3910, 2519, 2545 · тип=sign-unit · conf=0.82
  - фикс: Перейти на знак-устойчивую метрику относительной силы: использовать rs_btc_pp = pair_chg − btc_chg_24h (уже вычисляется, стр.2719) как первичный сигнал опережения, либо строить ratio с учётом знака знаменателя (например rs = pair_chg − btc_chg_24h, или sign-aware outperformance), и переоценить пороги бонусов/штрафов на joined-датасете. Если ratio оставляют для совместимости с калибровкой — разделить пути по знаку btc_chg_24h, чтобы '0<rs<2' не путало BTC-down с BTC-up.
- **Тихий fail-open: при отказе любого источника _fetch_symbol_data_parallel подставляет нейтральные дефолты, сигнал выходит с занулёнными OI/CVD/funding-trend фичами**
  - `screener-features` · /Users/nikitasudian/Desktop/трейдинг/screener.py:6148-6163 (defaults+except pass); следствия в score_symbol 2602-2611, 2657-2668, 2776 · тип=fail-open-stale · conf=0.82
  - фикс: Различать 'нет данных' и 'нейтрально': при пустых oi_hist/funding_hist/trades для критичных фич либо возвращать None из score_symbol (skip символа), либо выставлять явный data_quality-флаг и понижать score/грейд. Минимум — не пропускать трап-фильтр OI при отсутствии истории (например, если len(oi_hist)<25 → не выдавать high-confidence сигнал, а не молча oi_change=0).
- **Verdict cache key omits direction → cached GO/TP/SL reused for the OPPOSITE side**
  - `rt-filter` · /Users/nikitasudian/Desktop/трейдинг/claude_realtime_filter.py:788, 840-843, 900-901 · тип=fail-open-stale · conf=0.82
  - фикс: Include direction (and ideally a coarse price bucket) in the cache key: key = (symbol.upper(), setup, (candidate.get('direction') or '').upper()). Apply the same change in _cache_key_to_str/_cache_key_from_str (use a 3-tuple, split on a delimiter that can't appear in the fields). Optionally also re-run the unlock veto on cache-hits, or simply do not cache GO verdicts across direction changes.
- **rs_btc ratio flips sign in down markets — falling underperformer scores as a bullish leader**
  - `xcut-sign-unit` · /Users/nikitasudian/Desktop/трейдинг/screener.py:2716-2719, used 2998-3004, 3139-3144, 3327-3334 · тип=sign-unit · conf=0.82
  - фикс: Only treat rs_btc as bullish outperformance when both legs share sign, e.g. require pair_chg and btc_chg_24h same sign before using the >1.3/0<rs<2 thresholds, or gate the ratio bonuses behind rs_btc_pp > 0 (real positive outperformance). Simplest: replace the ratio-magnitude bonuses entirely with the already-validated rs_btc_pp (percentage-point) signal, which does not flip sign.

### 🔴 LIVE — LOW
- **detect_htf_trend включает живую (формирующуюся) свечу в поиск свинг-пивотов и в SMA-fallback — единственный структурный детектор без обрезки [-1]**
  - `screener-scoring` · /Users/nikitasudian/Desktop/трейдинг/screener.py:869-901 (определение); вызовы 2639-2640 для daily_trend/h4_trend · тип=look-ahead · conf=0.9
  - фикс: Привести к общему контракту: в начале detect_htf_trend отрезать живую свечу — 'highs, lows, closes = highs[:-1], lows[:-1], closes[:-1]' (или принимать уже обрезанные ряды), и SMA-fallback считать на closes[:-1] с price = (markPrice/последний завершённый close), как сделано в detect_phase_hadiukov._swing_dir и calc_ema_structure.
- **detect_htf_trend confirms swing pivots using the still-forming candle as a neighbor**
  - `xcut-sign-unit` · /Users/nikitasudian/Desktop/трейдинг/screener.py:878-893 · тип=look-ahead · conf=0.9
  - фикс: Compute swings on highs[:-1] / lows[:-1] (drop the forming candle) as the other structure detectors do, so the trend label is stable and uses only completed candles.
- **CVD 5m silently widens its window to 'all available trades' when <5 trades in 5 min**
  - `xcut-sign-unit` · /Users/nikitasudian/Desktop/трейдинг/pump_detector.py:855-866 · тип=fail-open-stale · conf=0.86
  - фикс: When fewer than N trades fall in the 5-min window, either skip the CVD gate (treat as unknown / no-bonus) instead of substituting an arbitrarily long window, or scale the threshold / surface the actual window length so a stale value can't satisfy a 5-min condition.
- **detect_oi_divergence: цена берётся по завершённой свече closes[-2], а OI по oi_hist[-1] (текущий незакрытый час) → off-by-one рассинхрон двух рядов**
  - `screener-features` · /Users/nikitasudian/Desktop/трейдинg/screener.py:517-526; вызов 2668; маппинг в positioning_regime 2676-2677 (_POSITIONING_MAP 2285-2290); score 2900-2902 · тип=off-by-one · conf=0.85
  - фикс: Выровнять оба ряда на завершённые данные: oi_end = oi_hist[-2], oi_start = oi_hist[-(lookback+2)] (симметрично цене, пропустив текущий незакрытый OI-снимок). Либо явно использовать closes[-1] и oi_hist[-1] для 'сейчас' в обоих, но не смешивать [-2] для цены с [-1] для OI.
- **ATR_COIL: recent_atr включает текущую НЕЗАКРЫТУЮ свечу highs[-1]/lows[-1] → заниженный TR → ratio смещён вниз, паттерн срабатывает легче**
  - `pump-scoring` · /Users/nikitasudian/Desktop/трейдинг/pump_detector.py:1192-1193 · тип=look-ahead · conf=0.85
  - фикс: Исключить forming-свечу из recent_atr: `recent_atr = _atr_simple(highs[-8:-1], lows[-8:-1], closes[-9:-2])` и одновременно поправить выравнивание close на prev-candle (передавать closes с тем же диапазоном индексов, что highs/lows, чтобы c_[i-1] был именно close свечи перед h_[i]). Согласовать обе ветки на закрытых свечах [-2] и старше.
- **Мягкий bad-hours score-гейт (>=90 в плохие UTC-часы) обходится momentum-алертами**
  - `pump-gates-send` · pump_detector.py:1988 (momentum gate только HARD_BLOCK) против 2046 (bad-hours score-гейт только в scan) · тип=fail-open-stale · conf=0.85
  - фикс: В _check_momentum перед отправкой повторно проверять bad-hours score-гейт по текущему _utc_h и c['score_final'] (или score), симметрично scan loop, чтобы решение о времени бралось на момент отправки.
- **_cached: успешный fetcher, вернувший None, кэшируется на полный TTL и блокирует повторные попытки; при последующем отказе stale-путь недоступен**
  - `data-failopen` · /Users/nikitasudian/Desktop/трейдинг/free_data.py:61-74 (стр.72) · тип=fail-open-stale · conf=0.85
  - фикс: Не кэшировать (или кэшировать с коротким negative-TTL ~ttl/10) пустые результаты: на стр.72 при data is None либо не записывать в кэш, либо ставить укороченный ts/маркер negative-cache. Тогда модуль будет быстрее ретраить при восстановлении источника, а не держать 'нет данных' до истечения полного TTL.
- **_score_priority counts the record being processed against itself (self-inclusion off-by-one), inflating rule priority -> larger live penalty**
  - `calibration-loop` · learning_generator.py:learning_generator.py:240-261 (_score_priority); 405-415 (process_all passes full history including current rca) · тип=off-by-one · conf=0.85
  - фикс: Exclude the record under analysis from its own priority count, e.g. compare on a stable id: n = sum(1 for r in rca_history if r is not rca and r.get('outcome_category')=='LOSS' and (...)) — or match on (symbol, run_ts) inequality. Re-evaluate the n>=5/>=20 thresholds after removing the inflation.
- **choch_conviction даёт −30 к min-score для short_dist на основании БЫЧЬЕГО choch (направленческий конфликт)**
  - `screener-gates` · /Users/nikitasudian/Desktop/трейдинг/screener.py:4063 (определение), 6423-6424 (применение) · тип=sign-unit · conf=0.83
  - фикс: Сделать скидку направленно-осознанной: применять −30 от choch_conviction только к лонговым setup (squeeze/breakout/bos_fvg/swing-long). Для short_dist использовать аналогичный флаг по bear_choch (choch_1h=='bear_choch') или вовсе не давать скидку. Т.е. ввести choch_conviction_short = (choch_1h=='bear_choch') и в гейте выбирать нужный флаг по направлению setup.
- **liq_long_usd/liq_short_usd молча обнуляются при отсутствии liq_stats или символа — фича для s1/s4/s5 тихо зануляется**
  - `screener-scoring` · /Users/nikitasudian/Desktop/трейдинг/screener.py:2796-2799 (использования: s1 2909-2914, s4 3463-3466, s5 3540-3545) · тип=fail-open-stale · conf=0.82
  - фикс: Различать отсутствие данных и нулевые ликвидации: например прокидывать liq_available флаг (liq_stats is not None and symbol in liq_stats). Если данные недоступны — логировать предупреждение и/или не применять liq-зависимые ветки явно (а не через тихий 0.0), чтобы деградация источника была видна и не маскировалась под 'тихий рынок'.
- **TLDB gate is direction-blind: short-loss rules penalize/prohibit LONG candidates at the most bullish regime**
  - `calibration-loop` · trade_learnings_db.py / learning_generator.py / screener.py:trade_learnings_db.py:480-591 (_featurize_result); learning_generator.py:166-182 (_EXCL_TOKENS); screener.py:4137-4201 (_apply_tldb_gate), 6216 · тип=sign-unit · conf=0.82
  - фикс: Add a direction token to _featurize_result (e.g. tokens.add(f"direction:{r.get('direction','?')}")) and emit direction-qualified labels from learning_generator (_EXCL_TOKENS / _RULE), OR have _apply_tldb_gate skip/only-apply direction-specific conditions matching result direction. Crucially: attach the long/short direction to the result dict BEFORE calling _apply_tldb_gate (move the gate after direction resolution, or compute setup_dir into the dict in score_symbol), then gate prohibited/correction matches on direction so a short-derived rule only fires on shorts.
- **_cached отдаёт УСТАРЕВШИЙ кэш как свежий при отказе источника, без потолка возраста и без флага staleness (silent fail-open/stale)**
  - `data-failopen` · /Users/nikitasudian/Desktop/трейдинг/free_data.py:61-74 (ключевая строка 71) · тип=fail-open-stale · conf=0.8
  - фикс: Ввести потолок возраста на stale-путь и пометку: на стр.71 возвращать старый data ТОЛЬКО если (now - entry['ts']) < ttl * STALE_FACTOR (например 3-5x), иначе None. Лучше — возвращать сам entry с метаданными {'data':..., 'stale': True, 'age': ...} (или второе значение), чтобы консьюмеры в screener.py могли понизить вес/пропустить фичу. Минимум — логировать на WARNING фактический возраст отданного stale-значения, а в скоринге трактовать stale-enrichment как отсутствующее (None), а не как валидную фичу.
- **Macro context has no staleness gate — stale macro silently injected into live Claude context on persistent refresh failure**
  - `rt-filter` · /Users/nikitasudian/Desktop/трейдинг/claude_realtime_filter.py:242-245, 222-229, 560-564 · тип=fail-open-stale · conf=0.78
  - фикс: In _get_macro_lines (or in build_context before appending), check freshness: if time.time() - _MACRO_CACHE['ts'] > MACRO_MAX_AGE (e.g. 900s), either omit the MACRO block or prepend an explicit '⚠ MACRO STALE (Nм назад) — игнорируй' line so Claude discounts it. Recording ts without reading it is the bug.

### 🟡 АНАЛИЗ — MEDIUM
- **joined_dataset / shorts_joined включают только сигналы с локальным klines-файлом — двойная фильтрация по survivorship-вселенной**
  - `xcut-pit-survivorship` · /Users/nikitasudian/Desktop/трейдинг/backtests/joined_dataset.py + /Users/nikitasudian/Desktop/трейдинг/backtests/shorts_joined.py:joined_dataset.py:98-102 (if sym not in klcache ... if kl is None: continue); shorts_joined.py:128-132 · тип=survivorship-pit · conf=0.92
  - фикс: Логировать, сколько сигналов отброшено из-за отсутствия klines, и по каким символам; в идеале дотянуть klines для ВСЕХ символов из resolved.csv (включая делистнутые/низколиквидные) либо явно сообщать покрытие как долю и его ликвидностный профиль, чтобы вывод не выдавался за репрезентативный.
- **_fetch_price_at_horizon takes bars[0] from un-reversed Bybit kline (newest-first) → resolved price is ~15-30 min LATER than the requested horizon (off-by-one / forward time-shift)**
  - `reject-tracker` · /Users/nikitasudian/Desktop/трейдинг/reject_tracker.py:134-152 (return bars[0][4] at line 150; window built at 143-144) · тип=off-by-one · conf=0.9
  - фикс: Mirror the rest of the codebase: select the bar whose open timestamp is nearest target_dt instead of bars[0]. e.g. `bars = resp.json()['result']['list']; if not bars: return None; best = min(bars, key=lambda b: abs(int(b[0]) - int(target_dt.timestamp()*1000))); return float(best[4])`. Equivalently `list(reversed(bars))` then pick by min-distance. Also fix the inverted-looking window: bar open_ts at target-15m covers [target-15m,target], so the nearest-by-open bar to target is the one opening at target-15m or target; nearest-by-distance selection handles both.
- **Вся вселенная исторического анализа выбрана по ТЕКУЩЕЙ ликвидности (survivorship + look-ahead universe selection)**
  - `xcut-pit-survivorship` · /Users/nikitasudian/Desktop/трейдинг/pump_analysis/fetch_historical.py + /Users/nikitasudian/Desktop/трейдинг/pump_analysis/symbols_new.json:fetch_historical.py:90-123 (get_top30_symbols); symbols_new.json:1-10 (source/description) · тип=survivorship-pit · conf=0.9
  - фикс: Строить историческую вселенную point-in-time: для каждого месяца брать символы, ликвидные/листингованные НА ТОТ момент (Bybit instruments-info launchTime + помесячные снапшоты turnover, либо включить ВСЕ когда-либо листингованные перпы за период, включая делистнутые). Минимум — зафиксировать в отчётах дисклеймер о survivorship и не повышать USE_VALIDATED_V1/не масштабировать шорты только по этим цифрам; в идеале пересобрать klines по PIT-списку и перепрогнать feature_matrix.
- **_resolve_exit favours TP over stop regardless of touch order → look-ahead R-multiple (4h AND 24h siblings of the known bug)**
  - `outcome-tracker` · /Users/nikitasudian/Desktop/трейдинг/outcome_tracker.py:403-411 (helper); called at 469-472 (4h) and 540-543 (24h) · тип=look-ahead · conf=0.82
  - фикс: Make exit resolution order-aware, reusing the single source of truth. When both hit_tp1 and hit_stop are true, compare time_to_mfe vs time_to_mae (tp-side touch time vs stop-side touch time) and pick whichever happened first; when the order is unknown (timing None) resolve pessimistically to stop, matching outcome_model.outcome. Concretely pass tp_touch_h / stop_touch_h into _resolve_exit and: if hit_tp1 and hit_stop: return (tp1,'tp1') only if tp_touch_h is not None and stop_touch_h is not None and tp_touch_h < stop_touch_h else (stop,'sl'). Then recompute historical resolved.csv via the existing migrate path.

### 🟡 АНАЛИЗ — LOW
- **gate_analytics / resolve_rejects 'signal correct' uses any-epsilon directional move (move>0 / move<0) with NO magnitude or fee threshold → systematically inflates pct_signal_correct and biases verdicts toward 'too_strict'**
  - `reject-tracker` · /Users/nikitasudian/Desktop/трейдинг/reject_tracker.py:243-259 (favorable loop + verdict thresholds); same logic at 191-192 · тип=sign-unit · conf=0.9
  - фикс: Require a magnitude threshold (and ideally TP-vs-stop semantics) before crediting the signal. Minimal: `fav = m > FEE_TP_THRESH if d=='ЛОНГ' else m < -FEE_TP_THRESH` with FEE_TP_THRESH on the order of round-trip fees + a small target (e.g. >= +0.5..1.0%). Better: resolve against whether an ATR-based TP was reached before an ATR-based stop within the horizon (consistent with outcome_tracker's R logic) rather than a single point-return sign. Recalibrate the 35/50/60 verdict cutpoints after changing the base metric.
- **Short SL is displayed with a misleading minus sign (label only)**
  - `xcut-sign-unit` · /Users/nikitasudian/Desktop/трейдинг/pump_detector.py:1702-1710 · тип=sign-unit · conf=0.9
  - фикс: Make the sign of the displayed % match the side: for is_rug print SL as (+{_sl:.1f}%) (price up = stop) and TP as (−{_tp:.1f}%), keeping the price levels unchanged. Or label them as 'stop above / target below' to avoid sign ambiguity.
- **MFE/MAE use whole-window extremes with no ordering — magnitude look-ahead leaking into live context and calibration**
  - `outcome-tracker` · /Users/nikitasudian/Desktop/трейдинг/outcome_tracker.py:452-453, 460-461 (4h); 524-525, 531-532 (24h) · тип=look-ahead · conf=0.85
  - фикс: When a stop touch exists, cap the excursion window at the stop-touch time: compute MFE/MAE only up to min(stop_touch_h, horizon) so excursions that occur after the position would have been stopped are excluded. At minimum, store a stop-truncated mfe variant (mfe_to_stop) for the calibration consumers so the edge analysis isn't fit on post-stop excursion.
- **utc_session feature token uses scan-time wall clock (datetime.utcnow().hour) rather than the candidate's signal time**
  - `calibration-loop` · trade_learnings_db.py:trade_learnings_db.py:581-589 (_featurize_result utc_session) · тип=fail-open-stale · conf=0.85
  - фикс: Featurize utc_session from an explicit timestamp on the result (e.g. result['kline_1h_ts'] or a run_ts), falling back to now only if absent, and switch to datetime.now(timezone.utc) to avoid the deprecated naive utcnow(). Ensure the same timestamp source is used when pattern_engine mines historical sessions so live and historical session tokens align.
- **close_end relies on bars[0] being newest (positional, not timestamp-sorted) — fail-stale risk on ordering change**
  - `outcome-tracker` · /Users/nikitasudian/Desktop/трейдинг/outcome_tracker.py:157-161 · тип=fail-open-stale · conf=0.82
  - фикс: Select close_end by max bar open-timestamp like pump_detector: close_end = float(max(bars, key=lambda b: int(b[0]))[4]); don't depend on positional ordering.
- **Сигналы по символам, которые нельзя резолвить (делист/нет данных), молча не попадают в resolved.csv — survivorship в наборе исходов**
  - `xcut-pit-survivorship` · /Users/nikitasudian/Desktop/трейдинг/outcome_tracker.py:136-175 (_fetch_klines_extremes) + 433/506 (резолв только при price_4h/24h не None) · тип=survivorship-pit · conf=0.82
  - фикс: При невозможности резолва за разумный срок (напр. >48h в pending) помечать исход как unresolved/delisted и писать строку в resolved.csv (с явным флагом, R=NaN или штрафной), чтобы форвард-трекеры могли учитывать выживаемость, а не молча её игнорировать.
- **Momentum burst (5m) и 1H-trend гейты молча fail-open при сбое kline API → алерт уходит на голом дрейфе**
  - `pump-gates-send` · pump_detector.py:1833-1872 (try/except + проверки _burst_pct/_chg_1h) · тип=fail-open-stale · conf=0.8
  - фикс: Сделать гейты fail-closed: если 5m kline не получен (_burst_pct is None) — пропускать кандидат (continue, не алерт), т.к. burst-подтверждение обязательно по дизайну. Для 1H можно оставить мягче, но при невозможности подтвердить burst — не слать. Минимально: заменить `if _burst_pct is not None and _burst_pct < MOMENTUM_BURST_PCT` на `if _burst_pct is None or _burst_pct < MOMENTUM_BURST_PCT: continue`.