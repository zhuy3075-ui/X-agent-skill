# 同机服务器部署指南

本指南将 PostgreSQL、MCP API 和独立 Worker 部署到同一台 Linux 服务器。
固定示例路径为 `/opt/x-agent-skill`、`/etc/scweet`、`/var/lib/scweet-mcp`，
服务用户为 `scweet`。如修改路径，须同步修改 config、systemd 和客户端配置。
模板需在你的目标服务器执行检查；本文不是已部署完成的声明。

## 1. 获取代码与安装环境

准备 Python 3.10+、venv、Git、PostgreSQL 14+、systemd；远程访问额外需要 nginx 和已配置的 TLS 证书。
PostgreSQL 仅监听本机；X 采集网络／代理由部署者配置。使用服务器管理员执行：

```bash
sudo useradd --system --home-dir /var/lib/scweet-mcp --create-home --shell /usr/sbin/nologin scweet
sudo install -d -o scweet -g scweet -m 0750 /opt/x-agent-skill
sudo -u scweet git clone --branch v1.0.1 https://github.com/zhuy3075-ui/X-agent-skill.git /opt/x-agent-skill
sudo -u scweet /usr/bin/python3 /opt/x-agent-skill/install.py --skip-skills --no-register-codex
sudo install -d -o root -g scweet -m 0750 /etc/scweet
sudo install -d -o scweet -g scweet -m 0700 /var/lib/scweet-mcp/private /var/lib/scweet-mcp/exports
sudo install -o root -g scweet -m 0640 /opt/x-agent-skill/mcp/config.server.example.json /etc/scweet/config.json
```

如果用户或目录已存在，先核对其用途和权限，不重复创建或覆盖已有私有配置。
若 `/usr/bin/python3` 版本不满足要求，使用已安装的受支持 Python 的绝对路径。
服务器不安装技能到服务用户目录；技能只需安装在实际使用的 Agent 客户端。

## 2. 数据库角色与初始化

为此服务创建专用数据库。以 PostgreSQL 管理员进入 psql：

```bash
sudo -u postgres psql
```

依次执行，`\password` 通过隐藏输入设置密码，不将密码写进 SQL 历史：

```sql
CREATE ROLE scweet_owner LOGIN;
\password scweet_owner
CREATE ROLE scweet_runtime LOGIN;
\password scweet_runtime
CREATE DATABASE scweet_mcp OWNER scweet_owner;
\connect scweet_mcp
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO scweet_owner;
\quit
```

检查 PostgreSQL 本机连接规则允许这两个角色安全认证。
以管理员的安全编辑器填写 `/var/lib/scweet-mcp/private/pgpass`，两行分别为：

```text
127.0.0.1:5432:scweet_mcp:scweet_owner:<owner 密码>
127.0.0.1:5432:scweet_mcp:scweet_runtime:<运行密码>
```

设置 `sudo chown scweet:scweet /var/lib/scweet-mcp/private/pgpass` 和
`sudo chmod 600 /var/lib/scweet-mcp/private/pgpass`。
密码中的 `:` 或 `\` 按 [PostgreSQL 官方规则](https://www.postgresql.org/docs/current/libpq-pgpass.html)转义。
以下连接字符串不包含密码，可用于初始化：

```bash
sudo -u scweet env SCWEET_MCP_DATABASE_URL='host=127.0.0.1 port=5432 dbname=scweet_mcp user=scweet_owner passfile=/var/lib/scweet-mcp/private/pgpass' /opt/x-agent-skill/.venv/bin/python /opt/x-agent-skill/run.py init-db --config /etc/scweet/config.json
```

以 PostgreSQL 管理员在这个专用数据库中执行运行授权；无需给予运行角色建表或删除权限：

```bash
sudo -u postgres psql -d scweet_mcp
```

```sql
GRANT CONNECT ON DATABASE scweet_mcp TO scweet_runtime;
GRANT USAGE ON SCHEMA public TO scweet_runtime;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO scweet_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO scweet_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE scweet_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO scweet_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE scweet_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO scweet_runtime;
\quit
```

这些授权覆盖当前表和 owner 后续创建的表／分区。共享数据库不能照搬 ALL TABLES 授权；
应按具体表授权。初始化后把 owner 密码移至仅管理员可读的文件，服务用户只保留 runtime 行，
以便真正分离迁移权限；将来初始化／升级时临时使用管理员维护的 owner passfile。

## 3. 私有配置与账号

`/etc/scweet/config.json` 使用模板默认的这些绝对路径：

```json
{
  "mcp_credentials_file": "/var/lib/scweet-mcp/private/worker-env.json",
  "mcp_artifact_dir": "/var/lib/scweet-mcp/exports",
  "mcp_manifest_db_path": "/var/lib/scweet-mcp/manifest.sqlite3"
}
```

这里展示的是需要核对的字段；完整配置还包含模板里的主机、端口和监测设置。
不要删掉这些字段或将原始 token 放入普通 config。
用 README 的 [浏览器步骤](../README.md#3-从浏览器获取-x-auth_token)取得自己的 X token，然后在服务器隐藏输入：

```bash
sudo -u scweet /opt/x-agent-skill/.venv/bin/python /opt/x-agent-skill/mcp/credentials.py --file /var/lib/scweet-mcp/private/worker-env.json --credential-ref SCWEET_X_ACCOUNT_A
```

多个账号重复执行并换成 B／C 引用。安全复制私有文件也可，必须核对路径与读取权限。

使用安全编辑器分别创建 `/etc/scweet/api.env` 与 `/etc/scweet/worker.env`：

```dotenv
# 两个文件都要包含：
SCWEET_MCP_DATABASE_URL="host=127.0.0.1 port=5432 dbname=scweet_mcp user=scweet_runtime passfile=/var/lib/scweet-mcp/private/pgpass"
# 只在 api.env 中包含：
SCWEET_MCP_API_TOKEN=<本机生成的随机 MCP 访问密钥，至少 32 字符>
```

密钥可由密码管理器生成，或在受限终端用 Python `secrets.token_urlsafe(48)` 生成。
它用于 Agent 访问 MCP，与 X auth_token 不同。两文件设置 `root:scweet`、`0640` 权限；
只将 MCP 访问密钥通过客户端安全配置交给获授权的 Agent，不把 X token 发给客户端。

## 4. systemd 托管

```bash
sudo install -m 0644 /opt/x-agent-skill/mcp/deploy/scweet-mcp.service /etc/systemd/system/scweet-mcp.service
sudo install -m 0644 /opt/x-agent-skill/mcp/deploy/scweet-worker.service /etc/systemd/system/scweet-worker.service
sudo systemd-analyze verify /etc/systemd/system/scweet-mcp.service /etc/systemd/system/scweet-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now scweet-mcp.service scweet-worker.service
sudo systemctl status scweet-mcp.service scweet-worker.service
```

服务分别读取 api.env 和 worker.env，使用同一 config，崩溃后由 systemd 重启。
API 只绑定 `127.0.0.1:8765`。如果服务器 PostgreSQL 单元名不同，调整模板的 After；
使用 Docker 数据库时单独保证数据库已就绪，再启动服务。

## 5. HTTPS 与客户端连接

修改 `mcp/deploy/nginx.conf.example` 的域名、证书和私钥路径，放到 nginx 的 http 配置上下文，
然后执行 `sudo nginx -t` 和 `sudo systemctl reload nginx`。公网只开放实际需要的 HTTPS 443；
不要把 PostgreSQL 5432 和 MCP 8765 暴露到公网。
模板关闭响应缓冲并保留 Bearer 请求头，适用于流式 MCP；详见
[nginx proxy 文档](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。

在 Agent 客户端配置 **Streamable HTTP** 地址 `https://你的域名/mcp`，
请求头为 `Authorization: Bearer <MCP 访问密钥>`。各客户端字段不同，以其 HTTP MCP 配置入口为准；
不要复制 stdio 的 command／args 作为远端配置。此服务使用预配置 Bearer，不提供 OAuth 登录或多租户隔离。

