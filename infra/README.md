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

此版本已部署到现有合成测试环境，源提交 `985d2f3`，Agent 版本 `7`，API 就绪版本 `0000002`，数据库迁移 `0004`。应用 ACR 构建 `cpc`，镜像摘要 `sha256:94b25924e325818caca7b01bd4883d229343b96a0cf17d3c23d69ce10266dea0`；Agent 构建 `cpb`，摘要 `sha256:9470c54167e27a25c4b5f96fa54a610163dc61d6fa17e20dbd0da272090aa3ef`。模型仍为 `gpt-6.1-sol`，没有修改基础设施、网络、SKU 或监控连接。

此版本包含新的 Agent 操作、Worker 和前端，不能仅更新页面。后续升级继续使用已批准的模型、私网和身份；按现有流程经审批后构建两个隔离上下文、发布新版 Agent，再依次执行 `phase=migrate`、迁移 Job、`phase=worker`、`phase=api`。应用上下文必须包含 `knowledge_demo.py`，避免本地环境掩盖缺失模块。健康检查要求新增表和任务列已经存在。

迁移保留已有数学与英语记录，并为12个内置知识点建立目录。确认新表已向 API/Worker 数据库身份授予读写权限。新增目录或活动存在时不允许直接降级至 `0003`；代码回退也不能撤销已经创建的私人学习数据。

维护权限使用 Entra **API 应用注册**中的应用角色 `Teacher`、`Admin`，角色设为启用、`allowedMemberTypes=["User"]`。现有环境需由获准管理员添加角色定义，并在 API 企业应用中将角色分配给明确指定的测试用户；不向第三个账号或群组开放访问，不关闭应用分配要求。重新登录取得新的 API access token 后才生效。新建注册脚本提供角色定义，但仍默认只分配普通访问，不自动指定教师或管理员。`entra.py verify` 接受已指定两位用户的默认或上述 API 角色，SPA 分配仍必须是默认角色；额外用户、群组或其他角色仍会被拒绝。本次代码实现没有修改云端应用注册或分配。

本次单账号真实合成验收已确认：普通用户没有维护入口；数学上传自动新增「绝对值」，生成 4 道参考练习，提交后获得参考反馈，作答前不显示参考答案，参考活动不写入 `mastery_evidence`。两位用户均按要求保留普通权限，未修改云端应用角色或分配，因此教师维护尚未在线验收。英语图片上传复验、第二账号隔离和未分配账号拒绝登录仍按先前决定延期；不把这些项目标记为通过。

完整上线验收还包括：教师能增加/整理、普通用户维护请求收到403；两个用户看到同一通用目录，但不能读取对方原图、发现记录、参考练习或反馈；数学与英语上传都触发发现；重复上传与并发同名发现只新增一次；低清晰度不扩展目录；重放及重试不重复创建反馈。使用合成题并重新检查延迟入库的模型内容追踪。每页增加一次模型发现调用，参考生成和反馈也按实际调用另计费用。

## 数学开放判分升级

此版本已推送并部署到现有合成测试环境，源提交 `ab5cfb9440f76f22270269fe7eff2a488b074296`。应用 ACR 构建 `cpg`，摘要 `sha256:023bc995266f87f397f116c38857c7efb10b1779703adff8fa875eff2f131cf0`；Agent 构建 `cph`，摘要 `sha256:81bc064556e6d12fa5c29782521a5ebe7006b531c909278cdb18b4e6a5c92668`。Agent `8` 已 active/enabled，API 就绪版本 `0000006`，Worker 使用同版应用镜像并实际更新 `replicaTimeout=7200`。两个阶段的实际资源写入分别只有原有 API 和 Worker，其余有效配置、模型、权限、网络及监控保持不变，没有执行迁移。

数学 `analyze_submission` 版本 `2` 才启用开放评估；新版 Agent 接受版本 `1` 并保留旧响应字段与长度约束，供旧 Worker 在切换期间继续使用。英语、知识点和原有练习操作仍使用版本 `1`。不能只更新前端，也不能让新版 Worker 调用旧 Agent。

