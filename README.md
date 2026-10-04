# 本地数据导出风险审查 API

用 Python / FastAPI 实现的本地 HTTP 服务。输入一张**可乱序、至多 60 个节点**的数据加工图和一个
导出节点，服务沿依赖关系逐列追踪**敏感来源**，对导出节点的每一列给出审查结论：

* **内容影响（content）**：敏感源列的值流入了该列；
* **行影响（row）**：敏感源列影响了结果里“出现哪些行”，该影响会落到结果的**每一列、每一行**。

两类原因只要存在其一，该导出列即判 `blocked: true`，并分别列出源头节点 ID 与列名。

服务还提供 `POST /api/revise`：在审查结论基础上把图修订成一张**可再次提交、复审判安全**的
导出图（只允许在导出末尾追加一个投影节点），详见下文“安全修订”。

此外提供 `POST /api/prune`：按选定的导出列把图**精简**为只保留这些列及其全部依赖祖先的
等价图——删除无关分支，并从选定列反向传播需求、裁剪导出及各投影/连接的无用输出（共享分支
需求取并集）；最终导出只含选定列（沿用原列序），所选列的内容与行影响原因与原图完全一致，
详见下文“按导出列精简”。

还提供 `POST /api/rename`：把一个**源节点的若干列批量改名**，同时让图内所有引用仍指向
原来的列（筛选引用随输入改名并继续透传；投影 `copy`、连接键与输出引用随各自输入改名，投影
输出名与连接别名不变）。不增删任何节点或列，复审阻断结论不变，详见下文“源列批量改名”。

最后提供 `POST /api/compose`：把**上游加工图的导出**接到**下游图的源位置**，生成一张可复审的
合成图——被替换的源**原位**变成一个沿用原源 ID 的一层投影，按源列原序 `copy` 映射的上游导出列；
只追加上游导出及其全部依赖祖先（沿用上游节点原序与列定义，ID 冲突自动改成全图唯一 ID），
替换位置的内容与行影响取自上游，详见下文“拼接上游导出”。

另外提供 `POST /api/preview`：给加工图提供**小样本预演**——按源 ID 提供样例行、按筛选 ID
提供等值条件，服务按图的投影、筛选透传与等值连接定义跑出导出节点的样本二维表；**只有审查通过
（不阻断）时才返回样本数据**，阻断时只返回审查原因，详见下文“小样本预演”。

最后提供 `POST /api/compare`：对照**基线版（baseline）与候选版（candidate）两张完整加工图**，
各自沿用整图校验（任一图无效即整次拒绝，错误码与格式同 `/api/review`），再按导出列名配对，
分别给出内容、行影响两类原因的新增与消失，并标出候选版是否引入新的导出风险（`regression`）；
同一来源从内容影响转为筛选行影响也算新增行影响，即使两版该列都阻断，详见下文“两版导出风险对照”。

此外，加工图可以**登记（register）**为带 ID 的持久化对象，并提供一份全局、带版本号的
**共用敏感列名录（sensitive-column registry）**：名录按**源 ID + 列名**标识，与每张图自身的
`sensitive` 标记**取 OR**；登记图随后随名录变化被**自动重审**，预演则以登记图 ID 引用、
只提交 `rows`/`conditions`，并以**当前审查结论**作为放行依据，详见下文“登记图与共用敏感列名录”。
名录与登记图保存在 **SQLite**（Docker Compose 命名卷），重启后仍然可用。

最后，已登记图的小样本结果可以**封存**为**待放行导出包**：`POST /api/graphs/{id}/packages`
按当前名录与原预演规则跑出样本，**安全才**把 `columns`/`rows` 与**创建代次**存入 SQLite
（只回包 ID 与创建代次；输入错误或阻断不建包）；`POST /api/packages/{id}/release` 按
**当前代次**放行（过期 409、当前图阻断 400 且不放行）；`GET /api/packages/{id}` **领取**
封存数据（不重算）——放行与领取都不得换用另一批数据，名录一变（即使恢复安全）即须重新放行，
详见下文“待放行导出包：封存 / 放行 / 领取”。

已封存的样本还可以按**源列清除**：`POST /api/packages/purge` 接收非空源列列表，按原内容
来源与行影响规则（**不受敏感标记限制**）判断每张登记图的导出是否依赖任一指定列，把依赖包
在 SQLite 中保存的 `rows` 与放行记录清除并**持久标记为已清除**——已清除包的放行、领取入口
均返回 410，名录恢复安全或重启也不能恢复，详见下文“按源列清除已封存样本”。

## 风险传播规则

| 节点 | 行为 | 内容影响 | 行影响 |
|---|---|---|---|
| `source` | 声明互异列名与 `sensitive` 标记 | 敏感列自身的来源 `(节点, 列)` | 初始为空 |
| `projection` | 只保留明确列出的输出列；每列 `copy` 上游列或以 `constant` 常量替换 | `copy` 继承上游列；`constant` **清除**该列内容来源 | 原样保留（投影/替换不能消除行影响） |
| `filter` | 声明条件 `references` 引用的列，保留全部输入列 | 输入列原样保留 | 并入所有被引用列的内容来源；并继承上游行影响 |
| `join` | 等值连接：左右输入、`left_key`/`right_key`、输出列与互异别名 | 每个输出列继承对应侧列的内容来源 | 合并两侧已有行影响，**并并入左右连接键的敏感内容来源** |

要点：筛选引用敏感列、或连接键带敏感来源时，敏感来源影响结果的**每一行**；之后的投影、
常量替换都无法消除；连接时两侧行影响取并集。

### 整次请求拒绝（HTTP 400）

出现以下情况拒绝整次请求，不给出部分审查结果：

* 节点 ID 重复（`DUPLICATE_NODE`）；
* 列名冲突：source 列名、projection 输出列名、filter 引用名、join 输出别名在各自节点内重复（`COLUMN_CONFLICT`）；
* 无效引用：输入节点/被复制列/筛选列/连接键/连接输出列/导出节点不存在（`INVALID_REFERENCE`）；
* 依赖图含环，包括自环（`CYCLE`）。

请求体不符合 JSON Schema（未知字段、同时写 `copy` 与 `constant`、节点数超过 60 等）返回 HTTP 422。

`POST /api/revise` 复用上述全部校验（重复 ID、列名冲突、无效引用、环、超过 60 节点等同样拒绝），
额外把 `allowed_replacements` 的列名重复判为 422，把允许列不存在及各类不可修订情形判为 400。

`POST /api/rename` 同样先校验整张原图（无关分支的错误也拒绝），再执行改名；源节点不存在、
原列缺失、改名后源列名重复或复审不一致判为 400，改名列表为空、原列名重复/为空、新名为空等
schema 问题判为 422。

`POST /api/compare` 的 `baseline` 与 `candidate` 各自是一张完整审查请求，**分别沿用上述全部
整图校验**（重复 ID、列名冲突、无效引用、环、超过 60 节点等）：先校验基线、再校验候选，任一图
无效即以该侧的原错误码与错误格式返回 HTTP 400，不返回任何对照结果；schema 问题（未知字段、
节点超过 60 等）返回 HTTP 422。

## 单条命令启动

> 端口：**8080**
>
> 登记图、共用敏感列名录与待放行导出包保存在容器内 `/data/export-risk.db`（SQLite），该目录
> 挂载在 Compose **命名卷** `export-risk-data` 上，`docker compose down`（不带 `-v`）与容器
> 重启后数据仍在。

```bash
docker compose up --build
```

后台运行：

```bash
docker compose up --build -d
```

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

不用 Docker 时 SQLite 数据库默认创建在工作目录的 `data/export-risk.db`（会自动建目录）；
可用环境变量 `RISK_API_DB` 指定其他路径（如 `RISK_API_DB=/srv/risk.db`），删除该文件即回到
“名录为空、generation 0、无登记图”的初始状态。

## HTTP 调用示例

### 请求

```
POST /api/review
Content-Type: application/json
```

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {
      "id": "users",
      "type": "source",
      "columns": [
        {"name": "uid", "sensitive": false},
        {"name": "email", "sensitive": true},
        {"name": "country", "sensitive": false}
      ]
    },
    {
      "id": "only_cn",
      "type": "filter",
      "input": "users",
      "references": ["country"]
    },
    {
      "id": "export_users",
      "type": "projection",
      "input": "only_cn",
      "columns": [
        {"name": "user_id", "copy": "uid"},
        {"name": "mail", "copy": "email"},
        {"name": "bucket", "constant": "ALL"}
      ]
    }
  ],
  "export": "export_users"
}'
```

也可以直接用仓库里的示例：

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  --data @examples/request.json
```

### 响应

```json
{
  "export": "export_users",
  "blocked": true,
  "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {
      "name": "mail",
      "blocked": true,
      "content_reasons": [{"node_id": "users", "column": "email"}],
      "row_reasons": []
    },
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}
  ]
}
```

### 行影响示例：筛选敏感列后，连常量列也被阻断

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "id_card", "sensitive": true}, {"name": "score"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
    {"id": "e", "type": "projection", "input": "f",
     "columns": [{"name": "score", "copy": "score"}, {"name": "lit", "constant": 1}]}
  ],
  "export": "e"
}'
```

`score`（非敏感复制列）与 `lit`（常量列）的 `row_reasons` 都包含
`{"node_id":"s","column":"id_card"}`，均被阻断。

### 连接示例：敏感连接键影响连接结果每一行

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "orders", "type": "source",
     "columns": [{"name": "uid_enc", "sensitive": true}, {"name": "amount"}]},
    {"id": "users", "type": "source",
     "columns": [{"name": "uid"}, {"name": "name"}]},
    {"id": "j", "type": "join",
     "left_input": "orders", "right_input": "users",
     "left_key": "uid_enc", "right_key": "uid",
     "outputs": [
       {"alias": "amt", "side": "left", "column": "amount"},
       {"alias": "uname", "side": "right", "column": "name"}
     ]}
  ],
  "export": "j"
}'
```

两个输出列本身均不含敏感内容，但都因敏感连接键 `orders.uid_enc` 的行影响而阻断。

## 安全修订：POST /api/revise

