#!/usr/bin/env python3
"""Самопроверка локальной книги и OFI. Запуск: python3 test_dom_book.py
Проверяет ровно те места, где рождаются тихие баги: разрыв u, u==1 после рестарта,
дельты до снапшота, восстановление размера снятой заявки, знак OFI."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dom_collector import Book, ofi_step


def snap(u, b, a, seq=1):
    return {"type": "snapshot", "data": {"u": u, "seq": seq, "b": b, "a": a}}


def delta(u, b, a, seq=1):
    return {"type": "delta", "data": {"u": u, "seq": seq, "b": b, "a": a}}


# 1. дельта ДО снапшота не применяется и книга остаётся dirty
bk = Book()
assert bk.apply(delta(5, [["100", "1"]], [])) == "wait"
assert bk.dirty and not bk.b, "дельта до снапшота не должна менять книгу"

# 2. снапшот поднимает книгу
assert bk.apply(snap(10, [["100", "2"], ["99", "3"]], [["101", "1"]])) == "ok"
assert not bk.dirty and bk.b == {100.0: 2.0, 99.0: 3.0} and bk.a == {101.0: 1.0}

# 3. нормальная дельта: обновление, вставка, удаление (size=0)
assert bk.apply(delta(11, [["100", "5"], ["98", "7"], ["99", "0"]], [])) == "ok"
assert bk.b == {100.0: 5.0, 98.0: 7.0}, bk.b

# 4. РАЗРЫВ последовательности -> resync, книга сброшена, не склеиваем молча
assert bk.apply(delta(99, [["100", "1"]], [])) == "resync"
assert bk.dirty and not bk.b, "после разрыва книга обязана быть пустой и dirty"

# 5. после разрыва дельты игнорируются, пока не придёт снапшот
assert bk.apply(delta(100, [["100", "1"]], [])) == "wait"
assert bk.apply(snap(200, [["100", "1"]], [["101", "1"]])) == "ok"
assert not bk.dirty

# 6. u==1 = снапшот после рестарта сервиса Bybit, даже если type=delta
bk.u = 500
assert bk.apply({"type": "delta", "data": {"u": 1, "seq": 9, "b": [["50", "4"]], "a": [["51", "2"]]}}) == "ok"
assert bk.b == {50.0: 4.0} and not bk.dirty, "u==1 обязан сбросить книгу"

# 7. pre_sizes снимает размер ДО применения (иначе снятие невосстановимо)
bk = Book()
bk.apply(snap(1000, [["100", "9"]], [["101", "1"]]))
m = delta(1001, [["100", "0"]], [])
pre = bk.pre_sizes(m)
assert pre[("b", 100.0)] == 9.0, "должны знать размер ДО удаления"
bk.apply(m)
assert 100.0 not in bk.b
assert bk.b.get(100.0, 0.0) - pre[("b", 100.0)] == -9.0, "убыль восстанавливается"

# 8. best()
bk = Book()
bk.apply(snap(1, [["100", "2"], ["99", "5"]], [["101", "3"], ["102", "4"]]))
assert bk.best() == (100.0, 2.0, 101.0, 3.0)

# 9. depth(): нотионал по n уровням
db, da = bk.depth(2)
assert abs(db - (100 * 2 + 99 * 5)) < 1e-9 and abs(da - (101 * 3 + 102 * 4)) < 1e-9

# 10. OFI: знак и эквивалентность отмены бида маркет-селлу (Cont-Kukanov-Stoikov)
bk = Book()
bk.prev_bp, bk.prev_bq, bk.prev_ap, bk.prev_aq = 100.0, 10.0, 101.0, 10.0
# бид подрос в цене -> давление вверх, OFI > 0
assert ofi_step(bk, 100.5, 8.0, 101.0, 10.0) > 0
# бид упал в цене -> давление вниз
assert ofi_step(bk, 99.5, 8.0, 101.0, 10.0) < 0
# бид на той же цене, объём срезали с 10 до 4 -> OFI = 4-10 = -6
assert abs(ofi_step(bk, 100.0, 4.0, 101.0, 10.0) - (-6.0)) < 1e-9
# аск на той же цене, объём срезали с 10 до 4 -> зеркально +6
assert abs(ofi_step(bk, 100.0, 10.0, 101.0, 4.0) - 6.0) < 1e-9
# первый апдейт без истории -> 0
b2 = Book()
assert ofi_step(b2, 100.0, 1.0, 101.0, 1.0) == 0.0

# 11. адаптивный порог считается по p99 и не срабатывает на малой выборке
bk = Book()
bk.apply(snap(1, [["100", "1"]], [["101", "1"]]))
assert bk.thr is None, "без выборки порога быть не должно"
bk.last_thr_ts = 0
for i in range(600):
    bk.b[100.0] = 1.0 + (i % 50)
    bk.sample_sizes()
assert bk.thr is not None and bk.thr > 40, f"p99 порог выглядит неправдоподобно: {bk.thr}"

print("OK: все 11 проверок книги/OFI прошли")
