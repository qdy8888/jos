# jos 动态并发调度模块

Python 3.12 / Ubuntu 24.04，仅标准库。此版本是独立模块，未接入生产注册后台。

## 运行验证

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 scripts/simulate.py
python3 scripts/resource_probe.py --service YOUR_EXISTING.service > resource-report.json
```

资源脚本只读 `/proc`、cgroup 和 `systemctl show`，不修改服务、不重启进程、不访问订单。报告不包含命令行、环境变量和凭据。可加 `--task-pid-file worker-pids.json`，文件为权威任务系统提供的 PID 数组；未提供时任务数量为 unknown，不能拿进程总数充当注册任务数。需要现有业务数据库的只读查询才能准确统计排队、远程与运行任务。报告可能包含设备无关的进程名称、PID，分享前可检查。

## 调度规则

- 任意数量设备可逐条提交，持久化 SQLite WAL；磁盘容量是实际队列边界。
- 相同请求 ID 重复提交幂等；同一设备仅允许一个未完成任务。设备完成后才接受新的请求 ID。
- 每次最多启动一台，默认间隔五秒；不创建每台设备一个排队进程。
- 可用余量取主机 `MemAvailable` 与当前 cgroup、所有父级 `memory.max/high - memory.current` 的最小值。不把 8GB 配额当可用内存。不依赖 swap 扩大并发。
- admission = min(配置并发上限 - 活动数, floor((余量 - 保留内存)/任务预算) - 活动数, 单批上限)。活动和状态不明任务均额外预留一份任务增长预算，因此偏保守。
- 内存不足或监测失败仅停止新启动，不停止现有工作；连续三次健康采样才恢复。一次大批量不会绕过预算。
- 持久化 starting 意图后才调用外部适配器。适配器失败暂停该任务；核对异常暂停全部新启动。
- 队列文件及 `.dispatcher.lock` 必须在同一本机持久化目录。进程级锁禁止多个 dispatcher。SQLite 不应放 NFS。多主机部署必须改用集中数据库及分布式 fencing。
- 核心不购买号码、不修改账号、不取消订单、不杀工作进程；重试沿用同一 task ID/context。

## 最小接入协议（必须先审查生产代码）

可信本地适配器工厂返回对象：

`reconcile(task)` 返回 `running`（已有任务）、`done`（已完成）、`blocked`（断线/未知/待人工确认）、`absent`（权威确认无执行者、原订单及业务状态已核对并可安全恢复）。超时、PID 消失、设备断线都不能独自作为 absent 证据。PID 必须结合启动时间或外部稳定执行 ID，避免 PID 复用。

`ensure_started(task)` 必须以 task ID 实现外部持久幂等和设备互斥，解析 `task['context']` 中的原订单 ID/业务检查点，并恢复原任务。必须先查订单再决定下一步。订单创建应使用供应商幂等键；若供应商不支持，则需业务 outbox/核对流程，结果不明确时返回 blocked，禁止再次购买。调度模块本身不能保证任意外部系统 exactly-once。

批量入口仅调用 `Scheduler.submit(id, device, context)`，不再直接 spawn。为保持单 dispatcher，Web 服务应通过同进程队列/RPC交给该实例提交，或新增只写队列连接（不要在每个 Web worker 构造 Scheduler）。当前提交接口用于单 dispatcher 集成，未提供公开 HTTP API。

```bash
python3 -m jos_scheduler --db /var/lib/jos-scheduler/queue.db \
  --adapter your_existing_backend.scheduler_adapter:create --max-running 1
```

示例模块名是接入点占位符，不是现成的生产注册实现。调度器与工作进程应位于同一受监测 cgroup；若生产采用独立 unit，必须改为读取所有目标工作服务和共同父级资源，核对已有在途任务后才启用。停止调度器应仅停止调度进程；不要使用会同时杀工作进程的 systemd KillMode/control group 结构。此版本不提供未经验证的生产 unit，也不设置 MemoryMax=8G。

## 并发建议与待验证项

默认 commissioning 上限为 1，不是对真实任务安全性的保证；缺少实测时不应启用生产启动。768MiB 任务预算、1536MiB 保留内存都为暂定值。

先在生产只读采样：真实 MemTotal/MemAvailable、服务与祖先 MemoryMax/MemoryHigh、其他服务占用、memory.events 的 OOM/oom_kill 增量、完整注册周期的进程树/cgroup 峰值、业务运行任务数。随后在可控单台验证中测量任务峰值和增长，按峰值加余量设置 task_bytes，保留 OS/数据库/后台和突发余量，再逐步提高 commissioning ceiling。CPU、上游限流、磁盘和连接数可能进一步限制并发。

尚未验证：生产服务是否 8GB、主机是否真有 8GB、真实单任务峰值、现有业务任务/订单恢复语义、生产源码与服务配置。没有生产访问或报告，本次未执行生产资源检测、部署或调整内存。

## 仿真结果

见 simulation-results.json：10/30/50/100 台全部提交并完成，模拟 8GiB 主机、其他占用 2GiB、每任务 512MiB、预算 768MiB、预留 1536MiB，测试上限设置 8，实际峰值并发为 4。每组注入低内存、断线、一次已核实工作进程退出和调度器重启，保留原任务上下文。模型内存为算术预算，不分配真实 8GiB，也不代表生产注册峰值。注入压力时可用余量为 256MiB；调度器不杀任务并暂停新启动。8 个单元测试另覆盖重复提交、单实例、回执丢失、恢复和监测失败。
