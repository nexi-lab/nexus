# Block 4: Distributed Semaphore

## Overview

Implement distributed semaphore for scenarios requiring multiple concurrent access permits.

**Depends on**: Block 3 (Distributed Lock) ✅

---

## Problem Statement

Block 3 提供的是 **互斥锁**（Mutex），只允许一个持有者。但很多场景需要 **信号量**（Semaphore）：

| 场景 | 需求 |
|------|------|
| 读写锁 | 允许多个 reader，但 writer 独占 |
| Rate limiting | 允许 N 个并发请求 |
| Resource pool | 限制同时使用的资源数 |
| Chatroom | 允许 N 个同时发言 |
| Worker queue | 限制并发 worker 数 |

---

## Requirements

### Functional Requirements

1. **Semaphore 基本操作**
   ```python
   # 创建/获取 semaphore（允许 3 个并发）
   sem = await nx.semaphore("/resource", permits=3)

   # 获取一个 permit
   async with sem.acquire():
       # 使用资源
       pass

   # 或者获取多个 permits
   async with sem.acquire(count=2):
       # 使用 2 个资源槽位
       pass
   ```

2. **读写锁（基于 Semaphore）**
   ```python
   # 读锁（允许多个）
   async with nx.read_lock("/config.json"):
       config = nx.read("/config.json")

   # 写锁（独占）
   async with nx.write_lock("/config.json"):
       nx.write("/config.json", new_content)
   ```

3. **超时和 TTL**
   ```python
   # 获取超时
   async with sem.acquire(timeout=5.0):
       pass

   # Permit TTL（防止死锁）
   async with sem.acquire(ttl=30.0):
       pass
   ```

### Non-Functional Requirements

- **高可用**：Redis/Dragonfly 故障时优雅降级
- **性能**：获取/释放延迟 < 10ms
- **公平性**：可选 FIFO 排队
- **可观测**：暴露当前 permits 使用情况

---

## Design Decisions (TBD)

### 方案选择

需要评估以下方案的 pros/cons：

1. **Redis INCR/DECR + Lua Script**
2. **Redis Streams**
3. **现有库（redlock-py, aioredlock）**

### 待决策问题

| 问题 | 选项 | 决策 |
|------|------|------|
| 公平性 | FIFO vs 随机 | TBD |
| 故障恢复 | TTL 过期 vs heartbeat | TBD |
| 读写锁实现 | Semaphore-based vs 专用实现 | TBD |
| API 风格 | Context manager vs explicit acquire/release | TBD |

---

## API Design (Draft)

```python
# Option A: Semaphore 对象
sem = await nx.semaphore("/resource", permits=3)
async with sem.acquire():
    pass

# Option B: 直接 context manager
async with nx.acquire_permits("/resource", permits=3, count=1):
    pass

# Option C: 读写锁专用 API
async with nx.read_lock("/file"):
    pass
async with nx.write_lock("/file"):
    pass
```

---

## Implementation Phases (Draft)

| Phase | 内容 |
|-------|------|
| Phase 1 | 基础 Semaphore（Redis INCR/DECR） |
| Phase 2 | 读写锁（基于 Semaphore） |
| Phase 3 | 公平排队（Redis Streams） |
| Phase 4 | 监控和可观测性 |

---

## Related

- Block 3: Distributed Lock ✅
- Issue: TBD
- startup_sync 测试：需要完整环境，后续补充

---

*Created: 2025-01-31*
*Status: Draft - Pending design decisions*
