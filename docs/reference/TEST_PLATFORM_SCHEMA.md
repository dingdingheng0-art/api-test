# 数据库表结构：test_platform

共 110 张表。

## aerich

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `version` | varchar(255) | 是 |  |  |  |
| `app` | varchar(100) | 是 |  |  |  |
| `content` | json | 是 |  |  |  |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## agent_blackboards —— Agent黑板表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `history_summary` | json | 否 |  |  | 历史摘要列表 |
| `current_state` | json | 否 |  |  | 当前状态 |
| `tool_results_refs` | json | 否 |  |  | 工具结果引用 |
| `context_variables` | json | 否 |  |  | 上下文变量 |
| `updated_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 更新时间 |
| `task_id` | int | 是 | 唯一 |  | 所属任务 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |
| `task_id` | `task_id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_agent_bl_agent_ta_2899d6cf` | `task_id` | `agent_tasks.id` |

## agent_steps —— Agent步骤表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `step_number` | int | 是 |  |  | 步骤序号 |
| `input_context` | json | 否 |  |  | 输入上下文（精简版） |
| `ai_thinking` | longtext | 否 |  |  | AI思考过程 |
| `ai_decision` | longtext | 否 |  |  | AI决策 |
| `tool_name` | varchar(100) | 否 |  |  | 工具名称 |
| `tool_input` | json | 否 |  |  | 工具输入 |
| `tool_output_summary` | longtext | 否 |  |  | 工具输出摘要 |
| `tool_output_full_ref` | varchar(200) | 否 |  |  | 完整输出引用 |
| `ai_response` | longtext | 否 |  |  | AI响应 |
| `is_final` | tinyint(1) | 否 |  | `0` | 是否为最终响应 |
| `token_input` | int | 否 |  | `0` | 输入Token数 |
| `token_output` | int | 否 |  | `0` | 输出Token数 |
| `duration_ms` | int | 否 |  | `0` | 耗时(毫秒) |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `task_id` | int | 是 |  |  | 所属任务 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |
| `uid_agent_steps_task_id_b1894f` | `task_id`, `step_number` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_agent_st_agent_ta_105f6406` | `task_id` | `agent_tasks.id` |

## agent_tasks —— Agent任务表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `session_id` | int | 是 |  |  | 关联会话ID |
| `goal` | longtext | 是 |  |  | 任务目标（用户原始请求） |
| `status` | varchar(20) | 否 |  | `pending` | 状态 |
| `max_steps` | int | 否 |  | `500` | 最大步骤数 |
| `current_step` | int | 否 |  | `0` | 当前步骤 |
| `final_response` | longtext | 否 |  |  | 最终响应 |
| `error_message` | longtext | 否 |  |  | 错误信息 |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `updated_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 更新时间 |
| `completed_at` | datetime(6) | 否 |  |  | 完成时间 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## ai_code_generation —— AI代码生成记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `path` | varchar(500) | 是 |  |  | 接口路径 |
| `method` | varchar(10) | 是 |  |  | 请求方法 |
| `summary` | varchar(200) | 否 |  |  | 接口描述 |
| `parameters` | json | 否 |  |  | 请求参数 |
| `request_body` | longtext | 否 |  |  | 请求体 |
| `response_example` | longtext | 否 |  |  | 响应示例 |
| `generated_code` | longtext | 是 |  |  | 生成的代码 |
| `file_name` | varchar(200) | 是 |  |  | 文件名 |
| `language` | varchar(50) | 否 |  | `python` | 编程语言 |
| `framework` | varchar(50) | 否 |  | `pytest` | 测试框架 |
| `status` | varchar(7) | 否 |  | `enable` | 记录状态 |
| `generation_time` | double | 否 |  |  | 生成耗时(秒) |
| `code_lines` | int | 否 |  |  | 代码行数 |
| `download_count` | int | 否 |  | `0` | 下载次数 |
| `copy_count` | int | 否 |  | `0` | 复制次数 |
| `last_used_time` | datetime(6) | 否 |  |  | 最后使用时间 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## api_key —— API 密钥表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `user_id` | int | 是 |  |  | 用户ID（密钥拥有者） |
| `name` | varchar(100) | 是 |  |  | 密钥名称 |
| `service` | varchar(50) | 是 |  |  | 服务类型 |
| `key_value` | varchar(1000) | 是 |  |  | 密钥值 |
| `description` | longtext | 否 |  |  | 描述 |
| `is_active` | tinyint(1) | 否 |  | `1` | 是否启用 |
| `expires_at` | datetime(6) | 否 |  |  | 过期时间 |
| `usage_count` | int | 否 |  | `0` | 使用次数 |
| `last_used_at` | datetime(6) | 否 |  |  | 最后使用时间 |
| `creator_id` | int | 是 |  |  | 创建人ID |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## api_test_api —— 接口测试接口信息表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 接口名称 |
| `num` | int | 否 |  |  | 接口序号 |
| `desc` | varchar(255) | 否 |  | `` | 接口描述 |
| `project_id` | int | 否 |  |  | 所属的服务id |
| `module_id` | int | 否 |  |  | 所属的模块id |
| `time_out` | int | 否 |  | `60` | request超时时间，默认60秒 |
| `addr` | varchar(255) | 否 |  |  | 接口地址 |
| `method` | varchar(7) | 否 |  | `GET` | 请求方式 |
| `level` | varchar(2) | 否 |  | `P1` | 接口重要程度：P0、P1、P2 |
| `headers` | json | 是 |  |  | 头部信息 |
| `params` | json | 是 |  |  | url参数 |
| `body_type` | varchar(10) | 否 |  | `json` | 请求体数据类型，json/form/text/urlencoded |
| `data_form` | json | 是 |  |  | form-data参数 |
| `data_urlencoded` | json | 是 |  |  | form_urlencoded参数 |
| `data_json` | json | 是 |  |  | json参数 |
| `data_text` | longtext | 否 |  |  | 文本参数 |
| `response` | json | 是 |  |  | 响应对象 |
| `extracts` | json | 是 |  |  | 提取信息 |
| `validates` | json | 是 |  |  | 断言信息 |
| `status` | varchar(7) | 否 |  | `enable` | 此接口状态，对应swagger的废弃状态, enable/disable |
| `use_count` | int | 否 |  | `0` | 被引用次数，即多少个步骤直接使用了此接口 |
| `mock_response` | json | 是 |  |  |  |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_ap_module__c0aa34` | `module_id` | 否 |
| `idx_api_test_ap_project_db20bb` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## api_test_case —— 接口测试用例表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 用例名称 |
| `num` | int | 否 |  |  | 用例序号 |
| `desc` | longtext | 否 |  |  | 用例描述 |
| `status` | int | 否 |  | `0` | 用例状态，0未调试-不执行，1调试通过-要执行，2调试通过-不执行，3调试不通过-不执行，默认未调试-不执行 |
| `run_times` | int | 否 |  | `1` | 执行次数，默认执行1次 |
| `variables` | json | 是 |  |  | 用例级的公共参数 |
| `output` | json | 是 |  |  | 用例出参（步骤提取的数据） |
| `skip_if` | json | 是 |  |  | 是否跳过的判断条件 |
| `suite_id` | int | 是 |  |  | 所属的用例集id |
| `headers` | json | 是 |  |  | 用例的头部信息 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_ca_suite_i_dcb028` | `suite_id` | 否 |
| `PRIMARY` | `id` | 是 |

## api_test_case_suite —— 接口测试用例集表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 用例集名称 |
| `num` | int | 否 |  |  | 用例集在对应服务下的序号 |
| `suite_type` | varchar(9) | 否 |  | `base` | 用例集类型，base: 基础用例集，api: 单接口用例集，process: 流程用例集，assist: 造数据用例集 |
| `parent` | int | 否 |  |  | 父级用例集id |
| `project_id` | int | 是 |  |  | 所属的服务id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_ca_project_f2f530` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## api_test_module —— 接口测试模块表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 模块名 |
| `num` | int | 否 |  |  | 模块在对应服务下的序号 |
| `parent` | int | 否 |  |  | 父级模块id |
| `project_id` | int | 是 |  |  | 所属的服务id |
| `controller` | varchar(255) | 否 |  |  | 当前模块在swagger上的controller名字 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_mo_project_facac5` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## api_test_project —— 接口测试服务表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 是 | 唯一 |  | 服务名称 |
| `manager` | int | 是 |  |  | 服务的管理员id |
| `script_list` | json | 是 |  |  | 引用的脚本文件 |
| `num` | int | 否 |  |  | 当前服务的序号 |
| `business_id` | int | 是 |  |  | 所属业务线 |
| `source_type` | varchar(255) | 否 |  |  | 服务对应的接口文档地址类型，swagger、apifox |
| `source_addr` | varchar(255) | 否 |  |  | 服务对应的接口文档地址 |
| `source_name` | varchar(255) | 否 |  |  | 服务对应的接口文档中的服务名字 |
| `source_id` | int | 否 |  |  | 拉取数据来源的id |
| `last_pull_status` | int | 否 |  | `1` | 最近一次swagger拉取状态，0拉取失败，1未拉取，2拉取成功 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_pr_busines_feaf24` | `business_id` | 否 |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## api_test_project_env —— 接口测试服务环境表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `host` | varchar(255) | 否 |  | `http://localhost:8018` | 服务地址 |
| `variables` | json | 是 |  |  | 服务的公共变量 |
| `env_id` | int | 是 |  |  | 对应环境id |
| `project_id` | int | 是 |  |  | 所属的服务id |
| `headers` | json | 是 |  |  | 服务的公共头部信息 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_pr_env_id_8091f5` | `env_id` | 否 |
| `idx_api_test_pr_project_305b54` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## api_test_report —— 接口测试报告表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(128) | 是 |  |  | 测试报告名称 |
| `is_passed` | int | 否 |  | `1` | 是否全部通过，1全部通过，0有报错 |
| `run_type` | varchar(16) | 否 |  | `task` | 报告类型，task/suite/case/api |
| `status` | int | 否 |  | `1` | 当前节点是否执行完毕，1执行中，2执行完毕 |
| `retry_count` | int | 否 |  | `0` | 已经执行重试的次数 |
| `env` | varchar(128) | 否 |  | `test` | 运行环境 |
| `temp_variables` | json | 否 |  |  | 临时参数 |
| `process` | int | 否 |  | `1` | 进度节点, 1: 解析数据、2: 执行测试、3: 写入报告 |
| `trigger_type` | varchar(8) | 否 |  | `page` | 触发类型，pipeline:流水线、page:页面、cron:定时任务 |
| `batch_id` | varchar(128) | 是 |  |  | 运行批次id，用于查询报告 |
| `trigger_id` | json | 否 |  |  |  |
| `project_id` | int | 是 |  |  | 所属的服务id |
| `summary` | json | 是 |  |  | 报告的统计 |
| `notified` | tinyint(1) | 否 |  | `0` | 标记是否已经通知过了 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_re_batch_i_4465ad` | `batch_id` | 否 |
| `idx_api_test_re_project_35a3a9` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## api_test_report_case —— 测试报告用例表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(128) | 是 |  |  | 测试用例名称 |
| `case_id` | int | 否 |  | `0` | 执行记录对应的用例id, 如果是运行接口，则为null |
| `suite_id` | int | 否 |  | `0` | 执行用例所在的用例集id |
| `report_id` | int | 是 |  |  | 测试报告id |
| `result` | varchar(128) | 否 |  | `waite` | 步骤测试结果，waite：等待执行、running：执行中、fail：执行不通过、success：执行通过、skip：跳过、error：报错 |
| `case_data` | json | 是 |  |  | 用例的数据 |
| `summary` | json | 是 |  |  | 用例的报告统计 |
| `retry_count` | int | 否 |  | `0` | 已经执行重试的次数 |
| `error_msg` | longtext | 是 |  |  | 用例错误信息 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_re_case_id_0043b6` | `case_id` | 否 |
| `idx_api_test_re_report__98102b` | `report_id` | 否 |
| `idx_api_test_re_suite_i_028854` | `suite_id` | 否 |
| `PRIMARY` | `id` | 是 |

## api_test_report_step —— 接口测试报告的步骤数据表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(128) | 是 |  |  | 测试步骤名称 |
| `case_id` | int | 否 |  |  | 步骤所在的用例id |
| `step_id` | int | 否 |  |  | 步骤id |
| `element_id` | int | 是 |  |  | 步骤对应的元素/接口id |
| `report_case_id` | int | 是 |  |  | 用例数据id |
| `report_id` | int | 是 |  |  | 测试报告id |
| `status` | varchar(6) | 否 |  | `resume` | resume:放行、pause:暂停、stop:中断 |
| `process` | varchar(16) | 否 |  | `waite` | 步骤执行进度，waite：等待解析、parse: 解析数据、before：前置条件、after：后置条件、run：执行测试、extract：数据提取、validate：断言 |
| `result` | varchar(16) | 否 |  | `waite` | 步骤测试结果，waite：等待执行、running：执行中、fail：执行不通过、success：执行通过、skip：跳过、error：报错 |
| `step_data` | json | 是 |  |  | 步骤的数据 |
| `summary` | json | 是 |  |  | 步骤的统计 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_re_case_id_c179e5` | `case_id` | 否 |
| `idx_api_test_re_report__9a6418` | `report_case_id` | 否 |
| `idx_api_test_re_report__9f82f6` | `report_id` | 否 |
| `idx_api_test_re_step_id_a6b219` | `step_id` | 否 |
| `PRIMARY` | `id` | 是 |

