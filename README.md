# tree-sitter C Analysis Map

40万ステップ規模のC言語システムを対象に、tree-sitterで構文解析し、関数、include、関数呼び出し、構造体/typedef/enum/マクロの一覧をSQLite DBへ保存する解析マップ生成ツールです。

フェーズ2では、`calls` テーブルをもとにミドルウェア関数呼び出し、タスク間メッセージ連携、データアクセス解析の土台を作ります。

## セットアップ

```powershell
pip install -r requirements.txt
```

オフライン環境では、事前に用意したwheelhouseを使います。

```powershell
pip install --no-index --find-links offline_packages/wheelhouse -r requirements.txt
```

## 実行コマンド

```powershell
python main.py --mode ast --src samples
python main.py --mode scan --src "C:\Path\To\Source"
python main.py --mode analyze --src "C:\Path\To\Source"
python main.py --mode suggest-rules
python main.py --mode generate-rules-template
python main.py --mode inspect-calls --name APP_SendMsg
python main.py --mode list-rules
python main.py --mode middleware
python main.py --mode event-route --event EVT_START
python main.py --mode export
python main.py --mode graph
```

## フェーズ2の推奨フロー

```powershell
python main.py --mode suggest-rules
python main.py --mode generate-rules-template
python main.py --mode inspect-calls --name APP_SendMsg
copy middleware_rules.template.json middleware_rules.json

# middleware_rules.json を手動編集して enabled=true と arg_index を調整する

python main.py --mode list-rules
python main.py --mode middleware
python main.py --mode event-route --event EVT_START
python main.py --mode export
```

## 出力ファイル

- `output/c_analysis.db`
- `output/functions.xlsx`
- `output/calls.xlsx`
- `output/includes.xlsx`
- `output/macros.xlsx`
- `output/types.xlsx`
- `output/callee_summary.xlsx`
- `output/middleware_rule_candidates.xlsx`
- `middleware_rules.template.json`
- `output/inspect_calls_<関数名>.xlsx`
- `output/active_middleware_rules.xlsx`
- `output/event_route_<event_id>.xlsx`
- `output/middleware_calls.xlsx`
- `output/message_edges.xlsx`
- `output/data_accesses.xlsx`
- `output/call_graph.dot`

## SQLiteテーブル

- `files`: 解析対象ファイルのパス、ファイル名、拡張子、行数、サイズ
- `functions`: 関数定義、開始行、終了行、シグネチャ
- `calls`: 関数呼び出し、呼び出し元関数名、呼び出し先名、行番号、呼び出しテキスト
- `includes`: include文、include名、行番号
- `structs`: struct/union/enumの名前、種別、開始行、終了行
- `typedefs`: typedef名、typedef文、行番号
- `macros`: defineマクロ名、マクロ本文、行番号
- `middleware_calls`: ルールに一致したミドルウェア呼び出し、抽出引数、推定信頼度
- `message_edges`: message_send/message_recvから推定したタスク間メッセージ連携
- `data_accesses`: file/db/sharedアクセス系ルールから推定したデータアクセス

## ルール候補抽出

`suggest-rules` は `calls` テーブルを集計し、呼び出し先関数名と件数を件数降順で標準出力へ表示します。

- `output/callee_summary.xlsx`: `callee_name`, `count`
- `output/middleware_rule_candidates.xlsx`: `suggested_type`, `callee_name`, `count`, `reason_keyword`, `recommended_match_type`, `recommended_rule_name`

`generate-rules-template` は候補Excelから `middleware_rules.template.json` を生成します。すべての候補は初期状態で `"enabled": false` です。確認済みの候補だけを `"enabled": true` に変更してから `middleware` を実行してください。

## 呼び出し実例確認

```powershell
python main.py --mode inspect-calls --name APP_SendMsg
```

指定した関数名に完全一致する `calls.callee_name` を検索し、最大100件を標準出力とExcelへ出力します。`extracted_args` はJSON文字列で、引数が6個を超える場合でも全引数が入ります。

出力列:

