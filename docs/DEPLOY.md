# 部署指南

## 1. 准备数据库备份

```bash
cp /usr/local/s-ui/db/s-ui.db /usr/local/s-ui/db/s-ui.db.bak
```

## 2. 无 Git 安装

直接在服务器新建 `docker-compose.yml`：

```yaml
# 默认使用最新版 latest；如需固定版本，把 latest 改成 Release 版本，例如 v0.1.3。
# 可用版本见：https://github.com/oldwangnewbe/sui-traffic-reset/releases
services:
  sui-traffic-reset:
    image: ghcr.io/oldwangnewbe/sui-traffic-reset:latest
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
      RESET_WEB_PORT: 8080
    ports:
      # 默认只允许服务器本机访问；公网访问可改成 0.0.0.0:8787:8080
      - 127.0.0.1:8787:8080
    volumes:
      # 如果 s-ui 数据库目录不是默认路径，请修改冒号左侧。
      - /usr/local/s-ui/db:/data
```

默认会使用最新版。至少修改 `RESET_ADMIN_PASSWORD`；如果要锁定版本，再把 `image` 里的 `latest` 改成指定版本。
如果放在 HTTPS 反向代理后面，建议把 `RESET_COOKIE_SECURE` 改成 `1`。

```bash
docker compose up -d
```

## 3. 源码部署

如果你想自己构建镜像，可以拉取项目：

```bash
git clone https://github.com/oldwangnewbe/sui-traffic-reset.git
cd sui-traffic-reset
```

## 4. 一键部署

```bash
./install.sh
```

脚本会自动：

- 检查 Docker Compose
- 生成 `.env`
- 备份 `s-ui.db`
- 构建并启动容器
- 打印 Web 地址和管理员登录信息

如果你想手动配置，继续按下面步骤操作。

## 5. 配置环境变量

```bash
cp .env.example .env
nano .env
```

至少需要修改：

```env
RESET_ADMIN_PASSWORD=your-strong-password
```

如果 s-ui 数据库目录不是默认路径，也修改：

```env
SUI_DB_DIR=/path/to/s-ui/db
```

## 6. 启动

本地构建安装：

```bash
docker compose up -d --build
```

使用发布镜像安装：

```bash
docker compose -f docker-compose.image.yml up -d
```

如果要选择固定版本，在 `.env` 里设置：

```env
SUI_TRAFFIC_RESET_VERSION=v0.1.3
```

## 7. 访问

默认只监听服务器本机：

```text
http://127.0.0.1:8787
```

使用 `.env` 中的 `RESET_ADMIN_USER` 和 `RESET_ADMIN_PASSWORD` 登录。管理员登录后可以创建普通用户账号，并把普通用户绑定到对应的 s-ui 客户端名。

如果需要公网访问：

```env
RESET_WEB_BIND=0.0.0.0:8787
```

然后重启：

```bash
docker compose up -d
```

## 8. 更新

更新到最新源码并重新构建：

```bash
git pull
docker compose up -d --build
```

切换到指定 Release：

```bash
git fetch --tags
git checkout v0.1.3
docker compose up -d --build
```

如果使用发布镜像，只需要修改 `.env` 中的版本并重启：

```bash
SUI_TRAFFIC_RESET_VERSION=v0.1.3 docker compose -f docker-compose.image.yml up -d
```