## api_test_step —— 接口测试用例步骤表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 步骤名称 |
| `desc` | longtext | 是 |  |  | 步骤描述 |
| `num` | int | 否 |  | `0` | 步骤序号，执行顺序按此序号来 |
| `status` | varchar(7) | 否 |  | `enable` | 是否执行此步骤, enable/disable，默认enable |
| `run_times` | int | 否 |  | `1` | 执行次数，默认执行1次 |
| `up_func` | json | 是 |  |  | 步骤执行前的函数 |
| `down_func` | json | 是 |  |  | 步骤执行后的函数 |
| `skip_if` | json | 是 |  |  | 是否跳过的判断条件 |
| `skip_on_fail` | int | 否 |  | `1` | 当用例有失败的步骤时，是否跳过此步骤，1跳过，0不跳过，默认跳过 |
| `data_driver` | json | 是 |  |  | 数据驱动，若此字段有值，则走数据驱动的解析 |
| `quote_case` | int | 否 |  |  | 引用用例的id |
| `case_id` | int | 是 |  |  | 步骤所在的用例的id |
| `time_out` | int | 否 |  | `60` | request超时时间，默认60秒 |
| `replace_host` | int | 否 |  | `0` | 是否使用用例所在项目的域名，1使用用例所在服务的域名，0使用步骤对应接口所在服务的域名 |
| `headers` | json | 是 |  |  | 头部信息 |
| `params` | json | 是 |  |  | url参数 |
| `data_form` | json | 是 |  |  | form-data参数 |
| `data_urlencoded` | json | 是 |  |  | form_urlencoded参数 |
| `data_json` | json | 是 |  |  | json参数 |
| `data_text` | longtext | 否 |  |  | 文本参数 |
| `body_type` | varchar(10) | 否 |  | `json` | 请求体数据类型，json/form/text/urlencoded |
| `extracts` | json | 是 |  |  | 提取信息 |
| `validates` | json | 是 |  |  | 断言信息 |
| `pop_header_filed` | json | 是 |  |  | 头部参数中去除指定字段 |
| `api_id` | int | 否 |  |  | 步骤所引用的接口的id |
| `allow_redirect` | tinyint(1) | 否 |  | `0` | 是否允许重定向 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_st_case_id_475b46` | `case_id` | 否 |
| `PRIMARY` | `id` | 是 |

## api_test_task —— 接口测试任务表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 任务名称 |
| `num` | int | 否 |  | `0` | 任务序号 |
| `env_list` | json | 是 |  |  | 运行环境 |
| `case_ids` | json | 是 |  |  | 用例id |
| `task_type` | varchar(16) | 否 |  | `cron` | 定时类型 |
| `cron` | varchar(128) | 是 |  |  | cron表达式 |
| `is_send` | varchar(10) | 否 |  | `not_send` | 是否发送报告，not_send/always/on_fail |
| `merge_notify` | int | 否 |  | `0` | 多个环境时，是否合并通知（只通知一次），默认不合并，0不合并、1合并 |
| `receive_type` | varchar(11) | 否 |  | `ding_ding` | 接收测试报告类型: ding_ding、we_chat、email |
| `webhook_list` | json | 是 |  |  | 机器人地址 |
| `email_server` | varchar(255) | 否 |  |  | 发件邮箱服务器 |
| `email_from` | int | 否 |  |  | 发件人邮箱 |
| `email_to` | json | 是 |  |  | 收件人邮箱 |
| `skip_holiday` | int | 否 |  | `1` | 是否跳过节假日、调休日 |
| `status` | varchar(7) | 否 |  | `disable` | 任务的启用状态, enable/disable，默认disable |
| `is_async` | int | 否 |  | `0` | 任务的运行机制，0：串行，1：并行，默认0 |
| `skip_on_fail` | int | 否 |  | `0` | 用例不通过时，是否跳过后续用例的执行，0：不跳过，1：跳过，默认0 |
| `suite_ids` | json | 是 |  |  | 用例集id |
| `call_back` | json | 是 |  |  | 回调给流水线 |
| `project_id` | int | 是 |  |  | 所属的服务id |
| `push_hit` | int | 否 |  | `1` | 任务不通过时，是否自动记录，0：不记录，1：记录，默认1 |
| `conf` | json | 是 |  |  | 运行配置，ui存浏览器，app存运行服务器、手机、是否重置APP |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_api_test_ta_project_d6273b` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_case —— APP测试用例表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 用例名称 |
| `num` | int | 否 |  |  | 用例序号 |
| `desc` | longtext | 否 |  |  | 用例描述 |
| `status` | int | 否 |  | `0` | 用例状态，0未调试-不执行，1调试通过-要执行，2调试通过-不执行，3调试不通过-不执行，默认未调试-不执行 |
| `run_times` | int | 否 |  | `1` | 执行次数，默认执行1次 |
| `variables` | json | 是 |  |  | 用例级的公共参数 |
| `output` | json | 是 |  |  | 用例出参（步骤提取的数据） |
| `skip_if` | json | 是 |  |  | 是否跳过的判断条件 |
| `suite_id` | int | 是 |  |  | 所属的用例集id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_suite_i_f621fb` | `suite_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_case_suite —— APP测试用例集表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 用例集名称 |
| `num` | int | 否 |  |  | 用例集在对应服务下的序号 |
| `suite_type` | varchar(9) | 否 |  | `base` | 用例集类型，base: 基础用例集，api: 单接口用例集，process: 流程用例集，assist: 造数据用例集 |
| `parent` | int | 否 |  |  | 父级用例集id |
| `project_id` | int | 是 |  |  | 所属的服务id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_project_32251f` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_element —— APP测试元素表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 接口名称 |
| `num` | int | 否 |  |  | 接口序号 |
| `desc` | varchar(255) | 否 |  | `` | 接口描述 |
| `project_id` | int | 否 |  |  | 所属的服务id |
| `module_id` | int | 否 |  |  | 所属的模块id |
| `by` | varchar(255) | 否 |  |  | 定位方式 |
| `element` | longtext | 否 |  |  | 元素值 |
| `wait_time_out` | int | 否 |  | `5` | 等待元素出现的时间，默认5秒 |
| `page_id` | int | 否 |  |  | 所属的页面id |
| `template_device` | int | 否 |  |  | 元素定位时参照的设备，定位方式为bounds时根据此设备参照分辨率 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_module__ea1316` | `module_id` | 否 |
| `idx_app_ui_test_page_id_6708ef` | `page_id` | 否 |
| `idx_app_ui_test_project_f857b7` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_module —— APP测试模块表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 模块名 |
| `num` | int | 否 |  |  | 模块在对应服务下的序号 |
| `parent` | int | 否 |  |  | 父级模块id |
| `project_id` | int | 是 |  |  | 所属的服务id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_project_24acf1` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_page —— APP测试页面表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 接口名称 |
| `num` | int | 否 |  |  | 接口序号 |
| `desc` | varchar(255) | 否 |  | `` | 接口描述 |
| `project_id` | int | 否 |  |  | 所属的服务id |
| `module_id` | int | 否 |  |  | 所属的模块id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_module__78fefe` | `module_id` | 否 |
| `idx_app_ui_test_project_9c5541` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_project —— APP测试项目表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 是 | 唯一 |  | 服务名称 |
| `manager` | int | 是 |  |  | 服务的管理员id |
| `script_list` | json | 是 |  |  | 引用的脚本文件 |
| `num` | int | 否 |  |  | 当前服务的序号 |
| `business_id` | int | 是 |  |  | 所属业务线 |
| `app_package` | varchar(255) | 是 |  |  | 被测app包名 |
| `app_activity` | varchar(255) | 是 |  |  | 被测app要启动的AndroidActivity |
| `template_device` | int | 是 |  |  | 元素定位时参照的设备 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_busines_7475ed` | `business_id` | 否 |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_project_env —— APP测试项目环境表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `host` | varchar(255) | 否 |  | `http://localhost:8018` | 服务地址 |
| `variables` | json | 是 |  |  | 服务的公共变量 |
| `env_id` | int | 是 |  |  | 对应环境id |
| `project_id` | int | 是 |  |  | 所属的服务id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_env_id_4f5e64` | `env_id` | 否 |
| `idx_app_ui_test_project_bc0726` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_report —— APP测试报告表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(128) | 是 |  |  | 测试报告名称 |
| `is_passed` | int | 否 |  | `1` | 是否全部通过，1全部通过，0有报错 |
| `run_type` | varchar(16) | 否 |  | `task` | 报告类型，task/suite/case/api |
| `status` | int | 否 |  | `1` | 当前节点是否执行完毕，1执行中，2执行完毕 |
| `retry_count` | int | 否 |  | `0` | 已经执行重试的次数 |
| `env` | varchar(128) | 否 |  | `test` | 运行环境 |
| `temp_variables` | json | 否 |  |  | 临时参数 |
| `process` | int | 否 |  | `1` | 进度节点, 1: 解析数据、2: 执行测试、3: 写入报告 |
| `trigger_type` | varchar(8) | 否 |  | `page` | 触发类型，pipeline:流水线、page:页面、cron:定时任务 |
| `batch_id` | varchar(128) | 是 |  |  | 运行批次id，用于查询报告 |
| `trigger_id` | json | 否 |  |  |  |
| `project_id` | int | 是 |  |  | 所属的服务id |
| `summary` | json | 是 |  |  | 报告的统计 |
| `notified` | tinyint(1) | 否 |  | `0` | 标记是否已经通知过了 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_batch_i_0d8cec` | `batch_id` | 否 |
| `idx_app_ui_test_project_769b49` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_report_case —— APP测试报告的用例数据表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(128) | 是 |  |  | 测试用例名称 |
| `case_id` | int | 否 |  | `0` | 执行记录对应的用例id, 如果是运行接口，则为null |
| `suite_id` | int | 否 |  | `0` | 执行用例所在的用例集id |
| `report_id` | int | 是 |  |  | 测试报告id |
| `result` | varchar(128) | 否 |  | `waite` | 步骤测试结果，waite：等待执行、running：执行中、fail：执行不通过、success：执行通过、skip：跳过、error：报错 |
| `case_data` | json | 是 |  |  | 用例的数据 |
| `summary` | json | 是 |  |  | 用例的报告统计 |
| `retry_count` | int | 否 |  | `0` | 已经执行重试的次数 |
| `error_msg` | longtext | 是 |  |  | 用例错误信息 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_case_id_a1fe78` | `case_id` | 否 |
| `idx_app_ui_test_report__e95ba3` | `report_id` | 否 |
| `idx_app_ui_test_suite_i_aae069` | `suite_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_report_step —— APP测试报告的步骤数据表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(128) | 是 |  |  | 测试步骤名称 |
| `case_id` | int | 否 |  |  | 步骤所在的用例id |
| `step_id` | int | 否 |  |  | 步骤id |
| `element_id` | int | 是 |  |  | 步骤对应的元素id |
| `report_case_id` | int | 是 |  |  | 用例数据id |
| `report_id` | int | 是 |  |  | 测试报告id |
| `status` | varchar(6) | 否 |  | `resume` | resume:放行、pause:暂停、stop:中断 |
| `process` | varchar(16) | 否 |  | `waite` | 步骤执行进度，waite：等待解析、parse: 解析数据、before：前置条件、after：后置条件、run：执行测试、extract：数据提取、validate：断言 |
| `result` | varchar(16) | 否 |  | `waite` | 步骤测试结果，waite：等待执行、running：执行中、fail：执行不通过、success：执行通过、skip：跳过、error：报错 |
| `step_data` | json | 是 |  |  | 步骤的数据 |
| `summary` | json | 是 |  |  | 步骤的统计 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_case_id_7e2d46` | `case_id` | 否 |
| `idx_app_ui_test_report__5c8beb` | `report_case_id` | 否 |
| `idx_app_ui_test_report__7ecd9f` | `report_id` | 否 |
| `idx_app_ui_test_step_id_d37974` | `step_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_run_phone —— APP测试运行终端表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 | 唯一 |  | 设备名字 |
| `num` | int | 否 |  |  | 序号 |
| `os` | varchar(8) | 否 |  |  | 设备系统类型：Android/ios |
| `os_version` | varchar(255) | 否 |  |  | 设备系统版本号 |
| `device_id` | varchar(255) | 否 |  |  | 终端设备id |
| `extends` | json | 是 |  |  | 设备扩展字段 |
| `screen` | varchar(64) | 否 |  |  | 屏幕分辨率 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_run_server —— APP测试运行服务器表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 | 唯一 |  | 服务器名字 |
| `num` | int | 否 |  |  | 序号 |
| `os` | varchar(8) | 否 |  |  | 服务器系统类型：windows/mac/linux |
| `ip` | varchar(32) | 否 |  |  | 服务器ip地址 |
| `port` | varchar(8) | 否 |  | `4723` | 服务器端口号 |
| `appium_version` | varchar(8) | 否 |  | `1.x` | appium版本 |
| `status` | int | 否 |  | `0` | 最近一次访问状态，0:未访问，1:访问失败，2:访问成功 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_step —— APP测试步骤表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 步骤名称 |
| `desc` | longtext | 是 |  |  | 步骤描述 |
| `num` | int | 否 |  | `0` | 步骤序号，执行顺序按此序号来 |
| `status` | varchar(7) | 否 |  | `enable` | 是否执行此步骤, enable/disable，默认enable |
| `run_times` | int | 否 |  | `1` | 执行次数，默认执行1次 |
| `up_func` | json | 是 |  |  | 步骤执行前的函数 |
| `down_func` | json | 是 |  |  | 步骤执行后的函数 |
| `skip_if` | json | 是 |  |  | 是否跳过的判断条件 |
| `skip_on_fail` | int | 否 |  | `1` | 当用例有失败的步骤时，是否跳过此步骤，1跳过，0不跳过，默认跳过 |
| `data_driver` | json | 是 |  |  | 数据驱动，若此字段有值，则走数据驱动的解析 |
| `quote_case` | int | 否 |  |  | 引用用例的id |
| `case_id` | int | 是 |  |  | 步骤所在的用例的id |
| `wait_time_out` | int | 否 |  | `10` | 等待元素出现的时间，默认10秒 |
| `execute_type` | varchar(255) | 是 |  |  | 执行方式 |
| `send_keys` | varchar(255) | 是 |  |  | 要输入的文本内容 |
| `extracts` | json | 是 |  |  | 提取信息 |
| `validates` | json | 是 |  |  | 断言信息 |
| `element_id` | int | 否 |  |  | 步骤所引用的元素的id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_case_id_83753c` | `case_id` | 否 |
| `PRIMARY` | `id` | 是 |

## app_ui_test_task —— APP测试任务表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 任务名称 |
| `num` | int | 否 |  | `0` | 任务序号 |
| `env_list` | json | 是 |  |  | 运行环境 |
| `case_ids` | json | 是 |  |  | 用例id |
| `task_type` | varchar(16) | 否 |  | `cron` | 定时类型 |
| `cron` | varchar(128) | 是 |  |  | cron表达式 |
| `is_send` | varchar(10) | 否 |  | `not_send` | 是否发送报告，not_send/always/on_fail |
| `merge_notify` | int | 否 |  | `0` | 多个环境时，是否合并通知（只通知一次），默认不合并，0不合并、1合并 |
| `receive_type` | varchar(11) | 否 |  | `ding_ding` | 接收测试报告类型: ding_ding、we_chat、email |
| `webhook_list` | json | 是 |  |  | 机器人地址 |
| `email_server` | varchar(255) | 否 |  |  | 发件邮箱服务器 |
| `email_from` | int | 否 |  |  | 发件人邮箱 |
| `email_to` | json | 是 |  |  | 收件人邮箱 |
| `skip_holiday` | int | 否 |  | `1` | 是否跳过节假日、调休日 |
| `status` | varchar(7) | 否 |  | `disable` | 任务的启用状态, enable/disable，默认disable |
| `is_async` | int | 否 |  | `0` | 任务的运行机制，0：串行，1：并行，默认0 |
| `skip_on_fail` | int | 否 |  | `0` | 用例不通过时，是否跳过后续用例的执行，0：不跳过，1：跳过，默认0 |
| `suite_ids` | json | 是 |  |  | 用例集id |
| `call_back` | json | 是 |  |  | 回调给流水线 |
| `project_id` | int | 是 |  |  | 所属的服务id |
| `push_hit` | int | 否 |  | `1` | 任务不通过时，是否自动记录，0：不记录，1：记录，默认1 |
| `conf` | json | 是 |  |  | 运行配置，ui存浏览器，app存运行服务器、手机、是否重置APP |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_app_ui_test_project_ffc519` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## apscheduler_jobs —— 定时任务执行计划表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `cron` | varchar(128) | 是 |  |  | cron表达式 |
| `task_code` | varchar(64) | 是 |  |  |  |
| `next_run_time` | varchar(128) | 是 |  |  | 任务下一次运行时间 |
| `job_id` | varchar(64) | 是 |  |  | 内存中的任务id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## auto_test_call_back —— 自动化测试回调流水线记录

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `ip` | varchar(128) | 否 |  |  | 访问来源ip |
| `url` | varchar(128) | 否 |  |  | 请求地址 |
| `method` | varchar(10) | 否 |  |  | 请求方法 |
| `headers` | json | 是 |  |  | 头部参数 |
| `params` | json | 是 |  |  | 查询字符串参数 |
| `data_form` | json | 是 |  |  | form_data参数 |
| `data_json` | json | 是 |  |  | json参数 |
| `status` | int | 否 |  | `1` | 回调状态, 0失败，1回调中，2成功 |
| `result` | json | 否 |  |  | 回调响应 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## auto_test_data_pool —— 测试数据池

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `env` | varchar(64) | 否 |  |  | 数据对应的环境 |
| `mobile` | varchar(128) | 否 |  |  | 手机号 |
| `password` | varchar(256) | 否 |  |  | 密码 |
| `business_order_no` | varchar(256) | 否 |  |  | 业务流水号 |
| `amount` | varchar(64) | 否 |  |  | 金额 |
| `business_status` | varchar(64) | 否 |  |  | 业务状态，自定义 |
| `use_status` | varchar(64) | 否 |  | `not_used` | 使用状态，未使用：not_used、使用中：in_use、已使用：used |
| `desc` | varchar(256) | 否 |  |  | 备注 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_auto_test_d_env_b56a48` | `env` | 否 |
| `idx_auto_test_d_mobile_c57ee1` | `mobile` | 否 |
| `PRIMARY` | `id` | 是 |

## auto_test_hits —— 自动化测试触发问题记录

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `date` | datetime(6) | 是 |  |  | 问题触发日期 |
| `hit_type` | varchar(128) | 是 |  |  | 问题类型 |
| `hit_detail` | longtext | 否 |  |  | 问题内容 |
| `test_type` | varchar(8) | 是 |  |  | 测试类型，接口、app、ui |
| `project_id` | int | 是 |  |  | 服务id |
| `env` | varchar(128) | 是 |  |  | 运行环境 |
| `record_from` | int | 否 |  | `1` | 数据记录的来源，1、人为/2、自动 |
| `report_id` | int | 是 |  |  | 测试报告id |
| `desc` | longtext | 否 |  |  | 备注 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_auto_test_h_env_e7e230` | `env` | 否 |
| `idx_auto_test_h_project_44dcaa` | `project_id` | 否 |
| `idx_auto_test_h_record__f786a8` | `record_from` | 否 |
| `idx_auto_test_h_report__1ff75c` | `report_id` | 否 |
| `PRIMARY` | `id` | 是 |

