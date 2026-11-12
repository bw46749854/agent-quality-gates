# 変更記録

本番コードや品質基盤を変えるたびに、変更記録を1つ作ります。変更記録は**影響分析の記録**であり、
同時に**編集してよい範囲の宣言**でもあります。
手順は `.claude/skills/analyze-impact/SKILL.md` にあります。

## 流れ

```bash
# 1. 作る（台帳から抽出した影響範囲が、あらかじめ書き込まれる）
python3 tools/tracekit.py change new --title "二重キャンセルを拒否する" --kind fix \
    --components COMP-ORDER --requirements REQ-002

# 2. 空欄を埋める → 実装する

# 3. 完了にする（全ゲートを実行し、通ったときだけ done になる。結果は記録に追記される）
python3 tools/tracekit.py change close CHG-001
```

途中でやめるときは `python3 tools/tracekit.py change abandon CHG-001 --reason "…"`。

## 先頭部分（フロントマター）

| 項目 | 意味 |
|---|---|
| `status` | `active`（進行中） / `done`（完了） / `abandoned`（中止） |
| `kind` | `feature`（要件の追加・変更） / `fix`（不具合の修正） / `refactor`（振る舞いを変えない整理） / `chore`（それ以外） |
| `requirements` | 対象の要件。`feature` と `fix` では必須。**内容の固まった要件（`draft` 以外）のファイルを変えたときは、その要件をここに入れる**（入っていないと、コミット前の検査と CI が止める） |
| `components` | **編集してよいコンポーネント。** ここにないコンポーネントの本番コードは、hook とコミット前の検査が止める |
| `quality_base` | **編集してよい品質基盤のパス**（glob 可。例: `[trace/gates.toml, tools/**]`）。品質基盤を変えないなら書かない |
| `approved_by` | `quality_base` の変更を承認した人の名前。`quality_base` を書いたら必須 |

## 品質基盤を変えるとき

品質基盤とは、検査そのものを決めているファイルです。ここを書き換えれば、コードを直さずに検査だけを通せます
（テストのゲートを、何も確かめないコマンドに差し替える、など）。そのため、変更には**人の承認の記録**を求めます。

| 対象 | 例 |
|---|---|
| ゲートの定義、台帳の設定、コンポーネントの台帳 | `trace/gates.toml`、`trace/config.toml`、`trace/components.toml` |
| 検査ツール | `tools/tracekit.py`、`tools/verify.py`、`tools/git-hooks/`、`tools/tests/test_tracekit.py` |
| CI と承認の設定 | `.github/workflows/`、`CODEOWNERS` |
| エージェントの hook・手順・行動規約 | `.claude/settings.json`、`.claude/hooks/`、`.claude/skills/`、`.claude/agents/`、`AGENTS.md`、`CLAUDE.md` |
| プロジェクトで足したもの | `trace/config.toml` の `quality_base`（リンターの設定など） |

```bash
python3 tools/tracekit.py change new --title "型検査を mypy から pyright に替える" --kind chore \
    --quality-base trace/gates.toml --approved-by "山田"
```

- 有効な変更記録の `quality_base` にそのパスがあり、`approved_by` が書かれているときだけ、hook・コミット前の検査・CI を通ります。
- 上の最初の4行は、設定で外せません。
- **導入の途中は、求めません。** ゲートや置き場所を設定し、最初のコンポーネントを宣言している段階だからです。
  導入の途中かどうかは、**変更する前の台帳**（コミット前なら `HEAD`、プルリクエストなら取り込み先との分かれ目）に
  コンポーネントがあるかで決めます。コンポーネントを宣言した台帳を一度コミットすれば、そのあとで手元の台帳を
  空にしても、保護は外れません。
- コンポーネントの台帳も品質基盤です。コンポーネントの追加・分割、依存の追加は、承認を得てから行います。
  新しいコンポーネントは台帳に書くまで `components` に指定できないので、先に `--quality-base trace/components.toml`
  で変更記録を作り、台帳を書いてから、変更記録の `components` に足します。
- 名前を書くだけなので、偽ることはできます。記録は「誰が認めたことになっているか」を差分に残して、
  レビューで見えるようにするためのものです。確実に止めるには、ブランチの保護で、これらのパスに
  コードオーナーのレビューを必須にします（README「チームで使うとき」）。

## 範囲が広がったとき

実装の途中で、範囲の外のコンポーネントも変える必要が分かったら、`components` に足すだけで済ませません。
`python3 tools/tracekit.py impact --components …` をやり直し、影響分析と回帰テスト計画を更新します。
設計の骨格が変わる場合は、設計の工程に戻ります。

## どの変更記録が「有効」か

| 場面 | 有効とみなす変更記録 |
|---|---|
| 編集の時点（hook） | 進行中（`active`）のもの |
| コミット前（`guard --staged`） | 進行中のもの ＋ 同じコミットに含まれる完了済み（`done`）のもの |
| プルリクエスト（`guard --base … --strict`） | そのプルリクエストで追加・更新されたもの（進行中・完了済みのどちらも） |

プルリクエストで条件が厳しくなるのは、閉じ忘れた変更記録が取り込まれたあと、
無関係な変更までその記録の範囲として通ってしまうのを防ぐためです。
1つの変更を複数のプルリクエストに分ける場合は、それぞれのプルリクエストで、変更記録に今回の内容を書き足します。

## 完了の条件

`change close` は、次をすべて満たしたときだけ完了にします。

- 「目的」「変更内容」「台帳に現れない影響の調査」「回帰テスト計画」「不確実な点・残るリスク」が書かれている
- 対象の要件が `implemented`（または `deprecated`）になっている
- `tools/verify.py` の全ゲートが PASS か、承認つきの SKIP（区分・理由・承認した人が書かれ、期限を過ぎていない）である。
  必須（`required = true`）のゲートは、SKIP にできない。
  SKIP は検証していないゲートとして、「検証結果」に理由・承認した人・期限つきで残る

完了にすると、変更記録の末尾に「検証結果」（実施日、検証した時点のコミット、各ゲートの結果）が追記されます。
