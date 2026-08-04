# PaperMind 规格库（specs/）

本目录是 PaperMind 的**单一事实来源（Single Source of Truth）**之一：代码回答「现在是什么」，规格回答「应该是什么」。

## 工作方式（SDD）

- **新功能**：`spec.md`（做什么/为什么）→ `plan.md`（怎么做）→ `tasks.md`（TDD 任务分解）→ 实现。模板在 `_templates/`。
- **存量模块**：`backend/` 下是从代码反推的**行为契约规格**，用于教学、评审与改动前的影响分析。
- **最高准则**：[`constitution.md`](constitution.md) 是项目宪法，一切规格与代码不得违反。

## 目录结构

```
specs/
├── constitution.md        # 项目宪法（最高准则）
├── README.md              # 本文件
├── _templates/            # spec / plan / tasks 模板
├── backend/
│   ├── core/              # config / settings / logger / database / models / schemas / main
│   ├── services/          # 13+ 个服务的行为契约
│   ├── routers/           # 8 个路由的行为契约
│   └── eval/              # 评测模块（dataset/metrics/run/trend/generate_qa）
└── phases/                # 各 Phase 新功能的完整 SDD 四件套
```

## 规格维护纪律

1. 改代码行为 → 同步改对应规格（宪法第 20 条）。
2. 规格与代码冲突 → 以代码为准，立即修订规格。
3. 每条验收标准（AC）必须可映射到自动化测试（宪法第 22 条）。
4. 规格评审作为提交前检查项：改动涉及的规格文件必须随代码同批提交。
