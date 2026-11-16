# 采集工作台 · 模拟门户 · 自动执行器

三个本地组件，全部使用虚构数据，只依赖 Python 3.11 标准库（无 Flask、无 pytest）：

| 组件 | 入口 | 默认地址 |
| --- | --- | --- |
| 模拟外部门户 | `python3 -m app.portal.main` | http://127.0.0.1:8801 |
| 采集工作台 | `python3 -m app.workbench.main` | http://127.0.0.1:8802 |
| 自动执行器 | `python3 -m app.workbench.executor` | 命令行进程 |

门户与工作台共用同一个 SQLite 文件（默认 `data/portal.db`），但**表结构互不相连**：
`portal_*` 表不知道工作台的存在，正如真实外部门户不会知道采集方是谁。这种隔离是刻意的——
工作台只能通过 HTTP 与门户交互，因此“回执丢失”“人工改单”才是真实存在的故障。

```
┌───────────────┐   HTTP（表单/API）   ┌────────────────┐
│ 模拟外部门户   │◄────────────────────►│ 自动执行器进程  │
│ 分步录入/回执  │                      │ 租约 + 逐字段核对│
└───────▲───────┘                      └───────┬────────┘
        │ 浏览器                                │ 同一个 SQLite
        │                                       ▼
┌───────┴───────┐                      ┌────────────────┐
│ 人工（登录门户）│                      │ 采集工作台 Web  │
│ 可能改同一条记录│                      │ 队列/冲突/接管  │
└───────────────┘                      └────────────────┘
```

## 快速开始

```bash
# 1. 门户
python3 -m app.portal.main --port 8801

# 2. 工作台（另开终端）
python3 -m app.workbench.main --port 8802 --portal http://127.0.0.1:8801

# 3. 在 http://127.0.0.1:8802/queue 上传 examples/demo_batch.csv，或：
python3 -m app.workbench.executor --portal http://127.0.0.1:8801 run --batch 1
```

一键演练（自动起停门户、注入故障、打印结论）：

```bash
python3 -m app.drill all          # 回执丢失 / 超时 / 他人改单 / 执行器崩溃
python3 -m app.drill drop_ack
python3 -m app.drill timeout
python3 -m app.drill manual_edit
python3 -m app.drill crash
python3 -m app.drill status
```

测试（标准库 `unittest`，每个用例用临时数据库和临时端口）：

```bash
python3 -m unittest discover -s tests -t .
```

## 业务流程

1. **导入 CSV**（`/queue` 或 `POST /api/import`）。列名为 `record_key,full_name,phone,address,service_level,note`，
   也接受中文表头 `业务编号/姓名/手机号/地址/服务档位/备注`；编码先试 UTF-8，失败再试 GB18030。
   导入时逐行校验，并按业务键读取门户当前值作为**提交前版本（基准快照）**；
   与本批次或未完成批次重复的业务键会被拒绝，避免同一业务键并发提交。
2. **自动执行**。执行器为每一行申请数据库租约（`wb_executors`），按
   `打开新表单 → 取门户版本历史 → 预检 → 暂存 → 提交` 推进，每个网络步骤之间重新确认租约。
3. **回执丢失/超时**。写请求可能已在门户落地，因此**绝不盲目重试**，也**绝不直接记成功**：
   行进入 `needs_reconcile`，工作台读取门户记录与版本历史，用本次表单令牌（`form_token`）
   在历史里证明“这次写入是否落地”，再给出结论。
4. **冲突队列**。若他人已修改同一记录，该行进入 `/conflicts`，保留：
   - 提交前版本（基准）
   - 本次目标
   - 门户当前值
   - 逐字段判定：`本次已生效` / `本次未生效` / `他人修改` / `与人工修改冲突` / `与目标一致但非本次写入`
   每个字段的归属来自门户版本历史：只有历史中带有本次表单令牌的变更才算“我们写的”；
   双方都写过同一字段时，**最后一次写入者**说了算。
5. **人工接管**。`/row/<id>` 可接管单条：撤销该行全部执行器租约；被撤销的执行器在下一步
   检查时立即放弃，不会覆盖人工更正。接管后可手工提交（门户版本校验兜底）、采纳门户值、
   放回队列或关闭。