在 Agent 所在电脑安装 `x-agent-skill`，或从 `mcp/skills/x-agent-skill` 复制到它的技能目录。
服务器代码路径无需与 Agent 电脑一致；业务操作由 HTTP MCP 传输，Agent 不直接访问服务器文件。

## 6. 完成验收

1. 确认两个 systemd 服务运行，检查错误时使用 `journalctl -u scweet-mcp -u scweet-worker`。
2. 用 MCP 客户端连接 HTTPS 地址，确认发现 31 个工具，执行 `monitors_list` 只读查询。
   仅看到进程运行或 curl 返回 HTTP 状态，不代表完成了 MCP 协议验收。
3. 调用 accounts_register 注册 A／B 等引用，accounts_check 检查，jobs_get 确认结果；
   只有有效账号进入采集池。登录检查成功不保证每个帖子都可见。
4. 明确要求小范围采集后核对保存条数、覆盖日期、缺口；没有明确监测要求时不创建监测任务。
5. 仅在需要持续观察时创建小时监测，不配置通知目标；以后询问时由 Agent 查询汇报。

只读本机 stdio 健康检查可使用 runtime DSN 和服务器 config：

```bash
sudo -u scweet env SCWEET_MCP_DATABASE_URL='host=127.0.0.1 port=5432 dbname=scweet_mcp user=scweet_runtime passfile=/var/lib/scweet-mcp/private/pgpass' /opt/x-agent-skill/.venv/bin/python /opt/x-agent-skill/scripts/check_service.py --config /etc/scweet/config.json
```

该命令不检查 nginx、TLS 或外部 HTTP 链路；这些必须用客户端连接验收。

## 7. 升级、备份与停止

先备份 PostgreSQL、导出目录以及私有配置到受限位置；实际恢复演练后才依赖备份。
可使用 `pg_dump -Fc`，不要将数据库密码放在命令行。记录当前代码版本。
关闭 Worker/API 后，获取选定正式标签，更新依赖，再按版本说明执行 schema-owner init-db 并核对权限：

```bash
sudo systemctl stop scweet-worker scweet-mcp
sudo -u scweet git -C /opt/x-agent-skill fetch --tags
sudo -u scweet git -C /opt/x-agent-skill checkout v1.0.1
sudo -u scweet /opt/x-agent-skill/.venv/bin/python /opt/x-agent-skill/install.py --skip-skills --no-register-codex
```

迁移使用管理员持有的 owner passfile；迁移完成后恢复 runtime DSN。
保留 /etc/scweet 和私有 token，不用模板覆盖既有配置。确认新表权限后启动两服务并重做验收。
不要在数据库结构不兼容时直接回退代码；先核对回退方案与备份。
运维还需维护快照分区、数据库保留策略和磁盘空间，应用不会自动删除历史业务数据。

Windows／macOS 本机升级同样先停止 Worker 和客户端 stdio 服务，再选择标签并重跑安装器；
技能个人偏好会保留，凭据文件不会因重装被覆盖。停止监测用 monitor_set_state，
而停止整个服务器用 `sudo systemctl stop scweet-worker scweet-mcp`。