## auto_test_python_script —— python脚本

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(128) | 是 |  |  | 脚本名称 |
| `script_data` | longtext | 是 |  |  | 脚本代码 |
| `desc` | varchar(128) | 是 |  |  | 函数文件描述 |
| `num` | int | 否 |  |  | 当前函数文件的序号 |
| `script_type` | varchar(16) | 否 |  | `test` | 脚本类型，test：执行测试、mock：mock脚本、encryption：加密、decryption：解密 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_auto_test_p_name_8f5026` | `name` | 否 |
| `idx_auto_test_p_script__288660` | `script_type` | 否 |
| `PRIMARY` | `id` | 是 |

## auto_test_queue_instance —— 消息队列实例管理

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` |  |
| `queue_type` | varchar(9) | 否 |  | `active_mq` | 消息队列类型 |
| `instance_id` | varchar(128) | 否 |  | `test_platform_client` | rocket_mq 对应的 instance_id |
| `desc` | varchar(512) | 否 |  |  | 描述 |
| `host` | varchar(128) | 否 |  | `` | rabbit_mq 地址 |
| `port` | int | 否 |  |  | rabbit_mq 端口 |
| `account` | varchar(128) | 否 |  |  | rabbit_mq 账号 |
| `password` | varchar(128) | 否 |  |  | rabbit_mq 密码 |
| `access_id` | varchar(128) | 否 |  |  | rocket_mq access_id |
| `access_key` | varchar(128) | 否 |  |  | rocket_mq access_key |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## auto_test_queue_message_log —— 消息发送记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `instance_id` | int | 是 |  |  | 消息队列实例数据id |
| `topic_id` | int | 是 |  |  | topic id |
| `tag` | varchar(128) | 是 |  |  | tag |
| `options` | json | 是 |  |  | 自定义内容 |
| `message_type` | varchar(8) | 是 |  |  | 消息类型 |
| `message` | json | 是 |  |  | 消息内容 |
| `status` | longtext | 是 |  |  | 消息发送状态 |
| `response` | longtext | 是 |  |  | 消息发送响应 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## auto_test_queue_topic —— topic 管理

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` |  |
| `instance_id` | int | 否 |  |  | rocket_mq 实例数据id |
| `topic` | varchar(128) | 否 |  | `` | rocket_mq topic，rabbit_mq queue_name |
| `desc` | varchar(512) | 否 |  |  | 描述 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## auto_test_shell_command_record —— shell造数据

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `file_content` | longtext | 是 |  |  | 文件数据 |
| `command` | varchar(32) | 否 |  | `` | shell命令 |
| `command_out_put` | longtext | 是 |  |  | shell执行结果 |
| `cmd_id` | varchar(32) | 否 |  | `` | 日志里面的cmdId |
| `algo_instance_id` | varchar(128) | 否 |  | `` | 日志里面的algoInstanceId |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## auto_test_swagger_pull_log —— swagger拉取日志

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `status` | int | 否 |  | `1` | 拉取结果，0失败，1拉取中，2拉取成功 |
| `project_id` | int | 是 |  |  | 服务id |
| `desc` | longtext | 否 |  |  | 备注 |
| `pull_args` | json | 是 |  |  | 拉取时的参数 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## auto_test_user —— 自动化测试用户数据池

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `mobile` | varchar(128) | 否 |  |  | 手机号 |
| `company_name` | varchar(128) | 否 |  |  | 公司名 |
| `access_token` | varchar(2048) | 否 |  |  | access_token |
| `refresh_token` | varchar(2048) | 否 |  |  | refresh_token |
| `user_id` | varchar(128) | 否 |  |  | 用户id |
| `company_id` | varchar(128) | 否 |  |  | 公司id |
| `password` | varchar(256) | 否 |  |  | 密码 |
| `role` | varchar(128) | 否 |  |  | 角色 |
| `comment` | varchar(1024) | 否 |  |  | 备注 |
| `env` | varchar(64) | 否 |  |  | 数据对应的环境 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_auto_test_u_env_3d6ec6` | `env` | 否 |
| `idx_auto_test_u_mobile_98ba6b` | `mobile` | 否 |
| `PRIMARY` | `id` | 是 |

