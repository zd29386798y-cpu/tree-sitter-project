# 将来機能 実装計画

## 目的

本書は、`middleware_rules.json` を本番ソースに合わせて調整したあと、現在の解析基盤を次の段階へ拡張するための実装計画です。

対象ゴール:

- タスク相関図
- データフロー解析
- 業務処理ルート解析

Codex や GitHub Copilot が引き継いで実装できるように、追加するCLI、DBテーブル、処理方針、出力ファイル、受け入れ条件を明確にします。

## 現状の前提

すでに実装済み:

- Cソース再帰走査
- tree-sitter AST解析
- `functions` / `calls` / `includes` / `structs` / `typedefs` / `macros` 抽出
- SQLite保存
- Excel出力
- 関数呼び出しDOT出力
- `suggest-rules`
- `generate-rules-template`
- `inspect-calls`
- `list-rules`
- `middleware`
- `event-route`
- `middleware_calls` / `message_edges` / `data_accesses` 作成

今後の入力となる主要テーブル:

- `message_edges`
- `data_accesses`
- `middleware_calls`
- `files`
- `functions`
- `calls`

## 実装順序

推奨順序:

1. タスク相関図 `task-graph`
2. イベント別タスクチェーン `event-chain`
3. データフロー解析 `data-flow`
4. 業務処理ルート解析 `business-route`
5. HTMLレポート `report`

理由:

- 最初に `message_edges` からタスク間の直接関係を固める。
- 次にイベント起点で複数段のタスクチェーンを追跡する。
- その後、`data_accesses` と結合してデータの読み書きを追う。
- 最後にタスクチェーンとデータフローを統合して業務ルートとして見せる。

## フェーズ3: タスク相関図

### 目的

`message_edges` をもとに、タスク間の送受信関係をグラフ化する。

### 追加CLI

```powershell
python main.py --mode task-graph
python main.py --mode task-graph --event イベントID
```

### 入力

- `message_edges`
- `files`

### 追加DBテーブル

`task_edges`

```text
id
event_id
from_task
to_task
message_id
data_name
caller_function
file_id
line
confidence
edge_reason
```

### 出力

```text
output/task_graph.xlsx
output/task_graph.dot
output/task_graph_<event_id>.xlsx
output/task_graph_<event_id>.dot
```

### 処理方針

1. `message_edges` から `from_task`, `to_task` を取得する。
2. `UNKNOWN` は除外しない。
3. `from_task -> to_task` のエッジを作成する。
4. `event_id` 指定がある場合は対象イベントだけに絞る。
5. 重複エッジは集約し、件数や代表行を保持する。
6. SQLiteに `task_edges` として保存する。
7. ExcelとDOTを出力する。

### DOT仕様

- ノード: タスク名
- エッジ: `from_task -> to_task`
- ラベル: `event_id`, `message_id`, `data_name`, `count`, `confidence`
- `UNKNOWN` もノードとして残す

### 受け入れ条件

- `message_edges` が空でもエラー終了しない。
- 0件の場合も空のExcel/DOTを出力する。
- `--event` 指定時に対象イベントだけ出る。
- `UNKNOWN` を含む行も欠落しない。

## フェーズ4: イベント別タスクチェーン

### 目的

イベントIDを起点に、複数段のタスク連携を追跡する。

### 追加CLI

```powershell
python main.py --mode event-chain --event イベントID
```

### 入力

- `task_edges`
- `message_edges`

### 追加DBテーブル

`event_chains`

```text
id
event_id
depth
route_order
from_task
to_task
message_id
data_name
caller_function
file_path
line
confidence
visited_key
```

### 出力

```text
output/event_chain_<event_id>.xlsx
output/event_chain_<event_id>.dot
```

### 処理方針

1. 指定 `event_id` の `task_edges` を取得する。
2. タスクをノード、送受信をエッジとするグラフを作る。
3. 開始候補は `from_task` が少ない、または入次数が0のタスクとする。
4. BFSを基本にして `depth` を付与する。
5. ループ防止のため `visited_key` を使う。
6. 経路をExcelとDOTへ出力する。