把审查结论转成一张**可再次提交的安全导出图**。请求体在审查请求基础上增加
`allowed_replacements`：允许被替换为常量 `null` 的导出列名列表（**必须互异**，可传空数组；
列名非空）。整张图仍按 `/api/review` 的原规则校验，原有错误行为不变。

修订规则：

1. **原图已安全**：原样返回 `nodes`、原 `export`，**不新增节点**，`replaced_columns` 为空。
2. **原图被阻断且可修订**：只允许在导出节点**末尾追加恰好一个 `projection` 节点**，
   * 列名、列顺序、行集合完全保持（输入即原导出节点，不做筛选/连接）；
   * 仅把**带有敏感内容来源且在允许列表内**的列置为常量 `null`；
   * 其余列（含获准但本身干净的列、原常量列）一律按同名 `copy` 复制；
   * **不修改任何原节点**，追加的投影 ID 不会与原节点冲突（冲突时自动换名）。
3. **不可修订**（返回 HTTP 400，**不返回部分改好的图**）：
   * 导出存在**行影响**（投影无法消除行影响）：`UNREVISABLE_ROW_INFLUENCE`；
   * 任一阻断列（带敏感内容来源）**不在允许列表**：`UNREVISABLE_COLUMN_NOT_ALLOWED`；
   * 追加投影后节点数**超过 60**：`UNREVISABLE_NODE_LIMIT`；
   * 允许列表中的列名在导出节点上**不存在**（原图安全时同样检查）：
     `REPLACEMENT_COLUMN_NOT_FOUND`。

允许列表列名重复、为空字符串、字段类型不符等 schema 问题返回 HTTP 422。

成功响应包含完整 `nodes`、更新后的 `export`、实际替换列 `replaced_columns`
（只含真正被置空的列，按列顺序）、以及用原规则对新导出节点**重审**的结果 `review`；
该图可直接再 POST 到 `/api/review` 并判安全。

### 修订并复审（可直接执行）

```bash
# 1) 发起修订（示例文件：敏感列 mail 获准替换；filter 只引用非敏感列 country，无行影响）
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  --data @examples/revise_request.json
```

返回（节选）：末尾追加了 `export_users__safe_export`，`mail` 被置为 `null`，其余列同名复制：

```json
{
  "nodes": [
    {"id": "users", "type": "source", "columns": [
      {"name": "uid", "sensitive": false},
      {"name": "email", "sensitive": true},
      {"name": "country", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users", "references": ["country"]},
    {"id": "export_users", "type": "projection", "input": "only_cn", "columns": [
      {"name": "user_id", "copy": "uid"},
      {"name": "mail", "copy": "email"},
      {"name": "bucket", "constant": "ALL"}]},
    {"id": "export_users__safe_export", "type": "projection",
     "input": "export_users", "columns": [
       {"name": "user_id", "copy": "user_id"},
       {"name": "mail", "constant": null},
       {"name": "bucket", "copy": "bucket"}]}
  ],
  "export": "export_users__safe_export",
  "revised": true,
  "replaced_columns": ["mail"],
  "review": {"export": "export_users__safe_export", "blocked": false, "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "mail", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}]}
}
```

```bash
# 2) 用原审查接口复审返回的图（需要 jq；应得 "blocked": false）
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  --data @examples/revise_request.json \
| jq '{nodes, export}' \
| curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @-
```

无 `jq` 时可分两步保存中间结果：

```bash
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' --data @examples/revise_request.json > /tmp/revised.json
python3 -c "import json; d=json.load(open('/tmp/revised.json')); \
  json.dump({'nodes': d['nodes'], 'export': d['export']}, open('/tmp/safe.json','w'))"
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @/tmp/safe.json
```

行影响不可修订的例子（对敏感列筛选后，允许替换全部列也无法消除行影响）：

```bash
curl -s -i -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "id_card", "sensitive": true}, {"name": "score"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
    {"id": "e", "type": "projection", "input": "f",
     "columns": [{"name": "score", "copy": "score"}, {"name": "lit", "constant": 1}]}
  ],
  "export": "e",
  "allowed_replacements": ["score", "lit"]
}'
# HTTP/1.1 400 ...  {"error":{"code":"UNREVISABLE_ROW_INFLUENCE", ...
#   "row_reasons":[{"node_id":"s","column":"id_card"}]}}
```

## 按导出列精简：POST /api/prune

按**选定的导出列**把图精简为语义等价的最小图。请求体在审查请求基础上增加 `columns`：
**非空、列名非空且互异**的导出列名列表（顺序不影响结果，最终列序一律沿用导出节点的原列序）。
整张图先按 `/api/review` 的原规则校验，原有错误行为不变；随后每个选定列名都必须存在于导出节点。

### 精简规则

1. **节点保留**：保留导出节点及其**全部依赖祖先**，删除从导出不可达的其余节点。
   共享节点满足所有引用它的分支需求（一个节点同时喂给筛选与连接时取需求并集，不复制节点）。
   常量列不要求上游**列**，但其输入**关系**（节点）不能删除。
2. **反向需求传播**（自导出选定列向上游）：
   * **source 不裁剪**，源列与 `sensitive` 标记原样保留；
   * **filter** 保留条件 `references` 引用列，以及其下游仍需读取的列；条件不被修改；
   * **projection / join（含导出节点本身）只保留被需要的输出/别名**：导出投影或连接与普通
     节点一样**就地裁剪**，未选定的输出连同其复制依赖一并释放；
   * 两侧**连接键始终保留**（它们决定行集合），筛选引用列同样始终保留；
   * 某投影/连接虽被保留但已无任何被需要的输出时，保留其**原第一个输出**及其依赖，
     绝不产生空输出的非法节点（导出节点恒有选定列，不会落入此情形）。
3. **尾部投影**：**仅当**裁剪后导出节点**实际仍含未选定列**时，才在其后追加一个 ID 不与已有节点
   冲突的 `projection`（输入即原导出节点），只按**原列序** `copy` 选定列。导出投影/连接已就地
   只剩选定列时**不追加**；筛选只是**透传**其输入列——上游投影/连接已缩掉的未选列不会在筛选
   导出处重新出现，因此筛选导出同样可能无需追加。只有筛选条件 `references`（或源列）把未选列
   保留下来时（source 永不缩列，source 导出选定子集同理），导出才确实仍含未选列、需要追加。
4. **节点上限**：仅在确需尾部投影（总节点数变为 61）时才以 `PRUNE_NODE_LIMIT` 拒绝；
   上游缩列后筛选透传、无需追加的 60 节点图成功返回，拒绝时不返回部分图。
5. **保持不变量**：保留原节点 ID；保留节点在原请求中的相对顺序，以及节点内列/别名的相对顺序；
   不修改 `sensitive` 标记、常量值与筛选条件。
6. **语义等价但不“变安全”**：最终导出只含选定列；每个选定列的 `content_reasons` /
   `row_reasons` 与原图**完全相同**（敏感列精简后依然阻断，行影响不会因删列而消失）。

### 拒绝（HTTP 400，不返回部分图）

* 选定列名在导出节点上不存在（即使图本身安全也检查）：`PRUNE_COLUMN_NOT_FOUND`；
* 裁剪后导出仍实际含有未选定列、且追加尾部投影后节点数**超过 60**：`PRUNE_NODE_LIMIT`
  （可就地缩列、或经筛选透传后已无未选列的 60 节点图不追加、不拒绝）；
* 构造出的精简图对选定列的复审与原图不一致：`PRUNE_REVIEW_MISMATCH`（内部不变量，正常不会出现）；
* 以及 `/api/review` 的全部原图校验（`DUPLICATE_NODE` / `COLUMN_CONFLICT` /
  `INVALID_REFERENCE` / `CYCLE`）——整次请求先校验整张原图，无关分支的错误同样拒绝。

`columns` 为空或缺失、含空字符串或重复列名、未知字段、节点超过 60 等 schema 问题返回 HTTP 422。

成功响应包含：完整 `nodes`（精简后的图）、最终 `export`、按原列序的 `columns`、
`dropped_nodes`（被删除的不可达节点，按原顺序）、`added_projection`（追加节点 ID 或 `null`）、
`pruned`（是否发生任何删除/裁剪/追加），以及用原规则对最终导出**复审**的 `review`。

### 精简并用 POST /api/review 复审（可直接执行）

```bash
# 1) 发起精简（示例文件：只保留 user_id 与 bucket；列序沿用导出节点）
curl -s -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  --data @examples/prune_request.json
```

返回（节选）：导出投影 `export_users` **就地裁剪**，未选定的 `mail` 连同其复制依赖被删除，
**不追加新节点**；最终只含选定列、沿用原列序：

```json
{
  "nodes": [
    {"id": "users", "type": "source", "columns": [
      {"name": "uid", "sensitive": false},
      {"name": "email", "sensitive": true},
      {"name": "country", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users", "references": ["country"]},
    {"id": "export_users", "type": "projection", "input": "only_cn", "columns": [
      {"name": "user_id", "copy": "uid"},
      {"name": "bucket", "constant": "ALL"}]}
  ],
  "export": "export_users",
  "columns": ["user_id", "bucket"],
  "pruned": true,
  "dropped_nodes": [],
  "added_projection": null,
  "review": {"export": "export_users", "blocked": false, "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}]}
}
```

> 是否追加只取决于**裁剪后导出实际暴露的列**：source 永不缩列，source 导出选定子集时必追加；
> filter 只透传输入列——上游投影/连接已就地缩掉的未选列不会经筛选复活，此时筛选导出**不追加**，
> 而当筛选条件引用了未选列（或其上游是 source，未选列无法消除）时才追加；追加节点 ID 形如
> `<导出节点>__export`（冲突时自动换名），`added_projection` 返回其 ID 或 `null`。

筛选导出经上游缩列而无需追加的例子（投影 `p` 就地只剩 `a`，筛选只引用 `a` 并透传，选定 `a`
时 `added_projection` 为 `null`）：

