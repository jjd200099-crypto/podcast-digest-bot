"""Minimal credential-free liveness/readiness surface for cloud supervision."""

import asyncio
import json
import time


def health_snapshot(channel, active_jobs, *, now=None):
    now = time.monotonic() if now is None else now
    connection = channel.connection_snapshot()
    stalled = sum(now - started > 1200 for started in active_jobs.values())
    connected = connection.ready and connection.state == 'connected'
    return {'ok': bool(connected and not stalled), 'feishu_connected': bool(connected),
            'active_requests': len(active_jobs), 'stalled_requests': stalled}


async def serve_health(port, snapshot):
    async def handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=3)
            if line.split()[:2] != [b'GET', b'/healthz']:
                status, body = '404 Not Found', b'{}'
            else:
                state = snapshot()
                status = '200 OK' if state['ok'] else '503 Service Unavailable'
                body = json.dumps(state).encode()
            writer.write(f'HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
            await writer.drain()
        except (TimeoutError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, '0.0.0.0', port, limit=2048)
    async with server:
        await server.serve_forever()


async def watch_health(snapshot, *, interval=10, grace=120):
    unhealthy_since = None
    while True:
        now = time.monotonic()
        if snapshot()['ok']:
            unhealthy_since = None
        else:
            unhealthy_since = now if unhealthy_since is None else unhealthy_since
            if now - unhealthy_since >= grace:
                raise RuntimeError('Cloud health watchdog: reconnect or worker progress stalled')
        await asyncio.sleep(interval)