### visited_key案

```text
event_id + from_task + to_task + message_id + data_name
```

### 受け入れ条件

- 循環があっても無限ループしない。
- `depth` と `route_order` が付与される。
- `UNKNOWN` を含むタスクも欠落しない。
- `event_id` が存在しない場合は0件で終了する。

## フェーズ5: データフロー解析

### 目的

タスク間または関数間で、どのデータが読み書きされるかを追う。

### 追加CLI

```powershell
python main.py --mode data-flow
python main.py --mode data-flow --event イベントID
python main.py --mode data-flow --data データ名
```

### 入力

- `data_accesses`
- `message_edges`
- `middleware_calls`
- `files`

### 追加DBテーブル

`data_flow_edges`

```text
id
event_id
from_task
to_task
from_function
to_function
data_name
data_kind
direction
access_type
source_type
file_id
line
confidence
reason
```

### 出力

```text
output/data_flow.xlsx
output/data_flow.dot
output/data_flow_<event_id>.xlsx
output/data_flow_<event_id>.dot
output/data_flow_<data_name>.xlsx
```

### 処理方針

#### MESSAGE由来

`message_edges.data_name` を使う。

```text
from_task SEND data_name
to_task RECEIVE data_name
```

#### FILE由来

`data_accesses` の `file_write` と `file_read` を同じ `data_name` でつなぐ。

```text
TASK_A WRITE FILE_X
TASK_B READ FILE_X
```

#### DB由来

`db_write` と `db_read` を同じ `data_name` でつなぐ。

```text
TASK_A WRITE TABLE_X
TASK_B READ TABLE_X
```

#### SHARED由来

`shared_write` と `shared_read` を同じ `data_name` でつなぐ。

```text
TASK_A WRITE SHARED_DATA_X
TASK_B READ SHARED_DATA_X
```

### 受け入れ条件

- message / file / db / shared の各ソース種別が区別される。
- `--event` 指定で対象イベントに関連するデータだけを出せる。
- `--data` 指定で対象データだけを出せる。
- `UNKNOWN` は消さず、低confidenceとして残す。

## フェーズ6: 業務処理ルート解析

### 目的

イベント、タスク、メッセージ、データアクセスを統合して、業務処理ルートとして表示する。

### 追加CLI

```powershell
python main.py --mode business-route --event イベントID
```

### 入力

- `event_chains`
- `data_flow_edges`
- `message_edges`
- `data_accesses`
- `files`

### 追加DBテーブル

`business_routes`

```text
id
event_id
route_order
depth
node_type
task_name
function_name
data_name
data_kind
action
next_task
file_path
line
confidence
reason
```

### node_type

```text
EVENT
TASK
FUNCTION
MESSAGE
DATA
FILE
DB
SHARED_MEMORY
UNKNOWN
```

### action

```text
START
SEND
RECEIVE
READ
WRITE
CALL
PROCESS
END
```

### 出力

```text
output/business_route_<event_id>.xlsx
output/business_route_<event_id>.dot
output/business_route_<event_id>.html
```

### 処理方針

1. `event_chains` でイベント起点のタスク順序を取得する。
2. 各タスクに関連する `data_flow_edges` を結合する。
3. SEND / RECEIVE / READ / WRITE を時系列風に並べる。
4. `route_order` と `depth` を付与する。
5. Excel、DOT、HTMLで出力する。

### 出力イメージ

```text
EVT_START

[EVENT] EVT_START
  -> [TASK] TASK_A
       [WRITE] FILE_A
       [SEND] MSG_X / DATA_X
  -> [TASK] TASK_B
       [RECEIVE] MSG_X / DATA_X
       [READ] FILE_A
       [WRITE] TABLE_B
  -> [TASK] TASK_C
       [READ] TABLE_B
       [END]
```

### 受け入れ条件

