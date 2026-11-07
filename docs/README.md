# mall4j admin 登录接口测试（pytest + 拼图滑块识别）

复现浏览器登录完整流程：滑块初始化 → 拼图缺口识别 → 滑块校验 → 登录

## 目录结构（分层，各层相对独立）

```
api-test/
├── scripts/                      # 入口脚本（都从项目根跑：python scripts/xxx.py）
│   ├── run.py                    # ⭐ 接口用例入口
│   ├── run_unit.py               # 框架单元测试入口
│   ├── new_case.py               # 生成 Excel 用例模板
│   ├── to_excel.py               # YAML 用例导出成 Excel
│   └── serve_report.py           # 起本地服务看 allure 报告
│
├── testcases/                    # ⭐ 用例层
│   ├── cases/                    # YAML 用例数据（<项目>/<模块>/）
│   ├── cases_excel/              # ⭐ Excel 用例数据（<项目>/<模块>/*.xlsx）
│   ├── apis/                     # 可复用接口定义（<项目>/<模块>.yaml）
│   ├── mocks/                    # Mock 拦截表（<项目>.yaml）
│   ├── case_loader.py            # 扫目录、读 YAML/Excel
│   ├── case_runner.py            # 执行一条用例（步骤/断言/前后置）
│   ├── case_hierarchy.py         # 报告三层结构
│   ├── coverage.py               # 覆盖率统计（依赖用例加载方式）
│   ├── auth.py                   # 登录取 token
│   ├── project_login_handler.py  # 按项目触发登录
│   └── test_cases.py             # pytest 入口
│
├── framework/                    # pytest 插件层
│   ├── pytest_plugin.py          # 收集用例、命令行参数、mark 筛选
│   ├── hooks.py                  # 生命周期钩子
│   ├── fixtures.py               # 用例可用的 fixture
│   ├── allure_util.py            # allure 封装
│   ├── case_param.py             # 用例参数展示格式
│   ├── html_report_plugin.py     # HTML 报告加「模块/场景/用例」三列
│   ├── report_generator.py       # 报告生成
│   └── plugins/                  # 可选增强插件
│       ├── verify_plugin.py      # 数据库/缓存/MQ 连接管理
│       ├── ratelimit_plugin.py   # 令牌桶限流
│       ├── retry_plugin.py       # 用例级重试（只重试环境抖动）
│       └── factory_plugin.py     # 测试数据工厂加载 + 兜底清理
│
├── utils/                        # ⭐ 通用封装（按职责分子包）
│   ├── paths.py                  # ⭐ 全部路径常量
│   ├── http/                     # 请求执行、会话、业务客户端
│   ├── assertions/               # 断言执行 + 断言类型
│   ├── cases/                    # 用例校验/来源/状态/依赖/参数/变量/模块清单
│   ├── config/                   # config 读取、项目档案、密钥管理
│   ├── logs/                     # 日志（脱敏 / 按天分文件）
│   ├── slider/                   # 拼图缺口识别 + 无头浏览器驱动
│   ├── infra/                    # 限流 / 并行 / 缓存 / 安全闸 / 归因 / 通知
│   ├── excel/                    # Excel 解析（reader / cells / parser / flat_parser / loader）
│   └── tools.py                  # 用例里可调的自定义函数
│
├── verify/                       # 校验层（assert 里的 db_* / redis_* / mq_*）
├── factory/                      # 测试数据工厂 + 兜底清理
├── adapters/                     # 被测系统登录适配（mall4j 的 AES + 拼图定位）
│
├── ai/                           # ⭐ AI 能力层（可选，不配 key 也能用）
│   ├── capabilities/             # 用例生成 / 缺陷分析 / LLM 客户端
│   ├── agent/                    # 把框架能力包成智能体可调用的工具
│   └── mcp_server/               # MCP 服务端（stdio），给 Claude Code / Cursor / 平台
│
├── tools/                        # 维护与导入工具（不是测试用例）
│   ├── importer/                 # Swagger / OpenAPI / HAR / cURL → 用例
│   └── maint/                    # 断言加强 / 盘点 / 清脏数据 / 导表结构 / 扫密钥
│
├── unit_tests/                   # 框架自己的单元测试（不联网）
├── ci/                           # CI 统一入口 + .gitlab-ci.yml / Jenkinsfile
├── configs/                      # config.yaml / modules.yaml / factories.yaml
├── schemas/                      # 用例 JSON Schema
├── data/                         # 调试图片、导入演示数据
├── logs/  reports/               # 运行产物
├── conftest.py                   # 根 conftest：加载 framework 各层
└── pytest.ini                    # pytest 配置
```

