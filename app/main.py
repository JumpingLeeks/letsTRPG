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
    host_name: Optional[str] = None # 방장 닉네임
    is_started: bool = False # 게임 시작 여부
    current_turn: int = 0
    pending_actions: List[dict] = []
    last_gm_response: str = ""
    messages: List[dict] = []
    max_players: int = 4
    turn_start_time: float = 0.0
    last_activity: float = 0.0
    missed_turns: Dict[str, int] = {}

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

def get_sessions_info():
    return [{"session_id": sid, "player_count": len(s.players), "max_players": s.max_players} for sid, s in sessions.items()]

@app.websocket("/ws-lobby")
async def lobby_websocket(websocket: WebSocket):
    await manager.connect_lobby(websocket)
    try:
        # 접속 즉시 현재 방 목록 전송
        await websocket.send_json({"type": "lobby_update", "sessions": get_sessions_info()})
        while True:
            await asyncio.sleep(10) # 연결 유지용
            await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        manager.disconnect_lobby(websocket)
sessions: Dict[str, SessionState] = {}

# 로직 함수들
def check_timeout(state: SessionState):
    if not state.is_started: return False # 시작 전에는 타이머 정지
    if not state.players or state.current_turn >= len(state.players): return False
    now = time.time()
    if now - state.turn_start_time > 30:
        p_name = state.players[state.current_turn]
        state.missed_turns[p_name] = state.missed_turns.get(p_name, 0) + 1
        state.messages.append({"sender": "System", "text": f"({p_name}님이 응답이 없어 턴이 넘어갑니다.)"})
        state.current_turn += 1
        state.turn_start_time = now
        return True
    return False

def handle_host_migration(state: SessionState, leaving_player: str):
    """방장이 나갈 경우 다음 사람에게 방장 권한 위임"""
    if state.host_name == leaving_player:
        remaining = [p for p in state.players if p != leaving_player]
        if remaining:
            state.host_name = remaining[0]
            logger.info(f"Host migrated to: {state.host_name}")
        else:
            state.host_name = None

async def resolve_round(state: SessionState):
    """종합 판정 로직 - 퇴장 및 방장 위임 처리 포함"""
    logger.info(f"[{state.session_id}] Round resolution triggered...")
    departing = [p for p, c in state.missed_turns.items() if c >= 3]
    
    prompt = "--- ROUND SUMMARY ---\n"
    for a in state.pending_actions: prompt += f"[{a['player']}]: {a['action']}\n"
    
    if departing:
        # 이탈자 발생 시 방장 위임 체크
        for p in departing:
            handle_host_migration(state, p)
            if p in state.players: state.players.remove(p)
            if p in state.missed_turns: del state.missed_turns[p]
        
        prompt += f"\n[중요 알림] 다음 플레이어들이 오랜 시간 응답이 없어 파티에서 이탈합니다: {', '.join(departing)}\n"
        prompt += "이들이 사라진 이유를 서사적으로 묘사하고 남은 인원들에 대한 전개를 이어가줘."
        # 로비 업데이트 (인원수 변경 반영)
        await manager.broadcast_lobby({"type": "lobby_update", "sessions": get_sessions_info()})
    else:
        prompt += "\n위 상황을 종합하여 장면을 전개하고, 각 플레이어별 선택지 3개를 제시해줘."

    try:
        res_text = await ai_gm.generate_response(state.session_id, "SYSTEM", prompt)
        state.last_gm_response = res_text
        state.messages.append({"sender": "AI GM", "text": res_text})
        state.pending_actions = []
        state.current_turn = 0
        state.turn_start_time = time.time()
        # 모든 플레이어에게 상태 브로드캐스트
        await manager.broadcast(state.session_id, {"type": "state_update", "state": state.dict()})
    except Exception as e:
        logger.error(f"Resolution Error: {e}")

# ?뷀븘??
@app.post("/start")
async def start_game(request: JoinRequest):
    sid = request.session_id
    if sid not in sessions: raise HTTPException(404)
    state = sessions[sid]
    
    if state.host_name != request.player_name:
        raise HTTPException(403, "방장만 게임을 시작할 수 있습니다.")
    
    if state.is_started:
        return {"status": "already_started"}

    state.is_started = True
    state.turn_start_time = time.time()
    
    # 오프닝 요청
    prompt = "모험이 시작되었습니다! 현재 파티원들의 이름과 상황을 바탕으로 도입부 장면을 아주 흥미진진하게 묘사해줘. 그리고 각자에게 첫 행동 선택지를 줘."
    try:
        res_text = await ai_gm.generate_response(sid, "SYSTEM", prompt)
        state.last_gm_response = res_text
        state.messages.append({"sender": "AI GM", "text": res_text})
        await manager.broadcast(sid, {"type": "state_update", "state": state.dict()})
    except Exception as e:
        logger.error(f"Start Error: {e}")
        
    return {"status": "started"}

