# Batch 25 测试报告：接班审计与发布/QA 生成加固

## 1. 结论

对另一开发代理提交的 Batch 24、Batch 22L 与 Batch 23B 做了接班审计，确认并修复 6 类问题：

1. 真实 Electron 发布 E2E 硬编码仓库 venv，并被混入不安装后端依赖的默认 CI；
2. QA v2 部分断点会把整篇误判完成，永久漏掉 question type；
3. 损坏 JSONL、宽松权限和 symlink 可继续追加；
4. 普通 CLI 调用无需明确确认即可发送真实论文材料；
5. split 冻结的 PDF SHA 未与当前 DOI 映射文件核对；
6. 安装包扫描可被 `./`、`../` 和文件名大小写绕过。

修复严格遵循 SDD/TDD，生产默认检索策略未改变。本轮没有读取 `papers/`、
`backend/eval/private/`、`config.yaml` 或真实数据目录，没有调用 Kimi/Embedding。

## 2. 审计范围

- Batch 24：`18bc45d`、`ff34279`、`0b21a51`、`94cfeef`
- Batch 22L：`d25ad61`、`dda3e93`、`744938c`、`992a98e`
- Batch 23B：`1e4bc74`（只有 spec/plan/tasks，未发现实跑报告）
- CI、Electron 测试调度、QA v2 生成器、制品预算扫描、计划表和测试报告

## 3. RED → GREEN 轨迹

### 第一轮：CI 与安全续跑

- RED：新增接班契约后 `6 failed, 40 passed`；分别暴露 CI 未调度真实 E2E、partial resume
  漏类型、坏 JSON/权限/symlink 未拒绝，以及缺少显式出站确认。
- GREEN：真实 E2E 改由具备后端依赖的 CI job 显式执行；默认 Electron 套件只注册纯测试，
  未开启发布模式时两个文件各给出固定 skip；QA resume 改为严格状态机。

### 第二轮：冻结源与制品扫描

- RED：补充冻结 SHA、重复 UID/SHA、源文件漂移和规范化路径绕过用例；旧实现出现预期失败。
- GREEN：LLM 调用前校验每篇当前 PDF 的 SHA-256；安装包成员先按 POSIX 语义规范化，
  对敏感文件名大小写折叠后判定。定向集合 `64 passed`。

对应提交：`207b013`、`7207dce`、`7b3097f`、`1504c10`。

## 4. 全量回归与指标

| Gate | 结果 |
|---|---|
| 后端 pytest | **979 passed**，1434 warnings，约 21.25s |
| Python 依赖一致性 | `pip check`：No broken requirements found |
| 前端 Vitest | **15 files / 66 tests passed** |
| 前端 lint | **PASS，0 warnings** |
| 前端 build | **PASS**；保留既有大 chunk 警告 |
| Electron 默认纯测试 | **26 passed / 2 skipped / 0 failed** |
| Electron 真实发布 E2E | **10/10 passed**，约 14.9s |
| 公开 count RAG | Recall@5 **0.900** / MRR **0.775** / NDCG@5 **0.806**，PASS |
| 公开 BM25 RAG | Recall@5 **0.900** / MRR **0.783** / NDCG@5 **0.813**，PASS |
| 公开生成 Guardrail | citation P/R/F1、负例拒答率均 **1.000**，PASS |
| 独立失败事务 Harness | **11/11 scenarios**，PASS |

真实发布 E2E 需要回环监听权限和已安装的后端依赖，因此从默认 Electron job 拆出后放到
backend CI job；这不是跳过 Gate，而是把 Gate 移到可复现的执行环境。

## 5. 文档纠偏

- Batch 24 的 T4 证明的是新版本对旧数据目录/配置的升级兼容，不等同于旧二进制回滚。
- Batch 24 E2E 使用离线环境变量和回环死端口实现配置性外网隔离，不是系统调用级审计。
- Batch 22L 私有指标仅按已提交报告转录；本轮没有读取私有制品，不能声明独立复验。
- Batch 23B 仍只有三件套。固定四题与 top-5 证据发送 Kimi 前，需当前轮次明确授权。

## 6. 后续计划

下一批分为两个互不污染的方向：

1. Phase H UI 逐页迁移：先补真实浏览器视觉/交互基线，再按页面小批替换，持续保留 Batch 24/25
   发布 Gate；
2. v2 train 检索诊断：只在明确的私有本地读取边界内分析 train 失败类型，预注册候选并坚持
   train-first；不读取或消费 holdout，不因 dev 结果反复调参。