## 滑块识别的三个关键坑（踩过才知道）

**① `slidingImage` 不能当模板做 matchTemplate**
服务端下发的拼图块是**从背景图上挖下来的那一块**，本身已经不含背景内容。
拿它去匹配背景图，匹配的是"被挖走后的空洞纹理"，实测匹配分只有 0.2~0.5，
位置基本是随机的。

正确做法：拼图块图片**带 alpha 通道**，不透明区域就是拼图块的精确形状
（jigsaw 轮廓）。把这个形状贴到背景图上，找"形状内部最暗 + 轮廓最吻合"
的位置即可。实测一次命中率 8/8。

**② 校验接口必须是 POST，用 GET 会假通过**
```
GET  /captcha/get?verificationEnums=LOGIN&xPos=123
  -> {code:200, success:true, result:{...下一题数据...}}   # 传任何 x 都放行！

POST /captcha/get?verificationEnums=LOGIN&xPos=123
  -> {code:200, success:true, result:{...}}                 # x 正确
  -> {code:80202, message:"验证失败", result:null, success:false}   # x 错误
```
服务端把 GET 当成"取下一题"的请求，所以**不校验 xPos**。用 GET 写测试会
出现"传错 x 也通过"的假象。

**③ 校验通过时 `result` 不是 `true`，是下一题的初始化数据**
对照前端 JS `e.success ? e.result ? "解锁成功" : "解锁失败" : 重试`：
- 通过：`success=true`，`result` 是 dict（含 backImage / randomY / key…）
- 失败：`success=false`，`result=null`，`code=80202`
- 限流：`code=1003`「访问过于频繁」

判据是 `success is True and bool(result)`。

## 能做到哪一步

| 能力 | 状态 | 怎么验证 |
|---|---|---|
| 识别拼图缺口 | ✅ 一次命中 ~8/8 | 登录时自动执行，失败看 `data/debug/` |
| 纯接口滑块校验 | ✅ | 底层能力，不单独出用例 |
| 纯接口登录拿 token | ✅ 拿到真 JWT | `testcases/cases/mall4j/auth/` |
| **token 调通业务接口** | ✅ 拿到管理员真实资料 | `testcases/cases/mall4j/` |

> 滑块识别是**工具能力**，不单独写测试用例 —— 它是上面所有测试的底层依赖。

也就是说：**这套东西不是只在验证算法，是真的能登录进去并调业务接口。**

拿到的 token 可以这样用：

```python
from utils.http.api_client import ApiClient

with ApiClient.from_config() as api:      # 自动抓新 uuid → 滑块 → 登录
    info = api.current_user()             # 调任意业务接口
    print(info["result"]["username"])
```

## 安装
```powershell
cd C:\Users\17358\Desktop\PythonProject
uv sync
.\.venv\Scripts\Activate.ps1
```

## 必填配置
编辑 `configs/config.yaml`：

| 字段 | 说明 | 示例 |
|---|---|---|
| `base_url` | admin API | `http://103.236.97.137:8190/_admin` |
| `origin` / `referer` | 前端域名 | `http://103.236.97.137:8190/` |
| `uuid` | **一次性会话凭证**（见下方说明） | 浏览器 Network 里复制 |
| `login.username` | 账号 | `admin` |
| `login.password` | 明文密码（运行时自动 MD5） | 你的真实密码 |

