# 部署指南

## 1. 准备数据库备份

```bash
cp /usr/local/s-ui/db/s-ui.db /usr/local/s-ui/db/s-ui.db.bak
```

## 2. 拉取项目

```bash
git clone https://github.com/your-name/sui-traffic-reset.git
cd sui-traffic-reset
```

## 3. 配置环境变量

```bash
cp .env.example .env
nano .env
```

至少需要修改：

```env
RESET_WEB_TOKEN=your-strong-token
```

如果 s-ui 数据库目录不是默认路径，也修改：

```env
SUI_DB_DIR=/path/to/s-ui/db
```

## 4. 启动

```bash
docker compose up -d --build
```

## 5. 访问

默认只监听服务器本机：

```text
http://127.0.0.1:8787
```

如果需要公网访问：

```env
RESET_WEB_BIND=0.0.0.0:8787
```

然后重启：

```bash
docker compose up -d
```

## 6. 更新

```bash
git pull
docker compose up -d --build
```
