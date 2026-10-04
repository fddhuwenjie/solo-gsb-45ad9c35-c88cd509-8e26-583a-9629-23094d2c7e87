# 关系型测试数据生成 API

基于 **Python + FastAPI + Pydantic v2** 的服务：提交表结构、行数、字段类型、唯一约束、外键和随机种子，即可生成带外键关联、唯一约束保证、**完全可复现**的 JSON 测试数据（fixtures）；也可以把一份已有 fixture 交给 `POST /fixtures/anonymize` 做**确定性、格式保持的假名化（pseudonymization）**脱敏，用 `POST /fixtures/slice` 按条件提取**可直接运行的关系闭包子集**，或用 `POST /fixtures/merge` 把多份独立生成的 fixture **合并成一份可直接导入的 fixture**。

## 特性

- **按外键依赖顺序生成**：自动拓扑排序，子表外键值必定引用父表中已生成的记录；请求中表的先后顺序无所谓。
- **唯一约束保证不冲突**：支持列级 `unique` 与任意多列复合唯一约束（`unique_constraints`）；取值空间不足时直接返回 422，而不是悄悄产生重复。
- **可复现**：相同请求体 + 相同 `seed`（整数或字符串）永远得到相同结果；每张表/每列使用从 seed 派生的独立随机流，调整无关表不影响其他表数据。
- **全有或全无**：依赖成环、引用目标不存在、参数非法等错误全部在生成前校验，失败返回 `422`，响应体只有错误详情，**不含任何部分数据**。
- **确定性脱敏**（`POST /fixtures/anonymize`）：同一原值跨表映射为同一假名，外键仍指向对应目标，email / name / string / int 等类型保持可解析格式，未列出的列原样保留。
- **关系闭包裁剪**（`POST /fixtures/slice`）：按根表条件（表、列、值）选出根行，沿所有外键递归扩展——既纳入其引用的父行，也纳入引用它的子行——直到闭包稳定；支持每表行数上限与 seed 稳定截断，未命中的表保留空数组。
- **多份合并**（`POST /fixtures/merge`）：把两份以上独立生成的 fixture 合并成一份可直接导入的 fixture——整行相同且唯一键相同的行只保留一份；唯一键冲突但内容不同的行按 seed 生成格式合法的新唯一值，并沿外键依赖图（含自引用、复合约束、多层关系）同步改写所有引用。
- 自引用外键（树形结构）：第一行为 `null`，后续行引用已生成的更早行。
- 交互式文档：Swagger UI（`/docs`）与 ReDoc（`/redoc`）。

## 一键启动

需要 Docker（含 Compose v2）。

```bash
docker compose up --build
```

启动后即可访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/docs | Swagger UI，可直接在页面上发请求 |
| http://localhost:8000/redoc | ReDoc 文档 |
| http://localhost:8000/openapi.json | OpenAPI 3 Schema |
| http://localhost:8000/health | 健康检查 |

> Compose v1 环境可使用 `docker-compose up --build`。修改端口：编辑 `docker-compose.yml` 中的 `"8000:8000"`。

### 不使用 Docker（本地运行）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 快速示例

```bash
curl -s http://localhost:8000/fixtures \
  -H 'Content-Type: application/json' \
  -d '{
  "seed": 42,
  "tables": [
    {
      "name": "users",
      "rows": 5,
      "fields": {
        "id":   {"type": "int", "min_value": 1, "max_value": 100000, "unique": true},
        "name": {"type": "name"},
        "email": {"type": "email", "unique": true},
        "country": {"type": "enum", "choices": ["CN", "US", "JP"]}
      }
    },
    {
      "name": "orders",
      "rows": 10,
      "fields": {
        "total": {"type": "float", "min_value": 0, "max_value": 9999},
        "created_at": {"type": "datetime"}
      },
      "foreign_keys": [
        {"column": "user_id", "references_table": "users", "references_column": "id", "nullable": false}
      ]
    }
  ]
}'
```

