from __future__ import annotations

import asyncio
import io
from pathlib import Path

import numpy as np
import soundfile as sf
import websockets

from backend.app.ai.audio_preprocessing import preprocess_audio_chunk
from backend.app.core.security import create_access_token


async def main() -> None:
    raw = Path('/mnt/c/Users/user/Desktop/SAREI/data/recordings/22/20260419T174226Z_933c6d777da1d425.webm').read_bytes()
    processed_bytes, _ = preprocess_audio_chunk(raw)
    audio, sr = sf.read(io.BytesIO(processed_bytes), dtype='float32')
    if getattr(audio, 'ndim', 1) > 1:
        audio = audio.mean(axis=1)
    audio = np.asarray(audio, dtype=np.float32)

    token = create_access_token({'sub': '5'})
    host = __import__('sys').argv[1] if len(__import__('sys').argv) > 1 else '127.0.0.1'
    uri = f'ws://{host}:8000/api/v1/realtime/ws/999?token={token}'
    print('CONNECT', uri)

    async with websockets.connect(uri, max_size=None) as ws:
        first = await asyncio.wait_for(ws.recv(), timeout=5)
        print('RECV1', first)
        await ws.send('{"type":"start_stream"}')
        await ws.send('{"type":"audio_config","sample_rate":16000}')

        chunk_size = int(sr * 0.5)
        for start in range(0, len(audio), chunk_size):
            chunk = audio[start:start + chunk_size]
            if chunk.size:
                await ws.send(chunk.tobytes())
                await asyncio.sleep(0.05)

        await ws.send('{"type":"end_call"}')

        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=3)
                print('MSG', msg)
        except Exception as exc:
            print('DONE', type(exc).__name__, str(exc))


if __name__ == '__main__':
    asyncio.run(main())
