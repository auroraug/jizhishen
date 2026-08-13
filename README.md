# 集智审：农村集体建设工程全过程智能审查

面向镇级、中心村和农村集体资产监管部门的局域网 AI 审查 MVP。系统把项目原件转换为可定位事实，再以 SQL/Python 规则、LLM 语义一致性和政策检索完成交叉核验。

## 本版本的真实链路

- 项目默认空白创建，建档预算和合同额不会伪装成 AI 抽取事实。
- PDF/DOCX/TXT/图片支持批量上传、SHA256 去重、原件预览、段落版面坐标和可恢复删除。
- 扫描件只通过 MinerU 云端 OCR；未来本地 MinerU 已预留供应商类型，不接 OpenAI 视觉 OCR。
- 审查按资料分类、通用字段、付款、变更、合同、语义一致性、政策查询重排、Text2SQL、结果摘要和 OCR 十个可配置阶段组织。没有 Text2SQL 或 OCR 任务时明确记录为“未执行”，不伪造调用。
- 每个 OpenAI-compatible LLM 阶段可配置不同 base URL 和模型，也可在单次运行前覆盖；没有自动模型 fallback。Base URL 按完整 API 根路径使用（例如 llama.cpp 的 `/v1` 或火山方舟的 `/api/v3`），系统不会擅自追加路径；无密钥的本地服务也受支持。
- 算术、日期、付款上限、变更比例和资料完整性由只读 SQL/Python 规则执行。
- 证据定位到真实文档、页码、段落块和 bbox，在原 PDF 页面叠加高亮，不把文档硬拆成逐行伪预览。

## 完整可观测性

访问主站 `/trace`：

- 按项目和 run 查看每个阶段的 messages、system prompt、user prompt、模板变量、请求参数、原始供应商响应、structured output、Schema 校验、工具调用、规则输入输出、耗时和错误。
- 小于等于 256KB 的工件存 SQLite；更大的工件 gzip 落盘，并记录 SHA256 和原始大小。
- 支持完整 Trace JSON、Trace + 项目原件 ZIP 导出，以及永久删除。
- 单阶段测试保存为 `prompt_test` run，不修改项目结果；派生运行复用上游不可变快照并保存下游输出，也不覆盖原运行。
- Trace 永久保留，除非在调试控制台明确永久删除。删除最新业务 run 后会回退到上一业务 run 并重算风险角标。
- Authorization、API Key、Token、密码等字段在 Trace、API 和导出中统一脱敏。

注意：按当前 MVP 决策，`/trace` 没有访问保护，供应商密钥也以明文保存在服务器配置文件。只能部署在可信内网，禁止直接暴露到互联网。

## 模型与 Prompt

- `/trace#models` 可添加、删除、测试 OpenAI-compatible 供应商，发现 `/v1/models`，并为十个阶段配置全局默认路由。
- 配置原子写入持久化数据目录中的 `model-config.json`；真实文件已加入 `.gitignore`，仓库仅提供 `backend/model-config.example.json`。
- 模型配置本身暂不做版本管理，但每个 run 会固化当次有效配置快照。
- `/trace#prompts` 提供按阶段的草稿—测试—发布。发布版本不可修改；修改必须复制为新草稿。每个 run 固化完整 prompt_version、模板、JSON Schema 和 parser_version。

## 示例项目隔离

服务启动不再自动 seed。需要时在“项目台账”点击“安装示例项目”。示例项目带 `is_demo` 标记，默认不计入首页、风险角标、风险中心或业务运行统计，也不用于自动化全链路测试。

`structured_demo_data.json`、开发者真值表和全过程结构化业务数据只能在运行结束后用于验收比对，不得进入数据库、Prompt 或模型输入作为预置答案。

## 运行

复制 `.env.example` 为 `.env`，可填入云端 LLM 和 MinerU 作为首次启动配置：

```bash
docker compose up -d --build
```

访问 `http://工作站IP:8060`；调试控制台为 `/trace`；接口文档为 `/api/docs`。本轮代码只在本机完成和验证，尚未部署到局域网工作站。

## 验证

前端构建与 SSR：

```bash
npm test
```

后端隔离 Mock 全链路：

```bash
python -m unittest -v tests.test_observable_mvp
```

测试创建独立 `TEST-*` 项目和本地 OpenAI-compatible Mock，覆盖成功、超时、非法 JSON、tool call、Schema 失败、无隐藏 fallback、Prompt 不可变发布、派生运行、Trace 压缩/导出/删除等场景，不读取示例项目或验收真值。
