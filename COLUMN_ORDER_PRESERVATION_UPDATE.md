# 列顺序保持功能 - 更新日志

## 修改内容

修复了 `dynamic_viewer` 权限过滤时的列顺序问题。现在过滤后的 CSV 文件会**严格保持原始文件的列顺序**。

## 修改前

列顺序被打乱：
```
原始列: name, email, age, salary, password, ssn
输出列: name, email, mean(age), sum(salary)  ❌ 看起来正确
```

但在更复杂的场景中会出问题：
```
原始列: col1, col2, col3, col4, col5
配置:   visible=[col1, col3], aggregations={col5: "sum"}
输出列: col1, col3, sum(col5)  ❌ 顺序可能不对
```

## 修改后

按照原始文件列顺序输出：
```
原始列: col1, col2, col3, col4, col5
配置:   hidden=[col2], aggregations={col3: "mean"}, visible=[col1, col4, col5]
输出列: col1, mean(col3), col4, col5  ✓ 严格按原始顺序
```

## 实现逻辑

**旧逻辑**（错误）:
1. 添加所有 visible_columns
2. 附加所有 aggregated columns

**新逻辑**（正确）:
1. 遍历原始 CSV 的每一列（按顺序）
2. 对每一列：
   - 如果在 hidden_columns → 跳过
   - 如果在 aggregations → 添加 operation(column)
   - 如果在 visible_columns → 添加原始列

## 修改的文件

- `src/nexus/core/nexus_fs_rebac.py` (apply_dynamic_viewer_filter 方法, lines 1434-1518)

## 测试验证

新增专门的列顺序测试：
```bash
python test_column_order.py
```

测试了 3 种复杂场景：
1. ✓ 混合可见列和聚合列
2. ✓ 聚合列在开头和结尾
3. ✓ 自动计算的 visible_columns

所有原有测试继续通过：
```bash
python test_dynamic_viewer_integration.py
```

## 影响范围

- ✅ 不影响现有功能
- ✅ 向后兼容
- ✅ 所有测试通过
- ✅ 提升用户体验（列顺序更直观）

## 示例

### 示例 1：员工数据

```python
# 原始 CSV: id, name, department, salary, ssn
# 配置
column_config = {
    "hidden_columns": ["ssn"],
    "aggregations": {"salary": "mean"},
    "visible_columns": ["id", "name", "department"]
}

# 输出列顺序：id, name, department, mean(salary)
# ✓ 保持原始顺序，salary的位置被mean(salary)替换
```

### 示例 2：传感器数据

```python
# 原始 CSV: timestamp, sensor1, sensor2, sensor3, sensor4, status
# 配置
column_config = {
    "hidden_columns": ["status"],
    "aggregations": {"sensor1": "mean", "sensor3": "max"},
    "visible_columns": ["timestamp", "sensor2", "sensor4"]
}

# 输出列顺序：timestamp, mean(sensor1), sensor2, max(sensor3), sensor4
# ✓ 每个聚合列在其原始位置
```

## 技术细节

使用 pandas 按照原始列顺序构建结果：

```python
result_columns = []  # List of (column_name, series) tuples

for col in df.columns:  # 按原始顺序遍历
    if col in hidden_columns:
        continue
    elif col in aggregations:
        # 添加聚合列
        result_columns.append((f"{operation}({col})", agg_series))
    elif col in visible_columns:
        # 添加原始列
        result_columns.append((col, df[col]))

# 构建 DataFrame（保持顺序）
result_df = pd.DataFrame({name: series for name, series in result_columns})
```

## 相关文档

- `DYNAMIC_VIEWER_IMPLEMENTATION_SUMMARY.md`: 完整实现文档
- `test_column_order.py`: 列顺序测试
- `test_dynamic_viewer_integration.py`: 集成测试