响应示例（节选）：

```json
{
  "seed": 42,
  "row_counts": {"users": 5, "orders": 10},
  "tables": {
    "users": [
      {"id": 85921, "name": "Avery Chen", "email": "kqmptjx@example.com", "country": "CN"}
    ],
    "orders": [
      {"total": 128.4, "created_at": "2014-07-02T11:23:05", "user_id": 85921}
    ]
  }
}
```

## 请求体 `POST /fixtures`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `tables` | array | 是（≥1） | 表定义列表，表名不可重复 |
| `seed` | integer / string | 否（默认 `42`） | 随机种子，相同输入与 seed 结果一致 |

### TableSpec

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 表名（唯一） |
| `rows` | integer ≥ 0 | 生成行数 |
| `fields` | object | `列名 -> 字段定义`，至少一列 |
| `foreign_keys` | array | 外键定义，见下 |
| `unique_constraints` | array of string[] | 复合唯一约束，如 `[["a", "b"]]`，列必须存在 |

### FieldSpec

| 字段 | 适用类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `type` | — | — | `int` `float` `boolean` `string` `name` `email` `uuid` `date` `datetime` `enum` |
| `unique` | 全部 | `false` | 该列值不重复（可与复合约束叠加） |
| `nullable` | 全部 | `false` | 约 10% 概率生成 `null` |
| `min_value` / `max_value` | `int` `float` | int: 1 / 1,000,000；float: 0 / 1000 | 数值范围，min ≤ max |
| `min_length` / `max_length` | `string` | 8 / 16 | 字符串随机部分长度 |
| `choices` | `enum` | — | 非空可选值列表 |
| `prefix` | `string`、`email` | — | 生成值前缀 |

输出格式：`date` 为 `YYYY-MM-DD`；`datetime` 为 `YYYY-MM-DDTHH:MM:SS`；`uuid` 为 UUIDv4 字符串；
`name` 为 `名 姓`；`email` 域名为 `example.com`；`float` 保留 2 位小数。

### ForeignKey

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `column` | 必填 | 本表外键列名；若未在 `fields` 中声明，会按目标列类型自动补一列 |
| `references_table` | 必填 | 目标表，必须存在 |
| `references_column` | `"id"` | 目标列，必须存在且标记为 `unique` |
| `nullable` | `true` | 仅在目标表暂无可引用记录（典型：父表 0 行、自引用第一行）时写 `null`；只要存在可引用记录就一定引用。为 `false` 且无记录可引用时返回 422 |

自引用外键（`references_table` 等于本表）不参与成环判定，可用于生成树形数据；第一行必然为 `null`，
因此自引用 FK 请保持 `nullable: true`。

## 422 错误

所有错误均为 HTTP `422`，响应体形如 FastAPI 标准校验错误：

```json
{
  "detail": [
    {"loc": ["body", "tables", 0, "foreign_keys", "0"],
     "type": "unknown_fk_table",
     "msg": "外键引用的目标表 'ghosts' 不存在"}
  ]
}
```

触发 422 的情况包括：

- 请求体结构 / 类型错误、未知字段、缺必填项、`rows < 0` 等 Pydantic 校验失败；
- 表名重复、复合唯一约束引用不存在的列、同一列定义多个外键；
- 外键引用的**目标表不存在**、**目标列不存在**、目标列**没有 `unique` 标记**；
- **外键依赖成环**（A→B→A 等；错误信息列出环上的表）；
- 非空外键没有可引用记录（父表 0 行、自引用表第一行且 `nullable=false`）；
- 唯一列/复合唯一约束取值空间不足以容纳请求的行数（如唯一 `boolean` 列生成 3 行）。

任何 422 都不会返回已生成的数据。

## `POST /fixtures/anonymize`：对已有 fixture 做假名化脱敏

接收一份 `/fixtures` 响应（或任何同构的 fixture）、对应的表结构和**待脱敏列清单**，返回结构完全一致、敏感值被假名替换的新 fixture。

