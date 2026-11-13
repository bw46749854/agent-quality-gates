# 言語ごとの設定例

テンプレートの本体（台帳、検査ツール、skill、hook）は言語に依りません。
言語によって変わるのは次の3か所だけで、どれも設定ファイルに書きます。

| 何を決めるか | どこに書くか |
|---|---|
| 本番コードとテストの置き場所 | `trace/config.toml` の `source_roots` と `[tests]` |
| コンポーネントへの参照がコードにどう現れるか | `trace/components.toml` の `reference_patterns` |
| 各ゲートで実行するコマンド | `trace/gates.toml` の `cmd` |

下の道具は、広く使われているものの例です。プロジェクトですでに使っている道具があれば、そちらを設定してください。
コマンドは、**違反があれば 0 以外の終了コードで終わる**ものにします。

`trace/gates.toml` の例は、そのまま貼って使える形で書いてあります（ファイルの `[[gate]]` を、例の内容で置き換える）。
例にないゲート（`e2e`・`contract` など）は、使うなら同じ形で足します。
ゲートを実行しないと決めた場合は、区分（`skip_kind`）・理由（`skip_reason`）・承認した人の名前（`skip_approved_by`）を書きます。
区分は、確かめる対象がないなら `not_applicable`、対象はあるが一時的に外すなら `waiver`（期限 `skip_until` が必須）です。
必ず実行させたいゲートには `required = true` と書きます（理由や承認があっても外せなくなります）。

テストを実行するゲートには `count_pattern` を書きます。コマンドの出力から「実行したテストの件数」を読む正規表現で、
終了コードが 0 でも、件数が 0 か、件数を読めなければ不合格になります（対象のフォルダを間違えて何も実行していない、
という状態を合格にしないため）。括弧があればその中の数を、なければ合う行の数を件数として数えます。
出力の形は道具とその版で変わるので、一度実行して、合うことを確かめてください。

| 道具 | `count_pattern` の例 |
|---|---|
| Python unittest | `'Ran (\d+) tests?'` |
| pytest | `'(\d+) passed'` |
| vitest | `'Tests\s+(\d+) passed'` |
| jest | `'Tests:\s+(?:.*,\s+)?(\d+) passed'` |
| go test（`-v` を付ける） | `'(?m)^--- PASS'` |
| Gradle / Maven | 出力に件数が出ないので、結果ファイル（JUnit XML）を数えるコマンドをゲートに足す |
ここにある例が正しい TOML であることは、検査ツール自身のテストが確かめています。

## Python

```toml
# trace/config.toml
source_roots = ["src"]
[tests]
unit = ["tests/unit/**/test_*.py"]
integration = ["tests/integration/**/test_*.py"]
e2e = ["tests/e2e/**/test_*.py"]
```

```toml
# trace/gates.toml
[[gate]]
id = "static"
name = "静的解析"
cmd = "ruff check ."

[[gate]]
id = "types"
name = "型検査"
cmd = "mypy src"

[[gate]]
id = "architecture"
name = "アーキテクチャテスト"
cmd = "lint-imports"  # import-linter。層・独立・禁止の契約を書ける

[[gate]]
id = "unit"
name = "単体テスト"
cmd = "pytest tests/unit -q"
count_pattern = '(\d+) passed'

[[gate]]
id = "integration"
name = "結合テスト"
cmd = "pytest tests/integration -q"
count_pattern = '(\d+) passed'
```

`reference_patterns = ['^\s*(from|import)\s+myapp\.order\b']`

## TypeScript / JavaScript

```toml
# trace/config.toml
source_roots = ["src"]
[tests]
unit = ["src/**/*.test.ts"]                  # 本番コードの隣に置く場合
integration = ["tests/integration/**/*.test.ts"]
e2e = ["tests/e2e/**/*.spec.ts"]
```

```toml
# trace/gates.toml
[[gate]]
id = "static"
name = "静的解析"
cmd = "npx eslint ."

[[gate]]
id = "types"
name = "型検査"
cmd = "npx tsc --noEmit"

[[gate]]
id = "architecture"
name = "アーキテクチャテスト"
cmd = "npx depcruise src"  # dependency-cruiser。禁止する依存を規則として書ける

[[gate]]
id = "unit"
name = "単体テスト"
cmd = "npx vitest run src"
count_pattern = 'Tests\s+(\d+) passed'

[[gate]]
id = "integration"
name = "結合テスト"
cmd = "npx vitest run tests/integration"
count_pattern = 'Tests\s+(\d+) passed'

[[gate]]
id = "e2e"
name = "E2Eテスト"
cmd = "npx playwright test"
count_pattern = '(\d+) passed'
```

