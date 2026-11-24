# 作业回路

面向初一学生的中文数学学习助手。上传作业或试卷题图，查看逐题批改和知识点掌握情况，再按知识点做针对性练习。首版支持有理数、整式和一元一次方程中的单选、填空和计算题。

## 本地运行

准备 Python 3.12 或更高版本、[uv](https://docs.astral.sh/uv/)、Node.js 22.12 或更高版本。在仓库根目录运行：

```bash
uv sync --locked
npm --prefix apps/web ci
npm --prefix apps/web run build
EDU_MODE=local .venv/bin/python -m edu_app migrate
EDU_MODE=local .venv/bin/python -m edu_app serve --with-worker
```

打开 <http://127.0.0.1:8000>，点「试用合成样卷」。也可在右上角切换两个演示账号；作业、练习和掌握度各自独立。本地默认写入 `.edu-ai/`，使用 SQLite 和私有图片目录，不需要 Azure 账号或模型密钥。**默认替身只分析自带合成样卷**；自行上传的图片不会得到模拟分数。请勿上传真实学生数据。

## 批改与练习的规则

- 不使用题库、教师答案或预存的练习题。Azure 路径中，Foundry Agent 先看图转写，另一次模型调用只看数学表达式、题意和选项，不看学生作答。Worker 用精确分数和数学规则复核；读不清、题型不支持（如文字应用题）或结论冲突都显示「无法判定」，不当作错题。
- 知识点按最近 10 道不同题的有效作答计算，少于 3 道显示「证据不足」。重传不累计；修改转写产生参考结果，不计入掌握度。
- 练习只有通过题意、难度、唯一答案和去重校验才会发布。提交前不展示答案；看过提示的作答不计入掌握度。输入示例：`-2/3`、`3x-5`、`x=4`。

## 项目结构与验证

`apps/web` 是 React 学生端；`services/app` 是 FastAPI、队列 Worker 和数据库迁移；`services/foundry-agent` 是 Foundry Invocations 入口及本地替身；`packages/domain` 是共享契约、图片预处理和数学验证器。

```bash
uv sync --locked --all-extras
.venv/bin/python -m pytest
npm --prefix apps/web run build
npm --prefix apps/web exec -- playwright install chromium
npm --prefix apps/web run test:e2e
```

Python 测试默认使用临时 SQLite；设置 `EDU_TEST_DATABASE_URL` 为专用测试 PostgreSQL 的 `postgresql+psycopg://…` 地址可同时检查两个后端（需有权限创建和删除临时 schema）。浏览器测试自行启动使用临时数据库的本地应用。使用已安装的 Chrome 时，可跳过下载 Chromium 并设置 `PLAYWRIGHT_CHROMIUM_CHANNEL=chrome`。

**Azure 测试尚未部署。** 代码包含后端 PostgreSQL/Blob Storage/Service Bus、Entra ID 和 Foundry 适配器；当前没有基础设施模板或已配置的资源。真实模型的自动判分覆盖率和与人工判分一致率必须在获批后的独立 Azure 环境用合成样本测量，本地替身不能证明这些指标。