### 脱敏语义

- **确定性**：相同 fixture + 相同规则 + 相同 `seed`，脱敏结果完全一致；每个值的假名由 `sha256(seed, "anonymize", 类型, 原值, 轮次)` 派生。
- **跨表一致**：映射表按 `(类型, 原值)` 全局共享——同一个邮箱 / 姓名 / 字符串 / 整数出现在任何表、任何列，都会替换成**同一个假名**，因此等值关联（join 键）不会断。
- **格式保持**：假名仍然是同类型、可解析、符合列约束的值：

  | 类型 | 保持的格式 |
  | --- | --- |
  | `int` | JSON 整数；保持符号、十进制位数，并落在列的 `min/max` 内 |
  | `float` | JSON 浮点数（2 位小数），保持符号与量级 |
  | `boolean` | `true` / `false` |
  | `string` | 相同长度（含 `min/max_length`）、保留 `prefix` |
  | `name` | 仍是 `名 姓`（取自内置姓名表） |
  | `email` | 仍是 `local@域名`，**域名原样保留**，`prefix` 原样保留 |
  | `uuid` | 合法 UUIDv4 字符串 |
  | `date` / `datetime` | ISO 日期 / ISO 时间戳 |
  | `enum` | 仍是该列 `choices` 中的值 |

- **外键**：
  - 若目标列被脱敏，则外键列**必须同时列入清单**（否则返回 422）；脱敏后外键值跟随目标列的假名映射，父子记录依然对应；
  - 若只列出外键列、目标列未脱敏，则外键值**原样保留**，继续指向原目标；
  - 自引用外键（如树形 `parent_id`）同理，第一行的 `null` 与每个 `null` 的位置都保持不变。
- **null 语义**：原值为 `null` 时永远输出 `null`；单列 unique 允许多个 `null`，复合唯一约束中含 `null` 的组合不参与去重（SQL 语义）。
- **唯一约束**：同一类型的假名全局互不相同，单列 `unique` 与复合 `unique_constraints` 脱敏后依旧满足；无法满足（如唯一 name 列出现超过 20×30=600 个不同原值）时返回 422。
- **未列出的列原样保留**；fixture 中表结构之外的额外字段也原样保留。
- 校验全部通过后才输出：规则引用未知表/列、重复规则、外键目标脱敏而外键本身未列入、值无法按声明类型解析、fixture 与表结构不一致（表集合 / 行数 / 缺列 / 原始唯一约束或外键已被破坏）、类型不支持或唯一性无法满足时，整体返回 **422**，不返回半成品。

### 请求体

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `fixture` | `FixtureResponse` | 是 | 一份 `/fixtures` 响应：`{seed, row_counts, tables}` |
| `tables` | TableSpec[] | 是（≥1） | 与 fixture 对应的表结构定义（同 `/fixtures` 的 `tables`） |
| `sensitive_columns` | `{table, column}[]` | 否（默认 `[]`） | 待脱敏列清单；每项 `{"table": "users", "column": "email"}`，不可重复 |
| `seed` | integer / string | 否（默认 `42`） | 脱敏种子，与 fixture 自身的 seed 相互独立 |

### 调用示例

先生成一份 fixture：

```bash
curl -s http://localhost:8000/fixtures \
  -H 'Content-Type: application/json' \
  -d '{
    "seed": 42,
    "tables": [
      {"name": "users", "rows": 3, "fields": {
        "id": {"type": "int", "min_value": 1, "max_value": 100000, "unique": true},
        "name": {"type": "name"},
        "email": {"type": "email", "unique": true}}},
      {"name": "orders", "rows": 6, "fields": {
        "total": {"type": "float"}},
        "foreign_keys": [
          {"column": "user_id", "references_table": "users",
           "references_column": "id", "nullable": false}]}
    ]
  }' > /tmp/fixture.json
```

把返回体作为 `fixture` 字段提交脱敏（`users.id` 被脱敏时 `orders.user_id` 必须一起列出）：

