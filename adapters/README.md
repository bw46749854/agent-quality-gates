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
static       cmd = "ruff check ."
types        cmd = "mypy src"
architecture cmd = "lint-imports"            # import-linter。層・独立・禁止の契約を書ける
unit         cmd = "pytest tests/unit -q"
integration  cmd = "pytest tests/integration -q"
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
static       cmd = "npx eslint ."
types        cmd = "npx tsc --noEmit"
architecture cmd = "npx depcruise src"       # dependency-cruiser。禁止する依存を規則として書ける
unit         cmd = "npx vitest run src"
integration  cmd = "npx vitest run tests/integration"
e2e          cmd = "npx playwright test"
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
static       cmd = "golangci-lint run"
types        skip_reason = "コンパイルが型検査を兼ねる（static と unit の中で行われる）"
architecture cmd = "arch-go"                 # または golangci-lint の depguard
unit         cmd = "go test ./internal/... ./cmd/..."
integration  cmd = "go test ./test/integration/..."
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
static       cmd = "./gradlew checkstyleMain spotbugsMain"
types        skip_reason = "コンパイルが型検査を兼ねる"
architecture cmd = "./gradlew archTest"      # ArchUnit（Kotlin なら Konsist）のテストを別タスクにする
unit         cmd = "./gradlew test"
integration  cmd = "./gradlew integrationTest"
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
`architecture` ゲートの `skip_reason` に「tracekit の reference_patterns による検査で確認している」と書きます。

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
