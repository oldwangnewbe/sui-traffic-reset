# s-ui Traffic Reset

一个给 [s-ui](https://github.com/alireza0/s-ui) 使用的外置流量管理工具。它不修改 s-ui 源码，直接读取并更新 s-ui 的 SQLite 数据库，用于管理用户流量、到期时间和自动重置规则。

## 功能

- Web 控制台查看 s-ui 用户流量
- 单用户手动清零流量
- 设置用户到期时间或设为无限
- 为单个用户设置每月或每 N 天定时重置
- 为筛选出的用户批量设置定时重置
- 到期规则后台自动执行
- Docker Compose 一键部署
- 用户名密码登录，管理员和普通用户分权
- 普通用户只查看自己绑定客户端的流量、到期和下次刷新时间

## 截图

打开 Web 控制台后，你可以在用户列表中直接操作当前用户：

- `重置`：立即清零该用户流量
- `定时重置`：给该用户配置自动重置周期
- `到期`：修改该用户到期时间

## 快速开始

默认 s-ui 数据库路径：

```bash
/usr/local/s-ui/db/s-ui.db
```

一键 Docker Compose 部署：

```bash
git clone https://github.com/oldwangnewbe/sui-traffic-reset.git
cd sui-traffic-reset
./install.sh
```

手动部署：

```bash
git clone https://github.com/oldwangnewbe/sui-traffic-reset.git
cd sui-traffic-reset
cp .env.example .env
nano .env
docker compose up -d --build
```

请务必修改 `.env` 里的 `RESET_ADMIN_PASSWORD`。首次启动会自动创建或更新管理员账号。

默认 Web 入口只绑定本机：

```text
http://127.0.0.1:8787
```

如果需要公网访问，修改 `.env`：

```env
RESET_WEB_BIND=0.0.0.0:8787
```

然后访问：

```text
http://服务器IP:8787
```

## Docker Compose 配置

项目内置 [docker-compose.yml](./docker-compose.yml)。`install.sh` 会自动生成 `.env`、备份数据库并执行 `docker compose up -d --build`。

`.env.example`：

```env
SUI_DB_DIR=/usr/local/s-ui/db
CHECK_INTERVAL=60
TZ=Asia/Shanghai
RESET_ADMIN_USER=admin
RESET_ADMIN_PASSWORD=please-change-this-password
RESET_WEB_BIND=127.0.0.1:8787
```

字段说明：

| 变量 | 说明 |
| --- | --- |
| `SUI_DB_DIR` | s-ui 数据库目录，目录内应有 `s-ui.db` |
| `CHECK_INTERVAL` | 后台检查到期规则的间隔秒数 |
| `TZ` | 容器时区 |
| `RESET_WEB_BIND` | Web 服务绑定地址 |
| `RESET_ADMIN_USER` | Web 管理员用户名 |
| `RESET_ADMIN_PASSWORD` | Web 管理员密码 |

## 常用命令

查看日志：

```bash
docker compose logs -f
```

停止服务：

```bash
docker compose down
```

列出用户流量：

```bash
docker compose run --rm sui-traffic-reset list --all
```

手动重置某个用户：

```bash
docker compose run --rm sui-traffic-reset reset --user wang --enable-after-reset
```

手动重置所有用户：

```bash
docker compose run --rm sui-traffic-reset reset --all --enable-after-reset
```

添加每月 1 号 00:00 重置所有用户的规则：

```bash
docker compose run --rm sui-traffic-reset rule-add --all --cycle monthly --day 1 --time 00:00 --timezone Asia/Shanghai
```

查看自动规则：

```bash
docker compose run --rm sui-traffic-reset rule-list
```

执行到期规则：

```bash
docker compose run --rm sui-traffic-reset run-due
```

## 安全建议

- 第一次部署前备份数据库：

```bash
cp /usr/local/s-ui/db/s-ui.db /usr/local/s-ui/db/s-ui.db.bak
```

- 不要把 `.env`、`*.db`、`*.db-wal`、`*.db-shm` 提交到 GitHub。
- 如果公网开放 Web 页面，请使用强管理员密码，并建议套反代 HTTPS。
- 本工具直接写 s-ui 数据库，请确认只有可信用户可以访问。
- 页面里只能创建普通用户账号，管理员账号由 `.env` 管理，避免误创建高权限账号。

## 工作方式

工具会创建一张独立表：

```text
sui_traffic_reset_rules
sui_traffic_reset_accounts
```

自动重置规则和登录账号存放在这些表中，不会修改 s-ui 自身表结构。执行重置时会更新：

- `clients.up`
- `clients.down`
- `clients.expiry`
- `clients.enable`
- `clients.total_up` / `clients.total_down`，如果数据库中存在这些字段

## 许可证

MIT License