```bash
curl -s http://localhost:8000/fixtures/anonymize \
  -H 'Content-Type: application/json' \
  -d '{
    "fixture": <把 /tmp/fixture.json 的内容贴在这里>,
    "tables": [ ...与上一步相同的表结构... ],
    "sensitive_columns": [
      {"table": "users",  "column": "name"},
      {"table": "users",  "column": "email"},
      {"table": "users",  "column": "id"},
      {"table": "orders", "column": "user_id"}
    ],
    "seed": "anon-2026"
  }'
```

脱敏后：

```json
{
  "seed": "anon-2026",
  "row_counts": {"users": 3, "orders": 6},
  "tables": {
    "users": [
      {"id": 40217, "name": "Riley Vargas", "email": "wmqxbfz@example.com"}
    ],
    "orders": [
      {"total": 128.4, "user_id": 40217}
    ]
  }
}
```

`orders.total` 未列入清单，原样保留；`orders.user_id` 已跟随 `users.id` 的新假名；再次以相同参数调用结果逐字节一致。

> Python 中可直接把两个调用串起来：`POST /fixtures` 的 JSON 响应无需改动即可作为 `fixture` 字段。

## `POST /fixtures/slice`：提取关系闭包子集

接收一份已有 fixture、对应表结构、**根表筛选条件**（表名、列名和值）、每表可选行数上限及 seed，返回一份**可直接运行**的小子集：子集中任何非空外键都仍指向子集内的行，行内容、字段与约束完全不变。

### 裁剪语义

- **先选根行**：选中根表中 `column == value`（严格相等、类型必须匹配）的全部行；根行必定保留。
- **递归扩展到闭包稳定**，沿所有外键边双向扩展：
  - **父行是必选的**：已选行引用的父行（及父行引用的祖父行……）必须全部纳入，保证子集外键完整；
  - **子行是可选的**：引用了已选行的行在表上限允许时尽量纳入。
- **行数上限（`limits`）只允许截断可选子行**：
  - 上限容纳不下闭包必需的行（根行 + 全部父行）时，整体返回 **422**，不输出半成品；
  - 纳入某子行会强制其父行冲破父表上限时，同样 **422**（宁可整单失败，也不破坏闭包）；
  - 未在 `limits` 中出现的表不设上限。
- **截断由 seed 稳定决定**：每张可选表的候选行以 `sha256(seed, "slice", 表名)` 派生的独立随机流洗牌后按序接纳；相同 fixture、条件、上限与 seed 结果逐字节一致。
- **未命中表保留空数组**：闭包从未触及的表输出 `[]`；输出行保持其在原 fixture 中的顺序。
- **根条件无任何命中**时返回 `200`，所有表均为空数组（空闭包合法）；值为 `null` 时匹配该列为 `null` 的行。
- 校验全部通过后才输出：条件引用未知表/列、值类型不匹配、上限不是正整数或引用未知表、表结构不合法、fixture 与表结构不一致、原始 unique/FK 已损坏、上限阻断闭包时，整体返回 **422**。

### 请求体

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `fixture` | `FixtureResponse` | 是 | 一份 `/fixtures` 响应：`{seed, row_counts, tables}` |
| `tables` | TableSpec[] | 是（≥1） | 与 fixture 对应的表结构定义（同 `/fixtures` 的 `tables`；此处 `rows` 不参与校验） |
| `root` | `{table, column, value}` | 是 | 根表筛选条件，列必须存在（也允许是合成的外键列） |
| `limits` | object（表名 → 正整数） | 否（默认 `{}`） | 每表行数上限；未列出的表不设上限 |
| `seed` | integer / string | 否（默认 `42`） | 截断种子，决定可选子行的稳定取舍顺序 |

### 调用示例

表结构为 `companies → departments → employees`，先准备一份 fixture：

