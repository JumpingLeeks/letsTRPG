from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import uvicorn
import os
import time
import asyncio
import json
from dotenv import load_dotenv
import logging
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

load_dotenv()

from app.services.ai_gm import AIGMService
from app.services.dice import DiceService

# 로깅 설정
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("trpg-harness")
# 외부 라이브러리 로그 레벨 조정 (너무 시끄러움 방지)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.WARNING)

app = FastAPI(title="AI TRPG Real-time Server")
ai_gm = AIGMService()
dice_service = DiceService()

# 정적 파일 설정
static_path = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")

@app.get("/gui", response_class=HTMLResponse)
async def get_gui():
    with open(os.path.join(static_path, "index.html"), "r", encoding="utf-8") as f:
        return f.read()

# 데이터 모델
class SessionState(BaseModel):
    session_id: str
    players: List[str] = []
    host_name: Optional[str] = None
    is_started: bool = False
    current_turn: int = 0
    pending_actions: List[dict] = []
    last_gm_response: str = ""
    last_gm_choices: List[dict] = []
    messages: List[dict] = []
    strategy_messages: List[dict] = []
    player_skills: Dict[str, dict] = {} 
    player_stats: Dict[str, dict] = {} 
    player_statuses: Dict[str, str] = {}
    dying_counters: Dict[str, int] = {}
    max_players: int = 4
    turn_start_time: float = 0.0
    last_activity: float = 0.0
    missed_turns: Dict[str, int] = {}

class SkillRequest(BaseModel):
    session_id: str
    player_name: str
    skill_name: str
    ability: str

class JoinRequest(BaseModel):
    session_id: str
    player_name: str

class ChatRequest(BaseModel):
    session_id: str
    player_name: str
    message: str

# 웹소켓 매니저
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}
        self.lobby_connections: List[WebSocket] = []

    async def connect_lobby(self, websocket: WebSocket):
        await websocket.accept()
        self.lobby_connections.append(websocket)

    def disconnect_lobby(self, websocket: WebSocket):
        if websocket in self.lobby_connections:
            self.lobby_connections.remove(websocket)

    async def broadcast_lobby(self, message: Any):
        for connection in self.lobby_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.warning(f"[WS_LOBBY] broadcast failed: {e}")

    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
        self.active_connections[session_id].append(websocket)
        logger.info(f"[WS] Client connected to session '{session_id}' (total: {len(self.active_connections[session_id])})")

    def disconnect(self, websocket: WebSocket, session_id: str):
        if session_id in self.active_connections:
            if websocket in self.active_connections[session_id]:
                self.active_connections[session_id].remove(websocket)
                logger.info(f"[WS] Client disconnected from session '{session_id}' (remaining: {len(self.active_connections[session_id])})")

    async def broadcast(self, session_id: str, message: dict):
        if session_id in self.active_connections:
            for connection in self.active_connections[session_id]:
                try:
                    await connection.send_json(message)
                except Exception as e:
                    logger.warning(f"[WS] broadcast to session '{session_id}' failed: {e}")

manager = ConnectionManager()
sessions: Dict[str, SessionState] = {}

def get_sessions_info():
    return [{"session_id": sid, "player_count": len(s.players), "max_players": s.max_players} for sid, s in sessions.items()]

def handle_host_migration(state: SessionState, leaving_player: str):
    if state.host_name == leaving_player:
        remaining = [p for p in state.players if p != leaving_player]
        if remaining:
            state.host_name = remaining[0]
        else:
            state.host_name = None

