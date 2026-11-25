# Azure 合成数据测试环境

使用 `test.settings.json` 中的专用 North Central US 环境（East US 2 因订阅限制不可用，已获批改区）。这个发布会产生 Azure 费用，只处理合成题图。数据库无公网入口或密码登录；题图通过 Blob Private Endpoint 访问，存储公网、匿名访问和账户密钥均关闭。Web 界面有 HTTPS 公网入口，但业务 API 要求 Entra 登录并按作业所有者隔离。

## 发布顺序

1. 确认已审批的订阅、租户、地域、资源组和两位测试用户。使用两位已指定测试用户的 **Entra 用户对象 ID**（不是客户端 ID）运行 `python infra/release/entra.py check-users "$TEST_USER_1" "$TEST_USER_2"`。没有两位可用账号时不启动发布。批准后注册 `Microsoft.DBforPostgreSQL`、`Microsoft.ServiceBus`（如尚未注册），再检查 PostgreSQL、Service Bus 和 Foundry 可用配额；有阻塞时不更换地域、SKU 或放开数据库防火墙。
2. 用 `az deployment sub create --location northcentralus --name edu-ai-test-foundation-nc --template-file infra/main.bicep --parameters deployerObjectId="$DEPLOYER_OBJECT_ID"` 建立依赖，读取输出 `appOrigin`、`projectEndpoint` 和 `registryLoginServer`。部署者 ID 是当前 Azure CLI 用户在目标租户中的对象 ID。确认 Foundry 项目身份在账户范围具有 Foundry User 角色（账户创建项目时可自动授予），项目身份和三个工作负载身份均可从 ACR 拉取镜像。不要把 Application Insights 连接到 Foundry 项目，否则服务端会自动记录模型内容。
3. 用 `python infra/release/build_context.py --output-dir "$BUILD_CONTEXT_DIR"` 生成两个隔离目录，再用 `az acr build --registry creduaitestc2d102 --image edu-app:"$TAG" --file "$BUILD_CONTEXT_DIR/app/Dockerfile" --platform linux/amd64 "$BUILD_CONTEXT_DIR/app"` 构建应用。用相同参数、仓库 `edu-agent`、Dockerfile `$BUILD_CONTEXT_DIR/agent/Dockerfile` 和目录 `$BUILD_CONTEXT_DIR/agent` 构建 Agent。Dockerfile 参数使用实际本地路径，不是上下文内的相对路径。不要把仓库根目录作为 ACR 上下文。用 `az acr repository show --name creduaitestc2d102 --image <repository>:<tag> --query digest -o tsv` 解析发布标签；后续发布只用 `sha256:` 摘要。
4. 运行 `python infra/release/entra.py create "$TEST_USER_1" "$TEST_USER_2" --origin "$APP_ORIGIN"`。脚本将 API 与 SPA 设为单租户、要求分配，并逐一核对两个企业应用恰好只有上述两位用户。如果应用名称已存在，不会覆盖；核对归属后使用 `verify`，不要为规避错误而关闭分配要求。发布参数用脚本输出的两个 `appId`；记录两个 `objectId` 供清理。
5. 按下文「独立模型部署与 Agent 切换」先确认目标模型已部署，再在启用了 `hosted` 依赖的锁定 Python 环境中运行 `python infra/release/hosted_agent.py "$REGISTRY_LOGIN/edu-agent@$AGENT_DIGEST"`。脚本仅向本项目注册 Invocations 2.0.0 容器，等待版本变为 `active`，再输出 `agentUrl`。该入口只有 Worker 业务身份可调用，不给 Agent 数据库或题图存储权限。
6. 用下列参数先运行 `phase=migrate`，再用 `az containerapp job start` 启动 `job-init-edu-ai-test-c2d102`，通过 `az containerapp job execution show` 检查该次执行状态为 `Succeeded`。迁移 Job 在私网内创建数据库、映射两种应用身份、执行 Alembic 并授予应用读写权限。确认迁移成功且步骤 4 的 `verify` 通过后，才将相同参数的 `phase` 改成 `serve`，启用 API 和事件 Worker。两个阶段均用增量模式。

```bash
az deployment group create --resource-group rg-edu-ai-test-c2d102 \
  --name edu-ai-test-migrate --mode Incremental --template-file infra/runtime.bicep \
  --parameters phase=migrate imageDigest="$APP_DIGEST" agentUrl="$AGENT_URL" \
    apiClientId="$ENTRA_API_CLIENT_ID" spaClientId="$ENTRA_SPA_CLIENT_ID"
```