```json
{
  "seed": 1,
  "tables": {
    "companies": [
      {"id": 1, "name": "Acme Corp"},
      {"id": 2, "name": "Beta LLC"},
      {"id": 3, "name": "Cyon Inc"}
    ],
    "departments": [
      {"id": 10, "company_id": 1}, {"id": 11, "company_id": 1},
      {"id": 20, "company_id": 2}, {"id": 21, "company_id": 2},
      {"id": 30, "company_id": 3}
    ],
    "employees": [
      {"id": 100, "dept_id": 10}, {"id": 101, "dept_id": 10},
      {"id": 102, "dept_id": 11},
      {"id": 200, "dept_id": 20}, {"id": 201, "dept_id": 20},
      {"id": 202, "dept_id": 21}, {"id": 203, "dept_id": 21},
      {"id": 300, "dept_id": 30}
    ]
  }
}
```

以 `companies.id == 1` 为根、并把 `departments` 限制为 1 行：

```bash
curl -s http://localhost:8000/fixtures/slice \
  -H 'Content-Type: application/json' \
  -d '{
    "fixture": <把上一份 fixture 贴在这里，字段结构与 /fixtures 响应一致>,
    "tables": [ ...对应的表结构... ],
    "root": {"table": "companies", "column": "id", "value": 1},
    "limits": {"departments": 1},
    "seed": 7
  }'
```

不带 `limits` 时返回公司 1 的完整子树（1 家公司、2 个部门、3 名员工，`audit_log` 等无关表为空数组）；带上 `"limits": {"departments": 1}` 后，seed 决定保留部门 10 或 11 中的一个，`employees` 也只剩被保留部门下的行，所有外键仍然闭合：

```json
{
  "seed": 7,
  "row_counts": {"companies": 1, "departments": 1, "employees": 2, "audit_log": 0},
  "tables": {
    "companies": [{"id": 1, "name": "Acme Corp"}],
    "departments": [{"id": 10, "company_id": 1}],
    "employees": [
      {"id": 100, "dept_id": 10},
      {"id": 101, "dept_id": 10}
    ],
    "audit_log": []
  }
}
```

同样的请求与 seed 逐字节可复现；把 `departments` 限制到无法容纳其父闭包的程度（例如根行同时引用两个不同部门，却限制 `departments: 1`）会返回 `422 limit_blocks_closure`，响应体只有错误详情。

## `POST /fixtures/merge`：合并多份独立生成的 fixture

接收**两份以上** fixture（各自的 `/fixtures` 响应结构）、**同一份表结构**和一个合并 `seed`，把多份独立生成的数据合并成一份**可直接导入**的 fixture：合并结果满足全部单列 / 复合唯一约束与外键约束，可直接作为 `fixture` 再交给 `/fixtures/anonymize`、`/fixtures/slice` 或下一次 `/fixtures/merge` 使用。

### 合并语义

- **逐份校验**：每份 fixture 都先对照表结构校验——表集合一致、`row_counts` 与实际行数一致、每个声明列的值都能按声明类型解析、单列 `unique` / 复合 `unique_constraints` 与外键在 fixture 内部完好；任何一份不合格，整体返回 **422**，错误 `loc` 以 `["body", "fixtures", <序号>, ...]` 指出是哪一份。
- **整行相同去重**：唯一键相同且**整行**（含表结构之外的额外字段）完全相同的行是同一条记录，只保留第一份（SQL 语义：含 `null` 的唯一键不参与冲突与去重判定）。
- **冲突改写**：唯一键冲突但整行不同的行，保留先出现的行，后出现的行按 `sha256(seed, "merge", 表名, 列名)` 派生的随机流生成**格式合法的新唯一值**——遵守类型、`min/max_value`、`min/max_length`、`prefix`、`choices` 等约束；唯一外键列则从目标表的现有值中另选一个。
- **引用同步改写**：被改写的唯一值沿外键依赖图同步级联——同一份 fixture 内的子表外键、自引用外键、复合约束列和多层关系全部跟随新值；其他 fixture 的引用不受影响。中间表的键若是被上游级联改写的，会以该 fixture 的原始值为键继续向下游发布，连续改写始终解析到最终保留的记录（见下方三层级联示例）。复合唯一约束冲突优先只改写约束中的一列，单列无解时整体重生成该组合键。
- **原样保留**：未冲突的值和表结构之外的额外字段逐字节保留；行顺序稳定（按 `fixtures` 提交顺序、每份内部保持原行序），表顺序与表结构声明顺序一致；相同输入与 `seed` 结果逐字节可复现。
- **全有或全无**：唯一取值空间耗尽（如唯一 `boolean` 列合并后需要第三个值、唯一外键列的可引用值不够）、约束无法满足或输入不一致时，整体返回 **422**，不输出半成品。