一次规范 JPEG 合成题真实调用通过：完整复合参考包含 `-18ab²` 和代入结果 `2`，学生 `-2` 判为错误，两次真实工具结果均为 `2`。对捕获响应执行本地报告持久化复核，确认 `ai_review`、不具备掌握度资格且证据数为0；没有把这个本地持久化步骤冒充 Azure API/Worker 上传全链路。前两次直接调用脚本发送未经预处理的 PNG，被内部 JPEG 契约拒绝；修正脚本而非修改应用。复合参考答案可包含化简和求值两部分，不能用只等于字符串 `"2"` 的断言代替实际数学核验。线上登录上传、跨账号、区域框精度及广泛判分质量仍待验收。

当前提示词的合成评估生成任务已完成：数据集 `open-math-ab5cfb9` 版本 `1.0`、15条；评分器 `open-math-reference-ab5cfb9` 版本 `1`、8维。配置来源为 `services/foundry-agent/.foundry/agent-metadata.yaml` 的 `synthetic-test`，继续使用手动 SDK/metadata 部署上下文，没有初始化 azd 项目。原始 JSONL、评分器、远端标识与[审阅记录](../services/foundry-agent/.foundry/suites/open-math-ab5cfb9/review.json)已保存。所有数据行都是素材准备方案，`candidate_response` 不是线上判分结果；尚无实际 JPEG 或可执行 JSON 请求。生成服务报告数据集用量为501,856 tokens，不是已测费用金额；没有运行批量评估、模型评分或优化。空白语义、掌握度排除、盲解独立性、区域框及生成的 `0.5` 门槛均须校准后再独立审批执行。旧知识点评估及 Agent `7` 的结果保持不变。

经独立部署审批后，沿用现有身份、模型、网络和不可变镜像摘要，构建应用与 Agent 两个隔离上下文。**本次顺序与上面的历史升级不同：先发布兼容版本 `1` 的 Agent，再执行 `phase=api` 并确认新 API 就绪，最后执行 `phase=worker` 启用版本 `2` 的任务。** 每阶段先核对增量 `what-if`，不修改阶段外资源。新 API 可读旧报告；旧 API 不认识新报告字段，所以不得先让新 Worker 写报告再更新 API。

新字段保存在现有报告 JSON，通用知识点复用迁移 `0004` 的目录表，无需新增迁移。开放题即使计算成功也只标为 AI 复核，不写确定性掌握度证据。遇到回滚，应先停用或回退新 Worker，并保留能读取新报告的 API；已经生成新报告后不能直接回退旧 API，更不能删除报告或降级数据库来规避兼容性问题。

每页通常增加两次盲解和一次原图复核调用，混合页还可能有原规则解题调用；模型费用与延迟会增加。新 Worker 的开放数学请求默认最多等待600秒，其他操作保持原125秒预算，并在数学分页与逐题核验前续约，避免多页长任务被重新领取。事件 Worker 的整体执行上限与队列锁续约配套延长到2小时；这是上限而非预期耗时，部署时必须同时更新 `runtime.bicep` 的 Worker 配置。发布验收须用合成图片检查应用题、杨辉三角、多变量、大指数、绝对值和复合任务，并检查解题冲突、工具失败、空白/不清楚作答、旧报告、跨账号隔离和消息重放。离线模型替身不能代替真实识别和判分质量验收；此次仅获准生成草稿，没有批量评估或模型评分执行授权。

## 数学分析超时与断点恢复

源提交 `d5e7c24` 已发布到现有合成测试环境。应用 ACR 构建 `cpk`、摘要 `sha256:a5f5539799138062999603941ab9af2be04881fa955b3541c4b84c4f9d566d3d`；Agent 构建 `cpj`、摘要 `sha256:bbdceebf51c62f20008653cef0327ae5114c233bf3cc4e9dc7c79ec0b0f4183b`。迁移执行 `job-init-edu-ai-test-c2d102-sjeefub` 成功，Agent `9` 已启用，Worker 使用新应用镜像，API 就绪版本 `0000008` 已开启复核入口。未修改模型部署、网络或权限。