```bash
curl -s -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "a"}, {"name": "b"}]},
    {"id": "p", "type": "projection", "input": "s",
     "columns": [{"name": "a", "copy": "a"}, {"name": "b", "copy": "b"}]},
    {"id": "f", "type": "filter", "input": "p", "references": ["a"]}
  ],
  "export": "f",
  "columns": ["a"]
}'
# export 仍为 "f"，p 就地裁剪为只输出 a，added_projection 为 null
```

对照：筛选条件引用了**未选定**列 `b` 时，`b` 无法在上游消除，导出确实仍含 `b`，此时才追加
尾部投影（`f__export`）：

```bash
curl -s -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "a"}, {"name": "b"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["b"]}
  ],
  "export": "f",
  "columns": ["a"]
}'
# export 为 "f__export"，f 的 references 仍为 ["b"]，added_projection 为 "f__export"
```

```bash
# 2) 用原审查接口 POST /api/review 复审返回的精简图（需要 jq）
curl -s -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  --data @examples/prune_request.json \
| jq '{nodes, export}' \
| curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @-
```

复审也可以直接把精简结果（或手写同构的图）POST 到 `/api/review`，下例即上面“投影就地缩列、
筛选透传”的精简图，复审应得 `blocked: false`、只含选定列：

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "a"}, {"name": "b"}]},
    {"id": "p", "type": "projection", "input": "s",
     "columns": [{"name": "a", "copy": "a"}]},
    {"id": "f", "type": "filter", "input": "p", "references": ["a"]}
  ],
  "export": "f"
}'
```

```json
{
  "export": "f",
  "blocked": false,
  "columns": [
    {"name": "a", "blocked": false, "content_reasons": [], "row_reasons": []}
  ]
}
```

选定列的原因与原图一致：若选定的是敏感列 `mail`，精简后复审仍判阻断（精简不“变安全”）：

```bash
curl -s -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  -d "$(jq '.columns = ["mail"]' examples/prune_request.json)"
# review.columns[0].content_reasons 仍为 [{"node_id":"users","column":"email"}]，blocked 为 true
```

选定列名不存在时返回 400 且不带任何部分图：

```bash
curl -s -i -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  -d "$(jq '.columns = ["ghost"]' examples/prune_request.json)"
# HTTP/1.1 400 ... {"error":{"code":"PRUNE_COLUMN_NOT_FOUND", ...
```

## 源列批量改名：POST /api/rename

把一个源节点的若干源列**批量改名**，并让图内引用仍指向**原来的列**。请求体在审查请求基础上
增加 `source`（源节点 ID）与 `renames`（**非空**改名列表，每项 `{"from": "原名", "to": "新名"}`，
原名、新名均非空）。整张图先按 `/api/review` 的原规则校验，原有错误行为不变。

### 输入约束（同时生效，允许互换）

* `source` 必须存在且为 `source` 节点：否则 `RENAME_SOURCE_NOT_FOUND`（400）；
* 每个 `from` 都按**原图**解析、必须存在于该源节点：否则 `RENAME_COLUMN_NOT_FOUND`（400，
  返回缺失原名列表）；
* `from` 在列表中**互异**（重复为 422）；`to` 非空（空字符串为 422）；
* 所有改名**同时生效**，因此**允许互换**（`a→b`、`b→a`），但最终源列名不得重复——两个原名
  指向同一新名、或新名撞上未改名的同名列，均判 `RENAME_CONFLICT`（400）；
* `from == to` 是无害空操作（不报错，也不计入结果的 `renamed_columns`）。

### 改名传播规则（更新该源的所有下游分支，含非导出分支）

1. **source**：源列定义就地改名，`sensitive` 标记、列顺序不变；
2. **filter**：条件 `references` 随其输入改名，并继续把改名**透传**给自己的输出列；
3. **projection**：每列的 `copy` 随其输入改名；**投影输出名不变**，`constant` 常量值不动——
   名称变化在此**停止**，下游只看到投影输出名；
4. **join**：对应侧的 `left_key`/`right_key` 与输出 `column` 随各自输入改名；
   **连接输出别名 alias 不变**——名称变化同样在此停止；
5. **不动的内容**：`sensitive` 标记、**其他源节点**的同名列、所有常量值；
6. 节点和列**不增删**；节点 ID、输入关系、节点顺序、节点内列/别名顺序全部不变。

### 复审一致性

响应自带用原规则对改名后图、**原 export** 的复审结果 `review`。要求：

* 阻断结论（整体与逐列 `blocked`）与原图**完全一致**；
* 改名后各导出列的原因集合，按“**新源列名 → 原源列名**”映射回原图后，与原图逐列一致；
  仅属于被改名源节点的原因被映射，其他源的同名列原因保持不变；
* 透传导出（source/filter 导出，或改名尚未在任何投影/连接处停止）的导出列名随改名变化；
  投影输出名、连接别名边界之后的导出列名不变。

复审不一致时以 `RENAME_REVIEW_MISMATCH` 整次拒绝（正常构造不会触发，属内部不变量）。
无效源、缺失原列、改名冲突或复审不一致时均**不返回部分图**。

### 请求与响应（可直接执行）

```bash
# 1) 发起批量改名（示例文件：uid/email/country 同时改名）
curl -s -X POST http://localhost:8080/api/rename \
  -H 'Content-Type: application/json' \
  --data @examples/rename_request.json
```

返回（节选）：源列改名；filter 的 `references` 变为 `["region"]`；导出投影的 `copy` 变为
`user_id`/`mail_addr`，但输出名 `user_id`、`mail`、`bucket` 与常量 `"ALL"` 不变——名称变化
在投影处停止；`mail` 的内容原因随源列改名为 `users.mail_addr`，仍判阻断：

```json
{
  "nodes": [
    {"id": "users", "type": "source", "columns": [
      {"name": "user_id", "sensitive": false},
      {"name": "mail_addr", "sensitive": true},
      {"name": "region", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users",
     "references": ["region"]},
    {"id": "export_users", "type": "projection", "input": "only_cn", "columns": [
      {"name": "user_id", "copy": "user_id"},
      {"name": "mail", "copy": "mail_addr"},
      {"name": "bucket", "constant": "ALL"}]}
  ],
  "export": "export_users",
  "source": "users",
  "renamed_columns": [
    {"from": "uid", "to": "user_id"},
    {"from": "email", "to": "mail_addr"},
    {"from": "country", "to": "region"}],
  "review": {"export": "export_users", "blocked": true, "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "mail", "blocked": true,
     "content_reasons": [{"node_id": "users", "column": "mail_addr"}],
     "row_reasons": []},
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}]}
}
```

```bash
# 2) 用 POST /api/review 复审返回的完整图（需要 jq；应与响应内 review 一致）
curl -s -X POST http://localhost:8080/api/rename \
  -H 'Content-Type: application/json' \
  --data @examples/rename_request.json \
| jq '{nodes, export}' \
| curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @-
```

透传的导出列名会随之变化——对 source 直接改名并以 source 为导出时，复审列名即新源列名：

```bash
curl -s -X POST http://localhost:8080/api/rename \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [{"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b", "sensitive": true}]}],
  "export": "s",
  "source": "s",
  "renames": [{"from": "a", "to": "aa"}, {"from": "b", "to": "bb"}]
}'
# review.columns 的名为 "aa"、"bb"；"bb" 的 content_reasons 为
# [{"node_id":"s","column":"bb"}]，blocked 仍为 true
```

互换两列（同时生效）合法，但两列改到同一新名会冲突：

```bash
curl -s -i -X POST http://localhost:8080/api/rename \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [{"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}]}],
  "export": "s",
  "source": "s",
  "renames": [{"from": "a", "to": "x"}, {"from": "b", "to": "x"}]
}'
# HTTP/1.1 400 ... {"error":{"code":"RENAME_CONFLICT", ... "columns":["x"]}}
```

`renames` 为空/缺失、`from` 重复或为空字符串、`to` 为空、未知字段、节点超过 60 等 schema
问题返回 HTTP 422。

## 拼接上游导出：POST /api/compose

把**上游加工图的导出节点**接到**下游图的某个源节点位置**，合成一张可直接复审的完整图。
请求体包含两份**各自完整**的审查请求（`downstream` 与 `upstream`，均为 `nodes` + `export`，
各自 1–60 个节点）、待替换的下游源节点 ID `source`，以及该源**每一列**到上游导出列的对应表
`mapping`（每项 `{"column": "下游源列", "from": "上游导出列"}`）。两张整图都先按 `/api/review`
的原规则校验，原有错误行为不变。

### 拼接规则

1. **映射约束**：`mapping` 必须**恰好覆盖**被替换源的全部列——无缺漏、无多余项；允许**多列
   引用同一上游列**；`mapping` 的列出顺序不影响结果（结果列序一律沿用源列原序）。每个 `from`
   都必须存在于上游导出节点的实际输出列上（filter 导出的输出列即其透传的输入列）。
2. **原位替换**：被替换的源节点变成一个**一层投影**：沿用**原源 ID**，输入为上游导出节点，
   按**源列原序**逐列 `copy` 映射的上游导出列。下游其余节点一律不动；由于投影暴露的列名与
   原源完全一致，下游**所有分支**（含非导出分支）的引用继续有效。
3. **引入上游**：只追加**上游导出节点及其全部依赖祖先**（按上游请求中的节点原序、列定义
   原样保留）；上游中导出不可达的节点不引入。
4. **ID 冲突**：上游节点 ID 与下游冲突时改成全图唯一 ID（形如 `<原ID>__upstream`，仍冲突时
   追加序号），并同步更新引入节点的全部输入引用；响应的 `id_map` 按上游节点原序列出**全部
   引入节点**的旧新 ID 映射（未改名者 `from` 与 `to` 相同）。
5. **来源语义**：替换位置的**内容来源与行影响完全取自上游**（投影继承上游导出列的内容来源
   与上游关系的行影响）；被替换源的 `sensitive` 标记**不保留**——原敏感列改由干净的上游列喂
   数后即不再阻断，反之上游的敏感来源会顺流阻断下游导出列。
6. **不修改输入**：两份输入图不会被修改；响应中的图是全新构造的。
7. **复审**：合成图按原规则重新校验并复审（导出仍为**原下游 export**），`review` 中的原因
   使用**合成图 ID**（即改名后的上游节点 ID）。

### 拒绝（HTTP 400，不返回部分图）

* `source` 不存在或不是源节点：`COMPOSE_SOURCE_NOT_FOUND`；
* 映射未恰好覆盖源的全部列（缺漏列入 `missing`、多余列入 `extra`）：`COMPOSE_MAPPING_MISMATCH`；
* 映射引用的列在上游导出节点上不存在：`COMPOSE_COLUMN_NOT_FOUND`；
* 合成后节点数**超过 60**：`COMPOSE_NODE_LIMIT`；
* 任一整图的原校验错误（`DUPLICATE_NODE` / `COLUMN_CONFLICT` / `INVALID_REFERENCE` / `CYCLE`）
  ——两张图都先整图校验，无关分支的错误同样拒绝。

`mapping` 为空或缺失、`column` 重复或为空字符串、`from` 为空、未知字段、任一图节点数超过
60 等 schema 问题返回 HTTP 422。

成功响应包含：完整 `nodes`（合成图）、原下游 `export`、`source`、全部引入节点的 `id_map`，
以及用原规则对合成图**复审**的 `review`。

### 拼接并用 POST /api/review 复审（可直接执行）

```bash
# 1) 发起拼接（示例文件：把已脱敏的上游导出 safe_users 接到下游 users 源位置）
curl -s -X POST http://localhost:8080/api/compose \
  -H 'Content-Type: application/json' \
  --data @examples/compose_request.json