### 🔐 密码别写进文件

`configs/config.yaml` 是要提交到 git 的，密码用**环境变量占位符**：

```yaml
login:
  username: "${MALL4J_USERNAME:admin}"
  password: "${MALL4J_PASSWORD:123456}"   # ← 环境变量没设时才用默认值
```

跑之前设一下：

```powershell
$env:MALL4J_PASSWORD = "你的真实密码"
```

> 占位符语法：`${变量名}`（必须设）或 `${变量名:默认值}`（没设就用默认）。
> 框架和 `utils/` 那套配置读的是同一个文件，都会展开。

### ⚠️ uuid 是一次性凭证 —— 重要
后端按 `uuid` 维护会话，并在滑块通过之后**消费**掉。同一个 uuid
第二次用会返回：
```json
{ "code": 80204, "message": "验证码已失效，请重新校验" }
```

更麻烦的是：**uuid 被消费之后，服务端对同一 uuid 的后续请求会直接放行**，
连 xPos 都不校验。所以"错误输入应该失败"这类断言必须用**全新的 uuid**，
否则会假通过。

测试里用 `fresh_client` fixture 自动取新 uuid；用例里要手写的话用
`{{tools.fresh_uuid()}}`。手动抓一个（排查用）：

```powershell
python -c "from utils.tools import fresh_uuid; print(fresh_uuid())"
```

`uuid` 解析优先级：环境变量 `MALL4J_UUID` > `config.yaml::uuid`
（代码在 `utils/http/request_util.py::_resolve_uuid`）。

## 跑测试

### ⭐ 接口用例（推荐）

```powershell
python scripts/run.py
```

登录用到的 uuid 由框架自动抓（开一次无头浏览器），不用手动准备。

```powershell
python scripts/run.py --list     # 只列出用例，不执行
python scripts/run.py --marks    # 列出所有可用标签 + 每个标签多少条
python scripts/run.py -m smoke   # 只跑冒烟用例
```

### 业务接口用例（YAML / Excel 数据驱动）

```powershell
python scripts/run.py                        # 跑当前启用的那套用例（默认 Excel）
python scripts/run.py --list                 # 只列出用例
python scripts/run.py --marks                # 列出所有可用标签 + 每���多少条
python scripts/run.py -k statistics          # 按关键字筛
python scripts/run.py --module auth          # 按模块筛
python scripts/run.py --module flow          # 只跑业务流用例
python scripts/run.py --report html          # 生成 HTML 报告
python scripts/run.py --report allure        # 生成 allure 报告
```

#### 用例来源开关（Excel 还是 YAML）

框架支持两套用例维护方式，**一次只启用一套** —— 两边都跑的话，
同一个接口会被跑两遍，报告也重复。

| 方式 | 用例目录 | 平台默认 |
|---|---|---|
| **Excel** | `testcases/cases_excel/` | ✅ |
| **YAML** | `testcases/cases/` | |

开关在 `configs/config.yaml`：

```yaml
case_source:
  mode: excel          # excel / yaml / both
  excel_dir: "testcases/cases_excel"
  yaml_dir: "testcases/cases"
```

命令行可以临时切，不用改配置：

```powershell
python scripts/run.py --case-source yaml --list    # 这次只列 YAML 用例
python scripts/run.py --case-source both           # 两边都跑（迁移过渡期）
```

> ⚠️ **两套用例分开放，不是多余的** —— 目录隔离才能保证「选 Excel 就完全不碰
> YAML」。混在同一个目录里的话，光看扩展名过滤不干净。

#### ⚠️ 新写的用例要标状态

Excel 和 YAML 用例都有「用例状态」这一项：

