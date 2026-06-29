# s-ui Traffic Reset

给 [s-ui](https://github.com/alireza0/s-ui) 用的外置流量面板和自动重置工具。

它不改 s-ui 源码，直接读取并更新 s-ui 的 SQLite 数据库。管理员可以在网页里查看用户流量、设置到期时间、手动重置流量、配置每月自动重置；普通用户只能查看自己绑定的客户端。

## 功能

- 查看 s-ui 用户流量、到期时间、下次刷新时间
- 单用户、筛选用户、全部用户流量重置
- 每月或每 N 天自动重置
- 管理员和普通用户分权登录
- 普通用户只看到自己绑定的客户端
- 可选接入 s-ui API 显示在线/离线状态
- Docker Compose 部署，支持 Docker Hub 版本镜像
- HttpOnly Cookie、CSRF 防护、登录失败限速和基础安全响应头

说明：目前原版 s-ui 没有稳定的在线 IP 列表接口，本工具只显示在线/离线，不显示具体 IP。

## 一行安装

服务器上已安装 Docker 和 Docker Compose 后执行：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/oldwangnewbe/sui-traffic-reset/main/install.sh)
```

脚本会做这些事：

- 检查 `/usr/local/s-ui/db/s-ui.db`
- 创建 `/opt/sui-traffic-reset`
- 自动生成管理员密码
- 在终端输出初始管理员账号和密码
- 备份 s-ui 数据库
- 写入 `docker-compose.yml`
- 拉取并启动 `oldwangnewbe/sui-traffic-reset:latest`

安装完成后可以直接公网访问：

```text
http://服务器IP:8787
```

如果只想允许服务器本机访问，可以这样安装：

```bash
RESET_WEB_BIND=127.0.0.1:8787 bash <(curl -Ls https://raw.githubusercontent.com/oldwangnewbe/sui-traffic-reset/main/install.sh)
```

如果 s-ui 数据库不在默认目录：

```bash
SUI_DB_DIR=/path/to/s-ui/db bash <(curl -Ls https://raw.githubusercontent.com/oldwangnewbe/sui-traffic-reset/main/install.sh)
```

固定版本安装：

```bash
SUI_TRAFFIC_RESET_VERSION=v0.2.0 bash <(curl -Ls https://raw.githubusercontent.com/oldwangnewbe/sui-traffic-reset/main/install.sh)
```

## 手动 Docker Compose 部署

不想用安装脚本时，在服务器新建 `docker-compose.yml`：

```yaml
services:
  sui-traffic-reset:
    image: oldwangnewbe/sui-traffic-reset:latest
    container_name: sui-traffic-reset
    restart: unless-stopped
    environment:
      SUI_DB: /data/s-ui.db
      CHECK_INTERVAL: 60
      TZ: Asia/Shanghai
      RESET_ADMIN_USER: admin
      RESET_ADMIN_PASSWORD: please-change-this-password
      RESET_SESSION_TTL: 604800
      RESET_LOGIN_MAX_ATTEMPTS: 8
      RESET_LOGIN_WINDOW: 600
      RESET_COOKIE_SECURE: 0
      SUI_PANEL_URL: ""
      SUI_API_TOKEN: ""
      SUI_API_TIMEOUT: 3
      SUI_ONLINE_CACHE_TTL: 5
      RESET_WEB_PORT: 8080
    ports:
      - 0.0.0.0:8787:8080
    extra_hosts:
      - host.docker.internal:host-gateway
    volumes:
      - /usr/local/s-ui/db:/data
```

然后启动：

```bash
docker compose up -d
```

请务必修改 `RESET_ADMIN_PASSWORD`。如果要使用指定版本，把 `latest` 改成对应版本号。

## 在线状态

如果只想管理流量，不需要配置 s-ui API。

如果想让管理员看到在线/离线状态，在 s-ui 后台创建 API Token，然后设置：

```env
SUI_PANEL_URL=http://host.docker.internal:2095
SUI_API_TOKEN=你的-s-ui-API-Token
```

如果 s-ui 有路径前缀，填写完整地址，例如：

```env
SUI_PANEL_URL=http://host.docker.internal:2095/app
```

## 常用命令

进入安装目录：

```bash
cd /opt/sui-traffic-reset
```

查看日志：

```bash
docker compose logs -f
```

停止：

```bash
docker compose down
```

升级到最新版：

```bash
docker compose pull
docker compose up -d
```

手动重置某个用户：

```bash
docker compose run --rm sui-traffic-reset reset --user wang --enable-after-reset
```

重置全部用户：

```bash
docker compose run --rm sui-traffic-reset reset --all --enable-after-reset
```

## 源码开发

```bash
git clone https://github.com/oldwangnewbe/sui-traffic-reset.git
cd sui-traffic-reset
cp .env.example .env
docker compose up -d --build
```

## 安全建议

- 第一次使用前备份数据库。
- 不要把 `.env`、`*.db`、`*.db-wal`、`*.db-shm` 上传到 GitHub。
- 公网访问时请使用强密码，并建议套 HTTPS 反向代理。
- HTTPS 后面建议设置 `RESET_COOKIE_SECURE=1`。
- 管理员账号由环境变量管理，页面里只能创建普通用户。

## License

MIT