async def resolve_round(state: SessionState):
    logger.info(f"[RESOLVE] Starting round resolution for session '{state.session_id}'")
    logger.info(f"[RESOLVE] Pending actions: {len(state.pending_actions)}")
    
    for p in state.players:
        stats = state.player_stats.get(p, {"hp": 10, "ap": 10})
        status = state.player_statuses.get(p, "Alive")
        if stats["hp"] <= 0 and status != "Dead":
            if status != "Dying":
                state.player_statuses[p] = "Dying"
                state.dying_counters[p] = 3
                logger.info(f"[RESOLVE] {p} entered DYING state (3 turns)")
            else:
                state.dying_counters[p] -= 1
                if state.dying_counters[p] <= 0:
                    state.player_statuses[p] = "Dead"
                    logger.info(f"[RESOLVE] {p} has DIED")
        elif stats["hp"] > 0 and status == "Dying":
            state.player_statuses[p] = "Alive"
            state.dying_counters[p] = 0
            logger.info(f"[RESOLVE] {p} recovered from Dying -> Alive")
        if stats["ap"] <= 0 and state.player_statuses[p] == "Alive":
            state.player_statuses[p] = "Exhausted"
            logger.info(f"[RESOLVE] {p} is now EXHAUSTED")

    departing = [p for p, c in state.missed_turns.items() if c >= 3]
    prompt = "--- ROUND SUMMARY ---\n"
    for a in state.pending_actions: prompt += f"[{a['player']}]: {a['action']}\n"

    if departing:
        logger.info(f"[RESOLVE] Departing players: {departing}")
        for p in departing:
            handle_host_migration(state, p)
            if p in state.players: state.players.remove(p)
            if p in state.missed_turns: del state.missed_turns[p]
        prompt += f"\n[중요 알림] {', '.join(departing)}님이 파티에서 이탈했습니다."
        await manager.broadcast_lobby({"type": "lobby_update", "sessions": get_sessions_info()})
    else:
        prompt += "\n위 행동들을 종합하여 서사를 전개하고, 각 생존 플레이어에게 선택지 3개를 제시하세요."

    try:
        logger.info(f"[RESOLVE] Calling AI GM for round resolution...")
        response = await ai_gm.generate_response(state, "SYSTEM", prompt)
        narrative = response.get("narrative", "")
        choices = response.get("choices", [])
        logger.info(f"[RESOLVE] ✅ AI response received: narrative={len(narrative)} chars, choices={len(choices)}")
        state.last_gm_response = narrative
        state.last_gm_choices = choices
        state.messages.append({"sender": "AI GM", "text": narrative, "choices": choices})
        state.pending_actions = []
        state.current_turn = 0
        state.turn_start_time = time.time()
        await advance_turn_if_needed(state)
        await manager.broadcast(state.session_id, {"type": "state_update", "state": state.dict()})
    except Exception as e:
        import traceback
        logger.error(f"[RESOLVE] ❌ Resolution Error: {e}\n{traceback.format_exc()}")

async def advance_turn_if_needed(state: SessionState):
    """행동 불능인 플레이어를 건너뜁니다."""
    while state.current_turn < len(state.players):
        p = state.players[state.current_turn]
        status = state.player_statuses.get(p, "Alive")
        if status == "Exhausted":
            state.player_stats[p]["ap"] = 1
            state.player_statuses[p] = "Alive"
            state.current_turn += 1
            continue
        elif status in ["Dying", "Dead"]:
            state.current_turn += 1
            continue
        break

async def check_timeout(state: SessionState):
    if not state.is_started or state.current_turn >= len(state.players): return False
    limit = 60 if state.current_turn == 0 else 30
    if time.time() - state.turn_start_time > limit:
        p_name = state.players[state.current_turn]
        state.missed_turns[p_name] = state.missed_turns.get(p_name, 0) + 1
        state.messages.append({"sender": "System", "text": f"({p_name}님이 응답이 없어 턴이 넘어갑니다.)"})
        state.current_turn += 1
        await advance_turn_if_needed(state)
        state.turn_start_time = time.time()
        if state.current_turn >= len(state.players):
            await resolve_round(state)
        else:
            await manager.broadcast(state.session_id, {"type": "state_update", "state": state.dict()})
        return True
    return False

