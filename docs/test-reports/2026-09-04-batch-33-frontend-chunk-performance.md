# Batch 33 测试报告：前端 chunk 与统计页性能收尾

## 1. 结论

Batch 33 按 SDD/TDD 完成。统计页不再加载完整 ECharts 入口，Ant Design 也不再被人工合并为
单一 `ui` 文件；生产构建的所有普通 JS chunk 均低于 `600KiB`，独立 PDF worker 低于
`1100KiB`。确定性预算 Gate 已接入 GitHub Actions，后续构建一旦回退会直接失败。

本批只修改前端依赖装配和构建边界，不改布局、API、数据库或 RAG。生产检索仍为 `hybrid`，
未读取私有 QA/PDF，未调用 Kimi 或 Embedding。

## 2. SDD / TDD 轨迹

- SDD/RED `412ada5`：先冻结普通 JS `600KiB`、PDF worker `1100KiB` 预算；旧构建被明确拒绝，
  `ui=1096.9KiB`、`StatsPage=1115.2KiB` 两项超限。
- GREEN `33dde63`：StatsPage 改为 ECharts core、按需注册 Bar/Pie/Graph、
  Title/Tooltip/Grid 和 CanvasRenderer；仅传递适配层需要的 `init/getInstanceByDom/dispose`，
  同时移除 Ant Design 全量 `manualChunks` 聚合。
- CI 加固 `d447ec1`：新增工作流契约先得到 `1 failed / 2 passed`，接入
  `npm run check:chunks` 后定向回归 `6 passed`。

## 3. 构建指标

| 项目 | 修改前 | 修改后 | 结论 |
|---|---:|---:|---|
| StatsPage 原始 JS | 1115.2KiB | 574.8KiB（588,576 bytes） | -48.5%，PASS |
| 全量 Ant Design `ui` chunk | 1096.9KiB | 已取消人工聚合 | PASS |
| 当前普通 JS 最大值 | >600KiB | 574.8KiB | ≤600KiB |
| PDF worker | 独立例外 | 1021.7KiB（1,046,214 bytes） | ≤1100KiB |
| Vite 大 chunk 警告 | 2 项 | 0 项 | PASS |

新构建共 32 个 JS/mjs chunk。预算以未压缩原始字节计，未通过调整 Vite 警告阈值或改用 gzip
口径规避问题。统计页 WAIT/PASS/错误/空库行为测试保持通过，关键页面懒加载入口未改动。

## 4. 完整回归

- 后端：`env -u PYTHONPATH venv/bin/python -m pytest tests/ -q` → **1091 passed**。
- 前端：`npm test -- --run` → **15 files / 66 passed**；`npm run lint`、`npm run build`、
  `npm run check:chunks` 均 PASS。
- Electron 默认：`npm test` → **26 passed / 2 skipped / 0 failed**。
- 发布 E2E：`PAPERMIND_RELEASE_E2E=1 ... node --test ...` → **10/10 passed**，后端进程干净关闭。
- Python 依赖：`pip check` → **No broken requirements found**。
- 公开检索：count 与 BM25 Recall@5 均为 `0.900`；MRR=`0.775/0.783`，
  NDCG@5=`0.806/0.813`，阈值 `0.85` 均 PASS。
- 公开生成 Guardrail：citation P/R/F1 与拒答率均 `1.000`；失败事务 **11/11 PASS**。

## 5. 已知边界与后续

- PDF worker 是浏览器 PDF 渲染所需的独立按需资源，继续使用单独预算，不混入普通业务 chunk。
- 本批门禁覆盖确定性构建体积与现有交互/a11y 契约，不等价于特定设备上的浏览器性能追踪。
- Batch 34 将进行最终发布审计：版本/制品、配置与数据迁移、隐私清单、回滚说明和全量 Gate。