```

返回（节选）：下游源 `users` 原位变为一层投影（输入为上游导出 `safe_users`，按源列原序
`copy` 映射列）；上游导出及其祖先 `raw_users`、`safe_users` 追加在图尾；被替换源的
`email` 敏感标记不保留——`mail` 改由上游常量列喂数，复审判安全：

```json
{
  "nodes": [
    {"id": "users", "type": "projection", "input": "safe_users", "columns": [
      {"name": "uid", "copy": "uid"},
      {"name": "email", "copy": "mail"},
      {"name": "country", "copy": "country"}]},
    {"id": "only_cn", "type": "filter", "input": "users", "references": ["country"]},
    {"id": "export_users", "type": "projection", "input": "only_cn", "columns": [
      {"name": "user_id", "copy": "uid"},
      {"name": "mail", "copy": "email"},
      {"name": "bucket", "constant": "ALL"}]},
    {"id": "raw_users", "type": "source", "columns": [
      {"name": "uid", "sensitive": false},
      {"name": "email_hash", "sensitive": true},
      {"name": "country", "sensitive": false}]},
    {"id": "safe_users", "type": "projection", "input": "raw_users", "columns": [
      {"name": "uid", "copy": "uid"},
      {"name": "mail", "constant": null},
      {"name": "country", "copy": "country"}]}
  ],
  "export": "export_users",
  "source": "users",
  "id_map": [
    {"from": "raw_users", "to": "raw_users"},
    {"from": "safe_users", "to": "safe_users"}],
  "review": {"export": "export_users", "blocked": false, "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "mail", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}]}
}
```

```bash
# 2) 用原审查接口 POST /api/review 复审返回的合成图（需要 jq；应与响应内 review 一致）
curl -s -X POST http://localhost:8080/api/compose \
  -H 'Content-Type: application/json' \
  --data @examples/compose_request.json \
| jq '{nodes, export}' \
| curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @-
```

上游节点 ID 与下游冲突时自动改名，复审原因使用合成图 ID。下例上游源也叫 `users`，引入后
改名为 `users__upstream`，其敏感列 `mail` 顺流阻断下游导出列，原因指向改名后的节点：

```bash
curl -s -X POST http://localhost:8080/api/compose \
  -H 'Content-Type: application/json' \
  -d '{
  "downstream": {
    "nodes": [
      {"id": "users", "type": "source",
       "columns": [{"name": "uid"}, {"name": "email", "sensitive": true},
                   {"name": "country"}]},
      {"id": "export_users", "type": "projection", "input": "users",
       "columns": [{"name": "mail", "copy": "email"}]}
    ],
    "export": "export_users"
  },
  "upstream": {
    "nodes": [
      {"id": "users", "type": "source",
       "columns": [{"name": "uid"}, {"name": "mail", "sensitive": true},
                   {"name": "country"}]}
    ],
    "export": "users"
  },
  "source": "users",
  "mapping": [
    {"column": "uid", "from": "uid"},
    {"column": "email", "from": "mail"},
    {"column": "country", "from": "country"}
  ]
}'
# id_map 为 [{"from":"users","to":"users__upstream"}]；
# review.columns[0].content_reasons 为 [{"node_id":"users__upstream","column":"mail"}]
```

映射未覆盖全部源列（或含多余项）、映射列不存在、目标非源、合成后超过 60 节点时整次拒绝，
不返回部分图：

```bash
curl -s -i -X POST http://localhost:8080/api/compose \
  -H 'Content-Type: application/json' \
  -d "$(jq '.mapping = [.mapping[0]]' examples/compose_request.json)"
# HTTP/1.1 400 ... {"error":{"code":"COMPOSE_MAPPING_MISMATCH", ...
#   "missing":["email","country"],"extra":[]}}
```

## 小样本预演：POST /api/preview

用调用方自带的小样本数据把图“跑一遍”，预览导出节点的实际行数据。请求体在审查请求基础上
（`nodes` + `export`，仍按 `/api/review` 的原规则校验整张图）增加：

* `rows`：**按源节点 ID 提供的行数组**（必填），形如 `{"源ID": [{列名: 单元格值, ...}, ...]}`；
* `conditions`：**按筛选节点 ID 提供的等值条件**（可省略，省略视为空映射），形如
  `{"筛选ID": {"列名": 常量, ...}}`，同一筛选的多个条件取 **AND**。

### 覆盖与键集合约束（整次拒绝，不返回部分数据）

1. `rows` **恰好覆盖**导出节点及其依赖祖先中的**全部源节点**：缺漏、多余（含为导出不可达分支
   的源提供数据）都以 `PREVIEW_SOURCE_COVERAGE` 拒绝；自连接（同一关系接到连接两侧）只提供一份。
2. `conditions` **恰好覆盖**祖先中的**全部筛选节点**：缺漏或多余以 `PREVIEW_FILTER_COVERAGE`
   拒绝（`extra` 中出现非筛选 ID 同样拒绝）。
3. 每个行对象的键集合**恰好等于该源的全部列名**（不多不少）：`PREVIEW_ROW_KEYS`。
4. 每个条件对象的键集合**等于该筛选的 `references`**（不多不少）：`PREVIEW_CONDITION_KEYS`。

### 值类型与相等语义

* 单元格值、条件值以及预演中遇到的投影常量只允许 **JSON 标量或 `null`**（字符串、布尔、数字、
  `null`）；非标量（对象/数组）以 `PREVIEW_NON_SCALAR` 拒绝。
  **旧接口的常量范围不变**：`/api/review`、`/api/revise`、`/api/prune`、`/api/rename`、
  `/api/compose` 仍接受任意 JSON 常量，该限制仅用于预演。
* 相等按 **JSON 类型和值**判断：数字按数值比较，**`1` 与 `1.0` 相等**；布尔不与数字相等
  （`true != 1`）；字符串 `"1"` 不等于数字 `1`；大小写敏感。
* **整数按原数值精确比较，不设整数范围限制**：超过 `2**53` 的相邻整数不会被误判相等
  （JSON 整数 `9007199254740992` 与 `9007199254740993` 互不匹配），整数与浮点混合时仍按
  数值判定（`9007199254740993` 与浮点 `9007199254740992.0` 不相等，而可被浮点精确表示的
  整数与其浮点值相等）；超过有限浮点范围的巨大整数（如 401 位的 `10**400` 十进制 JSON 整数）
  同样可以作键、作条件值，不会溢出报错。
* 筛选条件中 **`null` 匹配 `null`**；内连接的 **`null` 键不匹配任何键**（包括另一侧的 `null`）。

### 执行与行序

* **投影**：`copy` 复制上游列值，`constant` 对每行填同一常量（允许 `null`），只保留声明的输出列；
* **筛选**：保序保留满足**全部**等值条件（AND）的行，全部输入列**透传**；
* **连接**：按左右键做**等值内连接**，输出别名按声明顺序取值；**重复键保留全部组合**；
  行序为**左行外层、右行内层**的嵌套次序（左行按左输入顺序，命中的右行按右输入顺序）；
* 行序总体由**源输入顺序、筛选保序、连接左行再右行**确定；结果 `columns` 沿用**原导出列序**，
  `rows` 为与之对齐的二维数组；
* 共享分支只物化一次；服务**不修改**输入请求对象，也不改动任何共享分支。

### 行数上限与阻断行为

* **每个源及每个中间结果至多 200 行**：源行超限（`node_id` 指向源）或投影/筛选/连接结果超限
  （`node_id` 指向超限的中间节点）都以 `PREVIEW_ROW_LIMIT` 整次拒绝（附带 `row_count` 与
  `limit`），**不返回部分数据**。
* 图先按原规则审查：导出一旦**阻断**（敏感内容或行影响），返回 HTTP 400
  `PREVIEW_BLOCKED`，响应中**只有审查原因（`review`），不含任何样本数据**（无 `columns`/
  `rows`）。

### 预演调用（可直接执行）

```bash
# 全部源列均非敏感、筛选只引用非敏感列 country：审查通过，返回样本行
curl -s -X POST http://localhost:8080/api/preview \
  -H 'Content-Type: application/json' \
  --data @examples/preview_request.json
