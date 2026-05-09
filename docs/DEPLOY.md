# 部署指南

## 1. 准备数据库备份

```bash
cp /usr/local/s-ui/db/s-ui.db /usr/local/s-ui/db/s-ui.db.bak
```

## 2. 拉取项目

```bash
git clone https://github.com/oldwangnewbe/sui-traffic-reset.git
cd sui-traffic-reset
```

## 3. 一键部署

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

## 4. 配置环境变量

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

## 5. 启动

```bash
docker compose up -d --build
```

## 6. 访问

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

## 7. 更新

```bash
git pull
docker compose up -d --build
```
