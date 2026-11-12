"""在庫数の計算。保存先に依存しない純粋な規則だけを置く。"""


class InvalidQuantity(ValueError):
    """数量が 0 以下。"""


class InsufficientStock(Exception):
    """在庫が足りない。"""


def reserve_stock(current: int, quantity: int) -> int:
    """引当後の在庫数を返す。引き当てられないときは例外にする。"""
    if quantity <= 0:
        raise InvalidQuantity(f"数量が不正です: {quantity}")
    if current < quantity:
        raise InsufficientStock(f"在庫不足です: 在庫 {current} に対して {quantity}")
    return current - quantity


def release_stock(current: int, quantity: int) -> int:
    """解放後の在庫数を返す。"""
    if quantity <= 0:
        raise InvalidQuantity(f"数量が不正です: {quantity}")
    return current + quantity
