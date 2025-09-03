# GeminiCLI to API

**将 GeminiCLI 转换为 OpenAI 和 GEMINI API 接口**

---

## ⚠️ 许可证声明

**本项目采用 Cooperative Non-Commercial License (CNC-1.0)**

这是一个反商业化的严格开源协议，详情请查看 [LICENSE](LICENSE) 文件。

### ✅ 允许的用途：
- 个人学习、研究、教育用途
- 非营利组织使用
- 开源项目集成（需遵循相同协议）
- 学术研究和论文发表

### ❌ 禁止的用途：
- 任何形式的商业使用
- 年收入超过100万美元的企业使用
- 风投支持或公开交易的公司使用  
- 提供付费服务或产品
- 商业竞争用途

---

## 核心功能

**多端点双格式支持**
- **OpenAI 兼容端点**：`/v1/chat/completions` 和 `/v1/models`
  - 支持标准 OpenAI 格式（messages 结构）
  - 支持 Gemini 原生格式（contents 结构）
  - 自动格式检测和转换，无需手动切换
- **Gemini 原生端点**：`/v1/models/{model}:generateContent` 和 `streamGenerateContent`
  - 支持完整的 Gemini 原生 API 规范
  - 多种认证方式：Bearer Token、x-goog-api-key 头部、URL 参数 key

**灵活的密码管理**
- **分离密码支持**：API 密码（聊天端点）和控制面板密码可独立设置
- **多种认证方式**：支持 Authorization Bearer、x-goog-api-key 头部、URL 参数等

**流式支持**
- 实时流式响应
- 伪流式回退机制

**智能凭证管理**
- 多个 Google OAuth 凭证自动轮换
- 通过冗余认证增强稳定性
- 负载均衡与并发请求支持

**Web 认证界面**
- 简化的 OAuth 认证工作流
- 简易的凭证配置流程

## 支持的模型

所有模型均具备 1M 上下文窗口容量。每个凭证文件提供 1500 次请求额度。

- `gemini-2.5-pro`
- `gemini-2.5-pro-preview-06-05`
- `gemini-2.5-pro-preview-05-06`

*注：所有模型均支持伪流式变体*

---

## 安装指南

### Termux 环境

**初始安装**
```bash
curl -o termux-install.sh "https://raw.githubusercontent.com/su-kaka/gcli2api/refs/heads/master/termux-install.sh" && chmod +x termux-install.sh && ./termux-install.sh
```

**重启服务**
```bash
cd gcli2api
bash termux-start.sh
```

### Windows 环境

**初始安装**
```powershell
iex (iwr "https://raw.githubusercontent.com/su-kaka/gcli2api/refs/heads/master/install.ps1" -UseBasicParsing).Content
```

**重启服务**
双击执行 `start.bat`

### Linux 环境

**初始安装**
```bash
curl -o install.sh "https://raw.githubusercontent.com/su-kaka/gcli2api/refs/heads/master/install.sh" && chmod +x install.sh && ./install.sh
```

**重启服务**
```bash
cd gcli2api
bash start.sh
```

### Docker 环境

**Docker 运行命令**
```bash
# 使用通用密码
docker run -d --name gcli2api --network host -e PASSWORD=pwd -e PORT=7861 -v $(pwd)/data/creds:/app/creds ghcr.io/su-kaka/gcli2api:latest

# 使用分离密码
docker run -d --name gcli2api --network host -e API_PASSWORD=api_pwd -e PANEL_PASSWORD=panel_pwd -e PORT=7861 -v $(pwd)/data/creds:/app/creds ghcr.io/su-kaka/gcli2api:latest
```

**Docker Compose 运行命令**
1. 将以下内容保存为 `docker-compose.yml` 文件：
    ```yaml
    version: '3.8'

    services:
      gcli2api:
        image: ghcr.io/su-kaka/gcli2api:latest
        container_name: gcli2api
        restart: unless-stopped
        network_mode: host
        environment:
          # 使用通用密码（推荐用于简单部署）
          - PASSWORD=pwd
          - PORT=7861
          # 或使用分离密码（推荐用于生产环境）
          # - API_PASSWORD=your_api_password
          # - PANEL_PASSWORD=your_panel_password
        volumes:
          - ./data/creds:/app/creds
        healthcheck:
          test: ["CMD-SHELL", "python -c \"import sys, urllib.request, os; port = os.environ.get('PORT', '7861'); req = urllib.request.Request(f'http://localhost:{port}/v1/models', headers={'Authorization': 'Bearer ' + os.environ.get('PASSWORD', 'pwd')}); sys.exit(0 if urllib.request.urlopen(req, timeout=5).getcode() == 200 else 1)\""]
          interval: 30s
          timeout: 10s
          retries: 3
          start_period: 40s
    ```
2. 启动服务：
    ```bash
    docker-compose up -d
    ```

---

## ⚠️ 注意事项

- 当前 OAuth 验证流程**仅支持本地主机（localhost）访问**，即须通过 `http://127.0.0.1:7861/auth` 完成认证（默认端口 7861，可通过 PORT 环境变量修改）。
- **如需在云服务器或其他远程环境部署，请先在本地运行服务并完成 OAuth 验证，获得生成的 json 凭证文件（位于 `./geminicli/creds` 目录）后，再在auth面板将该文件上传即可。**
- **请严格遵守使用限制，仅用于个人学习和非商业用途**

---

## 配置说明