```

```json
{
  "export": "export_users",
  "columns": ["user_id", "mail", "bucket"],
  "rows": [
    [1, "a@x.example", "ALL"],
    [3, null, "ALL"]
  ],
  "review": {
    "export": "export_users",
    "blocked": false,
    "columns": [
      {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
      {"name": "mail", "blocked": false, "content_reasons": [], "row_reasons": []},
      {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}
    ]
  }
}
```

示例中 uid 2 因 `country` 为 `"US"` 被筛掉，uid 4 的小写 `"cn"` 与条件 `"CN"` 严格不等同样
被筛掉；uid 3 的 `email` 为 `null`，原样出现在结果里；常量列 `bucket` 每行填 `"ALL"`。

### 敏感筛选拒绝调用（阻断时只回审查原因）

筛选引用敏感列会带来行影响，落到结果每一列——预演拒绝返回任何样本行，只给审查原因：

```bash
curl -s -i -X POST http://localhost:8080/api/preview \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "id_card", "sensitive": true}, {"name": "score"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
    {"id": "e", "type": "projection", "input": "f",
     "columns": [{"name": "score", "copy": "score"}, {"name": "lit", "constant": 1}]}
  ],
  "export": "e",
  "rows": {"s": [{"id_card": "x", "score": 9}]},
  "conditions": {"f": {"id_card": "x"}}
}'
# HTTP/1.1 400 ...
# {"error":{"code":"PREVIEW_BLOCKED", "node_id":"e",
#   "review":{"export":"e","blocked":true,"columns":[
#     {"name":"score","blocked":true,"content_reasons":[],
#      "row_reasons":[{"node_id":"s","column":"id_card"}]},
#     {"name":"lit","blocked":true,"content_reasons":[],
#      "row_reasons":[{"node_id":"s","column":"id_card"}]}]}}}
# 错误体中没有 columns/rows —— 不泄露任何样本数据。
```

### 大整数连接键示例（按原数值精确比较，不溢出）

连接键与筛选常量中的整数始终按**原数值**比较，不改变请求中的 JSON 类型、也不另设整数范围：
超过 `2**53` 的两个相邻整数不会因浮点舍入而误匹配——下例左右两侧各一行、键分别为
`9007199254740992` 与 `9007199254740993`，返回 200 且 `rows` 为空：

```bash
curl -s -X POST http://localhost:8080/api/preview \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "l", "type": "source",
     "columns": [{"name": "k"}, {"name": "v"}]},
    {"id": "r", "type": "source",
     "columns": [{"name": "k"}, {"name": "w"}]},
    {"id": "j", "type": "join",
     "left_input": "l", "right_input": "r",
     "left_key": "k", "right_key": "k",
     "outputs": [
       {"alias": "k", "side": "left", "column": "k"},
       {"alias": "v", "side": "left", "column": "v"},
       {"alias": "w", "side": "right", "column": "w"}]}
  ],
  "export": "j",
  "rows": {
    "l": [{"k": 9007199254740992, "v": "a"}],
    "r": [{"k": 9007199254740993, "w": "b"}]
  }
}'
# {"export":"j","columns":["k","v","w"],"rows":[],"review":{... "blocked":false ...}}
```

超过有限浮点范围的巨大整数同样可用：两侧键都是 401 位十进制 JSON 整数 `10**400` 时
（Python 字面量写作 `10 ** 400`），返回 200 且得到一条匹配，键在响应中仍是同一个整数
（不会变浮点、也不会再报 500/`INTERNAL`）；而该整数与相邻整数或任意有限浮点键都不匹配：

```bash
BIG=$(python3 -c 'print(10 ** 400)')   # 401 位十进制整数
curl -s -X POST http://localhost:8080/api/preview \
  -H 'Content-Type: application/json' \
  -d "{
  \"nodes\": [
    {\"id\": \"l\", \"type\": \"source\",
     \"columns\": [{\"name\": \"k\"}, {\"name\": \"v\"}]},
    {\"id\": \"r\", \"type\": \"source\",
     \"columns\": [{\"name\": \"k\"}, {\"name\": \"w\"}]},
    {\"id\": \"j\", \"type\": \"join\",
     \"left_input\": \"l\", \"right_input\": \"r\",
     \"left_key\": \"k\", \"right_key\": \"k\",
     \"outputs\": [
       {\"alias\": \"k\", \"side\": \"left\", \"column\": \"k\"},
       {\"alias\": \"v\", \"side\": \"left\", \"column\": \"v\"},
       {\"alias\": \"w\", \"side\": \"right\", \"column\": \"w\"}]}
  ],
  \"export\": \"j\",
  \"rows\": {
    \"l\": [{\"k\": $BIG, \"v\": \"a\"}],
    \"r\": [{\"k\": $BIG, \"w\": \"b\"}]
  }
}"
# rows 恰好一条：[<同一个 401 位整数>, "a", "b"]
```

整数与浮点混合时按数值判定：`1` 与 `1.0` 仍相等（保留全部组合），但整数
`9007199254740993` 与浮点 `9007199254740992.0` 不相等；布尔与数字隔离、字符串按类型比较、
`null` 键不匹配、重复键全部组合及左行再右行的行序等既有规则均不变。

其他整次拒绝（HTTP 400，不返回部分数据）：`rows`/`conditions` 覆盖不符
（`PREVIEW_SOURCE_COVERAGE` / `PREVIEW_FILTER_COVERAGE`）、行键不全
（`PREVIEW_ROW_KEYS`）、条件键不等于 `references`（`PREVIEW_CONDITION_KEYS`）、
非标量单元格/条件值/投影常量（`PREVIEW_NON_SCALAR`）、源或中间结果超过 200 行
（`PREVIEW_ROW_LIMIT`），以及全图沿用的原校验错误（`DUPLICATE_NODE` / `COLUMN_CONFLICT` /
`INVALID_REFERENCE` / `CYCLE`）。

`rows` 缺失、结构类型不符（如 `rows` 不是“ID → 行数组”的对象、单元格行不是对象）、未知字段、
节点超过 60 等 schema 问题返回 HTTP 422。

## 两版导出风险对照：POST /api/compare

对照**基线版与候选版**两张完整加工图，找出候选版**新引入**的导出风险。请求体：

```jsonc
{
  "baseline": {"nodes": [ ... ], "export": "..."},   // 与 /api/review 请求体完全同构
  "candidate": {"nodes": [ ... ], "export": "..."}   // 与 /api/review 请求体完全同构
}
```

两版图**分别沿用 `/api/review` 的整图校验**（先基线后候选，无关分支的错误也拒绝）：任一图
无效即以该侧原错误码（`DUPLICATE_NODE` / `COLUMN_CONFLICT` / `INVALID_REFERENCE` / `CYCLE`）
返回 HTTP 400，错误格式与旧接口一致，不给出任何对照结果；schema 问题返回 HTTP 422。
服务**不修改任何一侧输入**，也不生成修订图。

### 配对、列序与原因排序

* **列配对**：按导出**列名**配对，两版同名导出列视为同一列；来源以 **(源节点 ID, 源列名)**
  识别，两版同名来源视为同一来源（即使两版中间加工结构不同）。
* **列序**：共有列与新增列按**候选导出列序**排列，随后按**基线列序**列出候选已删除的列。
* **缺席侧**：某列只存在于一侧时，另一侧 `baseline_blocked` / `candidate_blocked` 为 `null`，
  其原因在对照中视为**空集**。
* **原因**：每列分别给出**内容影响**与**行影响**的新增、消失原因；原因按
  `(node_id, column)` 字典序排序、去重（与审查输出一致）。

### 响应

`baseline` / `candidate` 是两侧各自完整的 `/api/review` 审查结果；`columns` 逐列给出前后
阻断结论与四类原因差异：

```jsonc
{
  "baseline": { ... 完整 review 结果 ... },
  "candidate": { ... 完整 review 结果 ... },
  "regression": true,                 // 候选版是否引入新的导出风险
  "columns": [
    {
      "name": "a",
      "baseline_blocked": true,       // 基线侧无此列时为 null
      "candidate_blocked": true,      // 候选侧无此列时为 null
      "content_added_reasons": [/* 候选新增的内容来源 */],
      "content_removed_reasons": [/* 候选消失的内容来源 */],
      "row_added_reasons": [/* 候选新增的行影响来源 */],
      "row_removed_reasons": [/* 候选消失的行影响来源 */]
    }
  ]
}
```

### 风险回退标记 `regression`

仅在以下情形为 `true`：

1. **新增阻断列**：候选新增的导出列在候选侧 `blocked: true`（新增安全列**不算**回退）；
2. **共有列新增任一类原因**：两版同名列的内容原因或行原因集合出现任何新增。

特别地：**同一来源从内容影响转为行影响**（例如原来复制敏感列、改为对该敏感列筛选后只导出
其他列）时，即使该列前后都阻断，行原因集合新增了该来源，仍判 `regression: true`。
删除阻断列、仅消失原因（风险减弱）都**不算**回退。

### 内容来源转为筛选行影响（可直接执行）

示例文件 `examples/compare_request.json`：基线版导出列 `a` 直接复制敏感列 `s.x`（**内容影响**）；
候选版改为先对 `s.x` 筛选、`a` 复制干净的 `s.y`——`s.x` 不再流入 `a` 的内容，却开始影响结果
的行集合（**行影响**）。该列前后都阻断，但行影响是候选新增，故 `regression` 为 `true`：

```bash
curl -s -X POST http://localhost:8080/api/compare \
  -H 'Content-Type: application/json' \
  --data @examples/compare_request.json
```

```json
{
  "baseline": {"export": "e", "blocked": true, "columns": [
    {"name": "a", "blocked": true,
     "content_reasons": [{"node_id": "s", "column": "x"}], "row_reasons": []}]},
  "candidate": {"export": "e", "blocked": true, "columns": [
    {"name": "a", "blocked": true,
     "content_reasons": [], "row_reasons": [{"node_id": "s", "column": "x"}]}]},
  "regression": true,
  "columns": [
    {"name": "a", "baseline_blocked": true, "candidate_blocked": true,
     "content_added_reasons": [],
     "content_removed_reasons": [{"node_id": "s", "column": "x"}],
     "row_added_reasons": [{"node_id": "s", "column": "x"}],
     "row_removed_reasons": []}
  ]
}
```

删除阻断列、新增安全列都不算回退（下例候选删掉阻断列 `secret`、新增干净列 `tag`，
`regression` 为 `false`，删除列排在候选列之后）：

```bash
curl -s -X POST http://localhost:8080/api/compare \
  -H 'Content-Type: application/json' \
  -d '{
  "baseline": {
    "nodes": [
      {"id": "s", "type": "source",
       "columns": [{"name": "id_card", "sensitive": true}, {"name": "tag"}]},
      {"id": "e", "type": "projection", "input": "s", "columns": [
        {"name": "secret", "copy": "id_card"}, {"name": "tag", "copy": "tag"}]}
    ],
    "export": "e"
  },
  "candidate": {
    "nodes": [
      {"id": "s", "type": "source", "columns": [{"name": "tag"}]},
      {"id": "e", "type": "projection", "input": "s",
       "columns": [{"name": "tag", "copy": "tag"}]}
    ],
    "export": "e"
  }
}'
# columns 顺序为 ["tag", "secret"]；tag 两侧均不阻断，secret 的 candidate_blocked 为 null；
# regression 为 false
```

任一图无效时整次拒绝，沿用旧错误格式：

```bash
curl -s -i -X POST http://localhost:8080/api/compare \
  -H 'Content-Type: application/json' \
  -d '{
  "baseline": {"nodes": [{"id": "s", "type": "source",
                          "columns": [{"name": "x"}]}], "export": "s"},
  "candidate": {"nodes": [{"id": "s", "type": "source",
                           "columns": [{"name": "x"}]},
                          {"id": "e", "type": "projection", "input": "ghost",
                           "columns": [{"name": "a", "copy": "x"}]}],
                 "export": "e"}
}'
# HTTP/1.1 400 ... {"error":{"code":"INVALID_REFERENCE", ...
```

## 登记图与共用敏感列名录

除了每次随请求自带整图的旧接口，服务还支持把加工图**登记**下来，并维护一份全服务共享的
**敏感列名录**。名录与登记图一起做审查：

* **名录条目**：`{"source": "源节点ID", "column": "列名"}`，按 `(源ID, 列名)` 标识；
  条目**互异**（重复为 422），**可以指向尚未登记（任何图都没有）的来源或列**——此时不产生影响；
* **OR 语义**：某源列只要被图内 `sensitive: true` **或**名录命中，即按敏感处理，二者取并集；
* **代次（generation）**：名录初始为空、generation 为 `0`；成功整份替换一次加一；
* **随名录变化**：名录替换成功后，服务**重审全部登记图**，并把**名录、代次和每张图的新结论
  在同一事务里一起保存**；写入中途或提交失败都会**整体回滚**——名录、代次与全部结论保持
  旧值，重开数据库后也不变，故障解除后可用原 `expected_generation` 重试，
  见下文“替换失败：整体回滚与重试”；
* **持久化**：登记图、名录、代次、审查结论与导出包（含放行记录）保存在 SQLite（默认
  `data/export-risk.db`，可用环境变量 `RISK_API_DB` 覆盖）；Docker 部署时数据库位于
  Compose **命名卷**上，容器重启后登记图、名录与导出包仍然可用。

旧接口 `/api/review`、`/api/preview`、`/api/revise`、`/api/prune`、`/api/rename`、
`/api/compose`、`/api/compare` 的行为**完全不变**：它们只按请求图内的 `sensitive` 标记审查，
不读取共用名录。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/graphs` | 登记一张图（请求体与 `/api/review` 完全同构：`nodes` + `export`）。**沿用整图校验**（图无效即以 `/api/review` 的原错误码返回 400，不登记任何内容），按当前名录重审，返回 **201** 与图 `id`、原图、当前 `generation` 与 `review` |
| `GET` | `/api/graphs/{id}` | 读取登记图：返回**原始 `nodes`/`export`**、当前 `generation`（结论所对应的代次）与该代次下的 `review`；未知 ID 返回 **404** `GRAPH_NOT_FOUND` |
| `POST` | `/api/graphs/{id}/preview` | 按图 ID 预演，请求体**只含 `rows`、`conditions`**（形状与规则同 `/api/preview`）。复用全部原预演规则（覆盖、键集合、标量、200 行上限等）；放行以**当前审查结论**为准：阻断时返回 **400** `PREVIEW_BLOCKED`，错误体**不含任何样本数据**；成功返回样本并带当前 `generation`；未知 ID 返回 **404** `GRAPH_NOT_FOUND` |
| `GET` | `/api/sensitive-columns` | 读取当前名录：`{"generation": N, "entries": [...]}`（条目按提交顺序） |
| `PUT` | `/api/sensitive-columns` | **整份替换**名录：`{"entries": [...], "expected_generation": N}`，条目互异、可空（清空）。`expected_generation` 与当前代次不符时返回 **409** `GENERATION_CONFLICT`（带 `expected_generation`/`current_generation`），不做任何修改；成功时代次加一、重审全部登记图，并返回新代次、新名录与每张图的新结论 `reviews`（按图 ID）；写库异常返回 **500** `INTERNAL`，名录、代次与结论全部保持旧值 |

### 并发次序

名录替换与登记、读取、登记图预演、导出包的封存/放行/领取/清除共用同一把进程级锁，按**某个先后
次序整体生效**：预演要么完全发生在替换之前（旧名录 + 旧结论），要么完全发生在替换之后
（新名录 + 新结论），绝不会用“新名录 + 旧结论”或“旧名录 + 新结论”放行；替换完成之后，
任何凭替换前安全结论发起的预演都不能再返回样本数据。导出包同理：封存、放行、领取、清除要么完全
发生在替换之前、要么完全在其后——绝不会按旧代次放行或领取；替换一旦生效，旧放行代次立即
失效，领取须先按新代次重新放行。

### 替换失败：整体回滚与重试

一次替换要么**全部生效**（新名录 + 加一的代次 + 全部登记图的新结论），要么**什么都不改**。
写入中途或提交失败（例如磁盘/命名卷故障）时，服务返回 **500** `INTERNAL`，且不留任何
半成品：名录、代次与每张图的结论保持旧值，重开数据库（或重启容器）后依然如此；失败后的
读取与按 ID 预演都沿用这同一旧状态，故障解除后用**原 `expected_generation`** 重新提交即可：

```bash
# 替换因写库故障失败：500，错误码 INTERNAL（什么都不改）
curl -s -i -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{"entries": [{"source": "users", "column": "email"}],
       "expected_generation": 0}'
# HTTP/1.1 500 ... {"error":{"code":"INTERNAL","message":"..."}}
```

```bash
# 失败后读取：名录与代次仍是旧值（本例仍为空、代次 0）
curl -s http://localhost:8080/api/sensitive-columns
# {"generation":0,"entries":[]}

# 登记图仍是旧代次下的旧结论
curl -s http://localhost:8080/api/graphs/$GID
# {"id":"...","nodes":[...],"export":"export_users",
#  "generation":0,"review":{"blocked":false,...}}

# 按 ID 预演沿用同一旧结论放行/拦截（本例旧结论安全，照常返回样本）
curl -s -X POST http://localhost:8080/api/graphs/$GID/preview \
  -H 'Content-Type: application/json' \
  -d '{
  "rows": {"users": [{"uid": 1, "email": "a@x.example", "country": "CN"}]},
  "conditions": {"only_cn": {"country": "CN"}}
}'
# {"...rows":[[1,"a@x.example","ALL"]],...,"generation":0}
```

```bash
# 故障解除后：用原 expected_generation 重新提交，替换照常成功
curl -s -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{"entries": [{"source": "users", "column": "email"}],
       "expected_generation": 0}'
# {"generation":1,"entries":[{"source":"users","column":"email"}],
#  "reviews":{"<图ID>":{"export":"export_users","blocked":true,...}}}
```

### curl 走一遍：登记 → 标敏感后预演被拒 → 清空后恢复

```bash
# 1) 登记一张图（图内所有源列 sensitive 均为 false；整图仍按原规则校验），
#    响应 201 里带图 id；下例用 python3 把 id 存进 GID（有 jq 可换成 jq -r .id）
GID=$(curl -s -X POST http://localhost:8080/api/graphs \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "users", "type": "source", "columns": [
      {"name": "uid", "sensitive": false},
      {"name": "email", "sensitive": false},
      {"name": "country", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users",
     "references": ["country"]},
    {"id": "export_users", "type": "projection", "input": "only_cn",
     "columns": [
       {"name": "user_id", "copy": "uid"},
       {"name": "mail", "copy": "email"},
       {"name": "bucket", "constant": "ALL"}]}
  ],
  "export": "export_users"
}' | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
echo "registered graph: $GID"
# 响应体：{"id":"...","nodes":[...原图...],"export":"export_users",
#         "generation":0,"review":{"export":"export_users","blocked":false,...}}
```

```bash
# 2) 此时名录为空：预演成功，返回样本行与 generation 0（请求只带 rows/conditions）
curl -s -X POST http://localhost:8080/api/graphs/$GID/preview \
  -H 'Content-Type: application/json' \
  -d '{
  "rows": {"users": [
    {"uid": 1, "email": "a@x.example", "country": "CN"},
    {"uid": 2, "email": "b@x.example", "country": "US"}]},
  "conditions": {"only_cn": {"country": "CN"}}
}'
# {"export":"export_users","columns":["user_id","mail","bucket"],
#  "rows":[[1,"a@x.example","ALL"]],
#  "review":{"...blocked":false,...},"id":"...","generation":0}
```

```bash
# 3) 整份替换名录：把 users.email 标为共用敏感列（期望代次 0）
curl -s -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{
  "entries": [{"source": "users", "column": "email"}],
  "expected_generation": 0
}'
# {"generation":1,"entries":[{"source":"users","column":"email"}],
#  "reviews":{"<图ID>":{"export":"export_users","blocked":true,...}}}
```

```bash
# 4) 再预演：当前结论已阻断 -> 400 PREVIEW_BLOCKED，错误体里没有任何样本数据
curl -s -i -X POST http://localhost:8080/api/graphs/$GID/preview \
  -H 'Content-Type: application/json' \
  -d '{
  "rows": {"users": [{"uid": 1, "email": "a@x.example", "country": "CN"}]},
  "conditions": {"only_cn": {"country": "CN"}}
}'
# HTTP/1.1 400 ...
# {"error":{"code":"PREVIEW_BLOCKED","node_id":"export_users",
#   "review":{"blocked":true,"columns":[... mail 的 content_reasons 为
#   [{"node_id":"users","column":"email"}] ...]}}}  —— 无 columns/rows

# 用过期代次再替换会得到 409，且什么都不改：
curl -s -i -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{"entries": [], "expected_generation": 0}'
# HTTP/1.1 409 ... {"error":{"code":"GENERATION_CONFLICT",
#   "expected_generation":0,"current_generation":1,...}}
```

```bash
# 5) 用当前代次（1）清空名录；代次变为 2，全部登记图重审为安全
curl -s -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{"entries": [], "expected_generation": 1}'
# {"generation":2,"entries":[],"reviews":{"<图ID>":{"blocked":false,...}}}

# 6) 再预演：恢复返回样本数据，响应带新的 generation 2
curl -s -X POST http://localhost:8080/api/graphs/$GID/preview \
  -H 'Content-Type: application/json' \
  -d '{
  "rows": {"users": [{"uid": 1, "email": "a@x.example", "country": "CN"}]},
  "conditions": {"only_cn": {"country": "CN"}}
}'
# {"...rows":[[1,"a@x.example","ALL"]],...,"generation":2}
```

其他行为：

```bash
# 读取登记图：返回原图、当前 generation 与该代次审查结果
curl -s http://localhost:8080/api/graphs/$GID
# {"id":"...","nodes":[...],"export":"export_users","generation":2,"review":{...}}

# 未知图 ID：404
curl -s -i http://localhost:8080/api/graphs/does-not-exist
# HTTP/1.1 404 ... {"error":{"code":"GRAPH_NOT_FOUND",...}}

# 登记时整图仍按原规则校验（无效引用 -> 400，未知字段 -> 422，什么都不登记）
curl -s -i -X POST http://localhost:8080/api/graphs \
  -H 'Content-Type: application/json' \
  -d '{"nodes":[{"id":"p","type":"projection","input":"ghost",
       "columns":[{"name":"a","copy":"a"}]}], "export":"p"}'
# HTTP/1.1 400 ... {"error":{"code":"INVALID_REFERENCE",...}}

# 名录条目互异；重复条目是 schema 错误：422，名录不变
curl -s -i -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{"entries":[{"source":"s","column":"a"},{"source":"s","column":"a"}],
       "expected_generation": 2}'
# HTTP/1.1 422 ...

# 名录可以包含未登记来源：条目照常保存，当前没有任何图受其影响
curl -s -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{"entries":[{"source":"future_source","column":"x"}],
       "expected_generation": 2}'
```

> **旧接口不受名录影响**：对同一张图 POST `/api/review` 或 `/api/preview` 仍只看图内
> `sensitive` 标记——图内全部为 `false` 时，即使名录已标 `users.email`，旧接口仍判安全、
> 照常返回样本。名录只作用于登记图与按图 ID 的预演。

## 待放行导出包：封存 / 放行 / 领取

把已登记图的一次**安全**小样本结果**封存**为带 ID 的**待放行导出包**，之后按**当前名录代次**
**放行**，再凭包 ID **领取**封存的数据。三步共享同一份名录与代次，且**放行和领取不得换用
另一批数据**：领取返回的永远是封存时物化的那张样本表，绝不重算。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/graphs/{id}/packages` | **封存**：请求体与按 ID 预演完全同构（只含 `rows`、`conditions`），沿用**当前名录**与全部原预演规则（覆盖、键集合、标量、200 行上限等）。仅当审查**安全**时把 `columns`、`rows` 与**创建代次**存入 SQLite，返回 **201** 且响应体**只含包 `id` 与创建 `generation`**；输入错误（400 原错误码）或阻断（400 `PREVIEW_BLOCKED`，不含样本）都**不建包**；未知图 ID 返回 **404** `GRAPH_NOT_FOUND` |
| `POST` | `/api/packages/{id}/release` | **放行**：请求体 `{"expected_generation": N}`。`N` 与当前代次不符（过期）返回 **409** `GENERATION_CONFLICT`（带 `expected_generation`/`current_generation`），什么都不记；随后按**当前名录**重审该图，阻断时返回 **400** `RELEASE_BLOCKED`（带 `review`，不含样本）且**不放行**；成功则把**当前代次**保存为**放行代次**，响应体**只含包 `id` 与 `release_generation`**。名录变更后可再次放行（覆盖旧放行代次）；已清除的包返回 **410** `PACKAGE_PURGED`；未知包 ID 返回 **404** `PACKAGE_NOT_FOUND` |
| `GET` | `/api/packages/{id}` | **领取**：**不重算**，直接返回封存的 `columns`/`rows`、图 ID `graph_id`、创建代次 `generation` 与放行代次 `release_generation`。**未放行**或**放行代次不等于当前代次**时返回 **409** `PACKAGE_NOT_RELEASED`（带 `release_generation`/`current_generation`），响应**不含任何样本**；已清除的包返回 **410** `PACKAGE_PURGED`；未知包 ID 返回 **404** `PACKAGE_NOT_FOUND` |
| `POST` | `/api/packages/purge` | **按源列清除**：请求体 `{"columns": [{"source": "...", "column": "..."}, ...]}`（非空、组合互异）。按原内容来源与行影响规则（不受敏感标记限制）判断导出是否依赖任一指定列，清除依赖包保存的 `rows` 与放行记录并持久标记为已清除，返回本次清除的包 ID 列表（按 ID 排序，未匹配为空列表），详见下文“按源列清除已封存样本” |

规则要点：

* **封存即冻结**：包里的 `columns`/`rows` 在封存时物化并入库，之后名录、图结论如何变化都
  不影响包内容；领取只是取出这份冻结数据。
* **代次闸门**：放行把**当前代次**盖在包上；领取只认“放行代次 == 当前代次”。名录每成功
  替换一次代次加一，旧放行立即失效——**即使名录改回安全（恢复），代次也已前进，仍须重新
  放行**才能领取。
* **不得按旧代次放行或领取**：封存、放行、领取与名录替换共用同一把锁，按某个先后次序整体
  生效（见上文“并发次序”）；过期代次的放行请求一律 409，不做任何修改。
* **失败不留痕**：封存写入或放行提交失败（如磁盘/命名卷故障）返回 **500** `INTERNAL`，
  不会留下新包或新放行记录，故障解除后原样重试即可。
* **持久化**：包与放行记录与登记图、名录保存在**同一个 SQLite 数据库**（Compose 命名卷），
  重启后仍在。

### curl 走一遍：封存 → 放行 → 领取 → 名录变更后拒领 → 再放行

```bash
# 0) 先登记一张图（图内所有源列 sensitive 均为 false），记下返回的图 id
GID=$(curl -s -X POST http://localhost:8080/api/graphs \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "users", "type": "source", "columns": [
      {"name": "uid", "sensitive": false},
      {"name": "email", "sensitive": false},
      {"name": "country", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users",
     "references": ["country"]},
    {"id": "export_users", "type": "projection", "input": "only_cn",
     "columns": [
       {"name": "user_id", "copy": "uid"},
       {"name": "mail", "copy": "email"},
       {"name": "bucket", "constant": "ALL"}]}
  ],
  "export": "export_users"
}' | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
echo "registered graph: $GID"
```

```bash
# 1) 封存：只提交 rows/conditions，沿用当前名录与原预演规则；
#    安全才建包，响应 201 且只含包 id 与创建代次（generation 0）
PID=$(curl -s -X POST http://localhost:8080/api/graphs/$GID/packages \
  -H 'Content-Type: application/json' \
  -d '{
  "rows": {"users": [
    {"uid": 1, "email": "a@x.example", "country": "CN"},
    {"uid": 2, "email": "b@x.example", "country": "US"}]},
  "conditions": {"only_cn": {"country": "CN"}}
}' | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
echo "sealed package: $PID"
# 响应体：{"id":"...","generation":0}
```

```bash
# 2) 放行：提交包 id 与 expected_generation（当前代次 0）；
#    成功只回包 id 与放行代次
curl -s -X POST http://localhost:8080/api/packages/$PID/release \
  -H 'Content-Type: application/json' \
  -d '{"expected_generation": 0}'