新版 Agent 的一次合成 `review_items` 调用返回 HTTP 200，已确认复核返回正确的题目索引和 AI 判分；API 健康检查、线上前端资源及未登录访问拒绝通过。登录后从上传到报告重试的流程未在线复验，复核失败及报告合并由本地回归覆盖。

数学协议 `2` 默认单次模型 SDK 超时为90秒（Agent 的 `EDU_MATH_MODEL_TIMEOUT_SECONDS`），整页处理截止时间为420秒（`EDU_MATH_PAGE_TIMEOUT_SECONDS`）；若收到 Worker 的读取预算，Agent 会再为返回响应预留至少5秒。Worker 的 `EDU_AGENT_TIMEOUT_SECONDS` 默认600秒，旧版与其它操作保持原45秒模型 SDK 超时及至多125秒 Worker 读取等待时间。发布前用合成题确认 Foundry 网关转发预算和任务关联头，并验证实际网关限制足以接收响应；不能用 Worker 的两小时执行上限替代验证。

日志按作业编号记录操作、模型阶段、耗时、上游状态或异常类型，不记录原图、题干、作答或模型响应正文。限流由 Worker 统一退避，不在 Agent 请求里等待 `Retry-After`。可重试任务最多自动尝试3次，使用带抖动的退避（上限120秒）。数学作业重试只请求未完成的页；成功写报告后删除临时断点。开放题的独立解题或原图复核超时可在本页预算内重试当前模型调用一次；仍失败时，本次报告保留已核验题，相关题目显示「待复核」，不判错、不写掌握度证据。此状态不表示后台仍在复核。

报告生成后，学生可在原作业点击一次「重试待复核」。Worker 用现有原图和待复核题的转写重新盲解和复核，不再次转写、判分其他题或覆盖人工核对。结果逐批写入新报告版本，自动重试跳过已写入的题。再次超时或服务失败仍保留已保存的报告；如果解题结论冲突，显示具体原因而非强行判分。

部署包含迁移 `0005` 和兼容旧 Agent 普通响应的新应用。经部署审批后，**先迁移数据库，再更新 API（`mathReviewRetryEnabled=false`）、Worker**。待旧 Worker 执行结束，再发布新版 Agent；确认 `review_items` 请求可用后，用 `phase=api`、`mathReviewRetryEnabled=true` 开放入口。新 Agent 仍接受没有 `review_items` 的旧请求。回滚时先关闭入口、等已排队的复核结束，再回退 Worker 或 Agent；保留能读取带「待复核」标记的新版 API。用合成单题、密集多题、多页作业和故障注入验收：待复核重试不重复读取整页或处理已核验题；复核失败不擦除现有报告，也不产生错误判分或掌握度证据。

## 全 AI 作业批改与掌握度估计

这一版本新增迁移 `0006`，为作业保留上传时的判分模式，并区分规则和 AI 证据。经部署审批后，先应用迁移，再更新 Worker、API 和 Agent；更新期间保持 `mathAiMasteryEnabled=false`。常规请求不发送 `ai_only` 字段；开启后，新作业使用支持 `ai_only=true` 的数学协议 `2`。不能在旧 Agent 或旧 Worker 尚在运行时开启。

用带人工答案的合成样卷对比误判、漏判和耗时，覆盖基础题、复合题、模糊作答和多页超时。核对新报告的主考点、重传去重、原报告内重试，以及知识点地图的来源标记和旧规则练习。准备切换时，用 `phase=api` 和 `mathAiMasteryEnabled=true` 启用新数学作业。已保存的历史报告、证据和英语流程不回填。

回退时先将 `mathAiMasteryEnabled` 设回 `false`，只影响新上传。已采用全 AI 模式的作业（包括后续待复核重试）仍需要支持该协议的 Worker 和 Agent。保留新版 API 与数据库迁移，才能继续读取新报告和估计；不要为了回退而删除 AI 证据或降级数据库。

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
