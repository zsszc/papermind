# Batch 25 规格：接班审计与发布/私有生成加固

## 1. 背景与目标

另一开发代理在 Batch 23F 后完成 Batch 24、Batch 22L，并建立 Batch 23B 规格。本批先对
这些变更做接班审计，修复会导致干净 CI 失败、私有 QA 断点续跑丢题或误触内容出站的
问题，再恢复后续开发。审计不读取或修改 `papers/`、`eval/private/`、`config.yaml`。

## 2. 已确认问题

1. Batch 24 两个真实后端 Electron 测试硬编码仓库 venv，并混入默认纯 Node 套件；干净
   Electron CI 没有该 venv/后端依赖，受限环境也会因回环 bind 权限使全部默认测试失败。
2. Batch 22L `--resume` 见到同一 paper_uid 任一行便跳过整篇；部分生成会永久缺失轮换类型。
3. `--resume` 静默忽略损坏 JSONL 后继续追加，可能把候选集永久写坏；已有文件权限和
   symlink 也未 fail closed。
4. 私有 QA CLI 不要求显式内容出站确认，普通命令即可把真实论文材料发送给外部 LLM。
5. AGENTS、计划表状态/测试数/提交栏与已提交报告不一致；Batch 23B 只有规格，尚未实跑。

## 3. 行为契约

### 3.1 发布 E2E 调度

- Electron 默认 `npm test` 只执行无需 Python、网络或回环监听的纯单元/策略测试。
- `release-flow` 与 `data-dir-migration` 仅在 `PAPERMIND_RELEASE_E2E=1` 时注册真实用例；
  Python 可由 `PAPERMIND_PYTHON` 显式指定，开发机仍可回退仓库 venv。
- CI 在已安装后端 requirements 的 backend job 中显式运行两个发布 E2E；任一失败阻断。
- 未启用时各文件只报告固定 skip，不得启动子进程、建真实数据目录或绑定端口。

### 3.2 QA v2 安全续跑

- resume 必须逐行严格解析已有 JSONL；空行、坏 JSON、未知 UID/split、重复 qa_id、重复
  question_type、非 0600 文件或 symlink 任一出现即拒绝追加。
- 仅当某篇已具备本轮要求的全部 question_type 才视为完成；部分论文只生成缺失类型，
  不重复已有类型，qa_id 保持唯一。
- CLI 的 splits/output 必须位于 `backend/eval/private/`，并要求显式
  `--confirm-content-egress`；缺少确认时在导入配置/数据库/LLM 前退出 2。

## 4. Gate

- RED/GREEN 专项覆盖 CI 静态调度、partial resume、坏 JSON、权限/symlink、显式出站确认。
- 后端、前端、Electron 纯单元全绿；发布 E2E 在获准回环环境单独全绿。
- 公开检索/生成/失败事务指标无回退；无真实 LLM、Embedding、私有内容访问。
- 生成接班审计测试报告，修正 AGENTS/计划表/历史提交占位。

## 5. 非目标

- 不重新执行或采信 Batch 22L 私有 train/dev/holdout，不修改其私有制品。
- 不执行 Batch 23B Kimi smoke；需当前用户再次明确授权本轮四题内容出站。
- 不把真实 E2E 降级成 fake；只调整其可复现的调度位置。
