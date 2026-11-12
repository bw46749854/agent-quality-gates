"""注文管理コンポーネント。在庫の規則は持たず、在庫管理の公開インターフェースに任せる。"""
from dataclasses import dataclass

from inventory.service import InventoryService


class OrderNotCancellable(Exception):
    """キャンセルできない状態の注文。"""


@dataclass
class Order:
    id: int
    product_id: str
    quantity: int
    status: str = "placed"


class OrderService:
    def __init__(self, inventory: InventoryService):
        self._inventory = inventory
        self._orders: dict[int, Order] = {}

    def place(self, product_id: str, quantity: int) -> Order:
        self._inventory.reserve(product_id, quantity)
        order = Order(id=len(self._orders) + 1, product_id=product_id, quantity=quantity)
        self._orders[order.id] = order
        return order

    def cancel(self, order_id: int) -> Order:
        order = self._orders[order_id]
        if order.status != "placed":
            raise OrderNotCancellable(f"注文 {order_id} は {order.status} のためキャンセルできません")
        self._inventory.release(order.product_id, order.quantity)
        order.status = "cancelled"
        return order