7. 在分享网址前检查 `/api/health`、未登录的业务 API 返回 401、未获分配账号无法登录。用两个已分配账号验证上传、真实模型报告、练习、掌握度和跨账号拒绝；检查队列重投递不重复计入掌握度。以独立标注的合成题分别衡量自动批改覆盖率（至少 80%）和自动批改项的一致率（至少 95%），不要用本地替身结果代替。

清理时，删除专用资源组及两项专用 Entra 应用注册和企业应用分配；使用记录的对象 ID 核对，不按名称批量删除。停止客户端访问并不会停止现有 Azure 资源费用。

## 已有环境的分阶段应用升级

英语版本需新增数据库迁移 `0003`，不能只发布前端。沿用现有资源和不可变镜像摘要，不重新执行基础设施模板，也不更改网络、SKU、身份分配或监控连接。

批准升级后，先构建应用和 Agent 的隔离上下文。应用镜像包含数学与英语演示模块，构建时会实际导入 API、Worker 和英语模块，避免本地环境掩盖漏打包问题。发布兼容数学的新版 Agent 后，用相同参数依次部署 `runtime.bicep` 的以下阶段：

1. `phase=migrate`：更新现有迁移 Job，执行并确认迁移成功、现有数学数据保留且新表已授予工作负载读写权限。
2. `phase=worker`：只更新现有事件 Worker；等待旧执行结束，核对新镜像及 Agent 地址，不修改 API。
3. `phase=api`：只更新现有 API/前端；等待就绪后检查登录、数学与英语合成数据流程。

每个阶段均先检查增量 `what-if`，确认不会删除资源或改动阶段外组件，再实际发布。`phase=serve` 保留用于原有一次性发布流程，不用于需要严格先后顺序的英语升级。若新版本失败，停止后续阶段，保留现有资源和迁移数据；数据库已产生英语数据后不能直接降级至 `0002`。

## 共享知识点版本升级

此版本尚未部署。它包含迁移 `0004`、新的 Agent 操作、Worker 和前端，不能仅更新页面。继续使用已批准的模型、私网和身份；按现有流程经审批后构建两个隔离上下文、发布新版 Agent，再依次执行 `phase=migrate`、迁移 Job、`phase=worker`、`phase=api`。应用上下文必须包含 `knowledge_demo.py`，避免本地环境掩盖缺失模块。健康检查要求新增表和任务列已经存在。

迁移保留已有数学与英语记录，并为12个内置知识点建立目录。确认新表已向 API/Worker 数据库身份授予读写权限。新增目录或活动存在时不允许直接降级至 `0003`；代码回退也不能撤销已经创建的私人学习数据。

维护权限使用 Entra **API 应用注册**中的应用角色 `Teacher`、`Admin`，角色设为启用、`allowedMemberTypes=["User"]`。现有环境需由获准管理员添加角色定义，并在 API 企业应用中将角色分配给明确指定的测试用户；不向第三个账号或群组开放访问，不关闭应用分配要求。重新登录取得新的 API access token 后才生效。新建注册脚本提供角色定义，但仍默认只分配普通访问，不自动指定教师或管理员。`entra.py verify` 接受已指定两位用户的默认或上述 API 角色，SPA 分配仍必须是默认角色；额外用户、群组或其他角色仍会被拒绝。本次代码实现没有修改云端应用注册或分配。

上线验收包括：教师能增加/整理、普通用户收到403；两个用户看到同一通用目录，但不能读取对方原图、发现记录、参考练习或反馈；数学与英语上传都触发发现；重复上传与并发同名发现只新增一次；低清晰度不扩展目录；参考作答不写入 `mastery_evidence`；重放及重试不重复创建反馈。使用合成题并重新检查延迟入库的模型内容追踪。每页增加一次模型发现调用，参考生成和反馈也按实际调用另计费用。

## 独立模型部署与 Agent 切换

`test.settings.json.model` 保留基础模板的 `gpt-5-mini`；`agentModel` 独立配置 Agent 的目标模型。当前选定 `gpt-6.1-sol`、版本 `2026-09-29`、`DataZoneStandard` 容量30（30,000 TPM）。它按实际 token 计费，不是 PTU；美国数据区推理不是单一地域推理，仍只允许合成数据。部署前重新核对配额和费用，不把容量当作费用上限。