1. 访问 `http://127.0.0.1:7861/auth` （默认端口，可通过 PORT 环境变量修改）
2. 完成 OAuth 认证流程（默认密码：`pwd`，可通过环境变量修改）
3. 配置客户端：

**OpenAI 兼容客户端：**
   - **端点地址**：`http://127.0.0.1:7861/v1`
   - **API 密钥**：`pwd`（默认值，可通过 API_PASSWORD 或 PASSWORD 环境变量修改）

**Gemini 原生客户端：**
   - **端点地址**：`http://127.0.0.1:7861`
   - **认证方式**：
     - `Authorization: Bearer your_api_password`
     - `x-goog-api-key: your_api_password` 
     - URL 参数：`?key=your_api_password`

### 环境变量配置

**基础配置**
- `PORT`: 服务端口（默认：7861）
- `HOST`: 服务器监听地址（默认：0.0.0.0）

**密码配置**
- `API_PASSWORD`: 聊天 API 访问密码（默认：继承 PASSWORD 或 pwd）
- `PANEL_PASSWORD`: 控制面板访问密码（默认：继承 PASSWORD 或 pwd）  
- `PASSWORD`: 通用密码，设置后覆盖上述两个（默认：pwd）

**凭证配置**

支持使用 `GCLI_CREDS_*` 环境变量导入多个凭证：

#### 凭证环境变量使用示例

**方式 1：编号格式**
```bash
export GCLI_CREDS_1='{"client_id":"your-client-id","client_secret":"your-secret","refresh_token":"your-token","token_uri":"https://oauth2.googleapis.com/token","project_id":"your-project"}'
export GCLI_CREDS_2='{"client_id":"...","project_id":"..."}'
```

**方式 2：项目名格式**
```bash
export GCLI_CREDS_myproject='{"client_id":"...","project_id":"myproject",...}'
export GCLI_CREDS_project2='{"client_id":"...","project_id":"project2",...}'
```

**启用自动加载**
```bash
export AUTO_LOAD_ENV_CREDS=true  # 程序启动时自动导入环境变量凭证
```

**Docker 使用示例**
```bash
# 使用通用密码
docker run -d --name gcli2api \
  -e PASSWORD=mypassword \
  -e PORT=8080 \
  -e GOOGLE_CREDENTIALS="$(cat credential.json | base64 -w 0)" \
  ghcr.io/su-kaka/gcli2api:latest

# 使用分离密码
docker run -d --name gcli2api \
  -e API_PASSWORD=my_api_password \
  -e PANEL_PASSWORD=my_panel_password \
  -e PORT=8080 \
  -e GOOGLE_CREDENTIALS="$(cat credential.json | base64 -w 0)" \
  ghcr.io/su-kaka/gcli2api:latest
```

注意：当设置了凭证环境变量时，系统将优先使用环境变量中的凭证，忽略 `creds` 目录中的文件。

### API 使用方式

本服务支持两套完整的 API 端点：

#### 1. OpenAI 兼容端点

**端点：** `/v1/chat/completions`  
**认证：** `Authorization: Bearer your_api_password`

支持两种请求格式，会自动检测并处理：

**OpenAI 格式：**
```json
{
  "model": "gemini-2.5-pro",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant"},
    {"role": "user", "content": "Hello"}
  ],
  "temperature": 0.7,
  "stream": true
}
```

**Gemini 原生格式：**
```json
{
  "model": "gemini-2.5-pro",
  "contents": [
    {"role": "user", "parts": [{"text": "Hello"}]}
  ],
  "systemInstruction": {"parts": [{"text": "You are a helpful assistant"}]},
  "generationConfig": {
    "temperature": 0.7
  }
}
```

#### 2. Gemini 原生端点

**非流式端点：** `/v1/models/{model}:generateContent`  
**流式端点：** `/v1/models/{model}:streamGenerateContent`  
**模型列表：** `/v1/models`

**认证方式（任选一种）：**
- `Authorization: Bearer your_api_password`
- `x-goog-api-key: your_api_password`  
- URL 参数：`?key=your_api_password`

**请求示例：**
```bash
# 使用 x-goog-api-key 头部
curl -X POST "http://127.0.0.1:7861/v1/models/gemini-2.5-pro:generateContent" \
  -H "x-goog-api-key: your_api_password" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {"role": "user", "parts": [{"text": "Hello"}]}
    ]
  }'

# 使用 URL 参数
curl -X POST "http://127.0.0.1:7861/v1/models/gemini-2.5-pro:streamGenerateContent?key=your_api_password" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {"role": "user", "parts": [{"text": "Hello"}]}
    ]
  }'
```

**说明：**
- OpenAI 端点返回 OpenAI 兼容格式
- Gemini 端点返回 Gemini 原生格式
- 两种端点使用相同的 API 密码

---

## 故障排除

**400 错误解决方案**
```bash
npx https://github.com/google-gemini/gemini-cli
```
1. 选择选项 1
2. 按回车确认
3. 完成浏览器中的 Google 账户认证
4. 系统将自动完成授权

---

## 支持项目

如果这个项目对您有帮助，欢迎支持项目的持续发展！

详细捐赠信息请查看：[📖 捐赠说明文档](docs/DONATE.md)

---

## 许可证与免责声明

本项目仅供学习和研究用途。使用本项目表示您同意：
- 不将本项目用于任何商业用途
- 承担使用本项目的所有风险和责任
- 遵守相关的服务条款和法律法规

项目作者对因使用本项目而产生的任何直接或间接损失不承担责任。
