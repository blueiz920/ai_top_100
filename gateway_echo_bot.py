# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
#     "websockets>=15.0",
# ]
# ///
import asyncio
import json
import logging
import os
import sys
import httpx
import websockets
from websockets.exceptions import ConnectionClosed
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("echobot")
# ── Config ───────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
KAKAO_GW_URL = "wss://bot-gateway.kakao.com/gateway"
KAKAO_API_URL = "https://kapi.kakao.com"
# ── Opcodes ──────────────────────────────────────────────────────────
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_READY = 3
OP_RESUME = 6
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11
# ── Close Code ───────────────────────────────────────────────────────
CLOSE_SERVER_RESTART = 1001   # Graceful shutdown → RESUME
CLOSE_AUTH_FAILED = 4001      # token 오류 → 중단
CLOSE_INVALID_PAYLOAD = 4003  # 프로토콜 버그 → 중단
CLOSE_REPLACED = 4004         # 중복 연결로 대체됨 → 중단
CLOSE_SESSION_TIMEOUT = 4009  # 세션 만료 → 새 IDENTIFY
RECONNECT_BACKOFF_SEC = 3.0
# ── 메시지 처리 ──────────────────────────────────────────────────────
async def on_message(d: dict) -> None:
    """MESSAGE_CREATE 이벤트를 받아 받은 내용을 그대로 callback으로 돌려준다."""
    content = d.get("content", "")
    callback_token = d.get("callbackToken", "")
    log.info("MESSAGE_CREATE received")
    if not callback_token:
        log.error("callbackToken 누락 — skip")
        return
    await send_callback(callback_token, content)
# ── 봇 callback ──────────────────────────────────────────
async def send_callback(callback_token: str, text: str) -> None:
    """봇 callback 엔드포인트로 카카오 스킬 응답(simpleText)을 전송한다."""
    url = f"{KAKAO_API_URL}/v1/bot/callback"
    body = {
        "skillResponse": {
            "version": "2.0",
            "template": {"outputs": [
                    {"simpleText": {"text": f"echo: {text}"}},
                ]
            },
        },
    }
    headers = {
        "X-Bot-Callback-Token": callback_token,
        "Authorization": f"KakaoAK {BOT_TOKEN}"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=body, headers=headers)
    if resp.status_code != 200:
        log.error("callback failed — status=%d", resp.status_code)
# ── 세션 상태 ────────────────────────────────────────────────────────
class Session:
    """RESUME에 필요한 session_id와 마지막으로 받은 시퀀스(seq)를 보관한다."""
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.last_seq: int = 0
    def can_resume(self) -> bool:
        return self.session_id is not None
    def reset(self) -> None:
        self.session_id = None
        self.last_seq = 0
# ── WebSocket 게이트웨이 ─────────────────────────────────────────────
async def gateway_loop(session: Session, handler=on_message) -> None:
    """게이트웨이에 한 번 연결해 핸드셰이크 후 heartbeat·수신 루프를 동시에 돈다."""
    log.info("connecting to %s", KAKAO_GW_URL)
    async with websockets.connect(KAKAO_GW_URL) as ws:
        # 1. HELLO 수신 — heartbeat_interval 확인
        hello = json.loads(await ws.recv())
        heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0
        if session.can_resume():
            # 재연결: RESUME으로 세션 복구
            log.info("RESUME — seq=%d", session.last_seq)
            await ws.send(json.dumps({"op": OP_RESUME, "d": {
                "token": BOT_TOKEN,
                "session_id": session.session_id,
                "seq": session.last_seq,
            }}))
        else:
            # 최초 연결: IDENTIFY → READY
            await ws.send(json.dumps({"op": OP_IDENTIFY, "d": {
                "token": BOT_TOKEN,
            }}))
            ready = json.loads(await ws.recv())
            session.session_id = ready["d"]["session_id"]
            session.last_seq = 0
            log.info("READY — gateway connected")
        # 2. heartbeat + 이벤트 수신을 동시에 실행
        await asyncio.gather(
            _heartbeat(ws, heartbeat_interval),
            _receive(ws, session, handler),
        )
async def _heartbeat(ws, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await ws.send(json.dumps({"op": OP_HEARTBEAT}))
async def _receive(ws, session: Session, handler=on_message) -> None:
    async for raw in ws:
        payload = json.loads(raw)
        if payload.get("s") is not None:
            session.last_seq = payload["s"]
        if payload.get("op") == OP_DISPATCH and payload.get("t") == "MESSAGE_CREATE":
            await handler(payload["d"])
def _should_resume(code: int | None) -> bool:
    if code in (CLOSE_AUTH_FAILED, CLOSE_INVALID_PAYLOAD, CLOSE_REPLACED):
        log.error("close %s — 재연결 중단 (프로토콜/인증 오류)", code)
        raise SystemExit(1)
    if code == CLOSE_SESSION_TIMEOUT:
        return False
    return True
async def run(session: Session, handler=on_message) -> None:
    while True:
        try:
            await gateway_loop(session, handler)
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
            log.warning("connection closed — code=%s", code)
            if not _should_resume(code):
                session.reset()
        except OSError as e:
            log.warning("connect failed — %s", e)
        log.info("reconnecting in %.1fs …", RECONNECT_BACKOFF_SEC)
        await asyncio.sleep(RECONNECT_BACKOFF_SEC)
async def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN 환경변수를 설정하세요")
        sys.exit(1)
    log.info("echo bot starting — gateway=%s", KAKAO_GW_URL)
    await run(Session())
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[echobot] stopped")