| 值 | 意思 | CI 跑吗 |
|---|---|---|
| `debugged` | 调试通过了 | ✅ |
| `draft` | 还没调试 | ❌ |
| `disabled` | 临时下线 | ❌ |
| `broken` | 已知有问题 | ❌ |

**新写的用例默认是 `draft`** —— CI 只跑调试过的，免得刚写的用例直接打线上。
本地调试加 `--run-draft` 可以全跑：

```powershell
python scripts/run.py --run-draft
```

> ⚠️ **调试好了记得改成 `debugged`**，不然 CI 永远不跑它。

#### 报告里怎么看用例来源

每条用例在 allure 报告里都带一个 **`用例来源`** 标签（`Excel` 或 `YAML`），
一眼能看出这条是哪来的。

> 用例 ID 里的函数名是 `test_api_case`（不带 "yaml"）——
> 因为两套用例走的是同一个测试函数，名字里带 yaml 会让人误以为跑的都是 YAML。

#### 报告里的项目名不用手填

Excel 的「项目」列框架**不读**，YAML 里也别写 `allure.epic`。
项目层统一由框架按项目档案解析 —— 这样报告里的项目名会跟着环境走，
不会出现同一个项目分裂成两棵树的情况。

### ⚠️ 测试环境别指向正式地址

配置里写了 `test` 环境，但 host 还是线上地址 —— 那就是在打线上，
而报告上写的是"测试环境"，看报告的人分辨不出来。

框架发现会**大声提醒**，改法：

```yaml
# configs/config.yaml
projects:
  mall4j:
    test:
      host: "https://test-admin-api.yourcompany.com"   # ← 你们自己的测试地址
```

```powershell
# 或者设环境变量（CI 里常用）
$env:MALL4J_TEST_HOST = "https://test-admin-api.yourcompany.com"
```

> CI 默认跑 `pre` 环境（见 `ci/Jenkinsfile`）。

### 🏷️ 标签（mark）怎么维护、怎么跑

**标签写在两个地方，会自动合并：**

| 位置 | 写法 | 作用范围 |
|---|---|---|
| `cases/<项目>/<模块>/module.yaml` | `marks: [flow]` | 整个模块 |
| `cases/<项目>/<模块>/<用例>.yaml` | `marks: [flow, p1, api]` | 单条用例 |

模块级标签也可以写在 `configs/modules.yaml` 里，但**就近原则**——
`module.yaml` 优先。新建模块时直接放一个 `module.yaml` 最省事：

```yaml
# testcases/cases/mall4j/flow/module.yaml
name: flow
description: 业务流测试
order: 60
marks: [flow]
```

`order` 决定执行顺序（小的先跑）。**登录模块要排最前**，后面的用例才有 token。

**跑指定标签：**

```powershell
python scripts/run.py --marks              # 先看有哪些标签
python scripts/run.py --list -m flow       # 看这个标签下有哪些用例
python scripts/run.py -m flow              # 只跑这个标签
python scripts/run.py -m "smoke or p0"     # 多个标签
python scripts/run.py -m "p0 and not slow" # 排除（和 pytest 语法一样）
```

**不用记命令 —— 先列出来看一眼：**

```
$ python scripts/run.py --marks
可用标签（按用例数排序）

  api            34 条   示例: 正向-正确账密、正向-直接调登录接口、反向-错误账密
  p1             24 条   示例: 反向-错误账密、首页看板数据一致性…
  statistics     21 条   示例: 订单统计-按周期、订单统计-按场景…
  auth            7 条   示例: 正向-正确账密…
  flow            4 条   示例: 登录后鉴权链路、首页看板数据一致性…
  p0              4 条   示例: 正向-正确账密、登录后鉴权链路
  p2              4 条   示例: 访问量统计-非法统计周期…
  smoke           3 条   示例: 正向-正确账密、查询当前用户…
  user            2 条   示例: 菜单与首页统计、查询当前用户
  negative        1 条   示例: 反向-跳过滑块
```

### 🧩 参数化数据怎么维护

