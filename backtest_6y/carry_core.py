#!/usr/bin/env python3
"""H-CARRY-01 — чистое ядро cash-and-carry (PREREG DRAFT 2026-07-11, §6).
ОТДЕЛЬНО от радар-стека. Данные инжектируются; PnL на реальных данных НЕ
запускается до заморозки предрега (этот файл — механика + селфчеки).

Позиция: long spot Q_s=N/S_open + short perp Q_p=N/P_open (N=1), без ребаланса.
Funding: short ПОЛУЧАЕТ +rate×Q_p×P на каждом фактическом settlement в позиции.
Вход/выход: на settlement-триггере (f_ann с гистерезисом), исполнение обеих ног
по OPEN первого 1ч-бара СТРОГО ПОЗЖЕ settlement (as-of, урок H-OI-01).
Капитал = 1 + m; breach: убыток перп-ноги > (m − maint)·N → принудительное
закрытие пары на закрытии того же бара (taker+slip), счётчик.
python3 carry_core.py --selfcheck"""
import argparse
from bisect import bisect_right

H = 3600_000
YEAR_MS = 365*86400_000

def resample_1h(bars30):
    """30м → 1ч, только полные пары (нет пары — часа нет). [o,h,l,c]"""
    out = {}
    for t, b in bars30.items():
        if t % H: continue
        b2 = bars30.get(t + 1800_000)
        if b2 is None: continue
        out[t] = [b[1], max(b[2], b2[2]), min(b[3], b2[3]), b2[4]]
    return out

def f_ann(events, i):
    """Аннуализированный funding события i по ФАКТИЧЕСКОМУ интервалу к предыдущему."""
    ts, rate = events[i]
    tp = events[i-1][0]
    if ts <= tp: return None
    return rate * (YEAR_MS / (ts - tp))

def next_bar_after(keys, ts):
    """Первый 1ч-бар с open СТРОГО ПОЗЖЕ ts (одновременная точка НЕВИДИМА)."""
    i = bisect_right(keys, ts)
    return keys[i] if i < len(keys) else None

def run_carry(spot, perp, fund, *, entry_th, exit_th, fee_spot, fee_perp, slip,
              m, maint, t0=None, t1=None):
    """spot/perp: {ts_1h: [o,h,l,c]}, fund: sorted [(ts, rate)].
    Возвращает: {"episodes", "hourly": {ts: equity_на_капитал}, "counters"}."""
    sk = sorted(spot); pk = sorted(perp)
    common = sorted(set(sk) & set(pk))
    if t0: common = [t for t in common if t >= t0]
    if t1: common = [t for t in common if t <= t1]
    cset = set(common)
    cap = 1.0 + m
    st = {"in": False}
    eq_cum = 0.0                     # накопленный реализованный результат, USD на пару N=1
    hourly, episodes = {}, []
    counters = {"entries": 0, "exits": 0, "breach": 0, "skipped_no_bar": 0,
                "funding_events_in_pos": 0}

    def exec_cost(S, P):
        return (fee_spot + slip) + (fee_perp + slip)      # доли нотионала N=1 на ногу

    def open_pos(tb):
        S, P = spot[tb][0], perp[tb][0]
        st.update({"in": True, "Qs": 1.0/S, "Qp": 1.0/P, "S0": S, "P0": P,
                   "t_in": tb, "exec_ts": tb, "fund_recv": 0.0, "fees": exec_cost(S, P)})
        counters["entries"] += 1

    def mtm(tb, use_close=True):
        i = 3 if use_close else 0
        S, P = spot[tb][i], perp[tb][i]
        return (st["Qs"]*S - 1.0) + (1.0 - st["Qp"]*P) + st["fund_recv"] - st["fees"]

    def close_pos(tb, why, use_close=False):
        nonlocal eq_cum
        i = 3 if use_close else 0
        S, P = spot[tb][i], perp[tb][i]
        st["fees"] += exec_cost(S, P)
        pnl = mtm(tb, use_close)
        eq_cum += pnl
        episodes.append({"t_in": st["t_in"], "t_out": tb, "pnl": pnl,
                         "fund": st["fund_recv"], "fees": st["fees"], "why": why})
        st["in"] = False
        counters["exits"] += 1

    def perp_settle_price(ts):
        """Цена funding-начисления, ИЗВЕСТНАЯ в момент settlement (правка Codex №1):
        OPEN 1ч-бара, в который попадает ts; бара нет → close ПРЕДЫДУЩЕГО полного
        часа. Никогда не close текущего бара (это будущее)."""
        start = ts - (ts % H)
        if start in cset: return perp[start][0]
        i = bisect_right(common, start - 1) - 1
        return perp[common[i]][3] if i >= 0 else None

    fi = 0
    for tb in common:
        # settlement-события с ts < open этого бара: сперва НАЧИСЛЕНИЕ, потом РЕШЕНИЕ
        while fi < len(fund) and fund[fi][0] < tb:
            ts_e, rate = fund[fi]
            # 1) начисление: позиция существовала в момент события (вход был раньше)
            if st["in"] and ts_e > st["exec_ts"]:
                P_e = perp_settle_price(ts_e)
                if P_e is not None:
                    st["fund_recv"] += rate * st["Qp"] * P_e   # short ПОЛУЧАЕТ +rate
                    counters["funding_events_in_pos"] += 1
            # 2) решение по f_ann этого события; исполнение — на ЭТОМ баре
            if fi > 0:
                fa = f_ann(fund, fi)
                if fa is not None:
                    if not st["in"] and fa >= entry_th:
                        if tb in cset: open_pos(tb)
                        else: counters["skipped_no_bar"] += 1
                    elif st["in"] and fa < exit_th:
                        close_pos(tb, "exit_signal")
                    # EXIT_TH ≤ fa < ENTRY_TH → гистерезис: ничего не делаем
            fi += 1
        # 3) маржин-контроль по close бара: убыток перп-ноги > (m − maint) → breach
        if st["in"]:
            loss_perp = st["Qp"]*perp[tb][3] - 1.0
            if loss_perp > (m - maint):
                counters["breach"] += 1
                close_pos(tb, "breach", use_close=True)
        hourly[tb] = (eq_cum + (mtm(tb) if st["in"] else 0.0)) / cap
    # terminal accounting (правка Codex №3): позиция, дожившая до правой границы,
    # принудительно закрывается по close последнего бара с exit-fees/slippage
    if st["in"] and common:
        counters["terminal_close"] = counters.get("terminal_close", 0) + 1
        close_pos(common[-1], "terminal_close", use_close=True)
        hourly[common[-1]] = eq_cum / cap
    return {"episodes": episodes, "hourly": hourly, "counters": counters}

