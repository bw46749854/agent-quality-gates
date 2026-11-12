"""在庫管理コンポーネントの公開インターフェース。ほかのコンポーネントはここだけを使う。"""
from typing import Protocol

from inventory.stock import release_stock, reserve_stock


class StockStore(Protocol):
    """在庫数の保存先。具体的な保存方法（DBなど）は外から渡す。"""

    def get(self, product_id: str) -> int: ...

    def set(self, product_id: str, quantity: int) -> None: ...


class InventoryService:
    def __init__(self, store: StockStore):
        self._store = store

    def available(self, product_id: str) -> int:
        return self._store.get(product_id)

    def reserve(self, product_id: str, quantity: int) -> None:
        self._store.set(product_id, reserve_stock(self._store.get(product_id), quantity))

    def release(self, product_id: str, quantity: int) -> None:
        self._store.set(product_id, release_stock(self._store.get(product_id), quantity))
