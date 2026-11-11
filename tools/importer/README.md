# 接口文档导入 → 测试用例

把接口文档（Swagger / OpenAPI / HAR / cURL）上传进来，自动解析成接口清单，
一键导出成**框架能直接跑的测试用例**。

---

## 快速开始

### 方式一：本地网页（推荐，像 Apifox 那样拖文件）

```powershell
python -m tools.importer.web
```

浏览器自动打开 `http://127.0.0.1:8770`，然后：

1. 把文档文件拖进页面（支持 `.json` / `.yaml` / `.har` / `.txt`）
2. 页面上显示解析结果 —— 接口数量、模块分布、每个接口的路径 / 参数 / 请求体 / 响应示例
3. 填上「项目名」和「鉴权请求头」
4. 点「导出 YAML 用例」或「导出 Excel 用例」

### 方式二：命令行

```powershell
# 只看解析结果，不落盘
python -m tools.importer.cli preview data\importer_demo\openapi_demo.json

# 导出 YAML 用例
python -m tools.importer.cli export data\importer_demo\openapi_demo.json --project mall4j

# 导出 Excel 用例
python -m tools.importer.cli export data\importer_demo\har_demo.har --format excel

# 看看支持哪些格式
python -m tools.importer.cli formats
```

---

## 支持的格式

| 格式 | 后缀 | 从哪来 |
|---|---|---|
| **OpenAPI 3.x** | `.json` / `.yaml` / `.yml` | 后端代码生成的接口文档 |
| **Swagger 2.0** | `.json` / `.yaml` | 老项目的接口文档 |
| **HAR** | `.har` | 浏览器 F12 → Network → 导出 HAR |
| **cURL** | `.txt` / `.sh` / `.curl` | 浏览器 F12 → 复制为 cURL |

格式是**自动识别**的 —— 先看内容特征，再看后缀，所以把 HAR 存成 `.json` 也能认出来。

---

## 导出成什么

### YAML 用例

落在 `testcases/cases/<项目>/<模块>/` 下，一个接口一个文件：

```
testcases/cases/mall4j/statistics/
├── module.yaml                    # 模块说明 / 标签 / 执行顺序
└── get_statistics_order.yaml      # 一条用例
```

生成的内容长这样：

```yaml
testcase:
  name: get_statistics_order
  description: 订单统计
  allure:
    epic: 接口文档导入
    feature: statistics
    story: 订单统计
  marks:
  - api
  steps:
  - id: get_statistics_order
    description: 订单统计
    path: /admin/statistics/order
    method: GET
    headers:
      accesstoken: '{{mall4j.token}}'
      uuid: '{{mall4j.uuid}}'
    params:
      searchType: LAST_SEVEN
      pageSize: 10
    assert:
    - type: equal
      field: code
      expected: 200
    - type: equal
      field: success
      expected: true
```

这是**框架原生的用例格式** —— 导出完直接跑：

```powershell
python scripts/run.py --project mall4j --module statistics
```

### Excel 用例

导出的 Excel 是框架认的那种四表结构：

| Sheet | 内容 |
|---|---|
| 模块信息 | 模块名 / 标签 / 说明 / 执行顺序 |
| 接口定义 | 每个接口一行 |
| 测试用例 | 每条用例一行 |
| 测试数据 | 参数化数据（先留空） |

**导出的 Excel 还能被框架读回来** —— 直接在上面改用例，改完照样跑：

```powershell
python scripts/run.py --case-file testcases/cases/mall4j/mall4j_imported.xlsx
```

---

## 几个设计上的取舍

### 模块名怎么来的

按接口的 `tags` 分模块；文档里没写 tags 就从路径推断，剥掉 `manager` / `api` / `v1` 这种通用前缀：

```
/admin/statistics/order  →  statistics
/order/list         →  order
/api/v1/goods/detail       →  goods
```

### 参数值哪来的

按这个顺序找：**文档里的 example → default → 枚举的第一个值 → 按类型造一个假值**。

最后一种（比如文档只写了 `"type": "string"` 没给例子）会造一个占位值，
但会被标记成"假值" —— 生成用例时**不会**用它去覆盖鉴权请求头，
避免出现 `accesstoken: string` 这种垃圾。

### 断言怎么生成的

看响应示例：认得 `code` / `success` / `message` / `result` 就自动生成对应断言，
`result` 里的字段生成 `is not None`。

**这些只是骨架**，生成完你多半要改 —— 把它当"省掉敲键盘"而不是"完全不用管"。

### 请求体和 Query 参数不串味

OpenAPI 里 requestBody 的字段单独存在 `body_params` 里，
不会混进 `params` —— 否则 POST 接口的请求体字段会变成 URL 上的 query 参数。

---

## 命令行参数

```
python -m tools.importer.cli preview <文件> [--json]
    只打印解析结果，不写文件

python -m tools.importer.cli export <文件>
    --project <名>       用例放在 cases/<名>/ 下（默认 mall4j）
    --epic <名>          报告分组名（默认「接口文档导入」）
    --format yaml|excel  导出格式（默认 yaml）
    --output <路径>       Excel 输出路径
    --overwrite          覆盖已存在的用例文件
    --no-auth            不自动加鉴权请求头

python -m tools.importer.cli web
    --host / --port      本地网页地址（默认 127.0.0.1:8770）
    --no-browser         不自动打开浏览器
```

---

## 代码结构

```
tools/importer/
├── models.py            统一数据模型（Endpoint / Param）
├── openapi_parser.py    OpenAPI 3 + Swagger 2 解析
├── har_parser.py        HAR 解析
├── curl_parser.py       cURL 解析
├── dispatcher.py        格式识别 + 分发
├── case_builder.py      生成 YAML / Excel 用例
├── cli.py               命令行入口
└── web.py               本地网页上传页
```

单元测试在 `unit_tests/test_importer.py`（45 条）——
和框架其它单测放在一起，`python scripts/run_unit.py` 会一并跑到。

要加新格式（比如 Postman），只要写一个解析器返回 `Endpoint` 列表，
在 `dispatcher.py` 里注册一下就行，生成用例和网页都不用改。

---

## 跑测试

```powershell
python -m pytest unit_tests/test_importer.py -v
```

这些是纯单元测试，不联网、不调接口。
