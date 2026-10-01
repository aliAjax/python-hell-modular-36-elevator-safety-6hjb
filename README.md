# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `POST /api/offline-records`：合并离线登记批次，请求体为`{"records":[...]}`；按批次内容和提交人做幂等。
- `GET /api/sync/<batch_id>`：查看同步批次和每条记录的检查点状态。
- `POST /api/sync/<batch_id>/resume`：人工续传，会重试阻塞和冲突项，已确认步骤保留不重放。
- `GET /api/recovery`：恢复运行视图，聚合未完成批次及每台设备的检验、整改、救援阻塞原因，可用`?equipment_id=`过滤。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

## 离线合并与恢复（当前修订）

维保队在地下车库断网登记，回网后通过`/api/offline-records`补传。每条记录必须带`type`和`record_id`，可选`role`/`user_id`声明登记人角色。支持的记录类型：

- `equipment_status`：设备停机/恢复，`action`为`suspend`、`out_of_service`或`return_to_service`，`revision`为登记时设备版本；版本过期返回冲突并带回当前修订。
- `alarm`：困人报警，按（设备、故障代码）自然键去重。已解除或已关闭的报警不会被旧包重新挂回设备，重复记录标记为`skipped`。
- `rescue_job`：救援任务，按`dedupe_key`去重；`phase`为`on_site`或`completed`时自动推进到场/完成状态；若报警尚未派发，由系统对账动作自动派发。
- `permit`：恢复许可，`phase`为`granted`时尝试校验并放行；检验未通过、整改未关闭或救援未结束时记录全部阻塞原因，许可保留在待审状态，不发放。

每条记录在一个数据库事务内与检查点同时落盘。写入失败只回滚当前项，已确认步骤保留；服务重启时自动扫描`pending`批次，仅重放`pending`/`failed`项。阻塞（`blocked`）和版本冲突（`conflict`）属于已确认结论，需人工处理后调用`resume`续传。

设备状态更新（停机/停用）在同一事务内立即级联失效所有未终结许可（`blocked`/`pending_review`/`granted`→`revoked`），审计记录标记`auto: true`；恢复运行时已发放许可自动`completed`。

两名调度员同时派发同一报警时，先到者按乐观版本号获胜，后到者收到409，响应中`current`为报警当前修订、`related.rescue_team`和`related.rescue_job`为最新获胜救援队。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验、未关闭整改和未结束救援共同限制，阻塞原因结构化返回。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