@app.websocket("/ws-lobby")
async def lobby_websocket(websocket: WebSocket):
    logger.info("[WS_LOBBY] New lobby connection")
    await manager.connect_lobby(websocket)
    try:
        await websocket.send_json({"type": "lobby_update", "sessions": get_sessions_info()})
        while True:
            await asyncio.sleep(10)
            try:
                await websocket.send_json({"type": "ping"})
            except Exception as e:
                logger.info(f"[WS_LOBBY] Ping failed, closing: {e}")
                break
    except WebSocketDisconnect:
        logger.info("[WS_LOBBY] Client disconnected")
    finally:
        manager.disconnect_lobby(websocket)

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    logger.info(f"[WS] New game connection for session '{session_id}'")
    await manager.connect(websocket, session_id)
    try:
        if session_id in sessions:
            await websocket.send_json({"type": "state_update", "state": sessions[session_id].dict()})
            logger.debug(f"[WS] Sent initial state to client for session '{session_id}'")
        while True:
            await asyncio.sleep(1)
            if session_id in sessions:
                await check_timeout(sessions[session_id])
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from session '{session_id}'")
        manager.disconnect(websocket, session_id)

@app.post("/chat")
async def chat(request: ChatRequest):
    sid = request.session_id
    logger.info(f"[CHAT] session={sid}, player={request.player_name}, message_preview={request.message[:80]}")
    if sid not in sessions:
        logger.warning(f"[CHAT] Session '{sid}' not found")
        raise HTTPException(404)
    state = sessions[sid]
    if state.current_turn >= len(state.players) or state.players[state.current_turn] != request.player_name:
        logger.warning(f"[CHAT] Not {request.player_name}'s turn (current_turn={state.current_turn}, players={state.players})")
        raise HTTPException(403, "Not your turn")

    msg = request.message
    if "[사용자 선택:" in msg:
        import re
        m = re.search(r'사용자 선택: "(.+?)", 판정 결과: (.+?),', msg)
        if m: msg = f"🎲 {m.group(1)} ({m.group(2)}!)"
    
    state.messages.append({"sender": request.player_name, "text": msg})
    state.pending_actions.append({"player": request.player_name, "action": request.message})
    state.current_turn += 1
    await advance_turn_if_needed(state)
    state.turn_start_time = time.time()
    
    logger.info(f"[CHAT] Turn advanced to {state.current_turn}/{len(state.players)}")
    await manager.broadcast(sid, {"type": "state_update", "state": state.dict()})
    if state.current_turn >= len(state.players):
        logger.info(f"[CHAT] All players acted, triggering round resolution")
        await resolve_round(state)
    return {"status": "ok"}

@app.post("/register_skill")
async def register_skill(request: SkillRequest):
    if request.session_id not in sessions: raise HTTPException(404)
    state = sessions[request.session_id]
    state.player_skills[request.player_name] = {"skill_name": request.skill_name, "ability": request.ability, "price": "미정"}
    await manager.broadcast(request.session_id, {"type": "state_update", "state": state.dict()})
    return {"status": "ok"}

