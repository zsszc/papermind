# Batch 22J 测试报告：真实语料 Benchmark v2 就绪门禁

## 1. 结论

Benchmark v2 的覆盖审计、论文级 split 预冻结、证据审计、冻结指纹和一次性 holdout
消费工具已完成，但真实语料 readiness Gate 正确失败。当前 36 个物理 PDF 只有 19 份
唯一内容，v1 已覆盖 18 份，只有 1 篇可进入 v2，低于预注册的 12 篇下限。

因此本批没有生成 v2 QA 或 split，没有运行盲化检索基线，没有读取或消费 holdout，
也没有向 Kimi 或其他外部服务发送真实论文内容。

## 2. Harness 与 TDD 轨迹

- RED `8837704`：新增 v2 覆盖、隔离、冻结和消费契约，因实现模块缺失按预期失败。
- GREEN `902ad65`：实现 `eval.benchmark_v2`，并禁止通用 `eval.run` 打开 holdout。
- 安全补充 RED/GREEN：PDF 软链接逃逸先复现失败，再加入拒绝与文件身份前后校验。
- 三个只读代理并行审计了真实语料库存、Harness 攻击面和隐私/holdout Gate；实现吸收了
  “QA 前冻结论文 split”“claim 先于读取且崩溃即消费”“公开输出只保留聚合计数”等要求。

## 3. 真实语料就绪度

| 指标 | 结果 |
|---|---:|
| 物理 PDF 文件 | 36 |
| 唯一 PDF 内容 | 19 |
| 重复副本 | 17 |
| SQLite 已导入论文 | 19 |
| v1 已覆盖唯一内容 | 18 |
| v2 当前合格论文 | 1 |
| 预注册最低论文数 | 12 |
| readiness | FAIL |

私有审计与 Gate 制品写入 `backend/eval/private/`，权限为 `0600` 且被 Git 忽略；公开
控制台与提交报告不包含文件名、路径、标题、DOI、paper UID、PDF SHA 或正文。

## 4. 安全与可复现契约

- PDF 内容 SHA 采用无缓存流式计算，拒绝软链接，并比较哈希前后的文件身份与元数据。
- 内容 SHA 与稳定 paper UID 双向冲突时 fail closed，物理重复文件不计入新论文数量。
- 论文 split 必须在 QA 编写前以排他创建冻结，train/dev/holdout 文件相互独立。
- 冻结制品绑定 dataset 原始字节、qrels、split、corpus、database、page 与 vector 指纹。
- holdout claim 固定为 `{freeze_sha256}-holdout.claim.json`，先排他创建再读取；崩溃也算消费。
- 通用评测 CLI 对 `--split holdout` 无条件拒绝，holdout 仅能走预注册专用 Gate。

## 5. 回归结果

| Harness | 结果 |
|---|---|
| 后端 pytest | 862 passed |
| 前端 Vitest | 39 passed |
| 前端 ESLint | 0 warnings |
| Electron node:test | 26 passed |
| 前端生产构建 | PASS（保留既有大 chunk 提示） |
| 公开 BM25 Gate | Recall@5 0.900、MRR 0.783、NDCG@5 0.813，PASS |
| Python 依赖一致性 | `pip check` 无冲突 |

## 6. 限制与下一步

- 二进制 SHA 无法识别“同一论文、不同 PDF 版本”的语义重复；未来可增加规范化页文本 SHA，
  但在此之前稳定 UID 冲突仍会 fail closed。
- 至少还需补充 11 篇真正不同、成功导入且未被 v1 覆盖的论文，才能继续 v2 QA 审稿。
- 下一批 Batch 22K 先提供只读聚合就绪度 API/UI，不删除重复文件、不暴露语料身份。
- 真实论文生成烟测仍需要用户对内容出站的明确授权；Kimi 额度可用不等同于该授权。