## auto_test_websocket —— WebSocket消息监控

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `command` | varchar(16) | 否 |  |  | 消息command |
| `message_id` | varchar(64) | 否 |  |  | message-id |
| `destination` | varchar(64) | 否 |  |  | destination |
| `headers` | longtext | 否 |  |  | headers |
| `message_body` | longtext | 否 |  |  | 消息内容 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_auto_test_w_destina_bdaa4f` | `destination` | 否 |
| `idx_auto_test_w_message_bc73c7` | `message_id` | 否 |
| `PRIMARY` | `id` | 是 |

## automation_script —— 自动化脚本表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `source_task_id` | int | 否 |  |  | 来源任务ID |
| `name` | varchar(255) | 是 |  |  | 脚本名称 |
| `description` | longtext | 否 |  |  | 脚本描述 |
| `script_type` | varchar(30) | 否 |  | `playwright_python` | 脚本类型 |
| `source` | varchar(20) | 否 |  | `ai_generated` | 来源 |
| `status` | varchar(20) | 否 |  | `active` | 状态 |
| `script_content` | longtext | 是 |  |  | Playwright脚本代码 |
| `original_steps` | json | 是 |  |  | 原始功能用例步骤 |
| `target_url` | varchar(2000) | 否 |  |  | 目标URL |
| `timeout_seconds` | int | 否 |  | `30` | 超时时间(秒) |
| `headless` | tinyint(1) | 否 |  | `1` | 无头模式 |
| `version` | int | 否 |  | `1` | 版本号 |
| `project_id` | int | 是 |  |  | 所属项目ID |
| `framework` | varchar(20) | 否 |  | `playwright` | 测试框架 |
| `language` | varchar(20) | 否 |  | `python` | 编程语言 |
| `creator_id` | int | 是 |  |  | 创建人ID |
| `test_case_id` | int | 是 |  |  | 关联的功能用例 |
| `test_module_id` | int | 否 |  |  | 所属测试模块 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_automati_test_cas_173bfd3f` | `test_module_id` | 否 |
| `fk_automati_test_cas_406ef642` | `test_case_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_automati_test_cas_173bfd3f` | `test_module_id` | `test_case_module.id` |
| `fk_automati_test_cas_406ef642` | `test_case_id` | `test_case.id` |

## config_business —— 业务线配置表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` |  |
| `name` | varchar(128) | 否 | 唯一 |  | 业务线名 |
| `code` | varchar(128) | 否 | 唯一 |  | 业务线编码 |
| `receive_type` | varchar(11) | 否 |  | `not_receive` | 接收通知类型：not_receive:不接收、we_chat:企业微信、ding_ding:钉钉 |
| `webhook_list` | json | 是 |  |  | 接收该业务线自动化测试阶段统计通知地址 |
| `env_list` | json | 是 |  |  | 业务线能使用的运行环境 |
| `bind_env` | varchar(5) | 否 |  | `human` | 绑定环境机制，auto：新增环境时自动绑定，human：新增环境后手动绑定 |
| `desc` | varchar(255) | 否 |  |  | 描述 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `code` | `code` | 是 |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## config_config —— 配置表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` |  |
| `name` | varchar(128) | 否 | 唯一 |  | 字段名 |
| `value` | longtext | 否 |  |  | 字段值 |
| `type` | int | 否 |  |  | 配置类型 |
| `desc` | longtext | 否 |  |  | 描述 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_config_conf_name_04d0dc` | `name` | 否 |
| `idx_config_conf_type_679fd5` | `type` | 否 |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## config_run_env —— 运行环境配置表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 | 唯一 |  | 环境名字 |
| `num` | int | 否 |  |  | 环境序号 |
| `code` | varchar(255) | 否 | 唯一 |  | 环境code |
| `desc` | varchar(255) | 否 |  |  | 备注 |
| `group` | varchar(255) | 否 |  |  | 环境分组 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `code` | `code` | 是 |
| `idx_config_run__code_b373dc` | `code` | 否 |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## config_saml —— SAML配置表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(50) | 是 |  |  | 配置名称 |
| `entity_id` | varchar(255) | 是 |  |  | SP Entity ID |
| `acs_url` | varchar(255) | 是 |  |  | 断言消费服务URL |
| `sls_url` | varchar(255) | 否 |  |  | 单点登出URL |
| `idp_entity_id` | varchar(255) | 是 |  |  | IdP Entity ID |
| `idp_sso_url` | varchar(255) | 是 |  |  | IdP SSO URL |
| `idp_sls_url` | varchar(255) | 否 |  |  | IdP SLS URL |
| `idp_x509_cert` | longtext | 是 |  |  | IdP X.509证书 |
| `sp_x509_cert` | longtext | 否 |  |  | SP X.509证书 |
| `sp_private_key` | longtext | 否 |  |  | SP私钥 |
| `name_id_format` | varchar(100) | 否 |  | `urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress` | NameID格式 |
| `attribute_mapping` | json | 是 |  |  | 属性映射配置 |
| `want_assertions_signed` | tinyint(1) | 否 |  | `1` | 要求断言签名 |
| `want_name_id_encrypted` | tinyint(1) | 否 |  | `0` | 要求NameID加密 |
| `authn_requests_signed` | tinyint(1) | 否 |  | `0` | 认证请求签名 |
| `logout_requests_signed` | tinyint(1) | 否 |  | `0` | 登出请求签名 |
| `is_default` | tinyint(1) | 否 |  | `0` | 是否为默认配置 |
| `status` | varchar(7) | 否 |  | `enable` | 状态 |
| `description` | longtext | 否 |  |  | 配置描述 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## config_type —— 配置类型表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` |  |
| `name` | varchar(128) | 否 | 唯一 |  | 字段名 |
| `desc` | varchar(255) | 是 |  |  | 描述 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## config_webhook —— webhook管理表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  |  | 排序 |
| `name` | varchar(255) | 否 |  |  | webhook名字 |
| `addr` | varchar(255) | 否 |  |  | webhook地址 |
| `secret` | varchar(255) | 否 |  |  | webhook秘钥 |
| `webhook_type` | varchar(9) | 否 |  | `ding_ding` | webhook类型，钉钉、企业微信、飞书 |
| `desc` | varchar(255) | 否 |  |  | 描述 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## conversation —— 对话记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `session_id` | varchar(255) | 是 |  |  | 会话ID |
| `title` | varchar(255) | 否 |  |  | 对话标题 |
| `user_id` | int | 是 |  |  | 用户ID |
| `is_active` | tinyint(1) | 否 |  | `1` | 是否活跃 |
| `llm_config_id` | int | 否 |  |  | 使用的LLM配置 |
| `project_id` | int | 是 |  |  | 所属项目 |
| `prompt_id` | int | 否 |  |  | 关联的提示词 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_conversa_llm_conf_a4125ed5` | `llm_config_id` | 否 |
| `fk_conversa_project_b9e618cc` | `project_id` | 否 |
| `fk_conversa_prompt_a12156f1` | `prompt_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_conversa_llm_conf_a4125ed5` | `llm_config_id` | `llm_config.id` |
| `fk_conversa_project_b9e618cc` | `project_id` | `project.id` |
| `fk_conversa_prompt_a12156f1` | `prompt_id` | `prompt.id` |

## func_error_record —— 自定义函数执行错误记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  |  | 错误title |
| `detail` | longtext | 否 |  |  | 错误详情 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## knowledge_base —— 知识库模型，支持项目级别的知识库管理

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | char(36) | 是 | 主键 |  | 知识库UUID |
| `name` | varchar(200) | 是 |  |  | 知识库名称 |
| `description` | longtext | 否 |  |  | 知识库描述 |
| `is_active` | tinyint(1) | 否 |  | `1` | 是否启用 |
| `chunk_size` | int | 否 |  | `1000` | 文本分块大小 |
| `chunk_overlap` | int | 否 |  | `200` | 分块重叠大小 |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `updated_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 更新时间 |
| `creator_id` | int | 否 |  |  | 创建者 |
| `project_id` | int | 是 |  |  | 所属项目 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_knowledg_system_u_6750fec4` | `creator_id` | 否 |
| `PRIMARY` | `id` | 是 |
| `uid_knowledge_b_project_80a09f` | `project_id`, `name` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_knowledg_project_db02b53f` | `project_id` | `project.id` |
| `fk_knowledg_system_u_6750fec4` | `creator_id` | `system_user.id` |

## knowledge_config —— 知识库全局配置模型，存储嵌入服务的默认配置

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  | 配置ID（固定为1，单例模式） |
| `embedding_service` | varchar(50) | 否 |  | `custom` | 嵌入服务类型 |
| `api_base_url` | varchar(500) | 否 |  |  | API基础URL |
| `api_key` | varchar(500) | 否 |  |  | API密钥 |
| `model_name` | varchar(100) | 否 |  | `text-embedding-ada-002` | 模型名称 |
| `chunk_size` | int | 否 |  | `1000` | 默认分块大小 |
| `chunk_overlap` | int | 否 |  | `200` | 默认分块重叠 |
| `updated_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 更新时间 |
| `updated_by_id` | int | 否 |  |  | 更新人 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_knowledg_system_u_982d56a3` | `updated_by_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_knowledg_system_u_982d56a3` | `updated_by_id` | `system_user.id` |

## knowledge_document —— 文档模型，支持多种文档类型

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | char(36) | 是 | 主键 |  | 文档UUID |
| `title` | varchar(200) | 是 |  |  | 文档标题 |
| `document_type` | varchar(10) | 是 |  |  | 文档类型 |
| `file_path` | varchar(1000) | 否 |  |  | 文件路径 |
| `url` | varchar(2000) | 否 |  |  | 网页链接 |
| `content` | longtext | 否 |  |  | 文档内容 |
| `status` | varchar(20) | 否 |  | `pending` | 处理状态 |
| `error_message` | longtext | 否 |  |  | 错误信息 |
| `file_size` | int | 否 |  |  | 文件大小(字节) |
| `page_count` | int | 否 |  |  | 页数 |
| `word_count` | int | 否 |  |  | 字数 |
| `uploaded_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 上传时间 |
| `processed_at` | datetime(6) | 否 |  |  | 处理完成时间 |
| `knowledge_base_id` | char(36) | 是 |  |  | 所属知识库 |
| `uploader_id` | int | 否 |  |  | 上传者 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_knowledg_knowledg_f7d239c5` | `knowledge_base_id` | 否 |
| `fk_knowledg_system_u_871d392f` | `uploader_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_knowledg_knowledg_f7d239c5` | `knowledge_base_id` | `knowledge_base.id` |
| `fk_knowledg_system_u_871d392f` | `uploader_id` | `system_user.id` |