@app.post("/start")
async def start_game(request: JoinRequest):
    sid = request.session_id
    logger.info(f"[START] Game start requested by '{request.player_name}' in session '{sid}'")
    if sid not in sessions:
        logger.warning(f"[START] Session '{sid}' not found")
        raise HTTPException(404)
    state = sessions[sid]
    if state.host_name != request.player_name or state.is_started:
        logger.warning(f"[START] Forbidden: host={state.host_name}, requester={request.player_name}, started={state.is_started}")
        raise HTTPException(403)

    state.is_started = True
    state.turn_start_time = time.time()
    await manager.broadcast(sid, {"type": "state_update", "state": state.dict()})

    p_names = ", ".join(state.players)
    skills_list = [(p, s) for p, s in state.player_skills.items() if p in state.players]
    skill_text = ""
    if skills_list:
        skill_text = "[스킬 목록]\n" + "".join(
            [f"- {p}: {s['skill_name']} ({s['ability']})\n" for p, s in skills_list]
        ) + "\n각 스킬의 대가(HP/AP 소모량)를 price_declarations 배열에 반드시 포함하세요."
    prompt = f"모험 시작! 플레이어: {p_names}\n{skill_text}\n장대한 오프닝 서사로 시작하고, 각 플레이어에게 선택지 3개를 제시하세요."
    logger.info(f"[START] Players: {p_names}")
    logger.info(f"[START] Calling AI GM for opening narrative...")

    try:
        response = await ai_gm.generate_response(state, "SYSTEM", prompt)
        narrative = response.get("narrative", "")
        choices = response.get("choices", [])
        price_declarations = response.get("price_declarations", [])
        logger.info(f"[START] ✅ AI response: narrative={len(narrative)} chars, choices={len(choices)}, prices={len(price_declarations)}")

        for decl in price_declarations:
            p = decl.get("player", "")
            desc = decl.get("description", "")
            if p in state.player_skills:
                state.player_skills[p]["price"] = desc
                logger.info(f"[START] Skill price set: {p} -> {desc}")

        state.last_gm_response = narrative
        state.last_gm_choices = choices
        state.messages.append({"sender": "AI GM", "text": narrative, "choices": choices})
        await manager.broadcast(sid, {"type": "state_update", "state": state.dict()})
        await manager.broadcast_lobby({"type": "lobby_update", "sessions": get_sessions_info()})
    except Exception as e:
        import traceback
        logger.error(f"[START] ❌ Start Error: {e}\n{traceback.format_exc()}")
    return {"status": "started"}

@app.post("/join")
async def join_session(request: JoinRequest):
    sid = request.session_id
    logger.info(f"[JOIN] Player '{request.player_name}' joining session '{sid}'")
    if sid not in sessions:
        sessions[sid] = SessionState(session_id=sid, turn_start_time=time.time(), last_activity=time.time())
        logger.info(f"[JOIN] Created new session '{sid}'")
    state = sessions[sid]
    name = request.player_name
    if name not in state.players:
        if not state.host_name:
            state.host_name = name
            logger.info(f"[JOIN] '{name}' assigned as host")
        state.players.append(name)
        state.player_stats[name] = {"hp": 10, "ap": 10}
        state.player_statuses[name] = "Alive"
        logger.info(f"[JOIN] '{name}' added to session (total players: {len(state.players)})")
    else:
        logger.info(f"[JOIN] '{name}' already in session, reconnecting")
    await manager.broadcast(sid, {"type": "state_update", "state": state.dict()})
    await manager.broadcast_lobby({"type": "lobby_update", "sessions": get_sessions_info()})
    return {"player_name": name, "session_id": sid, "is_host": state.host_name == name}

@app.post("/strategy_chat")
async def strategy_chat(request: ChatRequest):
    sid = request.session_id
    if sid in sessions:
        state = sessions[sid]
        state.strategy_messages.append({"sender": request.player_name, "text": request.message, "time": time.strftime("%H:%M")})
        await manager.broadcast(sid, {"type": "state_update", "state": state.dict()})
    return {"status": "ok"}

@app.post("/leave")
async def leave_session(request: JoinRequest):
    if request.session_id in sessions:
        state = sessions[request.session_id]
        if request.player_name in state.players:
            handle_host_migration(state, request.player_name)
            state.missed_turns[request.player_name] = 3
            await manager.broadcast(request.session_id, {"type": "state_update", "state": state.dict()})
            await manager.broadcast_lobby({"type": "lobby_update", "sessions": get_sessions_info()})
    return {"status": "ok"}

async def background_cleanup_task():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        to_del = [sid for sid, s in sessions.items() if now - s.last_activity > 3600]
        for sid in to_del: del sessions[sid]

@app.on_event("startup")
async def startup_event():
    logger.info("="*60)
    logger.info("[STARTUP] AI TRPG Harness Server starting...")
    logger.info(f"[STARTUP] Static files path: {static_path}")
    logger.info(f"[STARTUP] GOOGLE_API_KEY configured: {'Yes' if os.getenv('GOOGLE_API_KEY') else 'NO!'}")
    logger.info("="*60)
    asyncio.create_task(background_cleanup_task())

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