参数化就是**一条用例跑多组数据**。用例（接口定义）和测试数据是**分开两个文件**的：

```
testcases/cases/mall4j/statistics/
├── order.yaml              ← 用例：路径、参数、断言
└── data/
    └── order.yaml          ← 数据：参数化组合（同名自动配对）
```

**数据文件有三种写法：**

**① 场景列表**（最常用，推荐）—— 每项一个独立场景，场景名会显示在报告里：

```yaml
# data/order_scenarios.yaml
- name: 近七天
  data:
    searchType: LAST_SEVEN
- name: 近三十天
  data:
    searchType: LAST_THIRTY
```

**② 维度字典** —— 每个 key 是一个维度，框架自动做笛卡尔积：

```yaml
# data/order.yaml
searchType: [LAST_SEVEN, LAST_THIRTY]
```

**③ 外部文件** —— CSV / JSON，数据量大时用，在用例里直接写路径：

```yaml
# 用例文件里
parameters: "@data/users.csv"
```

**用例里怎么引用参数：**

```yaml
# order.yaml
name: 订单统计-按周期
steps:
  - id: order_statistics
    path: /admin/statistics/order
    method: GET
    params:
      searchType: '{{searchType}}'        # ← 直接引用
```

> 参数有两种引用写法，效果一样：
> `{{searchType}}`（直接写）和 `{{params.searchType}}`（带 `params.` 前缀，更明确）。
> 场景列表用哪种都行；维度字典建议用 `{{params.xxx}}`。

**只想跑其中一组参数？** 用 `-k` 按参数组名筛（支持中文）：

```powershell
python scripts/run.py --list -k "近七天"     # 先看会跑哪些
python scripts/run.py -k "近七天"            # 只跑近七天那组
```

**数据文件命名规则**：和用例文件**同名**就自动配对（`order.yaml` ↔ `data/order.yaml`），
也可以叫 `data/order_data.yaml`。`python scripts/run.py --list` 会显示每条用例的数据来自哪个文件、展开成几组。

### 业务流用例（把接口串起来验）

单接口测试验的是"这个接口返不返 200"，但真实故障往往是
**接口之间对不上**（概览说近七天 UV=261，访问量明细加起来是 255）。

`testcases/cases/mall4j/flow/` 下的用例专门干这个：

| 用例 | 验什么 |
|---|---|
| 首页看板数据一致性 | 概览 UV == 访问量明细之和；订单金额 == 店铺金额 |
| 统计周期口径一致性 | 近七天 / 近三十天重叠区间逐日相等；日期集合一致 |
| 登录后鉴权链路 | 用户/菜单/统计/商品一路通；换坏 token 必须 403 |
| 商品与销售统计关联 | 统计里的商品，拿到商品模块查详情，单价和名称要对得上 |

跨接口比对靠 `operation_type: noop` 步骤 + 内置工具函数：

```yaml
- id: check_uv
  operation_type: noop
  value: '{{tools.compare(index.result.lastSevenUV, tools.sum_field(view_list.result, "uvNum"))}}'
  assert:
    - type: equal
      field: value.matched
      expected: true
```

详细写法见 [docs_FRAMEWORK.md](docs_FRAMEWORK.md) 的「九之二、业务流测试」。

```bash
python scripts/run.py --module flow
```

新增业务用例只要在**当前启用的那套用例目录**下放一个文件，不用写 Python：

| 当前模式 | 放哪 |
|---|---|
| `excel` | `testcases/cases_excel/mall4j/<模块>/` |
| `yaml` | `testcases/cases/mall4j/<模块>/` |

Excel 的话直接填模板（`python scripts/new_case.py --module <模块>`），
YAML 的话长这样：

```yaml
testcase:
  name: user_info
  description: 查询当前登录用户信息
  marks: [smoke, p0]
  steps:
    - id: user_info
      path: /admin/user/info
      method: GET
      headers:
        accesstoken: '{{mall4j.token}}'
        uuid: '{{mall4j.uuid}}'
      assert:
        - type: equal
          field: code
          expected: 200
```