经审批后，在现有资源组只执行 `model.bicep`，不重放基础模板、网络或身份配置：

```bash
az deployment group what-if --subscription f1212123-43be-417e-ab0a-0b81570c04d8 \
  --resource-group rg-edu-ai-test-c2d102 --name edu-ai-model-sol-20261125 \
  --template-file infra/model.bicep
az deployment group create --subscription f1212123-43be-417e-ab0a-0b81570c04d8 \
  --resource-group rg-edu-ai-test-c2d102 --name edu-ai-model-sol-20261125 \
  --mode Incremental --template-file infra/model.bicep
az cognitiveservices account deployment show \
  --subscription f1212123-43be-417e-ab0a-0b81570c04d8 \
  --resource-group rg-edu-ai-test-c2d102 --name ai-edu-ai-test-c2d102 \
  --deployment-name gpt-6.1-sol \
  --query '{state:properties.provisioningState,model:properties.model,sku:sku}'
```

确认模型 Succeeded 且支持所需 Responses、图片与结构化输出后，用现有的真实 Agent 镜像摘要运行 `hosted_agent.py`。模型切换不需要重建镜像或更新 API/Worker；脚本比较完整定义，相同镜像但模型不同会创建新版本，完全相同则复用。发布后检查 active/enabled、实际 `MODEL_DEPLOYMENT_NAME` 与返回结果的模型来源，而不只检查控制台模型名称。

保留旧模型和版本。若需回退，用相同镜像运行以下命令创建恢复旧模型配置的 Agent 版本；不删除新模型或回退数据库：

```bash
.venv/bin/python infra/release/hosted_agent.py \
  "$REGISTRY_LOGIN/edu-agent@$AGENT_DIGEST" --model-deployment gpt-5-mini
```

`--model-deployment` 是显式临时覆盖，不改 `agentModel` 默认值；后续正常发布仍使用配置中的新模型。不要为切换模型开启内容追踪或增加 Agent 的数据库、Blob 权限。

## 部署排障

迁移连接超时时，先检查 PostgreSQL 的 `state` 和活动日志，再检查私网 DNS、路由和身份认证。若订阅治理自动化停止了服务器，联系订阅管理员确认并处理治理限制；不要通过反复启动服务器、放开公网或更换身份来绕过限制。服务器恢复为 `Ready` 后，再重跑迁移并核对执行状态。

应用和迁移使用 `sslmode=verify-full`，并显式读取 Debian 镜像中的 `/etc/ssl/certs/ca-certificates.crt`。`psycopg[binary]` 自带的 OpenSSL 默认信任路径可能不同于容器系统路径，因此不要仅依赖 `sslrootcert=system`，也不要关闭证书验证来规避错误。

上传返回图片服务不可用时，检查 Blob 的错误码及存储实际 `publicNetworkAccess`。订阅的 `StorageAccount_PublicNetwork_Modify` 策略会强制关闭公网；此时子网服务终结点规则不生效，不要重开公网或修改策略。基础模板已包含 Blob 私有终结点、`10.42.3.0/27` 专用子网及私有 DNS。已有环境经审批后可仅运行 `az deployment group create --resource-group rg-edu-ai-test-c2d102 --name edu-ai-test-blob-private --mode Incremental --template-file infra/blob-private.bicep`，避免重部署整个 VNet 覆盖治理添加的 NSG。客户端仍使用原 Blob 域名；在应用网络内验证其解析为专用子网地址，再重试合成图片。新增私有终结点和 DNS 约 US$7.80/月，另计流量与 DNS 查询。

Hosted Agent 保留运行控制台日志，但关闭 SDK 默认的自动插桩与遥测导出。还须断开 Foundry 项目的 Application Insights 连接：`responsesapi` 服务端追踪不受容器内容开关控制，单独关闭 SDK 也不能阻止模型内容记录。[微软官方停用说明](https://learn.microsoft.com/en-us/azure/foundry/observability/concepts/trace-data#disable-tracing)要求断开监控资源；因此不提供 Foundry 内置调用追踪视图，保留容器/队列运行监控。

旧环境的 `appi-connection` 不会因增量部署删去 Bicep 声明而自动消失。先核对连接属于本测试项目、类别为 `AppInsights` 且目标是专用监控资源，获得批准后仅删除该连接；不要删除 Application Insights 本身或已有日志。发布后以合成请求核对 `AppGenAIContent` 没有新增记录，并留出日志入库延迟。
