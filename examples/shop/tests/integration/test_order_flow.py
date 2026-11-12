"""注文管理と在庫管理をつないだ結合テスト。

保存先だけをメモリ上の代用品にしている（外部のDBを使わずに、2つのコンポーネントの連携を確かめるため）。
注文管理と在庫管理そのものは本物を使う。
"""
import unittest

from inventory.service import InventoryService
from inventory.stock import InsufficientStock, InvalidQuantity
from order.service import OrderNotCancellable, OrderService


class InMemoryStockStore:
    def __init__(self, stock: dict[str, int]):
        self._stock = dict(stock)

    def get(self, product_id: str) -> int:
        return self._stock[product_id]

    def set(self, product_id: str, quantity: int) -> None:
        self._stock[product_id] = quantity


class OrderFlowTest(unittest.TestCase):
    def setUp(self):
        self.inventory = InventoryService(InMemoryStockStore({"A": 10}))
        self.orders = OrderService(self.inventory)

    def test_placing_order_reduces_stock(self):
        """REQ-001/AC-1"""
        order = self.orders.place("A", 3)
        self.assertEqual(order.status, "placed")
        self.assertEqual(self.inventory.available("A"), 7)

    def test_order_beyond_stock_is_rejected_and_stock_unchanged(self):
        """REQ-001/AC-2"""
        with self.assertRaises(InsufficientStock):
            self.orders.place("A", 11)
        self.assertEqual(self.inventory.available("A"), 10)

    def test_non_positive_quantity_is_rejected_and_stock_unchanged(self):
        """REQ-001/AC-3"""
        with self.assertRaises(InvalidQuantity):
            self.orders.place("A", 0)
        self.assertEqual(self.inventory.available("A"), 10)

    def test_cancelling_returns_stock(self):
        """REQ-002/AC-1"""
        order = self.orders.place("A", 3)
        cancelled = self.orders.cancel(order.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.inventory.available("A"), 10)

    def test_cancelling_twice_is_rejected_and_stock_unchanged(self):
        """REQ-002/AC-2"""
        order = self.orders.place("A", 3)
        self.orders.cancel(order.id)
        with self.assertRaises(OrderNotCancellable):
            self.orders.cancel(order.id)
        self.assertEqual(self.inventory.available("A"), 10)


if __name__ == "__main__":
    unittest.main()