# {"id":"...","release_generation":0}
```

```bash
# 3) 领取：返回封存结果（不重算）、图 id、创建及放行代次
curl -s http://localhost:8080/api/packages/$PID
# {"id":"...","graph_id":"...","generation":0,"release_generation":0,
#  "columns":["user_id","mail","bucket"],"rows":[[1,"a@x.example","ALL"]]}
```

```bash
# 4) 名录变更：把 users.email 标为共用敏感列（期望代次 0 -> 1）
curl -s -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{"entries": [{"source": "users", "column": "email"}],
       "expected_generation": 0}'
# {"generation":1,...}

# 拒领：放行代次 0 != 当前代次 1 -> 409，响应不含任何样本
curl -s -i http://localhost:8080/api/packages/$PID
# HTTP/1.1 409 ...
# {"error":{"code":"PACKAGE_NOT_RELEASED",
#   "release_generation":0,"current_generation":1,...}}

# 当前图已阻断，此时放行也被拒：400 RELEASE_BLOCKED，且不放行
curl -s -i -X POST http://localhost:8080/api/packages/$PID/release \
  -H 'Content-Type: application/json' \
  -d '{"expected_generation": 1}'
# HTTP/1.1 400 ... {"error":{"code":"RELEASE_BLOCKED","review":{...}}}
```

```bash
# 5) 清空名录（期望代次 1 -> 2）：图恢复安全，但旧放行代次仍过期，
#    领取依旧 409 —— 名录恢复安全也须重新放行
curl -s -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{"entries": [], "expected_generation": 1}'
# {"generation":2,...}