@app.post("/join")
async def join_session(request: JoinRequest):
    now = time.time()
    sid = request.session_id
    if sid not in sessions:
        sessions[sid] = SessionState(session_id=sid, turn_start_time=now, last_activity=now)
    state = sessions[sid]
    state.last_activity = now
    
    name = request.player_name
    if name in state.players:
        if name in state.missed_turns: state.missed_turns[name] = 0
    else:
        # 방장 지정 (첫 번째 사람)
        if not state.host_name:
            state.host_name = name
            logger.info(f"Host assigned: {name} for session {sid}")

        temp = name
        c = 2
        while temp in state.players:
            temp = f"{name} {c}"
            c += 1
        name = temp
        if len(state.players) >= state.max_players: raise HTTPException(400, "Full")
        state.players.append(name)
        if len(state.players) == 1: state.turn_start_time = now
    
    await manager.broadcast(sid, {"type": "player_joined", "player_name": name, "state": state.dict()})
    # 로비 업데이트
    await manager.broadcast_lobby({"type": "lobby_update", "sessions": get_sessions_info()})
    return {"player_name": name, "session_id": sid, "is_host": state.host_name == name}

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(websocket, session_id)
    try:
        # ?곌껐 利됱떆 ?꾩옱 ?곹깭 ?꾩넚
        if session_id in sessions:
            await websocket.send_json({"type": "state_update", "state": sessions[session_id].dict()})
        
        while True:
            # 3珥덈쭏??타?꾩븘??泥댄겕 諛??곹깭 ?꾩넚 (?대줈 ?붿꽭?대씪??寃곌낵 泥섎━瑜??꾪븿)
            await asyncio.sleep(1)
            if session_id in sessions:
                state = sessions[session_id]
                changed = check_timeout(state)
                if changed:
                    if state.current_turn >= len(state.players):
                        await resolve_round(state)
                    else:
                        await manager.broadcast(session_id, {"type": "state_update", "state": state.dict()})
                else:
                    # ???쒓컙 ?숈씠?붾? ?꾪빐 ?곹깭 ?꾩넚 (?먰븳?ㅻ㈃ ?꾩슂??)
                    await websocket.send_json({"type": "time_sync", "server_now": time.time()})

    except WebSocketDisconnect:
        manager.disconnect(websocket, session_id)

@app.post("/chat")
async def chat(request: ChatRequest):
    sid = request.session_id
    if sid not in sessions: raise HTTPException(404)
    state = sessions[sid]
    state.last_activity = time.time()
    
    # 턴 체크
    if state.current_turn >= len(state.players) or state.players[state.current_turn] != request.player_name:
        raise HTTPException(403, "Not your turn")

    # 행동 기록 및 변환
    display_msg = request.message
    if "[사용자 선택:" in display_msg:
        import re
        m = re.search(r'사용자 선택: "(.+?)", 판정 결과: (.+?),', display_msg)
        if m: display_msg = f"🎲 {m.group(1)} ({m.group(2)}!)"
    
    state.messages.append({"sender": request.player_name, "text": display_msg})
    state.pending_actions.append({"player": request.player_name, "action": request.message})
    
    state.current_turn += 1
    state.turn_start_time = time.time()
    
    # [핵심 수정] AI 판정 전에 일단 현재 행동을 모든 유저에게 즉시 전파!
    await manager.broadcast(sid, {"type": "state_update", "state": state.dict()})
    
    # 마지막 플레이어였다면 판정 시작
    if state.current_turn >= len(state.players):
        # AI 판정은 시간이 걸리므로 별도의 비동기 태스크로 실행하거나 그대로 await
        # 여기서는 일관성을 위해 await 하되, 이미 위에서 행동은 전파되었으므로 유저는 대기감을 덜 느끼게 됨
        await resolve_round(state)
    
    return {"status": "ok"}

@app.get("/sessions")
async def list_sessions():
    return [{"session_id": sid, "player_count": len(s.players), "max_players": s.max_players} for sid, s in sessions.items()]

@app.post("/leave")
async def leave_session(request: JoinRequest):
    if request.session_id in sessions:
        state = sessions[request.session_id]
        if request.player_name in state.players:
            # 방장이면 즉시 위임
            handle_host_migration(state, request.player_name)
            
            # 즉시 제거 예약 (3회 미응답 처리)
            state.missed_turns[request.player_name] = 3
            logger.info(f"Player {request.player_name} marked for departure. Host migrated.")
            
            # 모든 플레이어에게 상태 브로드캐스트 (방장 변경 등 반영)
            await manager.broadcast(request.session_id, {"type": "state_update", "state": state.dict()})
            # 로비 업데이트
            await manager.broadcast_lobby({"type": "lobby_update", "sessions": get_sessions_info()})
            return {"status": "left"}
    return {"status": "not_found"}

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