> 想手动指定 uuid：`$env:MALL4J_UUID = "<uuid>"` 然后加 `--no-refresh`。

### 手动跑（等价于直接调 pytest）

```powershell
python scripts/run.py                  # 跑接口用例（YAML / Excel）
python scripts/run.py --log-cli-level=INFO   # 看请求日志（透传给 pytest）
pytest                                 # 只跑框架单元测试（unit_tests/）
```

跑接口用例前建议先看一眼 `python scripts/run.py --list` 确认有哪些用例。

### 滑块识别不准怎么查
登录失败时先看 `data/debug/` 下的标注图（背景图 / 拼图块 / 绿框预测位置），
定位偏了就是算法问题，不用怀疑报文。

## 调试滑块识别
`slider.save_debug: true`（已开）时每次识别都会在 `data/debug/` 落 3 张图：
- `{ts}_slider.png` —— 背景图
- `{ts}_slider_piece.png` —— 拼图块（能看到 alpha 通道的形状）
- `{ts}_slider_marked.png` —— 背景图 + 绿框标出预测位置

若框偏了，看 `logs/test.log` 里"三路结果"那行 —— 三个算法（形状 v2 / 形状 v1 /
暗度）给的位置若不一致，说明这题纹理复杂，可以再调 `gap_shape_v2.py` 的权重。

## 反向用例断言
反向用例（错密码/错用户/跳过滑块/空密码）的断言是按"
`code != 200` 或 `success is not True` 或 `result is not dict` 或
`accessToken/refreshToken` 缺失"写的宽松版。

## 业务接口的鉴权头是 `accesstoken`，不是 `Authorization: Bearer`

登录接口用 `Authorization: Bearer <uuid>`，但登录**之后**的业务接口
换成了 `accesstoken: <accessToken>`（全小写、无连字符），同时还要带 `uuid`。

用错头会拿到：
```json
{"code":403,"success":false,"message":"未登录或token失效"}
```

这个坑很容易踩 —— 因为前面所有步骤都成功了，只有最后调业务接口时才炸。
`utils/http/api_client.py` 已经封装好了，直接用就行。

## 常见问题
- **400 + 80204 验证码已失效** → uuid 被消费，换一个新 uuid
- **400 + 1003 访问过于频繁** → 服务端 QPS 限制，等一会儿再跑
  （`slider_init` / `slider_verify` 已内置退避重试）
- **滑块校验失败** → 看 `data/debug/` 的标注图对不对；
  也可能是服务端容差只有 ±10px，差一点就不通过
- **密码不对** → `login.password` 是**明文**，运行时实时 MD5，
  别把 MD5 后的小写 32 位 hex 写进去

---

## 框架详细手册

用例怎么写、断言支持哪些类型、Excel 模板怎么用、报告怎么看 ——
看 **[docs_FRAMEWORK.md](docs_FRAMEWORK.md)**。

常用命令速查：

```powershell
python scripts/run.py --list          # 列出所有用例
python scripts/run.py                 # 跑接口用例
python scripts/run.py --report html   # 出 HTML 报告
python scripts/run.py --report allure # 出 allure 报告
python scripts/run_unit.py            # 跑单元测试（框架自己的单测，不用网络）
python scripts/new_case.py --module xxx   # 生成 Excel 用例模板
```

### 三套测试的区别

| 命令 | 测什么 | 要网络 | 报告 |
|---|---|---|---|
| `python scripts/run.py` | 接口用例（YAML / Excel） | ✅ | `reports/` |
| `python scripts/run_unit.py` | 框架自己的单元测试 | ❌ | 终端 |
| `python -m ai.mcp_server` | MCP 服务（给 Claude Code / Cursor 用） | ❌ | stdio |

> 单元测试放在 `unit_tests/`，**不会**混进接口测试报告。
