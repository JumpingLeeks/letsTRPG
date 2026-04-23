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
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("trpg-harness")

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
            try: await connection.send_json(message)
            except: pass

    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
        self.active_connections[session_id].append(websocket)

    def disconnect(self, websocket: WebSocket, session_id: str):
        if session_id in self.active_connections:
            if websocket in self.active_connections[session_id]:
                self.active_connections[session_id].remove(websocket)

    async def broadcast(self, session_id: str, message: dict):
        if session_id in self.active_connections:
            for connection in self.active_connections[session_id]:
                try: await connection.send_json(message)
                except: pass

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
    incapacitated = []
    for p in state.players:
        stats = state.player_stats.get(p, {"hp": 10, "ap": 10})
        status = state.player_statuses.get(p, "Alive")
        if stats["hp"] <= 0 and status != "Dead":
            if status != "Dying":
                state.player_statuses[p] = "Dying"
                state.dying_counters[p] = 3
            else:
                state.dying_counters[p] -= 1
                if state.dying_counters[p] <= 0: state.player_statuses[p] = "Dead"
        elif stats["hp"] > 0 and status == "Dying":
            state.player_statuses[p] = "Alive"
            state.dying_counters[p] = 0
        if stats["ap"] <= 0 and state.player_statuses[p] == "Alive":
            state.player_statuses[p] = "Exhausted"

    departing = [p for p, c in state.missed_turns.items() if c >= 3]
    prompt = "--- ROUND SUMMARY ---\n"
    for a in state.pending_actions: prompt += f"[{a['player']}]: {a['action']}\n"
    
    if departing:
        for p in departing:
            handle_host_migration(state, p)
            if p in state.players: state.players.remove(p)
            if p in state.missed_turns: del state.missed_turns[p]
        prompt += f"\n[중요 알림] {', '.join(departing)}님이 파티에서 이탈했습니다.\n"
        await manager.broadcast_lobby({"type": "lobby_update", "sessions": get_sessions_info()})
    else:
        prompt += "\n위 상황을 종합하여 장면을 전개하고, 각 플레이어별 선택지 3개를 제시해줘."
    
    prompt += "\n\n※ 주의: 지문이나 선택지에 확률(%) 정보를 절대 노출하지 마세요."
    try:
        res_text = await ai_gm.generate_response(state, "SYSTEM", prompt)
        state.last_gm_response = res_text
        state.messages.append({"sender": "AI GM", "text": res_text})
        state.pending_actions = []
        state.current_turn = 0
        state.turn_start_time = time.time()
        # 첫 번째 플레이어가 행동 가능한지 체크 (자동 턴 넘김)
        await advance_turn_if_needed(state)
        await manager.broadcast(state.session_id, {"type": "state_update", "state": state.dict()})
    except Exception as e:
        logger.error(f"Resolution Error: {e}")

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
    await manager.connect_lobby(websocket)
    try:
        await websocket.send_json({"type": "lobby_update", "sessions": get_sessions_info()})
        while True:
            await asyncio.sleep(10)
            try: await websocket.send_json({"type": "ping"})
            except: break
    except WebSocketDisconnect: pass
    finally: manager.disconnect_lobby(websocket)

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(websocket, session_id)
    try:
        if session_id in sessions:
            await websocket.send_json({"type": "state_update", "state": sessions[session_id].dict()})
        while True:
            await asyncio.sleep(1)
            if session_id in sessions:
                await check_timeout(sessions[session_id])
    except WebSocketDisconnect:
        manager.disconnect(websocket, session_id)

@app.post("/chat")
async def chat(request: ChatRequest):
    sid = request.session_id
    if sid not in sessions: raise HTTPException(404)
    state = sessions[sid]
    if state.current_turn >= len(state.players) or state.players[state.current_turn] != request.player_name:
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
    
    await manager.broadcast(sid, {"type": "state_update", "state": state.dict()})
    if state.current_turn >= len(state.players):
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
    if sid not in sessions: raise HTTPException(404)
    state = sessions[sid]
    if state.host_name != request.player_name or state.is_started: raise HTTPException(403)
    
    state.is_started = True
    state.turn_start_time = time.time()
    await manager.broadcast(sid, {"type": "state_update", "state": state.dict()})
    
    p_names = ", ".join(state.players)
    skills = "".join([f"- {p}: {s['skill_name']}({s['ability']})\n" for p, s in state.player_skills.items() if p in state.players])
    
    prompt = f"모험 시작! 플레이어: {p_names}\n{f'[스킬]\\n{skills}\\n[지시] 각 스킬의 대가를 [PRICE: 이름: 내용] 형식으로 정해줘.' if skills else ''}\n지문에 확률 노출 금지. 3개씩 선택지 제시."
    try:
        res = await ai_gm.generate_response(state, "SYSTEM", prompt)
        import re
        for p, price in re.findall(r'\[PRICE: (.+?): (.+?)\]', res):
            if p in state.player_skills: state.player_skills[p]["price"] = price
        state.messages.append({"sender": "AI GM", "text": res})
        await manager.broadcast(sid, {"type": "state_update", "state": state.dict()})
        await manager.broadcast_lobby({"type": "lobby_update", "sessions": get_sessions_info()})
    except Exception as e: logger.error(f"Start Error: {e}")
    return {"status": "started"}

@app.post("/join")
async def join_session(request: JoinRequest):
    sid = request.session_id
    if sid not in sessions: sessions[sid] = SessionState(session_id=sid, turn_start_time=time.time(), last_activity=time.time())
    state = sessions[sid]
    name = request.player_name
    if name not in state.players:
        if not state.host_name: state.host_name = name
        state.players.append(name)
        state.player_stats[name] = {"hp": 10, "ap": 10}
        state.player_statuses[name] = "Alive"
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
    asyncio.create_task(background_cleanup_task())

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
