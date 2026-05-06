# Broadcast Streams Architecture

## The Core Challenge
Originally, this project utilized standard ephemeral "Point-to-Point" Redis Lists, relying mechanically on `LPUSH` and `RPOP` backend commands.

While this efficiently distributed load across homogenous worker swarms by destructively deleting the queue records, it meant that an IoT unit uniquely querying the queue for system-wide configuration updates would fatally blind other listener nodes to that exact same data point. The queue inherently shattered multi-observer architectures. 

## Final Implementation Strategy

We overhauled the internal memory topology to pivot completely to Redis Streams natively:

### 1. The `XADD` Append Log
Instead of dropping raw msgpack blobs into Lists, we execute atomic asynchronous `XADD` appends on the underlying stream topic paths.
`redis.xadd(f"q:{topic}", {"payload": body, "exp": str(exp_time)})`

By explicitly mapping an identical `exp` (Expiration TTL timestamp) onto every inbound packet payload struct, we embed absolute lifetimes straight into the datastore. We back this safely with a redundant global hardware TTL to guarantee orphaned topic keys expire cleanly without manually scripting complex `XTRIM` mechanisms.

### 2. Client Distinct Cursor Pointers
To successfully track individual devices inside an identical stream logic, the backend pulls the `X-Client-ID` from the incoming GET footprint. It checks Redis bounds for a dynamic string `cursor:{client_id}:q:{topic}` acting to store the very last globally recorded Stream ID for that node. It then invokes `XREAD`: tracking only messages exceeding that cursor integer safely!

### 3. Graceful Draining
Instead of failing instantly, our device querying loop pulls up to 50 stream packets at a time per API hit, gracefully sweeping down `XREAD` streams checking `current_time <= payloads[exp]`.
If a message is valid, it ships precisely *one* package per API return limit out natively, retaining the exact identical cursor mechanics while returning an elegant `HTTP 204 No Content` code strictly when the physical datastore is wiped or exhausted for that consumer client ID!