### 请求体

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `fixtures` | FixtureResponse[] | 是（≥2） | 待合并的 fixture，每份为 `{seed, row_counts, tables}`；各份的 `seed` 仅作元数据，不参与合并 |
| `tables` | TableSpec[] | 是（≥1） | 所有 fixture 共享的表结构定义（同 `/fixtures` 的 `tables`；`rows` 是生成元数据，不参与校验，合并结果行数为各份之和减去去重行） |
| `seed` | integer / string | 否（默认 `42`） | 合并种子，决定冲突改写的新值 |

### 调用示例

两份 fixture 的 `users.id` 都包含 `1` 和 `2`：第一份完全相同的行被去重，第二份中 `id=2` 但邮箱不同的行获得新 id，其订单的外键同步跟随：

```bash
curl -s http://localhost:8000/fixtures/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "fixtures": [
      {
        "seed": 1,
        "row_counts": {"users": 2, "orders": 1},
        "tables": {
          "users": [
            {"id": 1, "email": "a@x.example"},
            {"id": 2, "email": "b@x.example"}
          ],
          "orders": [{"id": 10, "user_id": 1}]
        }
      },
      {
        "seed": 2,
        "row_counts": {"users": 2, "orders": 1},
        "tables": {
          "users": [
            {"id": 1, "email": "a@x.example"},
            {"id": 2, "email": "c@x.example"}
          ],
          "orders": [{"id": 11, "user_id": 2}]
        }
      }
    ],
    "tables": [
      {"name": "users", "rows": 2, "fields": {
        "id": {"type": "int", "min_value": 1, "max_value": 100000, "unique": true},
        "email": {"type": "email", "unique": true}}},
      {"name": "orders", "rows": 1, "fields": {
        "id": {"type": "int", "min_value": 1, "max_value": 100000, "unique": true}},
       "foreign_keys": [
         {"column": "user_id", "references_table": "users",
          "references_column": "id", "nullable": false}]}
    ],
    "seed": 7
  }'
```

响应（`seed: 7` 下的真实输出）：

```json
{
  "seed": 7,
  "row_counts": {"users": 3, "orders": 2},
  "tables": {
    "users": [
      {"id": 1, "email": "a@x.example"},
      {"id": 2, "email": "b@x.example"},
      {"id": 91192, "email": "c@x.example"}
    ],
    "orders": [
      {"id": 10, "user_id": 1},
      {"id": 11, "user_id": 91192}
    ]
  }
}
```

第二份的 `{"id": 1, "email": "a@x.example"}` 与第一份整行相同，被去重；`{"id": 2, "email": "c@x.example"}` 唯一键冲突但内容不同，`id` 被改写为 seed 派生的新值 `91192`，第二份订单的 `user_id` 同步指向 `91192`；未冲突的 `email`、`orders.id` 等全部原样保留。相同请求与 seed 再次调用结果逐字节一致。

### 三层级联示例

链式结构 `orgs → sites → devices`：`sites.org_id` 是唯一外键（引用 `orgs.id`），而 `devices.site_org` 引用的正是 `sites.org_id` 这一列。两份 fixture 的 `orgs.id` 都是 `1` 但内容不同，第二份的 `id` 被改写后，改写会一级一级传到 `devices`：