## knowledge_document_chunk —— 文档分块模型，存储向量化后的文档片段

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | char(36) | 是 | 主键 |  | 分块UUID |
| `chunk_index` | int | 是 |  |  | 分块索引 |
| `content` | longtext | 是 |  |  | 分块内容 |
| `vector_id` | varchar(100) | 否 |  |  | 向量ID |
| `embedding_hash` | varchar(64) | 否 |  |  | 嵌入哈希 |
| `start_index` | int | 否 |  |  | 起始位置 |
| `end_index` | int | 否 |  |  | 结束位置 |
| `page_number` | int | 否 |  |  | 页码 |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `document_id` | char(36) | 是 |  |  | 所属文档 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |
| `uid_knowledge_d_documen_fcca5b` | `document_id`, `chunk_index` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_knowledg_knowledg_8e6c6932` | `document_id` | `knowledge_document.id` |

## knowledge_query —— 查询日志模型，记录知识库查询历史

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | char(36) | 是 | 主键 |  | 查询UUID |
| `query` | longtext | 是 |  |  | 查询内容 |
| `response` | longtext | 否 |  |  | 响应内容 |
| `retrieved_chunks` | json | 否 |  |  | 检索到的分块 |
| `similarity_scores` | json | 否 |  |  | 相似度分数 |
| `retrieval_time` | double | 否 |  |  | 检索耗时(秒) |
| `generation_time` | double | 否 |  |  | 生成耗时(秒) |
| `total_time` | double | 否 |  |  | 总耗时(秒) |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 查询时间 |
| `knowledge_base_id` | char(36) | 是 |  |  | 所属知识库 |
| `user_id` | int | 否 |  |  | 查询用户 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_knowledg_knowledg_1ad3a121` | `knowledge_base_id` | 否 |
| `fk_knowledg_system_u_1bb9b135` | `user_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_knowledg_knowledg_1ad3a121` | `knowledge_base_id` | `knowledge_base.id` |
| `fk_knowledg_system_u_1bb9b135` | `user_id` | `system_user.id` |

## llm_config —— LLM 配置表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `config_name` | varchar(255) | 否 |  |  | 用户自定义的配置名称，如'生产环境OpenAI'、'测试Claude配置' |
| `name` | varchar(100) | 是 |  |  | 模型名称，如 gpt-4, claude-3-sonnet |
| `provider` | varchar(50) | 是 |  |  | LLM 提供商 |
| `model_name` | varchar(100) | 是 |  |  | 模型名称（兼容字段） |
| `api_key` | varchar(500) | 是 |  |  | API 密钥 |
| `base_url` | varchar(500) | 否 |  |  | API 基础URL |
| `system_prompt` | longtext | 否 |  |  | 指导LLM行为的系统级提示词 |
| `temperature` | double | 否 |  | `0.7` | 温度参数 |
| `max_tokens` | int | 否 |  | `2000` | 最大令牌数 |
| `supports_vision` | tinyint(1) | 否 |  | `0` | 模型是否支持图片/多模态输入（如GPT-4V、Qwen-VL等） |
| `context_limit` | int | 否 |  | `128000` | 模型最大上下文Token数（GPT-4o: 128000, Claude: 200000, Gemini: 1000000） |
| `is_default` | tinyint(1) | 否 |  | `0` | 是否为默认配置 |
| `is_active` | tinyint(1) | 否 |  | `1` | 是否启用 |
| `creator_id` | int | 是 |  |  | 创建人ID |
| `project_id` | int | 否 |  |  | 所属项目（为空时表示全局配置） |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_llm_conf_project_5be2e32e` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_llm_conf_project_5be2e32e` | `project_id` | `project.id` |

## mcp_config —— MCP 配置表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `user_id` | int | 是 |  |  | 用户ID（配置拥有者） |
| `name` | varchar(255) | 是 |  |  | 远程 MCP 服务器的名称 |
| `url` | varchar(2048) | 是 |  |  | 远程 MCP 服务器的 URL |
| `transport` | varchar(50) | 否 |  | `streamable-http` | MCP 服务器的传输协议，例如 'streamable-http' |
| `headers` | json | 否 |  |  | 可选的认证头，例如 {'Authorization': 'Bearer YOUR_TOKEN'} |
| `is_enabled` | tinyint(1) | 否 |  | `1` | 是否启用此远程 MCP 服务器 |
| `creator_id` | int | 是 |  |  | 创建人ID |
| `command` | varchar(500) | 否 |  |  | 启动命令（已废弃） |
| `args` | json | 否 |  |  | 命令参数（已废弃） |
| `env` | json | 否 |  |  | 环境变量（已废弃，使用headers） |
| `working_dir` | varchar(500) | 否 |  |  | 工作目录（已废弃） |
| `auto_start` | tinyint(1) | 否 |  | `0` | 自动启动（已废弃） |
| `timeout` | int | 否 |  | `30` | 超时时间(秒)（已废弃） |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## message —— 消息表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `role` | varchar(20) | 是 |  |  | 角色 |
| `content` | longtext | 是 |  |  | 消息内容 |
| `message_type` | varchar(50) | 否 |  | `text` | 消息类型 |
| `metadata` | json | 否 |  |  | 元数据 |
| `tokens_used` | int | 否 |  |  | 使用的令牌数 |
| `conversation_id` | int | 是 |  |  | 所属对话 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_message_conversa_a556956b` | `conversation_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_message_conversa_a556956b` | `conversation_id` | `conversation.id` |

## module_review_results —— 模块评审结果表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | varchar(36) | 是 | 主键 |  |  |
| `report_id` | varchar(36) | 是 |  |  | 所属报告ID |
| `module_id` | varchar(36) | 是 |  |  | 评审模块ID |
| `module_id_str` | varchar(100) | 否 |  |  | 模块ID字符串 |
| `module_name` | varchar(200) | 否 |  |  | 模块名称 |
| `specification_score` | int | 否 |  | `0` | 规格说明评分 |
| `clarity_score` | int | 否 |  | `0` | 清晰度评分 |
| `completeness_score` | int | 否 |  | `0` | 完整性评分 |
| `consistency_score` | int | 否 |  | `0` | 一致性评分 |
| `feasibility_score` | int | 否 |  | `0` | 可行性评分 |
| `overall_score` | int | 否 |  | `0` | 总体评分 |
| `module_rating` | varchar(20) | 否 |  |  | 模块评价 |
| `issues_count` | int | 否 |  | `0` | 问题数量 |
| `severity_score` | int | 否 |  | `0` | 严重程度评分 |
| `issues` | json | 否 |  |  | 问题列表 |
| `strengths` | json | 否 |  |  | 优点列表 |
| `weaknesses` | json | 否 |  |  | 不足列表 |
| `recommendations` | json | 否 |  |  | 改进建议列表 |
| `analysis_content` | longtext | 否 |  |  | 分析内容 |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `updated_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 更新时间 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## oauth_config —— OAuth 2.0配置表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(100) | 是 |  |  | 配置名称 |
| `provider` | varchar(50) | 是 |  |  | OAuth提供商 |
| `client_id` | varchar(255) | 是 |  |  | Client ID |
| `client_secret` | varchar(255) | 是 |  |  | Client Secret |
| `authorize_url` | varchar(500) | 是 |  |  | 授权端点URL |
| `token_url` | varchar(500) | 是 |  |  | Token端点URL |
| `user_info_url` | varchar(500) | 否 |  |  | 用户信息端点URL |
| `redirect_uri` | varchar(500) | 是 |  |  | 回调地址 |
| `scope` | varchar(200) | 否 |  | `openid profile email` | 授权范围 |
| `status` | varchar(20) | 否 |  | `enabled` | 状态: enabled/disabled |
| `is_default` | tinyint(1) | 否 |  | `0` | 是否为默认配置 |
| `user_id_field` | varchar(50) | 否 |  | `id` | 用户ID字段 |
| `username_field` | varchar(50) | 否 |  | `name` | 用户名字段 |
| `email_field` | varchar(50) | 否 |  | `email` | 邮箱字段 |
| `avatar_field` | varchar(50) | 否 |  | `avatar_url` | 头像字段 |
| `description` | longtext | 否 |  |  | 配置描述 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## orchestrator_tasks —— 智能编排任务表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `user_id` | int | 是 |  |  | 用户ID |
| `project_id` | int | 是 |  |  | 项目ID |
| `chat_session_id` | int | 否 |  |  | 关联对话会话ID |
| `requirement` | longtext | 是 |  |  | 需求描述 |
| `status` | varchar(30) | 否 |  | `pending` | 状态 |
| `execution_plan` | json | 否 |  |  | 执行计划 |
| `execution_history` | json | 否 |  |  | 执行历史 |
| `current_step` | int | 否 |  | `0` | 当前步骤 |
| `waiting_for` | varchar(50) | 否 |  |  | 等待对象 |
| `user_notes` | longtext | 否 |  |  | 用户备注 |
| `requirement_analysis` | json | 否 |  |  | 需求分析结果 |
| `knowledge_docs` | json | 否 |  |  | 检索的知识文档 |
| `testcases` | json | 否 |  |  | 生成的测试用例 |
| `execution_log` | json | 否 |  |  | 执行日志(旧) |
| `error_message` | longtext | 否 |  |  | 错误信息 |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `started_at` | datetime(6) | 否 |  |  | 开始时间 |
| `completed_at` | datetime(6) | 否 |  |  | 完成时间 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## playwright_execution —— Playwright执行记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `browser` | varchar(20) | 否 |  | `chromium` | 浏览器 |
| `headless` | tinyint(1) | 否 |  | `1` | 是否无头模式 |
| `status` | varchar(20) | 否 |  | `pending` | 状态: pending, running, success, failed |
| `start_time` | datetime(6) | 否 |  |  | 开始时间 |
| `end_time` | datetime(6) | 否 |  |  | 结束时间 |
| `duration` | double | 否 |  |  | 执行时长(秒) |
| `stdout` | longtext | 否 |  |  | 标准输出 |
| `stderr` | longtext | 否 |  |  | 错误输出 |
| `exit_code` | int | 否 |  |  | 退出码 |
| `error_message` | longtext | 否 |  |  | 错误信息 |
| `screenshots` | json | 否 |  |  | 截图列表 |
| `videos` | json | 否 |  |  | 视频列表 |
| `creator_id` | int | 是 |  |  | 创建人ID |
| `code_id` | int | 是 |  |  | 关联的测试代码 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_playwrig_playwrig_4ae7e5c6` | `code_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_playwrig_playwrig_4ae7e5c6` | `code_id` | `playwright_generated_code.id` |

## playwright_exploration_step —— Playwright探索步骤记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `step_number` | int | 是 |  |  | 步骤序号 |
| `action` | varchar(100) | 是 |  |  | 操作类型: navigate, click, type, snapshot等 |
| `description` | longtext | 是 |  |  | 步骤描述 |
| `url` | varchar(500) | 否 |  |  | 当前页面URL |
| `screenshot` | longtext | 否 |  |  | 截图的base64数据或文件路径 |
| `page_title` | varchar(200) | 否 |  |  | 页面标题 |
| `elements_found` | json | 否 |  |  | 发现的元素信息 |
| `timestamp` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 步骤时间 |
| `duration` | double | 否 |  |  | 步骤耗时(秒) |
| `status` | varchar(20) | 否 |  | `success` | 状态: success, failed, warning |
| `error_message` | longtext | 否 |  |  | 错误信息 |
| `plan_id` | int | 是 |  |  | 关联的测试计划 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_playwrig_playwrig_b6d21ab2` | `plan_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_playwrig_playwrig_b6d21ab2` | `plan_id` | `playwright_test_plan.id` |

