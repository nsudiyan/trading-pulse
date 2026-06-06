# Кросс-биржевая валидация объёмного ядра (Bybit vs Binance)

**Покрытие:** 835 сигналов, 44 символов. price_match(Bybit) = 98.1%.

## Переносится ли edge на Bybit (живой венdue бота)?

- Согласие Bybit↔Binance vol_ratio_1h (Spearman): **0.908** (близко к 1 = одно и то же)
- Bybit vol_ratio_1h → MFE24h (Spearman): **0.449**  (Binance был 0.451)
- High-vol (Bybit vscore≥23, n=394) средний MFE24h = **5.96%**  vs нет-объёма (n=246) = 1.71%  vs все = 3.84%

## Вердикт
✅ ПЕРЕНОСИТСЯ — правка live на Bybit оправдана, edge сохраняется.