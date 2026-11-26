# Azure 部署与运维手册

本手册适用于 [`test.settings.json`](test.settings.json) 中已批准的 North Central US 专用环境，**只允许合成数据**。模板和发布脚本绑定该环境的资源命名，部分脚本还校验租户、订阅或镜像来源；不要原样部署到其他订阅。构建、运行和模型调用会产生 Azure 费用。仅想体验应用时，请用[本地演示](../README.md#本地运行)。

[环境与权限](#环境与权限) · [首次部署](#首次部署) · [已有环境升级](#已有环境的分阶段应用升级) · [验收](#发布验收) · [开关与回滚](#功能开关与回滚) · [排障](#部署排障) · [发布记录](#历史发布记录)

## 环境与权限

| 所需组件 | 用途与边界 |
| --- | --- |
| Azure Container Apps、ACR | API/前端、事件 Worker 和手动迁移 Job 共用 `edu-app` 镜像；Foundry 使用独立的 `edu-agent` 镜像。 |
| PostgreSQL Flexible Server、Blob Storage | 存储学习记录和题图。数据库用私网和 Entra 认证，存储通过 Blob 私有终结点，不开启公网、匿名访问或共享密钥。 |
| Service Bus、VNet、私有 DNS | Service Bus 触发 Worker，消息不携带题图；VNet 与私有 DNS 提供数据库和 Blob 的访问路径。 |
| Microsoft Foundry 账户、项目、模型与 Hosted Agent | Worker 经 Entra 调用 Hosted Invocations；Agent 不直接访问数据库、Blob 或队列。基础模板预配 `gpt-5-mini`，当前 Agent 的默认模型另行部署。 |
| Microsoft Entra ID 和托管身份 | 单租户 API/SPA 只向两位已分配的测试用户开放；API、Worker、迁移各用独立身份。 |
| Azure Monitor / Log Analytics | 用于容器和作业的运行日志；**不要将 Application Insights 连接到 Foundry 项目**，见[排障](#部署排障)。 |

发布前准备 Bash、Azure CLI（能运行 `az bicep` 和 `az containerapp`）、`jq`、`curl`、Git、Python 3.12+ 和 uv。部署者须有目标订阅部署资源、指派 Azure 角色、运行 ACR 构建、发布 Foundry Agent 的权限；首次部署还需能通过 Microsoft Graph 注册 API/SPA 并配置企业应用分配。确认所需资源提供程序已注册（尤其 `Microsoft.DBforPostgreSQL`、`Microsoft.ServiceBus`），以及目标地域支持所需 SKU 和模型容量。受限时联系管理员，不自行换区、换 SKU 或放开防火墙。

在仓库根目录用同一个 Bash 会话执行以下准备命令。先登录获准租户，并确认所选订阅；不要将测试用户 ID、访问令牌或凭据写入仓库。

```bash
SETTINGS=infra/test.settings.json
SUBSCRIPTION=$(jq -er .subscriptionId "$SETTINGS")
TENANT=$(jq -er .tenantId "$SETTINGS")
LOCATION=$(jq -er .location "$SETTINGS")
RG=$(jq -er .resourceGroup "$SETTINGS")
ACR=$(jq -er .registry "$SETTINGS")
APP=$(jq -er .api "$SETTINGS")
WORKER=$(jq -er .worker "$SETTINGS")
MIGRATION=$(jq -er .migration "$SETTINGS")

az login --tenant "$TENANT"
az account set --subscription "$SUBSCRIPTION"
az account show --query '{id:id,tenantId:tenantId}' -o table
uv sync --locked --extra hosted
```

## 首次部署

### 1. 核对测试用户，创建基础设施

将 `TEST_USER_1`、`TEST_USER_2` 设为两位获批用户的 **Entra 用户对象 ID**，不是应用客户端 ID。先检查这两位用户仍可用。核对 `what-if` 符合预期并获准后，才执行 `create`。下面的部署者 ID 来自当前 CLI 登录用户。

```bash
.venv/bin/python infra/release/entra.py check-users "$TEST_USER_1" "$TEST_USER_2"
DEPLOYER_OBJECT_ID=$(az ad signed-in-user show --query id -o tsv)
FOUNDATION=edu-ai-test-foundation-nc
az deployment sub what-if --location "$LOCATION" \
  --name "$FOUNDATION" --template-file infra/main.bicep \
  --parameters deployerObjectId="$DEPLOYER_OBJECT_ID"
```

```bash
az deployment sub create --location "$LOCATION" \
  --name "$FOUNDATION" --template-file infra/main.bicep \
  --parameters deployerObjectId="$DEPLOYER_OBJECT_ID"
OUTPUTS=$(az deployment sub show --name "$FOUNDATION" --query properties.outputs -o json)
APP_ORIGIN=$(jq -er .appOrigin.value <<< "$OUTPUTS")
PROJECT_ENDPOINT=$(jq -er .projectEndpoint.value <<< "$OUTPUTS")
REGISTRY_LOGIN=$(jq -er .registryLoginServer.value <<< "$OUTPUTS")
```

确认项目托管身份可拉取 ACR 镜像、调用模型；三个工作负载身份可拉取应用镜像。运行[独立模型部署](#独立模型部署与-agent-切换)中的 `model.bicep` 步骤，确认目标模型就绪后再注册 Agent。仅部署基础设施会缺少当前默认 Agent 使用的模型。

### 2. 配置登录

新环境且尚无注册时使用 `create`。若注册已存在或上次只创建了部分资源，先核对归属和配置，不要重建或关闭「需要分配」；已有完整注册直接走 `verify`。

```bash
.venv/bin/python infra/release/entra.py create "$TEST_USER_1" "$TEST_USER_2" --origin "$APP_ORIGIN"
```

```bash
ENTRA_APPS=$(.venv/bin/python infra/release/entra.py verify \
  "$TEST_USER_1" "$TEST_USER_2" --origin "$APP_ORIGIN")
ENTRA_API_CLIENT_ID=$(jq -er .api.appId <<< "$ENTRA_APPS")
ENTRA_SPA_CLIENT_ID=$(jq -er .spa.appId <<< "$ENTRA_APPS")
```

`appId` 用于部署；`objectId` 用于后续查找或清理注册，不可混用。回调地址为 `$APP_ORIGIN/auth-redirect.html`。脚本默认只授予普通访问权限，不授予 `Teacher` 或 `Admin`。

### 3. 构建镜像，发布 Agent

以干净的发布提交为输入，使用未占用的标签；同一提交重建时换用新标签。构建目录必须在仓库之外。ACR 只接收脚本生成的两个隔离目录，**不要上传仓库根目录**。

```bash
TAG=$(git rev-parse --short HEAD)
BUILD_CONTEXT_DIR=$(mktemp -d /tmp/edu-ai-release.XXXXXX)
.venv/bin/python infra/release/build_context.py --output-dir "$BUILD_CONTEXT_DIR"

az acr build --registry "$ACR" --image "edu-app:$TAG" \
  --file "$BUILD_CONTEXT_DIR/app/Dockerfile" --platform linux/amd64 "$BUILD_CONTEXT_DIR/app"
az acr build --registry "$ACR" --image "edu-agent:$TAG" \
  --file "$BUILD_CONTEXT_DIR/agent/Dockerfile" --platform linux/amd64 "$BUILD_CONTEXT_DIR/agent"
APP_DIGEST=$(az acr repository show --name "$ACR" --image "edu-app:$TAG" --query digest -o tsv)
AGENT_DIGEST=$(az acr repository show --name "$ACR" --image "edu-agent:$TAG" --query digest -o tsv)
printf 'app=%s\nagent=%s\n' "$APP_DIGEST" "$AGENT_DIGEST"
```

确认两个构建成功，且摘要均为 `sha256:` 加 64 位十六进制字符串。不要用标签部署。注册脚本使用 `test.settings.json` 的项目；确认与 `PROJECT_ENDPOINT` 一致。脚本只在版本 `active` 且 Agent `enabled` 时输出 `version` 和 `agentUrl`。

```bash
AGENT_RELEASE=$(.venv/bin/python -m infra.release.hosted_agent "$REGISTRY_LOGIN/edu-agent@$AGENT_DIGEST")
AGENT_URL=$(jq -er .agentUrl <<< "$AGENT_RELEASE")
jq -er .version <<< "$AGENT_RELEASE"
```

记录源提交、构建 ID、两个摘要和版本；发布后清理这次创建的隔离目录。

### 4. 迁移数据库，启用 Worker 和 API

以下函数供首次部署和已有环境升级使用。每个阶段分开执行预览和应用；仅在差异符合预期后运行 `create`。首次保持两个 AI 开关关闭，待兼容组件就绪后再按需要启用。

```bash
MATH_AI=false
ENGLISH_AI=false
MATH_RETRY=true

runtime_stage() {
  az deployment group "$1" --resource-group "$RG" --name "edu-ai-test-$2" \
    --mode Incremental --template-file infra/runtime.bicep \
    --parameters phase="$2" imageDigest="$APP_DIGEST" agentUrl="$AGENT_URL" \
      apiClientId="$ENTRA_API_CLIENT_ID" spaClientId="$ENTRA_SPA_CLIENT_ID" \
      mathAiMasteryEnabled="$MATH_AI" englishAiMasteryEnabled="$ENGLISH_AI" \
      mathReviewRetryEnabled="$MATH_RETRY"
}

runtime_stage what-if migrate
```

```bash
runtime_stage create migrate
MIGRATION_EXECUTION=$(az containerapp job start -g "$RG" -n "$MIGRATION" --query name -o tsv)
az containerapp job execution show -g "$RG" -n "$MIGRATION" \
  --job-execution-name "$MIGRATION_EXECUTION" --query properties.status -o tsv
```

迁移 Job 在私网内创建数据库、映射应用身份、执行 Alembic 并授予读写权限。重复**查询同一次执行**，直到 `Succeeded`；不要为查询状态再次启动。失败时停止发布，按[排障](#部署排障)处理。当前代码的迁移版本是 `0006`。迁移成功、登录配置 `verify` 通过后，按顺序预览和发布 `worker`、`api`：

```bash
runtime_stage what-if worker
# 审阅差异后运行：runtime_stage create worker
runtime_stage what-if api
# Worker 已配置且审阅差异后运行：runtime_stage create api
```

`phase=serve` 会一次更新 Worker 和 API；需要控制顺序时用上述独立阶段。上线后执行[发布验收](#发布验收)。

## 已有环境的分阶段应用升级

沿用现有资源，不重放 `main.bicep`，不改变身份、网络或监控连接。先保存当前 Agent 版本、两种镜像、API revision/流量、三个开关和数据库迁移版本。读取现有入口，按[配置登录](#2-配置登录)使用 `verify` 取得两个客户端 ID，不执行 `create`。

```bash
APP_FQDN=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)
APP_ORIGIN="https://$APP_FQDN"
REGISTRY_LOGIN=$(az acr show --name "$ACR" --query loginServer -o tsv)
PROJECT_ENDPOINT="https://$(jq -er .foundry "$SETTINGS").services.ai.azure.com/api/projects/$(jq -er .project "$SETTINGS")"
```

按[构建与注册](#3-构建镜像发布-agent)准备不可变镜像；只在迁移有变化时运行 `phase=migrate` 和迁移 Job。使用 Bicep 更新 API 时，把上述 `MATH_AI`、`ENGLISH_AI`、`MATH_RETRY` 设成读回的线上值（或本次明确批准的目标值），不要沿用首次部署的默认值。

通常先发布可服务旧 Worker 的新版 Agent，再升级 Worker，最后升级 API 并打开新功能。切换前检查协议和报告的双向兼容性：若旧 API 不能读取新 Worker 写出的报告，先部署能读取旧报告的新 API，并保持新功能关闭。等待旧任务完成或确认它们能由新版继续处理；不让旧 Worker 消费它不理解的新任务。发布记录中的特殊次序仅适用于对应版本。

如果预览包含不相关的 registry、ingress 或 scale 变更，不要套用模板。**仅更新镜像且 `EDU_AGENT_URL` 等配置未变时**，可用已验证过的定点方式；先更新 Worker 并核对配置，再更新 API：

```bash
APP_IMAGE="$REGISTRY_LOGIN/edu-app@$APP_DIGEST"
az containerapp job update -g "$RG" -n "$WORKER" --container-name worker --image "$APP_IMAGE"
az containerapp update -g "$RG" -n "$APP" --container-name api --image "$APP_IMAGE"
```

若阶段失败，停止后续步骤，按兼容性选择回滚，不删除已写入的数据。

## 发布验收

先查镜像、开关、正在承接流量的 revision 和 HTTP 结果。已有环境的 `APP_ORIGIN` 可从 API 的 HTTPS ingress FQDN 取得。

```bash
az containerapp job show -g "$RG" -n "$WORKER" -o json | jq -e '
  .properties.template.containers[] | select(.name == "worker") |
  {image, agentUrl: (.env[] | select(.name == "EDU_AGENT_URL") | .value)}
'
az containerapp revision list -g "$RG" -n "$APP" \
  --query '[].{revision:name,health:properties.healthState,traffic:properties.trafficWeight}' -o table
az containerapp show -g "$RG" -n "$APP" -o json | jq -e '
  .properties.template.containers[] | select(.name == "api") |
  {image, flags: [.env[] | select(.name | test("^EDU_(MATH_(AI_MASTERY|REVIEW_RETRY)|ENGLISH_AI_MASTERY)_ENABLED$"))]}
'
curl -fsS "$APP_ORIGIN/api/health"
curl -sS -o /dev/null -w '%{http_code}\n' "$APP_ORIGIN/api/submissions"
```

预期：Worker 和 API 均使用目标镜像，Worker 指向预期 Agent；目标 revision 健康且承接 100% 流量。健康接口返回 `{"status":"ready"}`，匿名业务请求返回 `401`。健康检查不调用模型。

然后用独立标注的合成题验收。直接调用 Agent 只能核对协议和样题结果；还要用两位已分配账号登录，覆盖数学/英语上传、批改、待复核重试、数学规则练习、英语客观练习和掌握度入库。检查重投递不重复计分、未分配账号拒绝登录，以及两个账号无法读取对方的题图或报告。教学质量另行测量：分别统计自动判分覆盖率（目标至少 80%）和已判分项与人工标注的一致率（目标至少 95%）。不能用模型冒烟或本地替身代替这些验收。

## 功能开关与回滚

三个开关都在 API 容器上。设置前先读回当前值；改完重复[发布验收](#发布验收)的版本、开关和流量检查。

| API 环境变量 | Bicep 参数 | 默认值 | 影响 |
| --- | --- | --- | --- |
| `EDU_MATH_AI_MASTERY_ENABLED` | `mathAiMasteryEnabled` | `false` | 新数学作业 |
| `EDU_ENGLISH_AI_MASTERY_ENABLED` | `englishAiMasteryEnabled` | `false` | 新英语作业和新建客观练习 |
| `EDU_MATH_REVIEW_RETRY_ENABLED` | `mathReviewRetryEnabled` | `true` | 数学报告的待复核重试入口 |

例如，在 API、Worker 和 Agent 均已支持英语新协议后，只开启英语，不改其他变量：

```bash
az containerapp update -g "$RG" -n "$APP" --container-name api \
  --set-env-vars EDU_ENGLISH_AI_MASTERY_ENABLED=true
```

关闭时把相应值改为 `false`。**AI 模式在记录创建时锁定**：关闭开关不会重算旧报告、删除证据或取消已排队任务。已锁定 AI 模式的作业、练习和后续重试仍依赖兼容的 Worker 与 Agent；已有新格式报告时也须保留能读取它们的 API。确需回退镜像或模型时，先阻止新任务进入不兼容的模式，核对已排队/执行任务及历史数据读取能力，再恢复兼容版本，不直接降级数据库。

经审批退役测试环境时，按资源组、两项专用 Entra 注册及企业应用的对象 ID 核对并清理，不按名称批量删除。只停用客户端访问不会停止 Azure 资源计费。

## 历史发布记录

以下是各版本发布时的事实，不是当前部署指令。最近一次记录为 [英语 AI 批改与掌握度](#英语-ai-批改与掌握度)：API `0000011`、Agent `11`、数据库 `0006`，三个开关均开启。

### 共享知识点版本升级

此版本已部署到现有合成测试环境，源提交 `985d2f3`，Agent 版本 `7`，API 就绪版本 `0000002`，数据库迁移 `0004`。应用 ACR 构建 `cpc`，镜像摘要 `sha256:94b25924e325818caca7b01bd4883d229343b96a0cf17d3c23d69ce10266dea0`；Agent 构建 `cpb`，摘要 `sha256:9470c54167e27a25c4b5f96fa54a610163dc61d6fa17e20dbd0da272090aa3ef`。模型仍为 `gpt-6.1-sol`，没有修改基础设施、网络、SKU 或监控连接。

此版本包含新的 Agent 操作、Worker 和前端，不能仅更新页面。后续升级继续使用已批准的模型、私网和身份；按现有流程经审批后构建两个隔离上下文、发布新版 Agent，再依次执行 `phase=migrate`、迁移 Job、`phase=worker`、`phase=api`。应用上下文必须包含 `knowledge_demo.py`，避免本地环境掩盖缺失模块。健康检查要求新增表和任务列已经存在。

迁移保留已有数学与英语记录，并为12个内置知识点建立目录。确认新表已向 API/Worker 数据库身份授予读写权限。新增目录或活动存在时不允许直接降级至 `0003`；代码回退也不能撤销已经创建的私人学习数据。

维护权限使用 Entra **API 应用注册**中的应用角色 `Teacher`、`Admin`，角色设为启用、`allowedMemberTypes=["User"]`。现有环境需由获准管理员添加角色定义，并在 API 企业应用中将角色分配给明确指定的测试用户；不向第三个账号或群组开放访问，不关闭应用分配要求。重新登录取得新的 API access token 后才生效。新建注册脚本提供角色定义，但仍默认只分配普通访问，不自动指定教师或管理员。`entra.py verify` 接受已指定两位用户的默认或上述 API 角色，SPA 分配仍必须是默认角色；额外用户、群组或其他角色仍会被拒绝。本次代码实现没有修改云端应用注册或分配。

本次单账号真实合成验收已确认：普通用户没有维护入口；数学上传自动新增「绝对值」，生成 4 道参考练习，提交后获得参考反馈，作答前不显示参考答案，参考活动不写入 `mastery_evidence`。两位用户均按要求保留普通权限，未修改云端应用角色或分配，因此教师维护尚未在线验收。英语图片上传复验、第二账号隔离和未分配账号拒绝登录仍按先前决定延期；不把这些项目标记为通过。

完整上线验收还包括：教师能增加/整理、普通用户维护请求收到403；两个用户看到同一通用目录，但不能读取对方原图、发现记录、参考练习或反馈；数学与英语上传都触发发现；重复上传与并发同名发现只新增一次；低清晰度不扩展目录；重放及重试不重复创建反馈。使用合成题并重新检查延迟入库的模型内容追踪。每页增加一次模型发现调用，参考生成和反馈也按实际调用另计费用。

### 数学开放判分升级

此版本已推送并部署到现有合成测试环境，源提交 `ab5cfb9440f76f22270269fe7eff2a488b074296`。应用 ACR 构建 `cpg`，摘要 `sha256:023bc995266f87f397f116c38857c7efb10b1779703adff8fa875eff2f131cf0`；Agent 构建 `cph`，摘要 `sha256:81bc064556e6d12fa5c29782521a5ebe7006b531c909278cdb18b4e6a5c92668`。Agent `8` 已 active/enabled，API 就绪版本 `0000006`，Worker 使用同版应用镜像并实际更新 `replicaTimeout=7200`。两个阶段的实际资源写入分别只有原有 API 和 Worker，其余有效配置、模型、权限、网络及监控保持不变，没有执行迁移。

数学 `analyze_submission` 版本 `2` 才启用开放评估；新版 Agent 接受版本 `1` 并保留旧响应字段与长度约束，供旧 Worker 在切换期间继续使用。英语、知识点和原有练习操作仍使用版本 `1`。不能只更新前端，也不能让新版 Worker 调用旧 Agent。

一次规范 JPEG 合成题真实调用通过：完整复合参考包含 `-18ab²` 和代入结果 `2`，学生 `-2` 判为错误，两次真实工具结果均为 `2`。对捕获响应执行本地报告持久化复核，确认 `ai_review`、不具备掌握度资格且证据数为0；没有把这个本地持久化步骤冒充 Azure API/Worker 上传全链路。前两次直接调用脚本发送未经预处理的 PNG，被内部 JPEG 契约拒绝；修正脚本而非修改应用。复合参考答案可包含化简和求值两部分，不能用只等于字符串 `"2"` 的断言代替实际数学核验。线上登录上传、跨账号、区域框精度及广泛判分质量仍待验收。

当前提示词的合成评估生成任务已完成：数据集 `open-math-ab5cfb9` 版本 `1.0`、15条；评分器 `open-math-reference-ab5cfb9` 版本 `1`、8维。配置来源为 `services/foundry-agent/.foundry/agent-metadata.yaml` 的 `synthetic-test`，继续使用手动 SDK/metadata 部署上下文，没有初始化 azd 项目。原始 JSONL、评分器、远端标识与[审阅记录](../services/foundry-agent/.foundry/suites/open-math-ab5cfb9/review.json)已保存。所有数据行都是素材准备方案，`candidate_response` 不是线上判分结果；尚无实际 JPEG 或可执行 JSON 请求。生成服务报告数据集用量为501,856 tokens，不是已测费用金额；没有运行批量评估、模型评分或优化。空白语义、掌握度排除、盲解独立性、区域框及生成的 `0.5` 门槛均须校准后再独立审批执行。旧知识点评估及 Agent `7` 的结果保持不变。

经独立部署审批后，沿用现有身份、模型、网络和不可变镜像摘要，构建应用与 Agent 两个隔离上下文。**本次顺序与上面的历史升级不同：先发布兼容版本 `1` 的 Agent，再执行 `phase=api` 并确认新 API 就绪，最后执行 `phase=worker` 启用版本 `2` 的任务。** 每阶段先核对增量 `what-if`，不修改阶段外资源。新 API 可读旧报告；旧 API 不认识新报告字段，所以不得先让新 Worker 写报告再更新 API。

新字段保存在现有报告 JSON，通用知识点复用迁移 `0004` 的目录表，无需新增迁移。开放题即使计算成功也只标为 AI 复核，不写确定性掌握度证据。遇到回滚，应先停用或回退新 Worker，并保留能读取新报告的 API；已经生成新报告后不能直接回退旧 API，更不能删除报告或降级数据库来规避兼容性问题。

每页通常增加两次盲解和一次原图复核调用，混合页还可能有原规则解题调用；模型费用与延迟会增加。新 Worker 的开放数学请求默认最多等待600秒，其他操作保持原125秒预算，并在数学分页与逐题核验前续约，避免多页长任务被重新领取。事件 Worker 的整体执行上限与队列锁续约配套延长到2小时；这是上限而非预期耗时，部署时必须同时更新 `runtime.bicep` 的 Worker 配置。发布验收须用合成图片检查应用题、杨辉三角、多变量、大指数、绝对值和复合任务，并检查解题冲突、工具失败、空白/不清楚作答、旧报告、跨账号隔离和消息重放。离线模型替身不能代替真实识别和判分质量验收；此次仅获准生成草稿，没有批量评估或模型评分执行授权。

### 数学分析超时与断点恢复

源提交 `d5e7c24` 已发布到现有合成测试环境。应用 ACR 构建 `cpk`、摘要 `sha256:a5f5539799138062999603941ab9af2be04881fa955b3541c4b84c4f9d566d3d`；Agent 构建 `cpj`、摘要 `sha256:bbdceebf51c62f20008653cef0327ae5114c233bf3cc4e9dc7c79ec0b0f4183b`。迁移执行 `job-init-edu-ai-test-c2d102-sjeefub` 成功，Agent `9` 已启用，Worker 使用新应用镜像，API 就绪版本 `0000008` 已开启复核入口。未修改模型部署、网络或权限。

新版 Agent 的一次合成 `review_items` 调用返回 HTTP 200，已确认复核返回正确的题目索引和 AI 判分；API 健康检查、线上前端资源及未登录访问拒绝通过。登录后从上传到报告重试的流程未在线复验，复核失败及报告合并由本地回归覆盖。

数学协议 `2` 默认单次模型 SDK 超时为90秒（Agent 的 `EDU_MATH_MODEL_TIMEOUT_SECONDS`），整页处理截止时间为420秒（`EDU_MATH_PAGE_TIMEOUT_SECONDS`）；若收到 Worker 的读取预算，Agent 会再为返回响应预留至少5秒。Worker 的 `EDU_AGENT_TIMEOUT_SECONDS` 默认600秒，旧版与其它操作保持原45秒模型 SDK 超时及至多125秒 Worker 读取等待时间。发布前用合成题确认 Foundry 网关转发预算和任务关联头，并验证实际网关限制足以接收响应；不能用 Worker 的两小时执行上限替代验证。

日志按作业编号记录操作、模型阶段、耗时、上游状态或异常类型，不记录原图、题干、作答或模型响应正文。限流由 Worker 统一退避，不在 Agent 请求里等待 `Retry-After`。可重试任务最多自动尝试3次，使用带抖动的退避（上限120秒）。数学作业重试只请求未完成的页；成功写报告后删除临时断点。开放题的独立解题或原图复核超时可在本页预算内重试当前模型调用一次；仍失败时，本次报告保留已核验题，相关题目显示「待复核」，不判错、不写掌握度证据。此状态不表示后台仍在复核。

报告生成后，学生可在原作业点击一次「重试待复核」。Worker 用现有原图和待复核题的转写重新盲解和复核，不再次转写、判分其他题或覆盖人工核对。结果逐批写入新报告版本，自动重试跳过已写入的题。再次超时或服务失败仍保留已保存的报告；如果解题结论冲突，显示具体原因而非强行判分。

部署包含迁移 `0005` 和兼容旧 Agent 普通响应的新应用。经部署审批后，**先迁移数据库，再更新 API（`mathReviewRetryEnabled=false`）、Worker**。待旧 Worker 执行结束，再发布新版 Agent；确认 `review_items` 请求可用后，用 `phase=api`、`mathReviewRetryEnabled=true` 开放入口。新 Agent 仍接受没有 `review_items` 的旧请求。回滚时先关闭入口、等已排队的复核结束，再回退 Worker 或 Agent；保留能读取带「待复核」标记的新版 API。用合成单题、密集多题、多页作业和故障注入验收：待复核重试不重复读取整页或处理已核验题；复核失败不擦除现有报告，也不产生错误判分或掌握度证据。

### 全 AI 作业批改与掌握度估计

源提交 `beadedadf60adde2b166613ed0668190b19b76e1` 已发布到现有合成测试环境。应用 ACR 构建 `cpn`、摘要 `sha256:444c73ac87d17ee2285503a5d7f8c7c140909a5e9d9269dda75f7bf205670c68`；Agent 构建 `cpm`、摘要 `sha256:6d6f036720efae529900a2633b4ffe18fe8961d26935a88efa37e25a9a11142c`。迁移执行 `job-init-edu-ai-test-c2d102-3hhs4fe` 成功，API 就绪版本为 `0000009`，Worker 使用同版镜像，Hosted Agent `10` 已启用。全 AI 开关为关闭，原有待复核重试入口保持开启；模型、网络和权限未变更。

对 Agent `10` 进行了一次 `ai_only=true` 的真实合成题调用：`(-8)+3` 的作答 `-5` 判为正确并归入「有理数加减」，`2×(-7)` 的作答 `14` 判为错误并归入「有理数乘除」。API 健康检查、匿名访问拒绝和前端资源核对通过。此调用没有经登录上传、Worker 或线上掌握度写入，不等同于全链路验收或判分质量评估；未运行新的批量评估。

这一版本新增迁移 `0006`，为作业保留上传时的判分模式，并区分规则和 AI 证据。发布顺序为迁移、API、Worker、Agent；更新期间保持 `mathAiMasteryEnabled=false`。常规请求不发送 `ai_only` 字段；开启后，新作业使用支持 `ai_only=true` 的数学协议 `2`。不能在旧 Agent 或旧 Worker 尚在运行时开启。

用带人工答案的合成样卷对比误判、漏判和耗时，覆盖基础题、复合题、模糊作答和多页超时。核对新报告的主考点、重传去重、原报告内重试，以及知识点地图的来源标记和旧规则练习。准备切换时，用 `phase=api` 和 `mathAiMasteryEnabled=true` 启用新数学作业。已保存的历史报告、证据和英语流程不回填。

回退时先将 `mathAiMasteryEnabled` 设回 `false`，只影响新上传。已采用全 AI 模式的作业（包括后续待复核重试）仍需要支持该协议的 Worker 和 Agent。保留新版 API 与数据库迁移，才能继续读取新报告和估计；不要为了回退而删除 AI 证据或降级数据库。

### 英语 AI 批改与掌握度

源提交 `cef80000c9a20db3307146aee7a91237c12a9589` 已发布到现有合成测试环境。应用 ACR 构建 `cpq`、摘要 `sha256:969d072bb7db03bc82aaf9f1aa92b5e1bd42973ba0f6cfb57f5850cf1496e5a0`；Agent 构建 `cpp`、摘要 `sha256:b6db35318e2cf9f8a3f04852c8c775b50d603c25d86964970f77dff06c4c1570`。Hosted Agent `11` 已启用，Worker 使用新应用镜像，API 就绪版本 `0000011` 承接 100% 流量，已开启英语 AI 批改。为了保留现有配置，仅更新了 Worker 镜像、API 镜像和英语开关，没有重放运行模板或执行数据库迁移。数学 AI 批改与待复核重试仍开启，模型、网络、身份和权限不变。

新版 Agent 的一次真实合成英语样卷调用返回两道有效结果：`She ___ a student.` 选 `is` 判正确，`book` 的复数写成 `book` 判错误，均归入语法。API 健康检查、匿名访问拒绝和新前端资源通过核对；没有在 Azure 上登录上传、提交在线练习或验证掌握度入库，也未运行批量评估。

英语复核复用现有 `0006` 数据库和协议 `2` 超时预算，不需要新迁移。新版 Agent 和 Worker 支持 `analyze_english` 版本 `2`（含原报告待复核批次）及在线客观练习的 `grade_english`。旧分析、练习生成和表达反馈的版本 `1` 仍可用。新版 API、Worker 和 Agent 全部就绪之前保持 `englishAiMasteryEnabled=false`。

实际切换前，用合成样卷和在线客观练习核对双解冲突、部分正确、留白、作答隐藏、原报告重试、样本来源及跨账号隔离。启用或回退只需调整 `phase=api` 的 `englishAiMasteryEnabled`；提交模板时显式传入当前的 `mathAiMasteryEnabled` 和 `mathReviewRetryEnabled`，避免默认值改变现有数学行为。回退只影响新作业和新练习；已经锁定 AI 模式的记录继续依赖新版 Worker 和 Agent。

## 独立模型部署与 Agent 切换

`test.settings.json.model` 保留基础模板的 `gpt-5-mini`；`agentModel` 独立配置 Agent 的目标模型。当前选定 `gpt-6.1-sol`、版本 `2026-09-29`、`DataZoneStandard` 容量30（30,000 TPM）。它按实际 token 计费，不是 PTU；美国数据区推理不是单一地域推理，仍只允许合成数据。部署前重新核对配额和费用，不把容量当作费用上限。

在目标资源组使用 `model.bicep`，不重放基础模板。预览差异并获准后再应用：

```bash
az deployment group what-if -g "$RG" --name edu-ai-test-agent-model \
  --mode Incremental --template-file infra/model.bicep
```

```bash
az deployment group create -g "$RG" --name edu-ai-test-agent-model \
  --mode Incremental --template-file infra/model.bicep
az cognitiveservices account deployment show -g "$RG" \
  --name "$(jq -er .foundry "$SETTINGS")" \
  --deployment-name "$(jq -er .agentModel.deployment "$SETTINGS")" \
  --query '{state:properties.provisioningState,model:properties.model,sku:sku}'
```

确认模型状态为 `Succeeded`，且支持 Responses、图片与结构化输出，再返回[首次部署](#首次部署)。对于仅切换模型的升级，可用现有 Agent 镜像摘要运行注册脚本，无需重建镜像或更新 API/Worker。脚本比较完整定义，相同镜像但模型不同会创建新版本，完全相同则复用。发布后核对 `active` / `enabled`、实际 `MODEL_DEPLOYMENT_NAME` 和合成请求结果，不只检查控制台模型名称。

保留旧模型和版本。若需回退，用相同镜像运行以下命令创建恢复旧模型配置的 Agent 版本；不删除新模型或回退数据库：

```bash
.venv/bin/python -m infra.release.hosted_agent \
  "$REGISTRY_LOGIN/edu-agent@$AGENT_DIGEST" --model-deployment gpt-5-mini
```

`--model-deployment` 是显式临时覆盖，不改 `agentModel` 默认值；后续正常发布仍使用配置中的新模型。不要为切换模型开启内容追踪或增加 Agent 的数据库、Blob 权限。

## 部署排障

| 现象 | 先检查 |
| --- | --- |
| 页面可打开但登录失败 | 用 `entra.py verify` 核对两项注册、回调地址和两位测试用户的分配。匿名访问业务 API 返回 `401` 是预期行为。 |
| 上传成功但作业长期未开始 | 对照作业 ID 查 API 派发和 Worker 日志、Service Bus 队列积压与死信、事件 Job 执行状态；先查原因，不手动重复提交。 |
| 报告显示「待复核」 | 已判题仍有效，待复核不等于判错或仍在后台运行。有重试入口时在原报告重试，不重新上传。 |
| Agent 超时或限流 | 查看模型阶段、上游状态和任务耗时。协议 v2 默认单次模型调用 90 秒、整页 420 秒、Worker 单次 Agent 等待上限 600 秒；Worker 整个执行上限 7200 秒。这些是不同层的预算，不是延迟承诺。 |

迁移连接超时时，先检查 PostgreSQL 的 `state` 和活动日志，再检查私网 DNS、路由和身份认证。若订阅治理自动化停止了服务器，联系订阅管理员确认并处理治理限制；不要通过反复启动服务器、放开公网或更换身份来绕过限制。服务器恢复为 `Ready` 后，再重跑迁移并核对执行状态。

应用和迁移使用 `sslmode=verify-full`，并显式读取 Debian 镜像中的 `/etc/ssl/certs/ca-certificates.crt`。`psycopg[binary]` 自带的 OpenSSL 默认信任路径可能不同于容器系统路径，因此不要仅依赖 `sslrootcert=system`，也不要关闭证书验证来规避错误。

上传返回图片服务不可用时，检查 Blob 的错误码及存储实际 `publicNetworkAccess`。订阅的 `StorageAccount_PublicNetwork_Modify` 策略会强制关闭公网；此时子网服务终结点规则不生效，不要重开公网或修改策略。基础模板已包含 Blob 私有终结点、`10.42.3.0/27` 专用子网及私有 DNS。已有环境经审批后可仅运行 `az deployment group create --resource-group rg-edu-ai-test-c2d102 --name edu-ai-test-blob-private --mode Incremental --template-file infra/blob-private.bicep`，避免重部署整个 VNet 覆盖治理添加的 NSG。客户端仍使用原 Blob 域名；在应用网络内验证其解析为专用子网地址，再重试合成图片。新增私有终结点和 DNS 约 US$7.80/月，另计流量与 DNS 查询。

Hosted Agent 保留运行控制台日志，但关闭 SDK 默认的自动插桩与遥测导出。还须断开 Foundry 项目的 Application Insights 连接：`responsesapi` 服务端追踪不受容器内容开关控制，单独关闭 SDK 也不能阻止模型内容记录。[微软官方停用说明](https://learn.microsoft.com/en-us/azure/foundry/observability/concepts/trace-data#disable-tracing)要求断开监控资源；因此不提供 Foundry 内置调用追踪视图，保留容器/队列运行监控。

旧环境的 `appi-connection` 不会因增量部署删去 Bicep 声明而自动消失。先核对连接属于本测试项目、类别为 `AppInsights` 且目标是专用监控资源，获得批准后仅删除该连接；不要删除 Application Insights 本身或已有日志。发布后以合成请求核对 `AppGenAIContent` 没有新增记录，并留出日志入库延迟。

## 合成评估记录

Agent 根目录的 [`.foundry/agent-metadata.yaml`](../services/foundry-agent/.foundry/agent-metadata.yaml) 记录发布版本和素材来源。[开放数学评估草稿](../services/foundry-agent/.foundry/suites/open-math-ab5cfb9/review.json) 未做批量运行；草稿 `candidate_response` 不是线上结果，生成的 `0.5` 门槛不是质量标准。

[知识点评估集](../services/foundry-agent/.foundry/suites/knowledge-smoke-curated-v1.json) 包含 15 个请求和 6 张规范合成 JPEG，蓝色作答为机器排版。从仓库根可执行以下离线命令，不登录 Azure、不调用模型：

```bash
.venv/bin/python -m edu_agent.knowledge_evaluation prepare
.venv/bin/python -m edu_agent.knowledge_evaluation validate
```

对 Agent `7` 的一次获批线上运行中，结构检查通过 14/15，模型评分通过 13/15，两者同时通过 12/15。人工复核还发现 5 道题的区域框未覆盖完整题干，部分用例预期也需要校准。这些结果不能证明通用判分准确率、手写识别质量或后续版本表现。

再次运行线上评估需单独授权，且须核对所测 Agent 版本和镜像摘要；`run --approve-live` 会发送真实合成请求，`--with-judge` 会额外调用评分模型，均会计费。结果写入被 Git 忽略的 `.foundry/results/`。它不会更新部署、数据库或监控配置。
