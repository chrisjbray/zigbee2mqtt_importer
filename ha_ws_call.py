#!/usr/bin/env python3
"""Run Home Assistant websocket API commands from inside the HA container.

Copied into the `homeassistant` container with `docker cp` and driven over
stdin by `ha.ws_call`, because the device registry has no REST endpoint and the
host virtualenv has no websockets library. Reads
`{"token": ..., "messages": [...]}` on stdin and writes the list of result
frames to stdout.
"""

import asyncio
import json
import sys

import websockets

URL = "ws://localhost:8123/api/websocket"


async def main():
    request = json.load(sys.stdin)

    async with websockets.connect(URL) as socket:
        await socket.recv()  # auth_required
        await socket.send(json.dumps({"type": "auth", "access_token": request["token"]}))
        auth = json.loads(await socket.recv())
        if auth.get("type") != "auth_ok":
            raise SystemExit(f"websocket authentication failed: {auth}")

        results = []
        for index, message in enumerate(request["messages"], start=1):
            await socket.send(json.dumps(dict(message, id=index)))
            while True:
                frame = json.loads(await socket.recv())
                if frame.get("id") == index and frame.get("type") == "result":
                    results.append(frame)
                    break

    json.dump(results, sys.stdout)


if __name__ == "__main__":
    asyncio.run(main())