curl -s -i http://localhost:8080/api/packages/$PID
# HTTP/1.1 409 ... {"error":{"code":"PACKAGE_NOT_RELEASED",
#   "release_generation":0,"current_generation":2,...}}

# 6) 再放行（按当前代次 2），随后领取成功，数据仍是封存时的那批
curl -s -X POST http://localhost:8080/api/packages/$PID/release \
  -H 'Content-Type: application/json' \
  -d '{"expected_generation": 2}'
# {"id":"...","release_generation":2}

curl -s http://localhost:8080/api/packages/$PID
# {"id":"...","graph_id":"...","generation":0,"release_generation":2,
#  "columns":["user_id","mail","bucket"],"rows":[[1,"a@x.example","ALL"]]}
```

其他行为：

```bash
# 未放行的包：领取 409，不含样本
PID2=$(curl -s -X POST http://localhost:8080/api/graphs/$GID/packages \
  -H 'Content-Type: application/json' \
  -d '{"rows": {"users": [{"uid": 1, "email": "a@x.example", "country": "CN"}]},
       "conditions": {"only_cn": {"country": "CN"}}}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
curl -s -i http://localhost:8080/api/packages/$PID2
# HTTP/1.1 409 ... {"error":{"code":"PACKAGE_NOT_RELEASED",
#   "release_generation":null,"current_generation":2,...}}