- `callee_name`
- `caller_name`
- `path`
- `line`
- `call_text`
- `extracted_args`
- `arg0`
- `arg1`
- `arg2`
- `arg3`
- `arg4`
- `arg5`

## イベント別ルート確認

```powershell
python main.py --mode event-route --event EVT_START
```

指定した `event_id` に完全一致する `message_edges` を検索し、可能な範囲で関連する `data_accesses` も合わせて出力します。`from_task` または `to_task` が `UNKNOWN` でも出力します。該当なしの場合もエラーにせず、0件としてExcelを作成します。

出力ファイル:

- `output/event_route_EVT_START.xlsx`

出力列:

- `route_order`
- `event_id`
- `from_task`
- `to_task`
- `caller_function`
- `data_name`
- `file_path`
- `line`
- `confidence`
- `raw_call_text`

`route_order` はMVPとして `line`, `file_path` の順で採番します。

## ミドルウェアルール

`middleware_rules.json` で以下のカテゴリを設定できます。

- `message_send`
- `message_recv`
- `file_read`
- `file_write`
- `db_read`
- `db_write`
- `shared_read`
- `shared_write`

各ルールは `enabled`、`match_type`、`name` を持ちます。`match_type` は `exact`、`wildcard`、`regex` に対応しています。

```json
{
  "message_send": [
    {
      "enabled": true,
      "match_type": "exact",
      "name": "MW_SendMsg",
      "event_arg_index": 0,
      "message_arg_index": 1,
      "to_task_arg_index": 2,
      "data_arg_index": 3
    }
  ]
}
```

`enabled` が `false` のルールは無視されます。古い `middleware_rules.json` との互換性のため、`enabled` が存在しないルールは有効として扱います。

## 有効ルール確認

```powershell
python main.py --mode list-rules
```

`middleware_rules.json` を読み込み、`enabled=true` または `enabled` 未指定で有効扱いのルールだけを表示します。`enabled=false` のルールは表示しません。ルールファイルが無い場合も警告を出し、空のExcelを出力します。

出力ファイル:

- `output/active_middleware_rules.xlsx`

出力列:

- `middleware_type`
- `match_type`
- `name`
- `event_arg_index`
- `message_arg_index`
- `from_task_arg_index`
- `to_task_arg_index`
- `data_arg_index`
- `confidence`
- `note`

## 除外フォルダ

既定では以下を除外します。

- `.git`
- `build`
- `out`
- `Debug`
- `Release`
- `.vs`

```powershell
python main.py --mode scan --src "C:\Path\To\Source" --exclude .git build out
```

## tree-sitter解析の限界

- マクロ展開はしません
- `#ifdef` 条件分岐は完全評価しません
- 関数ポインタ呼び出しは完全解決しません
- 同名関数やstatic関数の解決は後続課題です

## HTML説明資料

ツール全体の説明資料は以下を参照してください。

`docs/index.html`

## GitHub公開前クリーンアップ

GitHub公開前や他PC移行前に、現場ソース由来の解析結果を削除できます。

```powershell
python main.py --mode clean-project
```

削除対象:

- `output` 配下の `*.db`
- `output` 配下の `*.xlsx`
- `output` 配下の `*.dot`
- `output` 配下の `*.csv`
- プロジェクト配下の `__pycache__`

`samples/sample.c`、`docs/index.html`、ソースコード、README、ルールJSONは削除しません。実行時には削除対象一覧を表示してから削除します。

## GitHub経由の移行手順

GitHub経由で他PCへ移行する場合は、以下を参照してください。

`docs/github_migration.html`

## ミドルウェアルールの公開時注意

実ソースから生成・調整したルールはローカル管理にしてください。

- `middleware_rules.local.json` はGitHubに上げない
- `middleware_rules.template.local.json` はGitHubに上げない
- GitHub公開用の `middleware_rules.json` は空ルールにする
- GitHub公開用の `middleware_rules.template.json` はサンプルのみ、または空にする
- 現場環境では `middleware_rules.example.json` または `middleware_rules.template.json` をコピーして `middleware_rules.json` を作成する
- ルール調整後の `middleware_rules.json` は、必要に応じてローカル退避ファイルとして管理する