`reference_patterns = ['from\s+"(@app|\.\.?(/\.\.)*)/order[/"]']`

## Go

```toml
# trace/config.toml
source_roots = ["internal", "cmd"]
[tests]
unit = ["internal/**/*_test.go", "cmd/**/*_test.go"]
integration = ["test/integration/**/*_test.go"]
```

```toml
# trace/gates.toml
[[gate]]
id = "static"
name = "静的解析"
cmd = "golangci-lint run"

[[gate]]
id = "types"
name = "型検査"
skip_reason = "コンパイルが型検査を兼ねる（static と unit の中で行われる）"
skip_kind = "not_applicable"  # 確かめる対象がない。一時的に外すだけなら "waiver" と skip_until
skip_approved_by = ""  # 承認した人の名前を書く（空のままでは不合格になる）

[[gate]]
id = "architecture"
name = "アーキテクチャテスト"
cmd = "arch-go"  # または golangci-lint の depguard

[[gate]]
id = "unit"
name = "単体テスト"
cmd = "go test ./internal/... ./cmd/..."

[[gate]]
id = "integration"
name = "結合テスト"
cmd = "go test ./test/integration/..."
```

`reference_patterns = ['"example\.com/app/internal/order[/"]']`

1つのコマンドで複数のレベルのテストを実行する場合は、`levels` で宣言します
（宣言がないと、実行されないレベルとして検査がエラーにします）。

```toml
[[gate]]
id = "unit"
name = "単体テストと結合テスト"
cmd = "go test ./..."
levels = ["unit", "integration"]
```

## Java / Kotlin

```toml
# trace/config.toml
source_roots = ["src/main"]
[tests]
unit = ["src/test/**/*Test.java"]
integration = ["src/integrationTest/**/*IT.java"]
```

```toml
# trace/gates.toml
[[gate]]
id = "static"
name = "静的解析"
cmd = "./gradlew checkstyleMain spotbugsMain"

[[gate]]
id = "types"
name = "型検査"
skip_reason = "コンパイルが型検査を兼ねる"
skip_kind = "not_applicable"  # 確かめる対象がない。一時的に外すだけなら "waiver" と skip_until
skip_approved_by = ""  # 承認した人の名前を書く（空のままでは不合格になる）

[[gate]]
id = "architecture"
name = "アーキテクチャテスト"
cmd = "./gradlew archTest"  # ArchUnit（Kotlin なら Konsist）のテストを別タスクにする

[[gate]]
id = "unit"
name = "単体テスト"
cmd = "./gradlew test"

[[gate]]
id = "integration"
name = "結合テスト"
cmd = "./gradlew integrationTest"
```

`reference_patterns = ['^\s*import\s+com\.example\.order\.']`

## そのほかの言語

| 言語 | 依存ルールの検査に使える道具の例 |
|---|---|
| C# | NetArchTest、ArchUnitNET |
| PHP | deptrac |
| Rust | クレートの分割と公開範囲（`pub(crate)`）、cargo-deny |
| Ruby | packwerk |

専用の道具がない言語でも、`reference_patterns` による簡易な検査は働きます。その場合は、
`architecture` ゲートの `skip_kind` を `not_applicable` に、`skip_reason` に
「tracekit の reference_patterns による検査で確認している」と書き、`skip_approved_by` に、その判断を承認した人の名前を書きます。

## テストの効き目を確かめる道具（任意）

「テストは通るが、実装が間違っていても落ちない」ことを機械で見つけるには、変異テスト
（コードをわざと少し壊して、テストが落ちるかを調べる）が使えます。時間がかかるので、
ゲートを足すなら、夜間の CI や、リスクの高いコンポーネントに絞るのが現実的です。

| 言語 | 道具の例 |
|---|---|
| Python | mutmut |
| TypeScript / JavaScript / C# | Stryker |
| Java / Kotlin | PIT |
| Go | gremlins |

```toml
[[gate]]
id = "mutation"
name = "変異テスト（在庫管理のみ）"
cmd = "…"
```
