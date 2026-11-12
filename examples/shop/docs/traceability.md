<!-- 生成物です。`python3 tools/tracekit.py matrix --write` で作り直します。手で編集しないでください。 -->
# トレーサビリティ・マトリクス

## 要件 → コンポーネント → テスト

| 要件 | 状態 | コンポーネント | unit | integration | AC の網羅 |
|---|---|---|---|---|---|
| [REQ-001](requirements/REQ-001.md) 在庫がある商品を注文できる | implemented | COMP-ORDER, COMP-INVENTORY | 1 | 1 | 3/3 |
| [REQ-002](requirements/REQ-002.md) 注文をキャンセルすると在庫が戻る | implemented | COMP-ORDER, COMP-INVENTORY | — | 1 | 2/2 |

数字はその要件を参照しているテストファイルの数。**なし** は、要件が求める検証レベルにテストがないことを示す。

## コンポーネント → 要件（逆引き）

| コンポーネント | 責務 | 依存先 | 依存元 | 実現する要件 | 変更時に影響しうる要件 |
|---|---|---|---|---|---|
| COMP-INVENTORY 在庫管理 | 在庫数の確認・引当・解放を行い、在庫数が負にならないことを保証する | — | COMP-ORDER | REQ-001, REQ-002 | REQ-001, REQ-002 |
| COMP-ORDER 注文管理 | 注文の受付・取消と、注文の状態を管理する | COMP-INVENTORY | — | REQ-001, REQ-002 | REQ-001, REQ-002 |

## 受け入れ条件 → テスト

| 受け入れ条件 | テスト |
|---|---|
| REQ-001/AC-1 | `tests/integration/test_order_flow.py`（integration）<br>`tests/unit/test_stock.py`（unit） |
| REQ-001/AC-2 | `tests/integration/test_order_flow.py`（integration）<br>`tests/unit/test_stock.py`（unit） |
| REQ-001/AC-3 | `tests/integration/test_order_flow.py`（integration）<br>`tests/unit/test_stock.py`（unit） |
| REQ-002/AC-1 | `tests/integration/test_order_flow.py`（integration） |
| REQ-002/AC-2 | `tests/integration/test_order_flow.py`（integration） |
