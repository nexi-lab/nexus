# Event Subsystem

Unified event handling for Nexus: **EventBus** (pub/sub) + **EventLog** (persistence).

## Architecture

```
services/event_subsystem/
├── types.py          # FileEvent, FileEventType (service-layer data types)
├── subscriptions.py  # Reactive subscription patterns
├── bus/              # EventBus (pub/sub, transient)
│   ├── protocol.py   # EventBusProtocol, AckableEvent
│   ├── base.py       # EventBusBase (template method pattern)
│   ├── redis.py      # Redis Pub/Sub backend
│   ├── nats.py       # NATS JetStream backend
│   ├── factory.py    # Backend factory
│   └── decorators.py # @requires_started lifecycle decorator
└── log/              # EventLog (persistent, durable)
    ├── protocol.py   # EventLogProtocol
    ├── wal.py        # WAL-backed event log (Rust)
    ├── replay.py     # Event replay service
    ├── delivery.py   # Event delivery worker
    ├── dead_letter.py # Dead letter queue
    ├── metrics.py    # Observability metrics
    └── exporters/    # Event exporters (Kafka, NATS, PubSub)
```

## Usage

### Publish Events

```python
from nexus.system_services.event_subsystem import FileEvent, FileEventType, RedisEventBus

bus = RedisEventBus(redis_client)
await bus.start()

event = FileEvent(
    type=FileEventType.FILE_WRITE,
    path="/data/file.txt",
    zone_id="root",
)
await bus.publish(event)
```

### Batch Publish (50x faster)

```python
events = [FileEvent(...) for _ in range(100)]
await bus.publish_batch(events)  # Single RTT (Phase 4 - not yet implemented)
```

### Subscribe to Events

```python
async for event in bus.subscribe("root"):
    print(f"Received: {event.type} on {event.path}")
```

### Durable Subscriptions (NATS only)

```python
from nexus.system_services.event_subsystem import NatsEventBus

bus = NatsEventBus(nats_url="nats://localhost:4222")
await bus.start()

async for ackable_event in bus.subscribe_durable("root", "consumer-1"):
    try:
        # Process event
        await handle_event(ackable_event.event)
        await ackable_event.ack()
    except Exception:
        await ackable_event.nack(delay=5.0)  # Retry after 5s
```

## Performance

| Operation | Redis Pub/Sub | NATS JetStream |
|-----------|---------------|----------------|
| Single publish | 1-2ms | 5-10ms |
| Batch publish (100) | 5-10ms* | 20-30ms* |
| Throughput | 1K msg/s | 10-40K msg/s |
| Durability | Best-effort | Persistent (file-backed) |

*Batch API not yet implemented (Phase 4)

## Backend Selection

### When to Use Redis Pub/Sub

**Best for:**
- Low latency required (<5ms)
- Low to medium throughput (<1K messages/second)
- Simple pub/sub (no durability needed)

**Configuration:**
```python
from nexus.system_services.event_subsystem.bus import RedisEventBus

bus = RedisEventBus(redis_client)
```

### When to Use NATS JetStream

**Best for:**
- High throughput (>1K messages/second)
- Durability required (crash recovery, replay)
- Exactly-once delivery (deduplication)

**Configuration:**
```python
from nexus.system_services.event_subsystem.bus import NatsEventBus

bus = NatsEventBus(nats_url="nats://localhost:4222")
```

### Recommendation

- **Development/embedded**: Redis (simpler setup)
- **Production <1K msg/s**: Redis (lower latency)
- **Production >1K msg/s**: NATS (higher throughput)
- **Mission-critical**: NATS (durability guarantees)

## Architecture Notes

### Per NEXUS-LEGO-ARCHITECTURE

- **Service-layer types**: FileEvent and FileEventType are service-layer primitives, not kernel primitives
- **Kernel definition**: VFS + Metadata protocols ONLY
- **Events are service-layer**: Pub/sub and event logging are service-layer concerns, analogous to systemd/journald in Linux

### Template Method Pattern

EventBusBase provides lifecycle management with double-checked locking:
- `start()` and `stop()` are template methods
- Subclasses implement `_do_start()` and `_do_stop()`
- Eliminates 50+ lines of duplicated lifecycle code

### WAL-First Durability (Issue #1397)

When an EventLog is configured, events are durably persisted to WAL before being broadcast:

```python
bus = RedisEventBus(redis_client, event_log=wal_event_log)
await bus.publish(event)  # Persisted to WAL first, then published
```

## Migration from Old Paths

If you have code importing from old paths:

```python
# OLD (deprecated)
from nexus.system_services.event_subsystem.types import FileEvent, FileEventType
from nexus.system_services.event_subsystem.bus import RedisEventBus
from nexus.system_services.event_subsystem.log import WALEventLog

# NEW (current)
from nexus.system_services.event_subsystem import FileEvent, FileEventType, RedisEventBus, WALEventLog
```

## Related Issues

- **#2122**: Move EventBus from core/ to services/event_subsystem/
- **#1397**: Rust-Accelerated Event Log WAL
- **#1331**: Replace Dragonfly pub/sub with NATS JetStream
- **#1241**: Event delivery from operation_log (EventDeliveryWorker)
