# WebSocket API

Real-time event streaming via WebSocket connections.

## Overview

Nexus provides WebSocket endpoints for receiving real-time file system events. This enables applications to react immediately to file changes without polling.

## Connection

### Endpoint

```
ws://<host>:<port>/ws/<tenant_id>
wss://<host>:<port>/ws/<tenant_id>  # Production (recommended)
```

### Example Connection

**JavaScript:**
```javascript
const ws = new WebSocket('wss://nexus.example.com/ws/my-tenant');

ws.onopen = () => {
  console.log('Connected');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Event:', data);
};

ws.onclose = () => {
  console.log('Disconnected');
};
```

**Python:**
```python
import asyncio
import websockets

async def listen():
    async with websockets.connect('wss://nexus.example.com/ws/my-tenant') as ws:
        async for message in ws:
            print(f"Event: {message}")

asyncio.run(listen())
```

## Message Protocol

### Server → Client Messages

#### Event Message
```json
{
  "type": "event",
  "data": {
    "type": "created|modified|deleted|renamed",
    "path": "/documents/file.txt",
    "timestamp": 1706745600.123,
    "metadata": {}
  }
}
```

#### Ping (Heartbeat)
```json
{
  "type": "ping"
}
```

#### Subscription Confirmation
```json
{
  "type": "subscribed",
  "patterns": ["**/*.md"]
}
```

### Client → Server Messages

#### Pong (Heartbeat Response)
```json
{
  "type": "pong"
}
```

#### Subscribe (Update Filters)
```json
{
  "type": "subscribe",
  "patterns": ["**/*.md", "**/*.txt"],
  "event_types": ["created", "modified"]
}
```

## Filtering Events

You can filter events by path patterns and event types:

```javascript
// Subscribe to only markdown files
ws.send(JSON.stringify({
  type: 'subscribe',
  patterns: ['**/*.md'],
  event_types: ['created', 'modified']
}));
```

### Pattern Syntax

| Pattern | Matches |
|---------|---------|
| `*.md` | Markdown files in root |
| `**/*.md` | Markdown files anywhere |
| `docs/**` | Everything under docs/ |
| `*.{md,txt}` | .md and .txt files |

### Event Types

| Type | Description |
|------|-------------|
| `created` | New file created |
| `modified` | File content changed |
| `deleted` | File removed |
| `renamed` | File moved/renamed |

## Heartbeat

The server sends `ping` messages every 25 seconds. Clients must respond with `pong` within 10 seconds or the connection will be closed.

```javascript
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  if (data.type === 'ping') {
    ws.send(JSON.stringify({ type: 'pong' }));
    return;
  }

  // Handle other messages...
};
```

## Reconnection (Important)

**Clients MUST implement exponential backoff with jitter** to prevent server overload during outages.

### Recommended Algorithm (Full Jitter)

```
delay = random(0, min(30s, 1s × 2^attempt))
```

Reset `attempt` to 0 on successful connection.

### Example Sequence

| Attempt | Max Delay | Actual Delay (Random) |
|---------|-----------|----------------------|
| 1 | 1s | 0-1s |
| 2 | 2s | 0-2s |
| 3 | 4s | 0-4s |
| 4 | 8s | 0-8s |
| 5 | 16s | 0-16s |
| 6+ | 30s (capped) | 0-30s |

### JavaScript Implementation

