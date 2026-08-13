# Batch 15 测试报告：Electron 发布安全（2026-08-13）

## 1. 结论

Batch 15 已将 Electron 29.4.0 / electron-builder 24.13.3 升级到 43.3.0 / 26.15.3，Electron 官方 npm audit 从 **13 项（1 critical / 12 high）降为 0**。窗口现已启用 sandbox/CSP，默认拒绝弹窗、越界导航与全部权限，并使用 single-instance lock。发布资源扫描覆盖入口完整性、用户数据/真实配置、常见密钥和 Python 软链逃逸。

并行审查发现并修复两个额外问题：弹窗全拒绝会破坏 PDF 下载，已改为 fetch→Blob 下载并增加前端测试；macOS 关闭全部窗口后 `intentionalKill` 未复位，会让重开窗口后的后端失去自动重启，现已在启动前复位。

实际制品 Gate 当前仍为 **BLOCKED**：旧开发 venv 的 Python 是 x86_64，且链式软链接到 `/opt/miniconda3/bin/python3`，不可随 arm64 安装包发布。项目已改为从 `python-build-standalone` 固定 CPython 3.12.13 arm64/x64 URL 与 SHA-256，生成独立 staging；但本次网络多次返回异常/停滞文件，摘要校验正确拒绝，尚未获得可用于实际 unpacked build 的完整官方资产。

## 2. TDD 与缺陷证据

| 环节 | RED | GREEN |
|---|---|---|
| 窗口安全 | `Cannot find module '../security-policy'` | sandbox、导航/弹窗/权限、single-instance 与 CSP 共 6 个策略测试通过 |
| 制品扫描 | `Cannot find module '../scripts/verify-artifact'` | 必需入口、禁止路径、密钥、软链与运行时清单测试通过 |
| PDF 下载 | `Cannot resolve './download'` | Blob 下载 2 个测试通过；不再依赖被禁用的 `window.open` |
| 运行时 | 旧包含 tests/eval/cache；`python → python3 → /opt/miniconda3`；x64 Python 进入 arm64 包 | 固定双架构官方清单、SHA-256、依赖指纹、staging 隔离和架构 marker，共 3 个构建测试 |
| 下载完整性 | 实际文件大小/摘要异常，部分下载停滞 | 临时文件下载→SHA-256→原子替换；任何异常均阻断 staging/build |

## 3. 最终自动化 Gate

| 门禁 | 结果 |
|---|---|
| 后端 pytest | **505 passed**，927 warnings，13.04s |
| Python `pip check` | No broken requirements found |
| 前端 Vitest | **7 passed / 3 files** |
| 前端 lint | 通过，零 warning |
| 前端 build | 通过；既有 ui/StatsPage 大 chunk warning |
| 前端官方 npm audit | **0 vulnerabilities** |
| Electron node:test | **21 passed** |
| Electron main/lifecycle/security/build scripts `node --check` | 通过 |
| Electron 官方 npm audit | **0 vulnerabilities** |
| 旧 unpacked 制品扫描 | 预期失败：tests/eval/cache、缺两个 asar 模块、Python 链式逃逸 |
| 新 unpacked 制品扫描 | **BLOCKED**：官方运行时下载未通过 SHA-256，builder 未被允许继续 |

ErrorBoundary 测试仍会故意输出 React 错误栈，测试本身通过。前端构建的 chunk warning 与本批无回归关系。

## 4. 发布边界与下一步

- 本批代码与自动化安全 Gate 可合并，但不能据此宣称桌面安装包已经可发布；T6 必须在网络恢复后重跑 `npm run build:dir && npm run verify:artifact`。
- macOS arm64/x64 已有固定运行时来源；Windows/Linux 尚未进入运行时清单，目标平台构建会明确失败而不是误用本机 venv。
- Batch 16 继续实现随机端口与每次启动能力令牌，拒绝本机 8000 端口伪服务，并在可用制品上做真实 health/退出/PDF 冒烟。
- 代码已推送到 `origin/main`；测试报告与最终台账提交后再次推送。
