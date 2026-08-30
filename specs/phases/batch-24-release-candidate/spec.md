# Batch 24 规格：发布候选基础设施（E2E/可访问性/包体/升级回滚）

> 来源：开发计划与进度表 Batch 24+ 行（P2，Phase H 发布候选半区）。
> 定位：对**现有 UI/桌面壳**做发布候选加固；Figma 视觉逐页迁移是另一半，等 zip 另行立项。
> 纪律：零重型新依赖（不引 Playwright/Cypress）；全部复用现有 vitest + node --test 模式。

## 1. 目标

让「发布一个新版桌面包」从人肉冒烟变成有自动化 Gate：关键流程 E2E 基线、可访问性契约、
包体预算、安装升级回滚验证，四块各带硬门禁。

## 2. 现状（代码实证）

- 前端：vitest 55 例（组件/工具级 jsdom），无浏览器级 E2E
- Electron：node --test 26 例（backend-lifecycle / runtime-identity / security-policy / artifact-verifier 等）
- 后端：pytest 912 例；API 全流程已有 TestClient 覆盖
- 制品：electron-builder 产出 dmg/zip（当前根目录无现成制品，构建经 `npm run electron:build`）
- 数据目录：`PAPERMIND_DATA_DIR` 重定向机制已固化（Batch 8/16）

## 3. 任务契约

### T1（24A）：关键流程 E2E 基线（后端真启 + 桌面壳链路）

- `electron/test/release-flow.test.js`（node --test，复用 backend-lifecycle 模式）：
  真实拉起后端子进程（随机回环端口 + 能力令牌），经 HTTP 断言关键流程闭环：
  `GET /api/health` → 文献列表 → 检索 → 对话 SSE 首帧 → 统计页数据
- 全程 fake LLM 不可能（真进程）——LLM 相关断言只到「SSE 帧格式与错误帧契约」，
  不断言生成内容；Embedding 不可用时检索降级路径也纳入断言
- 超时硬上限、子进程必清理（泄漏即失败）；不访问网络（除本机回环）

### T2（24B）：可访问性契约基线（vitest/jsdom）

- 关键交互组件（ChatPanel 输入区、主导航、设置弹窗）：
  必填可访问名称（aria-label 或可见文本关联）、按钮可聚焦、Esc 关闭弹窗
- 只锁定契约不追求全量审计；新组件上架前必须过同一断言组

### T3（24C）：包体预算 Gate

- `scripts/check_artifact_budget.py`：扫描 `frontend/out/`（或指定目录）制品，
  断言：① 制品存在且非空；② dmg/zip 大小 ≤ 预算（初版 800MB，现状 500MB+ 缓冲）；
  ③ 制品内不含 config.yaml / .env / data/ / papers/ 等数据文件（复用 Batch 15 扫描思路）
- 无制品时显式 SKIP（exit 0 + 说明），有制品必硬 Gate；接入 npm script

### T4（24D）：安装/升级/回滚数据目录验证

- `electron/test/data-dir-migration.test.js`（node --test）：
  模拟 v1 数据目录（含 papers.db / vector_db / config.yaml）→ 以新实例身份启动后端 →
  断言数据完整可读、schema 迁移幂等、配置不被覆盖；
  回滚路径：旧版配置字段缺省时按模板补默认值且不丢用户 Key

## 4. 验收标准（可测试）

- [ ] AC1：T1 E2E 全流程 PASS，子进程零泄漏，总耗时 < 120s
- [ ] AC2：T2 a11y 契约组全绿；故意破坏 aria-label 的 RED 用例存在
- [ ] AC3：T3 无制品 SKIP 有制品 Gate；超预算/夹带数据文件必 fail
- [ ] AC4：T4 升级保留数据、迁移幂等、回滚不炸三断言
- [ ] AC5：三端既有测试全绿（后端 912 / 前端 55 / Electron 26）+ 新增用例
- [ ] AC6：零重型新依赖（package.json diff 无 playwright/cypress/puppeteer）

## 5. 非目标

- 真实浏览器 E2E（Playwright 类重依赖）、视觉回归、Figma 新 UI 逐页迁移、性能压测
- Batch 23B（真实内容出站，待授权）、Batch 22J 盲测（等 11 篇论文）

## 6. 风险与回退

- E2E 真启后端在 CI 无 BGE-M3 环境 → 断言降级路径而非可用路径
- 包体预算值随依赖演进 → 预算集中在脚本顶部常量，调整须提交说明
- 回退：四个任务互相独立，任一可单独关闭（脚本/test 文件删除即回退）