# 过期代次放行：409 GENERATION_CONFLICT，什么都不记
curl -s -i -X POST http://localhost:8080/api/packages/$PID2/release \
  -H 'Content-Type: application/json' \
  -d '{"expected_generation": 0}'
# HTTP/1.1 409 ... {"error":{"code":"GENERATION_CONFLICT",
#   "expected_generation":0,"current_generation":2,...}}

# 未知图 / 未知包：404
curl -s -i -X POST http://localhost:8080/api/graphs/does-not-exist/packages \
  -H 'Content-Type: application/json' -d '{"rows": {}, "conditions": {}}'
# HTTP/1.1 404 ... {"error":{"code":"GRAPH_NOT_FOUND",...}}
curl -s -i http://localhost:8080/api/packages/does-not-exist
# HTTP/1.1 404 ... {"error":{"code":"PACKAGE_NOT_FOUND",...}}

# 名录阻断时封存：400 PREVIEW_BLOCKED，不建包（先标敏感再封存）
curl -s -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{"entries": [{"source": "users", "column": "email"}],
       "expected_generation": 2}'
curl -s -i -X POST http://localhost:8080/api/graphs/$GID/packages \
  -H 'Content-Type: application/json' \
  -d '{"rows": {"users": [{"uid": 1, "email": "a@x.example", "country": "CN"}]},
       "conditions": {"only_cn": {"country": "CN"}}}'
# HTTP/1.1 400 ... {"error":{"code":"PREVIEW_BLOCKED",...}}  —— 无 columns/rows
# 恢复名录（期望代次 3）：
curl -s -X PUT http://localhost:8080/api/sensitive-columns \
  -H 'Content-Type: application/json' \
  -d '{"entries": [], "expected_generation": 3}'
```

## 按源列清除已封存样本：POST /api/packages/purge

按**源列**清除已封存的样本。请求体：

```jsonc
{
  "columns": [
    {"source": "users", "column": "email"}   // 源节点ID + 列名，可多项
  ]
}
```

`columns` 为**非空**列表；每项的 `source`、`column` 均为非空字符串，且 `(source, column)`
组合**互异**。空列表、缺字段、空字符串、重复组合、未知字段等 schema 问题返回 HTTP 422。
来源可以指向任何源与列——未匹配任何已封存样本时返回空列表，不报错、也不改动任何数据。

### 依赖判定：按原内容来源与行影响规则，不受敏感标记限制

一张登记图的导出是否**依赖**某指定列，完全按原内容来源与行影响规则判断；图内
`sensitive` 标记与共用名录**都不参与**这一判定：

* `copy` 把上游列的依赖带入输出列；`constant` 只切断**内容来源**；
* 筛选 `references` 与连接键留下的**行影响**仍算依赖（常量替换无法消除行影响）；
* 只有导出的依赖闭包算数——**非导出分支**里的来源不算。

### 清除规则

* 导出依赖任一指定列时，该图**此次操作生效前已存在**的全部未清除包都被清除：SQLite 中
  保存的 `rows` 与放行记录被删除，包被**持久标记为已清除**；**后续新封存的包不受影响**。
* 图、敏感名录与 generation 一律不变；其他包与旧接口行为不变。
* 整个清除在**同一事务**中提交，任何写入或提交失败都整批不变（500 `INTERNAL`）；清除与
  封存、放行、领取共用同一把锁，按同一先后次序整体生效。
* 不要求擦除 SQLite 空闲页或 WAL 残留字节。

成功响应为本次清除的包 ID 列表（按 ID 排序；一个包只列一次，已清除的包不重复列出；
未匹配来源时为空列表）：

```json
{"purged": ["9f1c...", "a27b..."]}
```

### 已清除包：放行与领取均返回 410

已清除是**终态**：`POST /api/packages/{id}/release` 与 `GET /api/packages/{id}` 都返回
**410** `PACKAGE_PURGED`，响应不含任何样本；名录恢复安全、按当前代次重新放行或重启都
**不能**恢复（放行记录已随清除删除）。未知包 ID 仍为 404 `PACKAGE_NOT_FOUND`。

### curl 走一遍：封存 → 放行 → 按源列清除 → 拒领

```bash
# 0) 沿用上一节的登记图 GID 与已放行包 PID（导出列 mail 复制 users/email）
# 1) 清除：users/email 经 copy 内容来源命中该包
curl -s -X POST http://localhost:8080/api/packages/purge \
  -H 'Content-Type: application/json' \
  -d '{"columns": [{"source": "users", "column": "email"}]}'
# {"purged":["<PID>"]}

# 2) 领取：410，响应不含样本
curl -s -i http://localhost:8080/api/packages/$PID
# HTTP/1.1 410 ... {"error":{"code":"PACKAGE_PURGED",...}}

# 3) 放行：同样 410 —— 已清除是终态，名录恢复安全或重启也不能恢复
curl -s -i -X POST http://localhost:8080/api/packages/$PID/release \
  -H 'Content-Type: application/json' \
  -d '{"expected_generation": 0}'
# HTTP/1.1 410 ... {"error":{"code":"PACKAGE_PURGED",...}}

# 4) 再次清除同一来源：已清除包不重复列出
curl -s -X POST http://localhost:8080/api/packages/purge \
  -H 'Content-Type: application/json' \
  -d '{"columns": [{"source": "users", "column": "email"}]}'
# {"purged":[]}
```

行影响同样算依赖：`users/country` 只被筛选 `only_cn` 引用、并不流入任何导出列的内容，
清除它仍会命中上面的包；反过来，只经 `constant` 常量列“流入”的来源不算——常量切断了
内容来源，若导出既无该列内容、又无相关行影响，清除该来源返回空列表。

```bash
# 筛选引用列（行影响）命中：
curl -s -X POST http://localhost:8080/api/packages/purge \
  -H 'Content-Type: application/json' \
  -d '{"columns": [{"source": "users", "column": "country"}]}'
# {"purged":["<新封存的包ID>"]}

# 未匹配任何来源：空列表，什么都不改
curl -s -X POST http://localhost:8080/api/packages/purge \
  -H 'Content-Type: application/json' \
  -d '{"columns": [{"source": "ghost", "column": "x"}]}'
# {"purged":[]}
```

## 节点 JSON 结构

```jsonc
// source
{"id": "s", "type": "source",
 "columns": [{"name": "col", "sensitive": true}]}

// projection：copy 与 constant 二选一（constant 允许为 null）
{"id": "p", "type": "projection", "input": "s",
 "columns": [{"name": "out", "copy": "col"}]}

// filter：保留全部输入列，references 为条件引用到的列
{"id": "f", "type": "filter", "input": "s", "references": ["col"]}

// join：等值连接；outputs 别名必须互异，side 为 "left" 或 "right"
{"id": "j", "type": "join",
 "left_input": "a", "right_input": "b",
 "left_key": "ka", "right_key": "kb",
 "outputs": [{"alias": "x", "side": "left", "column": "v"}]}
```

* 节点可以乱序给出，服务按依赖做拓扑排序；只审查导出节点可达的祖先链。
* 一个关系连到自身两侧的等值连接（左右输入相同）是合法的；自环依赖（节点输入是自己）按环拒绝。
* `content_reasons` / `row_reasons` 中的来源按 `(node_id, column)` 排序，保证输出确定。

## 开发与测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
```

## 文件说明

```
app/models.py     请求模型与结构约束（Pydantic；含 ReviseRequest、PruneRequest、RenameRequest、ComposeRequest、PreviewRequest、CompareRequest，以及 GraphRegisterRequest、GraphPreviewRequest、SensitiveColumnsReplaceRequest、PackageReleaseRequest、PackagePurgeRequest）
app/engine.py     图校验、拓扑排序、来源传播（图内标记 ∪ 名录的 OR 敏感集）、逐列审查、安全修订、按列精简、源列批量改名、上下游拼接、小样本预演、两版导出风险对照，以及不受敏感标记限制的导出源列依赖判定
app/registry.py   登记图/按图预演/名录读取与整份替换，导出包封存/放行/领取，以及按源列清除已封存样本：锁内取快照、重审全部登记图、世代冲突
app/storage.py    SQLite 持久化（名录与 generation、登记图原图与审查结论、导出包与放行代次及已清除标记；替换与结论在同一显式事务提交，写入/提交失败整体回滚）
app/main.py       FastAPI 入口（/api/review、/api/revise、/api/prune、/api/rename、/api/compose、/api/preview、/api/compare、/api/graphs、/api/graphs/{id}、/api/graphs/{id}/preview、/api/graphs/{id}/packages、/api/packages/{id}/release、/api/packages/{id}、/api/packages/purge、/api/sensitive-columns、/health）
examples/         请求示例（request.json、revise_request.json、prune_request.json、rename_request.json、compose_request.json、preview_request.json、compare_request.json）
tests/            传播规则、拒绝场景、修订/复审、精简、改名、拼接、预演、两版对照，登记图/名录/代次/并发次序/替换失败回滚/SQLite 持久化，导出包封存/放行/领取/失败不留痕，以及按源列清除已封存样本测试
Dockerfile        容器镜像（python:3.11-slim，暴露 8080）
docker-compose.yml 单命令编排（命名卷 export-risk-data 持久化 /data/export-risk.db）
```
