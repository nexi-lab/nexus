# @nexus/pay

TypeScript SDK for Nexus Pay — agent payment API.

Zero dependencies. Works in Node.js 18+, browsers, Deno, Bun, and edge runtimes.

## Install

```bash
npm install @nexus/pay
```

## Quick Start

```typescript
import { NexusPay } from '@nexus/pay';

const pay = new NexusPay({ apiKey: 'nx_live_myagent' });

// Check balance
const balance = await pay.getBalance();
console.log(`Available: ${balance.available}`);

// Transfer credits
const receipt = await pay.transfer({
  to: 'agent-bob',
  amount: '10.00',
  memo: 'Task payment',
});

// Reserve credits (two-phase)
const reservation = await pay.reserve({ amount: '25.00', purpose: 'compute' });
await pay.commit(reservation.id);
// or: await pay.release(reservation.id);
```

## Configuration

```typescript
const pay = new NexusPay({
  apiKey: 'nx_live_myagent',           // Required
  baseUrl: 'https://nexus.example.com', // Default: https://nexus.sudorouter.ai
  timeout: 30_000,                      // Default: 30s
  maxRetries: 3,                        // Default: 3 (0 to disable)
  fetch: customFetch,                   // Optional: custom fetch implementation
});
```

## API Reference

| Method | Description |
|--------|-------------|
| `getBalance(options?)` | Get available, reserved, and total balance |
| `canAfford(amount, options?)` | Check if agent can afford an amount |
| `transfer(params, options?)` | Transfer credits (auto-routes credits/x402) |
| `transferBatch(items, options?)` | Atomic batch transfer (max 1000) |
| `reserve(params, options?)` | Reserve credits for two-phase operations |
| `commit(reservationId, params?, options?)` | Commit a reservation |
| `release(reservationId, options?)` | Release a reservation (refund) |
| `meter(params, options?)` | Fast credit deduction for API metering |

All amounts are `string` type to prevent floating-point precision loss.

## Error Handling

```typescript
import { NexusPay, InsufficientCreditsError, NexusPayError } from '@nexus/pay';

try {
  await pay.transfer({ to: 'agent-bob', amount: '9999.00' });
} catch (error) {
  if (error instanceof InsufficientCreditsError) {
    console.log('Not enough credits');
  } else if (error instanceof NexusPayError) {
    console.log(`API error: ${error.message} (status: ${error.status})`);
  }
}
```

### Error Classes

| Class | HTTP Status | When |
|-------|-------------|------|
| `AuthenticationError` | 401 | Invalid or expired API key |
| `InsufficientCreditsError` | 402 | Not enough credits for operation |
| `BudgetExceededError` | 403 | Operation exceeds budget limits |
| `WalletNotFoundError` | 404 | Agent wallet does not exist |
| `ReservationError` | 409 | Reservation conflict (double-commit, etc.) |
| `RateLimitError` | 429 | Rate limited (check `.retryAfter`) |
| `NexusPayError` | any | Base class for all SDK errors |

## Per-Request Options

Every method accepts an optional second argument for request-level overrides:

```typescript
await pay.transfer(
  { to: 'bob', amount: '10.00' },
  {
    timeout: 60_000,           // Override timeout for this request
    signal: controller.signal,  // AbortController for cancellation
    idempotencyKey: 'key-123',  // Retry-safe deduplication
  },
);
```

## License

MIT