- イベントID単位で1つのルート表が出る。
- 根拠となるファイルパス、行番号、confidenceが残る。
- UNKNOWNがある場合も表示される。
- Excelで現場レビューできる。

## フェーズ7: HTMLレポート

### 目的

Excelだけでなく、レビューしやすいHTMLレポートを出す。

### 追加CLI

```powershell
python main.py --mode report --event イベントID
```

### 出力

```text
output/report_<event_id>.html
```

### 内容

- イベント概要
- タスク相関図
- データフロー
- 業務処理ルート
- ミドルウェア呼び出し一覧
- 根拠ソース
- confidence
- UNKNOWN一覧
- 追加調査が必要な箇所

## 推奨モジュール構成

既存の `analyzer/middleware.py` にすべて追加すると肥大化するため、以下の分割を推奨します。

```text
analyzer/task_graph.py
analyzer/event_chain.py
analyzer/data_flow.py
analyzer/business_route.py
analyzer/report_writer.py
```

`main.py` にはCLI接続だけを追加します。

## 実装時の共通方針

- 既存の `analyze` / `export` / `graph` / `middleware` を壊さない。
- 入力テーブルが空でもエラー終了しない。
- 0件でも空Excel、空DOT、空HTMLを出す。
- `UNKNOWN` を勝手に除外しない。
- 推定値には必ず `confidence` を残す。
- 根拠として `file_id`, `file_path`, `line`, `raw_call_text` を保持する。
- まずExcelで人間が確認できる出力を優先する。
- DOT/HTMLはレビュー用とし、正確性はDBとExcelで担保する。

## 最初にCodex / GitHub Copilotへ依頼する実装

最初はフェーズ3だけを依頼するのが安全です。

### 依頼文例

```text
フェーズ3として task-graph モードを追加してください。

要件:
- python main.py --mode task-graph
- python main.py --mode task-graph --event イベントID
- message_edges から from_task -> to_task の task_edges を作成
- UNKNOWN は除外しない
- event指定がある場合は対象event_idのみ
- SQLiteに task_edges テーブルを作成
- output/task_graph.xlsx と output/task_graph.dot を出力
- event指定時は output/task_graph_<event_id>.xlsx と output/task_graph_<event_id>.dot を出力
- message_edges が空でもエラー終了しない
- py -3.13 -m py_compile で構文確認する
```

## フェーズ別チェックリスト

| フェーズ | CLI | DB | Excel | DOT | HTML | 優先度 |
|---|---|---|---|---|---|---|
| task-graph | `task-graph` | `task_edges` | 必須 | 必須 | 不要 | 高 |
| event-chain | `event-chain` | `event_chains` | 必須 | 必須 | 不要 | 高 |
| data-flow | `data-flow` | `data_flow_edges` | 必須 | 必須 | 不要 | 中 |
| business-route | `business-route` | `business_routes` | 必須 | 任意 | 任意 | 中 |
| report | `report` | 追加なしでも可 | 任意 | 任意 | 必須 | 低 |

## リスクと対策

| リスク | 対策 |
|---|---|
| `middleware_rules.json` の引数位置が誤っている | `inspect-calls` で実例確認し、少数ルールから始める |
| `UNKNOWN` が多い | 除外せずに出力し、ルール改善対象として扱う |
| タスク名の粒度が合わない | タスク名正規化ルールを後続で追加する |
| 同じイベントIDが複数用途で使われる | `message_id` や `data_name` もキーに含める |
| データ名が式やポインタで曖昧 | raw_call_text と confidence を残す |
| DOTが大きくなりすぎる | event指定、data指定、depth制限を追加する |

## 完了判定

将来機能全体の完了条件:

- イベントIDを指定して、タスク間の起動関係が確認できる。
- イベントに関係するデータのread/write候補が確認できる。
- 業務処理ルートとして、イベント、タスク、メッセージ、データアクセスが1つの表で確認できる。
- すべての出力に根拠ファイル、行番号、confidenceが残る。
- UNKNOWNや低confidenceがレビュー対象として見える。