```bash
curl -s http://localhost:8000/fixtures/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "fixtures": [
      {
        "seed": 1,
        "row_counts": {"orgs": 1, "sites": 1, "devices": 1},
        "tables": {
          "orgs": [{"id": 1, "name": "acme"}],
          "sites": [{"org_id": 1, "name": "hq"}],
          "devices": [{"site_org": 1, "name": "router"}]
        }
      },
      {
        "seed": 2,
        "row_counts": {"orgs": 1, "sites": 1, "devices": 1},
        "tables": {
          "orgs": [{"id": 1, "name": "globex"}],
          "sites": [{"org_id": 1, "name": "lab"}],
          "devices": [{"site_org": 1, "name": "sensor"}]
        }
      }
    ],
    "tables": [
      {"name": "orgs", "rows": 1, "fields": {
        "id": {"type": "int", "min_value": 1, "max_value": 100000, "unique": true},
        "name": {"type": "string", "min_length": 2, "max_length": 12}}},
      {"name": "sites", "rows": 1, "fields": {
        "org_id": {"type": "int", "unique": true},
        "name": {"type": "string", "min_length": 2, "max_length": 12}},
       "foreign_keys": [
         {"column": "org_id", "references_table": "orgs",
          "references_column": "id", "nullable": false}]},
      {"name": "devices", "rows": 1, "fields": {
        "site_org": {"type": "int"},
        "name": {"type": "string", "min_length": 2, "max_length": 12}},
       "foreign_keys": [
         {"column": "site_org", "references_table": "sites",
          "references_column": "org_id", "nullable": false}]}
    ],
    "seed": 7
  }'
```

响应（`seed: 7` 下的真实输出）：

```json
{
  "seed": 7,
  "row_counts": {"orgs": 2, "sites": 2, "devices": 2},
  "tables": {
    "orgs": [
      {"id": 1, "name": "acme"},
      {"id": 77739, "name": "globex"}
    ],
    "sites": [
      {"org_id": 1, "name": "hq"},
      {"org_id": 77739, "name": "lab"}
    ],
    "devices": [
      {"site_org": 1, "name": "router"},
      {"site_org": 77739, "name": "sensor"}
    ]
  }
}
```

第二份的 `orgs.id` 因冲突被改写为 `77739`；`sites.org_id` 跟随到 `77739` 后，这一新键又作为级联继续向下游发布，因此第二份 `devices.site_org` 也指向 `77739`——每一级的引用都以各自 fixture 的原始值为键追踪到最终保留的记录，第一份的三行则全部原样保留。

## 复现性说明

- 每个列的随机流由 `sha256(seed + 表名 + 列名)` 独立派生：
  - 同表同 seed 多次调用结果完全一致；
  - 调换 `tables` 顺序（只影响拓扑执行次序）不会改变每张表的输出；
  - 新增/删除无关表不会影响其他表的数据。

## 项目结构

```
app/
  main.py        # FastAPI 路由、异常处理（统一 422）
  models.py      # Pydantic v2 请求/响应模型与字段级校验
  planner.py     # 跨表语义校验 + Kahn 拓扑排序（成环检测）
  generator.py   # 确定性随机生成、唯一约束、外键取值
  anonymizer.py  # fixture 假名化：跨表一致映射、格式保持、唯一/外键校验
  slicer.py     # fixture 闭包裁剪：根行选行、双向 FK 扩展、上限 seed 截断
  merger.py     # fixture 合并：逐份校验、整行去重、冲突改写与 FK 级联
  errors.py      # FixtureValidationError
tests/
  test_api.py            # /fixtures 的端到端测试
  test_anonymize_api.py  # /fixtures/anonymize 的端到端测试
  test_slice_api.py       # /fixtures/slice 的端到端测试
  test_merge_api.py       # /fixtures/merge 的端到端测试
Dockerfile
docker-compose.yml
requirements.txt
```

运行测试：

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```