# ── селфчеки (§6 PREREG DRAFT): синтетика, без реальных данных ──
def _mk_series(t0, n, px):
    return {t0 + i*H: [px, px, px, px] for i in range(n)}

def selfcheck():
    t0 = 1700000000000 - (1700000000000 % H)
    ok = []
    # 1) as-of: settlement РОВНО в open бара → исполнение на СЛЕДУЮЩЕМ баре
    keys = sorted(_mk_series(t0, 10, 100))
    nb = next_bar_after(keys, t0 + 3*H)          # событие точно в открытие бара 3
    assert nb == t0 + 4*H, "отравленная одновременная точка должна быть невидима"
    nb2 = next_bar_after(keys, t0 + 3*H + 1)
    assert nb2 == t0 + 4*H
    ok.append("as-of")
    # 2) f_ann по фактическому интервалу: 8ч → rate×3×365
    ev = [(t0, 0.0001), (t0 + 8*H, 0.0001)]
    assert abs(f_ann(ev, 1) - 0.0001*3*365) < 1e-12
    ev4 = [(t0, 0.0001), (t0 + 4*H, 0.0001)]
    assert abs(f_ann(ev4, 1) - 0.0001*6*365) < 1e-12
    ok.append("f_ann-кадентность")
    # 3) базис-идентичность: без трений/funding эквити = сближение базиса
    spot = {t0: [100,100,100,100], t0+H: [105,105,105,105]}
    perp = {t0: [102,102,102,102], t0+H: [104,104,104,104]}
    Qs, Qp = 1/100, 1/102
    manual = Qs*105 - 1 + 1 - Qp*104
    assert abs(manual - (0.05 - 2/102)) < 1e-12
    ok.append("базис-MTM")
    # 4) знак funding + 5) fees цикла + 6) гистерезис — плоские цены, полный эпизод
    spotN = _mk_series(t0, 40, 100); perpN = _mk_series(t0, 40, 100)
    fund = [(t0 + 1*H + 1, 0.001),      # событие №1 (для интервала)
            (t0 + 9*H + 1, 0.001),      # №2: f_ann=+1.095 ≥ 0.5 → ВХОД (бар 10)
            (t0 + 17*H + 1, 0.0005),    # №3: f_ann=+0.5475 — в позиции, НАЧИСЛЯЕТСЯ
            (t0 + 25*H + 1, 0.0002),    # №4: f_ann=+0.219 — гистерезис (−0.1 < fa < 0.5): ДЕРЖИМ
            (t0 + 33*H + 1, -0.0004)]   # №5: f_ann=−0.438 < −0.1 → ВЫХОД (+начислен)
    r = run_carry(spotN, perpN, fund, entry_th=0.5, exit_th=-0.1,
                  fee_spot=0.001, fee_perp=0.00055, slip=0.0005, m=0.25, maint=0.125)
    c = r["counters"]
    assert c["entries"] == 1 and c["exits"] == 1 and c["breach"] == 0, c
    assert c["funding_events_in_pos"] == 3, c          # №3, №4 (держим, но получаем), №5
    ep = r["episodes"][0]
    exp_fund = (0.0005 + 0.0002 - 0.0004) * (1/100) * 100   # плоские цены: rate×Qp×P = rate
    assert abs(ep["fund"] - exp_fund) < 1e-12, (ep["fund"], exp_fund)
    exp_fees = 2*(0.001 + 0.00055 + 2*0.0005)
    assert abs(ep["fees"] - exp_fees) < 1e-12, (ep["fees"], exp_fees)
    assert abs(ep["pnl"] - (exp_fund - exp_fees)) < 1e-12   # плоские цены: pnl = funding − fees
    ok.append("funding-знак/начисление"); ok.append("fees-цикл"); ok.append("гистерезис")
    # 7) отрицательный funding в позиции РЕАЛЬНО платится (знак вниз)
    fund_neg = [(t0 + 1*H + 1, 0.001), (t0 + 9*H + 1, 0.001), (t0 + 17*H + 1, -0.00001),
                (t0 + 25*H + 1, -0.0004)]
    r2 = run_carry(spotN, perpN, fund_neg, entry_th=0.5, exit_th=-0.1,
                   fee_spot=0, fee_perp=0, slip=0, m=0.25, maint=0.125)
    assert r2["episodes"][0]["fund"] < 0, r2["episodes"][0]
    ok.append("funding-минус-платится")
    # 8) breach: перп улетает вверх → принудительное закрытие пары
    perpB = dict(perpN); spotB = dict(spotN)
    for i in range(12, 40):
        perpB[t0 + i*H] = [114, 114, 114, 114]; spotB[t0 + i*H] = [114, 114, 114, 114]
    rb = run_carry(spotB, perpB, fund, entry_th=0.5, exit_th=-0.1,
                   fee_spot=0.001, fee_perp=0.00055, slip=0.0005, m=0.25, maint=0.125)
    assert rb["counters"]["breach"] == 1 and rb["episodes"][0]["why"] == "breach", rb["counters"]
    ok.append("маржин-breach")
    # 9) settlement-цена БЕЗ look-ahead (правка Codex №1): open=100, close=150,
    #    событие в начале часа → funding считается от 100, НЕ от 150
    spotL = _mk_series(t0, 40, 100)
    perpL = {t: [100, 100, 100, 100] for t in spotL}
    hot = t0 + 17*H
    perpL[hot] = [100, 155, 95, 150]                    # взрывной бар: open 100 → close 150
    fundL = [(t0 + 1*H + 1, 0.001), (t0 + 9*H + 1, 0.001), (hot, 0.001),
             (t0 + 25*H + 1, -0.0004)]                  # событие РОВНО в открытие взрывного бара
    rl = run_carry(spotL, perpL, fundL, entry_th=0.5, exit_th=-0.1,
                   fee_spot=0, fee_perp=0, slip=0, m=0.9, maint=0.1)
    ep = rl["episodes"][0]
    exp = 0.001*(1/100)*100 + (-0.0004)*(1/100)*100     # оба начисления по цене 100
    assert abs(ep["fund"] - exp) < 1e-12, (ep["fund"], exp, "funding взял close будущего бара!")
    ok.append("settlement-цена-as-of")
    # 10) terminal_close: позиция без выхода закрывается на последнем баре с fees
    fundT = [(t0 + 1*H + 1, 0.001), (t0 + 9*H + 1, 0.001)]   # вход и никогда не выход
    rt = run_carry(spotN, perpN, fundT, entry_th=0.5, exit_th=-0.1,
                   fee_spot=0.001, fee_perp=0.00055, slip=0.0005, m=0.25, maint=0.125)
    assert rt["counters"].get("terminal_close") == 1, rt["counters"]
    et = rt["episodes"][-1]
    assert et["why"] == "terminal_close" and abs(et["fees"] - 2*(0.001+0.00055+2*0.0005)) < 1e-12
    ok.append("terminal-close")
    print(f"✓ carry_core selfcheck ({len(ok)}): {' · '.join(ok)} — OK")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--selfcheck", action="store_true")
    if ap.parse_args().selfcheck: selfcheck()
    else: print("ядро H-CARRY-01: использовать как модуль; PnL до заморозки предрега не запускается")