## playwright_generated_code —— Playwright生成代码表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `framework` | varchar(50) | 否 |  | `playwright` | 测试框架 |
| `language` | varchar(20) | 否 |  | `typescript` | 编程语言 |
| `code` | longtext | 是 |  |  | 生成的测试代码 |
| `config_file` | longtext | 否 |  |  | 配置文件内容 |
| `status` | varchar(20) | 否 |  | `pending` | 状态: pending, generating, completed, failed |
| `llm_config_id` | int | 否 |  |  | 使用的LLM配置ID |
| `creator_id` | int | 是 |  |  | 创建人ID |
| `plan_id` | int | 是 |  |  | 关联的测试计划 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_playwrig_playwrig_6da0e691` | `plan_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_playwrig_playwrig_6da0e691` | `plan_id` | `playwright_test_plan.id` |

## playwright_heal_record —— Playwright自愈修复记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `status` | varchar(20) | 否 |  | `pending` | 状态: pending, healing, success, failed |
| `error_analysis` | longtext | 否 |  |  | 错误分析 |
| `fix_description` | longtext | 否 |  |  | 修复说明 |
| `changes` | json | 否 |  |  | 代码变更列表 |
| `llm_config_id` | int | 否 |  |  | 使用的LLM配置ID |
| `creator_id` | int | 是 |  |  | 创建人ID |
| `execution_id` | int | 是 |  |  | 关联的执行记录 |
| `fixed_code_id` | int | 否 |  |  | 修复后的代码 |
| `original_code_id` | int | 是 |  |  | 原始代码 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_playwrig_playwrig_9d256a58` | `original_code_id` | 否 |
| `fk_playwrig_playwrig_bec688f7` | `fixed_code_id` | 否 |
| `fk_playwrig_playwrig_ec1683f1` | `execution_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_playwrig_playwrig_9d256a58` | `original_code_id` | `playwright_generated_code.id` |
| `fk_playwrig_playwrig_bec688f7` | `fixed_code_id` | `playwright_generated_code.id` |
| `fk_playwrig_playwrig_ec1683f1` | `execution_id` | `playwright_execution.id` |

## playwright_test_plan —— Playwright测试计划表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `url` | varchar(500) | 是 |  |  | 应用URL |
| `max_depth` | int | 否 |  | `2` | 探索深度 |
| `timeout` | int | 否 |  | `60` | 超时时间(秒) |
| `status` | varchar(20) | 否 |  | `pending` | 状态: pending, exploring, completed, failed |
| `test_scenarios` | json | 否 |  |  | 测试场景列表 |
| `exploration_result` | json | 否 |  |  | 探索结果 |
| `llm_config_id` | int | 否 |  |  | 使用的LLM配置ID |
| `creator_id` | int | 是 |  |  | 创建人ID |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## project —— 项目表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 是 | 唯一 |  | 项目名称 |
| `description` | longtext | 否 |  |  | 项目描述 |
| `creator_id` | int | 是 |  |  | 创建人ID |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## project_credential —— 项目凭据表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `system_url` | varchar(500) | 否 |  |  | 系统地址 |
| `username` | varchar(100) | 否 |  |  | 用户名 |
| `password` | varchar(500) | 否 |  |  | 密码 |
| `user_role` | varchar(50) | 否 |  |  | 用户角色 |
| `project_id` | int | 是 |  |  | 所属项目 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_project__project_c8325d7b` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_project__project_c8325d7b` | `project_id` | `project.id` |

## project_member —— 项目成员表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `user_id` | int | 是 |  |  | 用户ID |
| `role` | varchar(20) | 否 |  | `tester` | 角色 |
| `project_id` | int | 是 |  |  | 所属项目 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |
| `uid_project_mem_project_5d4951` | `project_id`, `user_id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_project__project_c3c4e3b1` | `project_id` | `project.id` |

## prompt —— 提示词表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `user_id` | int | 是 |  |  | 创建用户ID |
| `name` | varchar(255) | 是 |  |  | 提示词名称 |
| `content` | longtext | 是 |  |  | 提示词内容 |
| `description` | longtext | 否 |  |  | 描述 |
| `prompt_type` | varchar(50) | 否 |  | `general` | 提示词类型 |
| `is_default` | tinyint(1) | 否 |  | `0` | 是否为默认提示词 |
| `is_active` | tinyint(1) | 否 |  | `1` | 是否启用 |
| `project_id` | int | 否 |  |  | 所属项目（NULL表示全局提示词） |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_prompt_project_e068ef83` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |
| `uid_prompt_user_id_5b2d6a` | `user_id`, `name`, `project_id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_prompt_project_e068ef83` | `project_id` | `project.id` |

## requirement_documents —— 需求文档表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | varchar(36) | 是 | 主键 |  |  |
| `project_id` | int | 是 |  |  | 项目ID |
| `title` | varchar(200) | 是 |  |  | 文档标题 |
| `description` | longtext | 否 |  |  | 文档描述 |
| `document_type` | varchar(50) | 是 |  |  | 文档类型 |
| `file_path` | varchar(500) | 否 |  |  | 文件路径 |
| `content` | longtext | 否 |  |  | 文档内容 |
| `status` | varchar(20) | 否 |  | `uploaded` | 状态 |
| `version` | varchar(20) | 否 |  | `1.0` | 版本号 |
| `is_latest` | tinyint(1) | 否 |  | `1` | 是否最新版本 |
| `parent_document_id` | varchar(36) | 否 |  |  | 父文档ID |
| `uploader_id` | int | 否 |  |  | 上传人ID |
| `uploaded_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 上传时间 |
| `updated_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 更新时间 |
| `word_count` | int | 否 |  | `0` | 字数 |
| `page_count` | int | 否 |  | `0` | 页数 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## requirement_modules —— 需求模块表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | varchar(36) | 是 | 主键 |  |  |
| `title` | varchar(200) | 是 |  |  | 模块名称 |
| `content` | longtext | 是 |  |  | 模块内容 |
| `start_page` | int | 否 |  |  | 起始页码 |
| `end_page` | int | 否 |  |  | 结束页码 |
| `start_position` | int | 否 |  |  | 起始位置 |
| `end_position` | int | 否 |  |  | 结束位置 |
| `order_num` | int | 否 |  | `0` | 排序 |
| `parent_module_id` | varchar(36) | 否 |  |  | 父模块ID |
| `is_auto_generated` | tinyint(1) | 否 |  | `1` | AI自动生成 |
| `confidence_score` | double | 否 |  |  | 置信度 |
| `ai_suggested_title` | varchar(200) | 否 |  |  | AI建议标题 |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `updated_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 更新时间 |
| `document_id` | varchar(36) | 是 |  |  | 所属文档 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_requirem_requirem_a3758c7d` | `document_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_requirem_requirem_a3758c7d` | `document_id` | `requirement_documents.id` |

## requirements —— 需求表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | varchar(36) | 是 | 主键 |  |  |
| `project_id` | int | 是 |  |  | 项目ID |
| `title` | varchar(200) | 是 |  |  | 需求标题 |
| `description` | longtext | 是 |  |  | 需求描述 |
| `type` | varchar(20) | 是 |  |  | 需求类型 |
| `priority` | varchar(10) | 否 |  | `medium` | 优先级 |
| `status` | varchar(20) | 否 |  | `draft` | 状态 |
| `stakeholders` | json | 否 |  |  | 相关人员列表 |
| `creator_id` | int | 否 |  |  | 创建人ID |
| `creator_name` | varchar(100) | 否 |  |  | 创建人姓名 |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `updated_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 更新时间 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## review_issues —— 评审问题表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | varchar(36) | 是 | 主键 |  |  |
| `report_id` | varchar(36) | 是 |  |  | 所属报告ID |
| `module_id` | varchar(36) | 否 |  |  | 相关模块ID |
| `issue_type` | varchar(20) | 是 |  |  | 问题类型 |
| `priority` | varchar(10) | 是 |  |  | 优先级 |
| `title` | varchar(200) | 是 |  |  | 问题标题 |
| `description` | longtext | 是 |  |  | 问题描述 |
| `suggestion` | longtext | 否 |  |  | 改进建议 |
| `location` | varchar(200) | 否 |  |  | 问题位置 |
| `page_number` | int | 否 |  |  | 页码 |
| `section` | varchar(100) | 否 |  |  | 章节 |
| `is_resolved` | tinyint(1) | 否 |  | `0` | 已解决 |
| `resolution_note` | longtext | 否 |  |  | 解决说明 |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `updated_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 更新时间 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## review_reports —— 评审报告表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | varchar(36) | 是 | 主键 |  |  |
| `document_id` | varchar(36) | 是 |  |  | 评审文档ID |
| `review_date` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 评审时间 |
| `reviewer` | varchar(100) | 否 |  | `AI需求评审助手` | 评审人 |
| `review_type` | varchar(20) | 否 |  | `comprehensive` | 评审类型 |
| `status` | varchar(20) | 否 |  | `pending` | 评审状态 |
| `overall_rating` | varchar(20) | 否 |  |  | 总体评价 |
| `completion_score` | int | 否 |  | `0` | 完整度评分 |
| `clarity_score` | int | 否 |  | `0` | 清晰度评分 |
| `consistency_score` | int | 否 |  | `0` | 一致性评分 |
| `completeness_score` | int | 否 |  | `0` | 完整性评分 |
| `testability_score` | int | 否 |  | `0` | 可测性评分 |
| `feasibility_score` | int | 否 |  | `0` | 可行性评分 |
| `total_issues` | int | 否 |  | `0` | 问题总数 |
| `high_priority_issues` | int | 否 |  | `0` | 高优先级问题 |
| `medium_priority_issues` | int | 否 |  | `0` | 中优先级问题 |
| `low_priority_issues` | int | 否 |  | `0` | 低优先级问题 |
| `summary` | longtext | 否 |  |  | 评审摘要 |
| `recommendations` | longtext | 否 |  |  | 改进建议 |
| `specialized_analyses` | json | 是 |  |  | 专项分析详情 |
| `created_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `updated_at` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 更新时间 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## script_execution —— 脚本执行记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `status` | varchar(20) | 否 |  | `pending` | 执行状态 |
| `started_at` | datetime(6) | 否 |  |  | 开始时间 |
| `completed_at` | datetime(6) | 否 |  |  | 完成时间 |
| `execution_time` | double | 否 |  |  | 执行耗时(秒) |
| `output` | longtext | 否 |  |  | 执行输出 |
| `error_message` | longtext | 否 |  |  | 错误信息 |
| `stack_trace` | longtext | 否 |  |  | 堆栈跟踪 |
| `screenshots` | json | 否 |  |  | 截图列表 |
| `videos` | json | 否 |  |  | 录屏列表 |
| `browser_type` | varchar(50) | 否 |  | `chromium` | 浏览器类型 |
| `viewport` | json | 否 |  |  | 视口大小 |
| `executor_id` | int | 是 |  |  | 执行人ID |
| `environment` | varchar(100) | 否 |  |  | 执行环境 |
| `parameters` | json | 否 |  |  | 执行参数 |
| `result_data` | json | 否 |  |  | 执行结果数据 |
| `execution_log` | longtext | 否 |  |  | 执行日志 |
| `celery_task_id` | varchar(255) | 否 |  |  | 任务ID |
| `script_id` | int | 是 |  |  | 执行的脚本 |
| `test_execution_id` | int | 否 |  |  | 测试执行 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_script_e_automati_f589a7a5` | `script_id` | 否 |
| `fk_script_e_test_exe_2c4d2497` | `test_execution_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_script_e_automati_f589a7a5` | `script_id` | `automation_script.id` |
| `fk_script_e_test_exe_2c4d2497` | `test_execution_id` | `test_execution.id` |

## swagger_diff_record —— yapi数据比对记录

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 是 |  |  | 比对标识，全量比对，或者具体分组的比对 |
| `is_changed` | int | 否 |  | `0` | 对比结果，1有改变，0没有改变 |
| `diff_summary` | longtext | 是 |  |  | 比对结果数据 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## system_error_record —— 系统错误记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `ip` | varchar(128) | 否 |  |  | 访问来源ip |
| `url` | varchar(128) | 否 |  |  | 请求地址 |
| `method` | varchar(10) | 否 |  |  | 请求方法 |
| `headers` | json | 是 |  |  | 头部参数 |
| `params` | json | 是 |  |  | 查询字符串参数 |
| `data_form` | json | 是 |  |  | form_data参数 |
| `data_json` | json | 是 |  |  | json参数 |
| `detail` | longtext | 否 |  |  | 错误详情 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## system_job_run_log —— 系统job执行记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `func_name` | varchar(255) | 是 |  |  | 执行方法 |
| `status` | int | 否 |  | `1` | 执行状态：0失败、1执行中、2执行成功 |
| `business_id` | int | 否 |  |  | 业务线id |
| `detail` | json | 是 |  |  | 执行结果数据 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## system_permission —— 权限表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` | 序号 |
| `name` | varchar(30) | 否 | 唯一 | `` | 权限名称 |
| `desc` | varchar(256) | 否 |  | `` | 权限备注 |
| `source_addr` | varchar(256) | 否 |  | `` | 权限路径 |
| `source_type` | varchar(256) | 否 |  | `api` | 权限类型， front前端, api后端 |
| `source_class` | varchar(256) | 否 |  | `api` | 权限分类, source_type为front时, menu菜单, button按钮;  source_type为api时, 为请求方法 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## system_role —— 角色表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` |  |
| `name` | varchar(30) | 否 | 唯一 | `` | 角色名称 |
| `extend_role` | json | 是 |  |  | 继承其他角色的权限 |
| `desc` | varchar(256) | 否 |  | `` | 权限备注 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## system_role_permissions —— 角色权限映射表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `role_id` | int | 是 |  |  | 角色id |
| `permission_id` | int | 是 |  |  | 权限id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## system_user —— 用户表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` |  |
| `sso_user_id` | varchar(50) | 否 |  | `` | 该用户在oss数据库的账号 |
| `account` | varchar(50) | 否 | 唯一 | `` | 账号 |
| `password` | varchar(128) | 否 |  | `` | 密码 |
| `name` | varchar(12) | 否 | 唯一 | `` | 姓名 |
| `phone_number` | varchar(12) | 否 |  | `` | 手机号 |
| `email` | varchar(255) | 否 |  | `` | 邮箱 |
| `email_password` | varchar(255) | 否 |  | `` | 邮箱密码 |
| `status` | varchar(7) | 否 |  | `enable` | 状态，enable/disable |
| `business_list` | json | 是 |  |  | 用户所在的业务线 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `account` | `account` | 是 |
| `idx_system_user_account_32beb3` | `account` | 否 |
| `idx_system_user_sso_use_e35641` | `sso_user_id` | 否 |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## system_user_roles —— 用户角色映射表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `user_id` | int | 是 |  |  | 用户id |
| `role_id` | int | 是 |  |  | 角色id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## test_case —— 测试用例表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 是 |  |  | 用例名称 |
| `description` | longtext | 否 |  |  | 用例描述 |
| `precondition` | longtext | 否 |  |  | 前置条件 |
| `level` | varchar(2) | 否 |  | `P2` | 用例等级 |
| `creator_id` | int | 是 |  |  | 创建人ID |
| `notes` | longtext | 否 |  |  | 备注 |
| `module_id` | int | 是 |  |  | 所属模块 |
| `project_id` | int | 是 |  |  | 所属项目 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_test_cas_project_7cacf1c8` | `project_id` | 否 |
| `fk_test_cas_test_cas_7aad9815` | `module_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_test_cas_project_7cacf1c8` | `project_id` | `project.id` |
| `fk_test_cas_test_cas_7aad9815` | `module_id` | `test_case_module.id` |

## test_case_module —— 测试用例模块表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(100) | 是 |  |  | 模块名称 |
| `description` | longtext | 否 |  |  | 模块描述 |
| `level` | smallint | 否 |  | `1` | 模块级别 |
| `creator_id` | int | 是 |  |  | 创建人ID |
| `parent_id` | int | 否 |  |  | 父模块 |
| `project_id` | int | 是 |  |  | 所属项目 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_test_cas_test_cas_faaac164` | `parent_id` | 否 |
| `PRIMARY` | `id` | 是 |
| `uid_test_case_m_project_499553` | `project_id`, `parent_id`, `name` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_test_cas_project_69408f2a` | `project_id` | `project.id` |
| `fk_test_cas_test_cas_faaac164` | `parent_id` | `test_case_module.id` |

## test_case_result —— 测试用例执行结果表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `status` | varchar(20) | 否 |  | `pending` | 执行状态 |
| `error_message` | longtext | 否 |  |  | 错误信息 |
| `stack_trace` | longtext | 否 |  |  | 堆栈跟踪 |
| `started_at` | datetime(6) | 否 |  |  | 开始时间 |
| `completed_at` | datetime(6) | 否 |  |  | 完成时间 |
| `execution_time` | double | 否 |  |  | 执行耗时(秒) |
| `mcp_session_id` | varchar(255) | 否 |  |  | MCP会话ID |
| `screenshots` | json | 否 |  |  | 截图列表 |
| `execution_log` | longtext | 否 |  |  | 执行日志 |
| `execution_id` | int | 是 |  |  | 测试执行 |
| `testcase_id` | int | 是 |  |  | 测试用例 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_test_cas_test_cas_9698463b` | `testcase_id` | 否 |
| `PRIMARY` | `id` | 是 |
| `uid_test_case_r_executi_9b8cc3` | `execution_id`, `testcase_id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_test_cas_test_cas_9698463b` | `testcase_id` | `test_case.id` |
| `fk_test_cas_test_exe_cbf44f61` | `execution_id` | `test_execution.id` |

