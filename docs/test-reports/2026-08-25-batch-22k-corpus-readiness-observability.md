# Batch 22K 测试报告：真实语料就绪度可观测性

## 1. 结论

Batch 22K 已完成真实语料 Benchmark v2 就绪度的只读 API 与统计页卡片。CLI 与应用
复用同一覆盖核心，真实库结果稳定为 WAIT：36 个物理 PDF、19 份唯一内容、17 个重复
副本、v1 覆盖 18 份、v2 合格 1 篇，距离 12 篇门槛尚缺 11 篇。

本批没有移动或删除 PDF，没有打开私有 QA/holdout，没有调用 Kimi、Embedding 或网络服务，
也没有生成新的私有评测制品。

## 2. SDD / TDD 轨迹

- RED `4e987e8`：后端因共享服务缺失在收集阶段失败；前端 4 个卡片契约全部失败。
- GREEN `104e28b`：实现可打包共享核心、严格只读端点、统计页三态卡片与独立重试。
- 三个只读代理并行审计了后端契约、前端 Harness 和隐私攻击面；据此补充 Electron
  打包闭包、manifest 自哈希、幽灵论文、根/PDF 软链接、快照变化与矛盾 PASS 防护。

## 3. 交付内容

- `GET /api/readiness/benchmark-v2`：固定 12 篇阈值，不接受路径或阈值输入。
- API 只返回状态、布尔值、阈值和七项聚合计数，响应设置 `Cache-Control: no-store`。
- 审计异常统一返回 UNAVAILABLE，未知计数为 null，异常原文只记录错误类型且不进入响应。
- `StatsPage` 独立加载文献统计和 readiness；空库仍显示卡片，网络/畸形响应失败关闭。
- Electron 可打包核心位于 `app/services`，不导入 `eval`；安装包继续拒绝私有评测目录。
- 评测 CLI 从同一应用核心重导出覆盖/readiness 函数，避免身份判断分叉。

## 4. 隐私与完整性 Gate

- API/Pydantic/前端三层白名单均不渲染文件名、路径、标题、DOI、UID、PDF SHA 或正文。
- corpus manifest 必须通过内容自哈希；合法 64 位伪哈希或篡改 documents 会失败。
- manifest 的每个数据库 PDF SHA 必须实际存在于当前物理集合，幽灵论文不能形成 PASS。
- 拒绝 papers 根目录和 PDF 软链接；哈希前后校验 inode/size/mtime，目录快照变化即失败。
- v1 身份仅从既有 coverage 制品提取并验证指纹，API 不打开 train/dev/holdout QA 文件。
- 上游返回额外身份字段或矛盾 PASS 时，路由投影/前端一致性校验会丢弃并显示不可用。

## 5. 真实语料 parity

| 字段 | CLI 共享核心 / API |
|---|---:|
| 状态 | WAIT |
| 物理 PDF | 36 |
| 唯一内容 | 19 |
| 重复副本 | 17 |
| v1 已覆盖 | 18 |
| v2 合格 | 1 |
| 门槛 / 缺口 | 12 / 11 |
| 未导入唯一内容 | 0 |

真实 API smoke 返回 HTTP 200 与 `Cache-Control: no-store`，且只包含上述安全 DTO。

## 6. 全量回归

| Harness | 结果 |
|---|---|
| 后端 pytest | 871 passed |
| 前端 Vitest | 45 passed |
| 前端 ESLint | 0 warnings |
| Electron node:test | 26 passed |
| 前端生产构建 | PASS（保留既有大 chunk 提示） |
| 公开 BM25 Gate | Recall@5 0.900、MRR 0.783、NDCG@5 0.813，PASS |
| Python 依赖一致性 | `pip check` 无冲突 |

本地虚拟环境未安装 `ruff`，因此额外定向 `ruff check` 未执行；项目规定的 pytest、前端
lint/build、Electron、公开评测和依赖 Gate 均已通过。

## 7. 限制与下一步

- 精简 Electron 包不会携带 `backend/eval/private`；缺失私有 v1 身份制品时按设计显示
  UNAVAILABLE，不会为方便 UI 而把真实评测身份打入安装包。
- 当前仍需补充至少 11 篇真正不同、成功导入的新论文后才能构建 Benchmark v2。
- 下一批 Batch 23A 先使用公开合成数据建立生成引用与拒答 Guardrail 离线 Harness；
  真实论文 Kimi smoke 仍需用户明确授权内容出站。
