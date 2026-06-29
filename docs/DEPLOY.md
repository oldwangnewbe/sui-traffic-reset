# 部署指南

推荐使用 Docker Hub 镜像部署。

## 一行安装

```bash
bash <(curl -Ls https://raw.githubusercontent.com/oldwangnewbe/sui-traffic-reset/main/install.sh)
```

默认要求 s-ui 数据库位于：

```text
/usr/local/s-ui/db/s-ui.db
```

如果数据库目录不同：

```bash
SUI_DB_DIR=/path/to/s-ui/db bash <(curl -Ls https://raw.githubusercontent.com/oldwangnewbe/sui-traffic-reset/main/install.sh)
```

公网访问：

```bash
RESET_WEB_BIND=0.0.0.0:8787 bash <(curl -Ls https://raw.githubusercontent.com/oldwangnewbe/sui-traffic-reset/main/install.sh)
```

固定版本：

```bash
SUI_TRAFFIC_RESET_VERSION=v0.2.0 bash <(curl -Ls https://raw.githubusercontent.com/oldwangnewbe/sui-traffic-reset/main/install.sh)
```

## 手动 Compose

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
      - 127.0.0.1:8787:8080
    extra_hosts:
      - host.docker.internal:host-gateway
    volumes:
      - /usr/local/s-ui/db:/data
```

启动：

```bash
docker compose up -d
```

## 在线状态

不配置 s-ui API 时，在线列显示“未配置”。配置后只显示在线/离线，不显示具体 IP。

```env
SUI_PANEL_URL=http://host.docker.internal:2095
SUI_API_TOKEN=你的-s-ui-API-Token
```

如果 s-ui 有路径前缀：

```env
SUI_PANEL_URL=http://host.docker.internal:2095/app
```

## 维护命令

```bash
cd /opt/sui-traffic-reset
docker compose logs -f
docker compose pull
docker compose up -d
```

## 源码构建

```bash
git clone https://github.com/oldwangnewbe/sui-traffic-reset.git
cd sui-traffic-reset
cp .env.example .env
docker compose up -d --build
```