## test_case_screenshot —— 测试用例截图表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `screenshot_path` | varchar(500) | 是 |  |  | 截图路径 |
| `title` | varchar(255) | 否 |  |  | 图片标题 |
| `description` | longtext | 否 |  |  | 图片描述 |
| `step_number` | int | 否 |  |  | 对应步骤 |
| `mcp_session_id` | varchar(255) | 否 |  |  | MCP会话ID |
| `page_url` | varchar(2000) | 否 |  |  | 页面URL |
| `uploader_id` | int | 是 |  |  | 上传人ID |
| `test_case_id` | int | 是 |  |  | 测试用例 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_test_cas_test_cas_f42c5af0` | `test_case_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_test_cas_test_cas_f42c5af0` | `test_case_id` | `test_case.id` |

## test_case_step —— 测试用例步骤表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `step_number` | int | 是 |  |  | 步骤编号 |
| `description` | longtext | 是 |  |  | 步骤描述 |
| `expected_result` | longtext | 是 |  |  | 预期结果 |
| `creator_id` | int | 是 |  |  | 创建人ID |
| `test_case_id` | int | 是 |  |  | 所属用例 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |
| `uid_test_case_s_test_ca_94bbd9` | `test_case_id`, `step_number` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_test_cas_test_cas_3104fa78` | `test_case_id` | `test_case.id` |

## test_execution —— 测试执行记录表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `status` | varchar(20) | 否 |  | `pending` | 执行状态 |
| `executor_id` | int | 是 |  |  | 执行人ID |
| `started_at` | datetime(6) | 否 |  |  | 开始时间 |
| `completed_at` | datetime(6) | 否 |  |  | 完成时间 |
| `total_count` | int | 否 |  | `0` | 总用例数 |
| `passed_count` | int | 否 |  | `0` | 通过数 |
| `failed_count` | int | 否 |  | `0` | 失败数 |
| `skipped_count` | int | 否 |  | `0` | 跳过数 |
| `error_count` | int | 否 |  | `0` | 错误数 |
| `celery_task_id` | varchar(255) | 否 |  |  | 任务ID |
| `suite_id` | int | 是 |  |  | 测试套件 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `fk_test_exe_test_sui_951839ef` | `suite_id` | 否 |
| `PRIMARY` | `id` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_test_exe_test_sui_951839ef` | `suite_id` | `test_suite.id` |

## test_suite —— 测试套件表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 是 |  |  | 套件名称 |
| `description` | longtext | 否 |  |  | 套件描述 |
| `creator_id` | int | 是 |  |  | 创建人ID |
| `max_concurrent_tasks` | smallint | 否 |  | `1` | 最大并发数 |
| `timeout` | int | 否 |  | `300` | 超时时间(秒) |
| `status` | varchar(20) | 否 |  | `active` | 状态: active/inactive |
| `last_execution` | datetime(6) | 否 |  |  | 最后执行时间 |
| `project_id` | int | 是 |  |  | 所属项目 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |
| `uid_test_suite_project_2ba138` | `project_id`, `name` | 是 |

| 外键 | 本表字段 | 指向 |
| --- | --- | --- |
| `fk_test_sui_project_8addcede` | `project_id` | `project.id` |

## test_suite_script —— 测试套件脚本关联表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建用户ID |
| `update_user` | int | 否 |  | `1` | 更新用户ID |
| `suite_id` | int | 是 |  |  | 测试套件ID |
| `script_id` | int | 是 |  |  | 脚本ID |
| `execution_order` | int | 否 |  | `0` | 执行顺序 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## test_work_bug_track —— 生产Bug跟踪表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` |  |
| `business_id` | int | 是 |  |  | 业务线id |
| `name` | varchar(255) | 否 |  | `` | bug名 |
| `detail` | longtext | 否 |  |  | bug详情 |
| `iteration` | varchar(128) | 否 |  | `` | 迭代 |
| `bug_from` | varchar(128) | 否 |  | `` | 缺陷来源 |
| `trigger_time` | varchar(128) | 否 |  | `` | 发现时间 |
| `manager` | int | 否 |  |  | 跟进负责人 |
| `reason` | varchar(128) | 否 |  | `` | 原因 |
| `solution` | varchar(128) | 否 |  | `` | 解决方案 |
| `status` | varchar(64) | 否 |  | `todo` | bug状态，todo：待解决、doing：解决中、done：已解决 |
| `replay` | int | 否 |  | `0` | 是否复盘，0：未复盘，1：已复盘 |
| `conclusion` | longtext | 否 |  |  | 复盘结论 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_test_work_b_busines_28fe3f` | `business_id` | 否 |
| `PRIMARY` | `id` | 是 |

## test_work_env —— 环境地址、账号管理表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` |  |
| `business` | int | 否 |  |  | 业务线，如果数据是账号，可以为空 |
| `name` | varchar(255) | 是 |  |  | 资源名 |
| `source_type` | varchar(255) | 是 |  |  | 资源类型，账号:account、地址:addr |
| `value` | varchar(255) | 是 |  |  | 数据值 |
| `password` | varchar(255) | 否 |  | `` | 登录密码 |
| `desc` | longtext | 否 |  |  | 备注 |
| `parent` | int | 否 |  |  | 当source_type为账号时，所属资源id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## test_work_kym —— KYM分析表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `project` | varchar(255) | 是 | 唯一 |  | 服务名 |
| `kym` | json | 是 |  |  | kym分析 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |
| `project` | `project` | 是 |

## test_work_todo —— 待处理任务

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `num` | int | 否 |  | `0` |  |
| `status` | varchar(7) | 否 |  | `todo` | 状态 |
| `title` | varchar(512) | 否 |  | `` | 任务title |
| `done_user` | int | 否 |  |  | 完成人 |
| `done_time` | datetime(6) | 否 |  |  | 完成时间 |
| `detail` | varchar(2048) | 否 |  | `` | 任务详情 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `PRIMARY` | `id` | 是 |

