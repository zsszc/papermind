# <功能名称> TDD 任务分解

> 模板使用说明：把 plan.md 拆成 2–5 分钟一个的 TDD 微循环任务。
> 每个任务严格 RED → GREEN → REFACTOR → COMMIT。写完后删除本说明。

## 任务清单

- [ ] T1：<任务名>
- [ ] T2：…

---

### T1：<任务名>

**目标**：<一句话>

**Step 1（RED）**：写失败测试

```python
# backend/tests/test_<名>.py
def test_<行为>():
    ...
```

**Step 2（验证 RED）**：

```bash
cd backend && env -u PYTHONPATH venv/bin/python -m pytest tests/test_<名>.py::test_<行为> -v
# 预期：FAIL，失败原因是 <功能缺失>，而非语法/导入错误
```

**Step 3（GREEN）**：最小实现

```python
# <实现代码>
```

**Step 4（验证 GREEN）**：

```bash
env -u PYTHONPATH venv/bin/python -m pytest tests/test_<名>.py::test_<行为> -v   # PASS
env -u PYTHONPATH venv/bin/python -m pytest tests/ -q                            # 全绿无回归
```

**Step 5（REFACTOR）**：<如有重复/坏味道，列出清理点；无则写「无」>

**Step 6（COMMIT）**：

```bash
git add <文件> && git commit -m "<type>: <说明>"
```

---

### T2：<任务名>

…