**Option 1: Use [reconnecting-websocket](https://github.com/pladaria/reconnecting-websocket) (Recommended)**

```bash
npm install reconnecting-websocket
```

```javascript
import ReconnectingWebSocket from 'reconnecting-websocket';

const ws = new ReconnectingWebSocket('wss://nexus.example.com/ws/my-tenant', [], {
  connectionTimeout: 10000,
  maxRetries: 50,
  maxReconnectionDelay: 30000,
  minReconnectionDelay: 1000,
  reconnectionDelayGrowFactor: 2,
});

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  if (data.type === 'ping') {
    ws.send(JSON.stringify({ type: 'pong' }));
    return;
  }

  if (data.type === 'event') {
    handleFileEvent(data.data);
  }
};
```

**Option 2: Manual Implementation**

```javascript
class NexusWebSocket {
  constructor(url) {
    this.url = url;
    this.attempt = 0;
    this.maxDelay = 30000;  // 30 seconds
    this.baseDelay = 1000;  // 1 second
    this.connect();
  }

  connect() {
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      console.log('Connected to Nexus');
      this.attempt = 0;  // Reset on success
    };

    this.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'ping') {
        this.ws.send(JSON.stringify({ type: 'pong' }));
      } else if (data.type === 'event') {
        this.onEvent?.(data.data);
      }
    };

    this.ws.onclose = () => {
      this.scheduleReconnect();
    };

    this.ws.onerror = (err) => {
      console.error('WebSocket error:', err);
      this.ws.close();
    };
  }

  scheduleReconnect() {
    // Full Jitter algorithm (AWS recommended)
    const expDelay = Math.min(this.maxDelay, this.baseDelay * Math.pow(2, this.attempt));
    const delay = Math.random() * expDelay;

    this.attempt++;
    console.log(`Reconnecting in ${Math.round(delay)}ms (attempt ${this.attempt})`);

    setTimeout(() => this.connect(), delay);
  }

  send(data) {
    if (this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  close() {
    this.ws.close();
  }
}

// Usage
const nexus = new NexusWebSocket('wss://nexus.example.com/ws/my-tenant');
nexus.onEvent = (event) => {
  console.log('File changed:', event.path, event.type);
};
```

### Python Implementation

**Option 1: Built-in Reconnection (Recommended)**

```python
import asyncio
import json
import websockets

async def listen_with_reconnect(url: str):
    """Connect with automatic reconnection using websockets library."""
    async for ws in websockets.connect(url):
        try:
            async for message in ws:
                data = json.loads(message)

                if data['type'] == 'ping':
                    await ws.send(json.dumps({'type': 'pong'}))
                elif data['type'] == 'event':
                    handle_event(data['data'])

        except websockets.ConnectionClosed:
            print('Disconnected, reconnecting...')
            continue  # Auto-reconnects on next iteration

def handle_event(event: dict):
    print(f"File {event['type']}: {event['path']}")

# Run
asyncio.run(listen_with_reconnect('wss://nexus.example.com/ws/my-tenant'))
```

**Option 2: Manual Backoff**

```python
import asyncio
import json
import random
import websockets

async def listen_with_backoff(url: str):
    """Connect with manual exponential backoff."""
    attempt = 0
    base_delay = 1.0
    max_delay = 30.0

    while True:
        try:
            async with websockets.connect(url) as ws:
                attempt = 0  # Reset on success
                print('Connected to Nexus')

                async for message in ws:
                    data = json.loads(message)

                    if data['type'] == 'ping':
                        await ws.send(json.dumps({'type': 'pong'}))
                    elif data['type'] == 'event':
                        print(f"Event: {data['data']}")

        except (websockets.ConnectionClosed, OSError) as e:
            # Full Jitter algorithm
            exp_delay = min(max_delay, base_delay * (2 ** attempt))
            delay = random.uniform(0, exp_delay)

            attempt += 1
            print(f'Disconnected. Reconnecting in {delay:.1f}s (attempt {attempt})')
            await asyncio.sleep(delay)

# Run
asyncio.run(listen_with_backoff('wss://nexus.example.com/ws/my-tenant'))
```

## Close Codes

| Code | Meaning | Action |
|------|---------|--------|
| 1000 | Normal closure | Reconnect if desired |
| 1001 | Server shutdown | Reconnect with backoff |
| 1008 | Policy violation | Check authentication |
| 1011 | Server error | Reconnect with backoff |

## Best Practices

1. **Always use `wss://` in production** - Encrypt your WebSocket traffic
2. **Implement heartbeat responses** - Respond to `ping` with `pong` within 10 seconds
3. **Use exponential backoff** - Prevent thundering herd on server restarts
4. **Filter aggressively** - Subscribe only to events you need
5. **Handle reconnection gracefully** - Update UI to show connection status

## See Also

- [RPC/Server API](rpc-api.md) - REST API documentation
- [Workflows](workflows.md) - Event-driven automation
- [Configuration](configuration.md) - Server setup