6. **恢复中断**。执行器崩溃后行停在 `running`，租约靠心跳与 PID 判定死亡；
   `recover` / 页面“恢复中断”会把尝试标记为 `abandoned` 并逐行核对，
   而不是假定失败或成功。
7. **最终核对**。`/verify` 读取门户当前记录，与目标逐字段比较并显示门户最终版本，
   可直接“在门户打开”人工确认。

## 行状态

| 状态 | 含义 |
| --- | --- |
| `pending` / `retry_wait` | 可自动执行 / 已确认未写入，可安全补交 |
| `running` | 正在执行或执行器已中断，等待接管/恢复 |
| `needs_reconcile` | 提交结果未知，必须与门户核对后才能继续 |
| `applied` / `resolved` / `noop` | 已生效 / 人工已处理 / 无需变更 |
| `conflict` | 冲突队列，自动执行器不会再碰 |
| `invalid` / `missing` / `failed` | 校验失败 / 门户无此记录 / 其他确定失败 |

## 故障注入

仅对 User-Agent 以 `collector-executor` 开头的请求生效，浏览器手工录入永远不会被注入故障。
规则存于 `wb_settings`，可在门户 `/drill`、工作台 `/settings` 或 CLI `--faults` 修改：

```
匹配:动作:次数[:阶段]
```

- 匹配：`HK-1001`、`*`、`row:3`
- 动作：`ok`、`timeout`（写已提交但响应迟到）、`drop_ack`（写已提交但连接被切断）、
  `reject`（422 校验失败，不写）、`lock`（409 已锁定，不写）、`5xx`（不写）
- 阶段：`submit`（默认）、`open`、`all`
- 次数：正整数或 `inf`

默认规则：

```
HK-1001:drop_ack:1
HK-1002:reject:1
HK-1003:timeout:1
*:ok
```

```bash
python3 -m app.workbench.executor --portal http://127.0.0.1:8801 \
  run --batch 1 --faults "HK-1001:drop_ack:1\n*:ok"
python3 -m app.workbench.executor --portal http://127.0.0.1:8801 \
  run --batch 1 --kill-after 0.6 --timeout 5   # 模拟执行器崩溃
python3 -m app.workbench.executor --portal http://127.0.0.1:8801 recover
```

## 安全不变量（为什么不会重复/覆盖）

- 每次重试都开**新表单**，不复用旧会话；旧表单提交会被门户以 `already_submitted` 拒绝。
- 提交前把门户当前值与基准快照逐字段比较，出现 `他人修改`/`冲突` 即转入冲突队列，绝不自动提交。
- 提交结果未知时先核对：用表单令牌在门户版本历史中证明写入是否属于本次提交。
  当前值与目标一致但历史里没有本次写入 → `与目标一致但非本次写入`，不重交、不冒领。
- 门户提交带版本号：表单基于版本 N，若记录已到 N+1 则 409，人工与执行器不会互相覆盖。
- 租约在每次网络步骤之间重新确认；接管会立即撤销租约，执行器的提交检查因此失效。
- 一个业务键同时只能有一个未完成的采集行；同批次内重复键直接判为无效。

## 目录

```
app/
  common.py              字段/校验/CSV/逐字段比对（含归属判定）
  storage.py             SQLite 结构（portal_* 与 wb_* 两套）
  web.py                 极简路由与 HTTP 服务
  portal/
    server.py            门户页面 + 执行器 JSON API + 故障注入
    faults.py            规则解析与计数（存数据库，跨进程一致）
    seed.py              5 条虚构记录
    main.py              门户 CLI
  workbench/
    engine.py            导入、租约、执行、核对、冲突、接管、恢复
    runner.py            批量/单行执行循环与作业记录
    portal_client.py     门户 HTTP 客户端（区分“确定失败/结果未知”）
    executor.py          独立进程命令行入口
    views.py             页面只读模型
    server.py            工作台页面与操作
    main.py              工作台 CLI
  drill.py               一键演练脚本
examples/demo_batch.csv  示例批次
tests/                   unittest 测试（临时库、临时端口）
```

所有记录、姓名、电话、地址均为虚构，仅用于本地演练。
