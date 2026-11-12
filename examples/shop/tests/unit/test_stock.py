"""在庫数の計算の単体テスト。境界値はここで確かめる。"""
import unittest

from inventory.stock import InsufficientStock, InvalidQuantity, reserve_stock


class ReserveStockTest(unittest.TestCase):
    def test_reduces_stock_by_quantity(self):
        """REQ-001/AC-1"""
        self.assertEqual(reserve_stock(10, 3), 7)

    def test_allows_reserving_exactly_all_stock(self):
        """REQ-001/AC-1 境界値: ちょうど在庫数"""
        self.assertEqual(reserve_stock(3, 3), 0)

    def test_rejects_one_more_than_stock(self):
        """REQ-001/AC-2 境界値: 1つ超過"""
        with self.assertRaises(InsufficientStock):
            reserve_stock(2, 3)

    def test_rejects_zero_and_negative_quantity(self):
        """REQ-001/AC-3"""
        for quantity in (0, -1):
            with self.subTest(quantity=quantity), self.assertRaises(InvalidQuantity):
                reserve_stock(10, quantity)


if __name__ == "__main__":
    unittest.main()