## web_ui_test_case —— web-ui测试用例表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 用例名称 |
| `num` | int | 否 |  |  | 用例序号 |
| `desc` | longtext | 否 |  |  | 用例描述 |
| `status` | int | 否 |  | `0` | 用例状态，0未调试-不执行，1调试通过-要执行，2调试通过-不执行，3调试不通过-不执行，默认未调试-不执行 |
| `run_times` | int | 否 |  | `1` | 执行次数，默认执行1次 |
| `variables` | json | 是 |  |  | 用例级的公共参数 |
| `output` | json | 是 |  |  | 用例出参（步骤提取的数据） |
| `skip_if` | json | 是 |  |  | 是否跳过的判断条件 |
| `suite_id` | int | 是 |  |  | 所属的用例集id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_suite_i_14c56f` | `suite_id` | 否 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_case_suite —— web-ui测试用例集表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 用例集名称 |
| `num` | int | 否 |  |  | 用例集在对应服务下的序号 |
| `suite_type` | varchar(9) | 否 |  | `base` | 用例集类型，base: 基础用例集，api: 单接口用例集，process: 流程用例集，assist: 造数据用例集 |
| `parent` | int | 否 |  |  | 父级用例集id |
| `project_id` | int | 是 |  |  | 所属的服务id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_project_d77237` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_element —— web-ui测试元素表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 接口名称 |
| `num` | int | 否 |  |  | 接口序号 |
| `desc` | varchar(255) | 否 |  | `` | 接口描述 |
| `project_id` | int | 否 |  |  | 所属的服务id |
| `module_id` | int | 否 |  |  | 所属的模块id |
| `by` | varchar(255) | 否 |  |  | 定位方式 |
| `element` | longtext | 否 |  |  | 元素值 |
| `wait_time_out` | int | 否 |  | `5` | 等待元素出现的时间，默认5秒 |
| `page_id` | int | 否 |  |  | 所属的页面id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_module__f0ae5f` | `module_id` | 否 |
| `idx_web_ui_test_page_id_67843d` | `page_id` | 否 |
| `idx_web_ui_test_project_401a31` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_module —— web-ui测试模块表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 模块名 |
| `num` | int | 否 |  |  | 模块在对应服务下的序号 |
| `parent` | int | 否 |  |  | 父级模块id |
| `project_id` | int | 是 |  |  | 所属的服务id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_project_f293bd` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_page —— web-ui测试页面表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 接口名称 |
| `num` | int | 否 |  |  | 接口序号 |
| `desc` | varchar(255) | 否 |  | `` | 接口描述 |
| `project_id` | int | 否 |  |  | 所属的服务id |
| `module_id` | int | 否 |  |  | 所属的模块id |
| `addr` | varchar(255) | 否 |  | `` | 地址 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_module__f30139` | `module_id` | 否 |
| `idx_web_ui_test_project_f3cb75` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_project —— web-ui测试项目表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 是 | 唯一 |  | 服务名称 |
| `manager` | int | 是 |  |  | 服务的管理员id |
| `script_list` | json | 是 |  |  | 引用的脚本文件 |
| `num` | int | 否 |  |  | 当前服务的序号 |
| `business_id` | int | 是 |  |  | 所属业务线 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_busines_df5d93` | `business_id` | 否 |
| `name` | `name` | 是 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_project_env —— web-ui测试项目环境表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `host` | varchar(255) | 否 |  | `http://localhost:8018` | 服务地址 |
| `variables` | json | 是 |  |  | 服务的公共变量 |
| `env_id` | int | 是 |  |  | 对应环境id |
| `project_id` | int | 是 |  |  | 所属的服务id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_env_id_c37653` | `env_id` | 否 |
| `idx_web_ui_test_project_6676cb` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_report —— web-ui测试报告表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(128) | 是 |  |  | 测试报告名称 |
| `is_passed` | int | 否 |  | `1` | 是否全部通过，1全部通过，0有报错 |
| `run_type` | varchar(16) | 否 |  | `task` | 报告类型，task/suite/case/api |
| `status` | int | 否 |  | `1` | 当前节点是否执行完毕，1执行中，2执行完毕 |
| `retry_count` | int | 否 |  | `0` | 已经执行重试的次数 |
| `env` | varchar(128) | 否 |  | `test` | 运行环境 |
| `temp_variables` | json | 否 |  |  | 临时参数 |
| `process` | int | 否 |  | `1` | 进度节点, 1: 解析数据、2: 执行测试、3: 写入报告 |
| `trigger_type` | varchar(8) | 否 |  | `page` | 触发类型，pipeline:流水线、page:页面、cron:定时任务 |
| `batch_id` | varchar(128) | 是 |  |  | 运行批次id，用于查询报告 |
| `trigger_id` | json | 否 |  |  |  |
| `project_id` | int | 是 |  |  | 所属的服务id |
| `summary` | json | 是 |  |  | 报告的统计 |
| `notified` | tinyint(1) | 否 |  | `0` | 标记是否已经通知过了 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_batch_i_9e643d` | `batch_id` | 否 |
| `idx_web_ui_test_project_2ed61c` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_report_case —— web-ui测试报告的用例数据表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(128) | 是 |  |  | 测试用例名称 |
| `case_id` | int | 否 |  | `0` | 执行记录对应的用例id, 如果是运行接口，则为null |
| `suite_id` | int | 否 |  | `0` | 执行用例所在的用例集id |
| `report_id` | int | 是 |  |  | 测试报告id |
| `result` | varchar(128) | 否 |  | `waite` | 步骤测试结果，waite：等待执行、running：执行中、fail：执行不通过、success：执行通过、skip：跳过、error：报错 |
| `case_data` | json | 是 |  |  | 用例的数据 |
| `summary` | json | 是 |  |  | 用例的报告统计 |
| `retry_count` | int | 否 |  | `0` | 已经执行重试的次数 |
| `error_msg` | longtext | 是 |  |  | 用例错误信息 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_case_id_d0ca75` | `case_id` | 否 |
| `idx_web_ui_test_report__382ac7` | `report_id` | 否 |
| `idx_web_ui_test_suite_i_bacf58` | `suite_id` | 否 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_report_step —— web-ui测试报告的步骤数据表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(128) | 是 |  |  | 测试步骤名称 |
| `case_id` | int | 否 |  |  | 步骤所在的用例id |
| `step_id` | int | 否 |  |  | 步骤id |
| `element_id` | int | 是 |  |  | 步骤对应的元素id |
| `report_case_id` | int | 是 |  |  | 用例数据id |
| `report_id` | int | 是 |  |  | 测试报告id |
| `status` | varchar(6) | 否 |  | `resume` | resume:放行、pause:暂停、stop:中断 |
| `process` | varchar(16) | 否 |  | `waite` | 步骤执行进度，waite：等待解析、parse: 解析数据、before：前置条件、after：后置条件、run：执行测试、extract：数据提取、validate：断言 |
| `result` | varchar(16) | 否 |  | `waite` | 步骤测试结果，waite：等待执行、running：执行中、fail：执行不通过、success：执行通过、skip：跳过、error：报错 |
| `step_data` | json | 是 |  |  | 步骤的数据 |
| `summary` | json | 是 |  |  | 步骤的统计 |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_case_id_a4514f` | `case_id` | 否 |
| `idx_web_ui_test_report__c352f2` | `report_case_id` | 否 |
| `idx_web_ui_test_report__f1545b` | `report_id` | 否 |
| `idx_web_ui_test_step_id_76cf05` | `step_id` | 否 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_step —— web-ui测试步骤表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 步骤名称 |
| `desc` | longtext | 是 |  |  | 步骤描述 |
| `num` | int | 否 |  | `0` | 步骤序号，执行顺序按此序号来 |
| `status` | varchar(7) | 否 |  | `enable` | 是否执行此步骤, enable/disable，默认enable |
| `run_times` | int | 否 |  | `1` | 执行次数，默认执行1次 |
| `up_func` | json | 是 |  |  | 步骤执行前的函数 |
| `down_func` | json | 是 |  |  | 步骤执行后的函数 |
| `skip_if` | json | 是 |  |  | 是否跳过的判断条件 |
| `skip_on_fail` | int | 否 |  | `1` | 当用例有失败的步骤时，是否跳过此步骤，1跳过，0不跳过，默认跳过 |
| `data_driver` | json | 是 |  |  | 数据驱动，若此字段有值，则走数据驱动的解析 |
| `quote_case` | int | 否 |  |  | 引用用例的id |
| `case_id` | int | 是 |  |  | 步骤所在的用例的id |
| `wait_time_out` | int | 否 |  | `10` | 等待元素出现的时间，默认10秒 |
| `execute_type` | varchar(255) | 是 |  |  | 执行方式 |
| `send_keys` | varchar(255) | 是 |  |  | 要输入的文本内容 |
| `extracts` | json | 是 |  |  | 提取信息 |
| `validates` | json | 是 |  |  | 断言信息 |
| `element_id` | int | 否 |  |  | 步骤所引用的元素的id |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_case_id_0b9f14` | `case_id` | 否 |
| `PRIMARY` | `id` | 是 |

## web_ui_test_task —— web-ui测试任务表

| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | int | 是 | 主键、自增 |  |  |
| `create_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 创建时间 |
| `update_time` | datetime(6) | 否 |  | `CURRENT_TIMESTAMP(6)` | 最后修改时间 |
| `create_user` | int | 否 |  | `1` | 创建人 |
| `update_user` | int | 否 |  | `1` | 最后修改人 |
| `name` | varchar(255) | 否 |  | `` | 任务名称 |
| `num` | int | 否 |  | `0` | 任务序号 |
| `env_list` | json | 是 |  |  | 运行环境 |
| `case_ids` | json | 是 |  |  | 用例id |
| `task_type` | varchar(16) | 否 |  | `cron` | 定时类型 |
| `cron` | varchar(128) | 是 |  |  | cron表达式 |
| `is_send` | varchar(10) | 否 |  | `not_send` | 是否发送报告，not_send/always/on_fail |
| `merge_notify` | int | 否 |  | `0` | 多个环境时，是否合并通知（只通知一次），默认不合并，0不合并、1合并 |
| `receive_type` | varchar(11) | 否 |  | `ding_ding` | 接收测试报告类型: ding_ding、we_chat、email |
| `webhook_list` | json | 是 |  |  | 机器人地址 |
| `email_server` | varchar(255) | 否 |  |  | 发件邮箱服务器 |
| `email_from` | int | 否 |  |  | 发件人邮箱 |
| `email_to` | json | 是 |  |  | 收件人邮箱 |
| `skip_holiday` | int | 否 |  | `1` | 是否跳过节假日、调休日 |
| `status` | varchar(7) | 否 |  | `disable` | 任务的启用状态, enable/disable，默认disable |
| `is_async` | int | 否 |  | `0` | 任务的运行机制，0：串行，1：并行，默认0 |
| `skip_on_fail` | int | 否 |  | `0` | 用例不通过时，是否跳过后续用例的执行，0：不跳过，1：跳过，默认0 |
| `suite_ids` | json | 是 |  |  | 用例集id |
| `call_back` | json | 是 |  |  | 回调给流水线 |
| `project_id` | int | 是 |  |  | 所属的服务id |
| `push_hit` | int | 否 |  | `1` | 任务不通过时，是否自动记录，0：不记录，1：记录，默认1 |
| `conf` | json | 是 |  |  | 运行配置，ui存浏览器，app存运行服务器、手机、是否重置APP |

主键：`id`

| 索引 | 字段 | 唯一 |
| --- | --- | --- |
| `idx_web_ui_test_project_89158d` | `project_id` | 否 |
| `PRIMARY` | `id` | 是 |
