# 数据库部署闭环设计

## 目标

让本项目在本机通过 Docker Compose 从空环境一键启动 PostgreSQL、执行数据库迁移并启动应用服务，同时能够明确判断部署成功或失败，并验证普通重启后数据仍然存在。

本次只覆盖本机 Compose 部署。生产环境的托管数据库、TLS、外部备份、密钥托管、监控告警和高可用不在范围内。现有单元测试继续使用 SQLite，以保持测试速度和兼容性。

## 架构

Compose 增加固定主版本的 `postgres` 服务和独立命名卷。数据库通过 `pg_isready` 暴露健康状态。一次性 `migrate` 服务复用后端镜像，在数据库健康后执行 `alembic upgrade head`；只有迁移成功退出后，`backend` 和 `scheduler` 才允许启动。`frontend` 继续依赖 `backend`。

`backend`、`scheduler` 和 `migrate` 使用同一个 PostgreSQL `DATABASE_URL`。Alembic 的运行时配置从 `DATABASE_URL` 读取连接地址，不依赖 `alembic.ini` 中仅供本地默认使用的 SQLite 地址。后端镜像加入 SQLAlchemy 所需的 PostgreSQL 驱动。

该启动顺序保证迁移只有一个执行者，避免 API 与调度器并发迁移，也让迁移失败成为可见的部署失败，而不是应用运行后的隐性错误。

## 配置与秘密

项目根目录提供可提交的 `.env.example`，列出 Compose 所需的数据库名、用户、密码和现有应用配置，不包含真实凭据。操作者复制为未提交的根目录 `.env` 后启动 Compose。

Compose 通过环境变量构造 PostgreSQL 连接信息，并向三个后端相关服务注入一致的 `DATABASE_URL`。开发默认值仅用于本机，不应被描述为生产安全配置。仓库现有 `backend/.env.example` 保留应用独立运行所需的默认 SQLite 配置，并补充 PostgreSQL URL 示例或指向根目录部署配置，避免两套配置含义冲突。

## 启动与数据流

部署顺序为：

1. Compose 创建 PostgreSQL 容器和命名卷。
2. PostgreSQL 健康检查通过。
3. `migrate` 连接 PostgreSQL 并升级到 Alembic `head`，成功后以状态码 0 退出。
4. Compose 启动 `backend` 和 `scheduler`。
5. `backend` 的 `/health/ready` 执行数据库探测；就绪后前端可通过内部网络访问后端。

普通 `docker compose restart` 或 `docker compose down` 不删除命名卷。只有操作者明确执行 `docker compose down -v` 时才清空数据库。

## 故障语义

- PostgreSQL 未健康时，迁移及应用服务保持未启动状态。
- Alembic 迁移失败时，`migrate` 非零退出，`backend` 和 `scheduler` 不启动；操作者通过迁移服务日志定位错误。
- 应用运行期间数据库不可用时，存活检查仍可响应，就绪检查返回 `503` 和稳定错误码 `database_unavailable`。
- 数据库恢复后，就绪检查无需重启应用即可恢复成功。
- 必需的 Compose 配置缺失时应在配置渲染或启动阶段明确失败，不静默退回 SQLite。

本次不自动导入仓库中的旧 `backend/x_digest.db`。SQLite 到 PostgreSQL 的数据搬迁属于独立的一次性迁移任务，避免在新部署路径中引入不可逆或未经确认的数据操作。

## 运维接口

部署文档提供可复制执行的命令，覆盖：

- 从 `.env.example` 创建本地配置；
- 构建并后台启动所有服务；
- 查看服务状态和关键日志；
- 检查前后端健康接口；
- 查询 Alembic 当前版本及核心表；
- 写入最小验收数据并在重启后确认仍存在；
- 停止服务但保留数据；
- 明确标注并单独说明会删除数据卷的清空命令。

## 测试与验收

自动化测试覆盖 Alembic 从环境读取连接 URL 的行为，并保留 SQLite 的现有迁移测试。若本机 Docker 可用，集成验收使用全新 PostgreSQL 卷执行完整闭环：

1. `docker compose config` 成功。
2. 空数据库升级到 Alembic `head`。
3. `backend`、`scheduler` 和 `frontend` 达到预期运行状态，`migrate` 成功退出。
4. PostgreSQL 中存在 `alembic_version` 和核心业务表。
5. 前后端健康接口返回成功，后端就绪检查实际连接 PostgreSQL。
6. 写入一条独立的验收记录，普通重启后记录仍存在。
7. 后端单元测试、Ruff 和 MyPy 通过。

验收记录必须使用专用、可识别的数据，测试完成后只清理该记录，不执行隐式卷删除。若 Docker 在执行环境中不可用，必须明确报告未运行的集成验收项，不能用静态配置检查替代并宣称闭环完成。
