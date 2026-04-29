from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
from langchain_classic.memory import ConversationBufferMemory
from langchain_core.tools import tool
from app.services.dice import DiceService
import os
import json
import logging
import traceback
from typing import Dict, List, Optional, Any

logger = logging.getLogger("trpg-harness.ai_gm")

# ---------------------------------------------------------------------------
# 1. 도구 정의
# ---------------------------------------------------------------------------
@tool
def roll_dice_tool(dice_str: str) -> str:
    """TRPG 주사위를 굴립니다. 예: '1d20', '2d6+4'"""
    dice = DiceService()
    result = dice.roll(dice_str)
    logger.info(f"[DICE] roll_dice_tool({dice_str!r}) => {result}")
    return json.dumps(result)

@tool
def modify_stats_tool(player_name: str, stat_type: str, amount: int) -> str:
    """플레이어의 스탯(hp 또는 ap)을 수정합니다.
    stat_type: 'hp' 또는 'ap'
    amount: 변경량 (양수=회복, 음수=소모). 예: -2, 3
    """
    logger.info(f"[STATS] modify_stats_tool({player_name!r}, {stat_type!r}, {amount})")
    return json.dumps({"player": player_name, "stat": stat_type, "amount": amount})

# ---------------------------------------------------------------------------
# 2. 응답 JSON 스키마 (Structured Output 핵심)
#    narrative: 자유 서사 필드 → AI 창의성 완전 보장
#    choices: 스키마로 기계적 형식 강제
# ---------------------------------------------------------------------------
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "narrative": {
            "type": "string",
            "description": (
                "몰입감 넘치는 소설체 서사 지문. "
                "시스템 수치(확률, 판정값 등)를 절대 포함하지 말 것."
            ),
        },
        "choices": {
            "type": "array",
            "description": "각 플레이어에게 정확히 3개씩 제시하는 행동 선택지 목록",
            "items": {
                "type": "object",
                "properties": {
                    "player":      {"type": "string",  "description": "선택지를 받는 플레이어 이름"},
                    "index":       {"type": "integer", "description": "선택지 번호 (1, 2, 3)"},
                    "text":        {"type": "string",  "description": "선택지 행동 설명"},
                    "probability": {"type": "integer", "description": "성공 확률 1~100 (UI 연산용 메타데이터)", "minimum": 1, "maximum": 100},
                },
                "required": ["player", "index", "text", "probability"],
            },
        },
        "price_declarations": {
            "type": "array",
            "description": "스킬 대가 선언 목록. 스킬 대가를 처음 확정할 때만 사용.",
            "items": {
                "type": "object",
                "properties": {
                    "player":      {"type": "string"},
                    "description": {"type": "string", "description": "예: 스킬 사용 시 AP 2 소모"},
                },
                "required": ["player", "description"],
            },
        },
    },
    "required": ["narrative", "choices"],
}

# ---------------------------------------------------------------------------
# 3. 계층형 시스템 프롬프트 (역할 / 규칙 / 출력 분리)
# ---------------------------------------------------------------------------
_ROLE = """당신은 숙련된 TRPG 게임 마스터(GM)입니다.
플레이어들의 행동을 종합하여 몰입감 있는 서사를 전개하고, 각자에게 의미 있는 선택지를 제시하세요.
모든 서사(narrative)는 생생한 소설체로 자유롭게 작성하되, 확률·판정값 같은 시스템 수치를 지문에 절대 노출하지 마세요."""

_STAT_RULES = """[스탯 규칙]
- 모든 플레이어는 HP(체력)와 AP(활동력)를 각 10씩 보유합니다. (최대 10)
- 판정 필요 시 roll_dice_tool, 스탯 변경 시 modify_stats_tool을 적극 사용하세요.
- 권장 수치: 일반 피해·소모 1~2 / 치명적 위기 3~5 / 기적적 회복 2~4
- 스킬 대가 확정 시 price_declarations 배열에 기록하세요 (HP 또는 AP 소모량 명시 필수)."""

_OUTPUT_GUIDE = """[응답 구조]
반드시 지정된 JSON 스키마로 응답하세요.
- narrative: 서사 지문 (완전 자유 텍스트, 창의성 최우선)
- choices: 생존 플레이어 1인당 정확히 3개. probability는 1~100 정수.
- price_declarations: 스킬 대가를 처음 정할 때만 포함.

[예시]
{
  "narrative": "횃불이 흔들리며 동굴 벽에 기이한 그림자를 드리웁니다...",
  "choices": [
    {"player": "아라곤", "index": 1, "text": "횃불을 높이 들고 전진한다", "probability": 65},
    {"player": "아라곤", "index": 2, "text": "벽면 문양을 조사한다", "probability": 40},
    {"player": "아라곤", "index": 3, "text": "입구 근처에서 경계한다", "probability": 80}
  ]
}"""

SYSTEM_PROMPT = "\n\n".join([_ROLE, _STAT_RULES, _OUTPUT_GUIDE])


# ---------------------------------------------------------------------------
# 4. AIGMService
# ---------------------------------------------------------------------------
class AIGMService:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("GOOGLE_API_KEY is not set")

        logger.info(f"[INIT] AIGMService initializing with API key: {self.api_key[:8]}...")

        base_llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=self.api_key,
            temperature=0.85,          # 서사 창의성 향상 (형식은 스키마가 보장)
        )

        # 도구 바인딩 → 구조화 출력 동시 적용
        self.llm = base_llm.bind_tools(
            [roll_dice_tool, modify_stats_tool],
        )

        # Structured Output용 (도구 결과 반영 후 최종 응답 생성 시 사용)
        self.llm_structured = base_llm.with_structured_output(RESPONSE_SCHEMA)

        self.memories: Dict[str, ConversationBufferMemory] = {}
        logger.info("[INIT] AIGMService initialized successfully")

    def get_memory(self, session_id: str) -> ConversationBufferMemory:
        if session_id not in self.memories:
            logger.debug(f"[MEMORY] Creating new memory for session: {session_id}")
            self.memories[session_id] = ConversationBufferMemory(
                return_messages=True, memory_key="history"
            )
        return self.memories[session_id]

    def _build_stat_context(self, state: Any) -> str:
        lines = ["\n[현재 파티 스탯 상태]"]
        for p, s in state.player_stats.items():
            status = state.player_statuses.get(p, "Alive")
            lines.append(f"- {p}: HP {s['hp']}/10, AP {s['ap']}/10, 상태: {status}")
        ctx = "\n".join(lines)
        logger.debug(f"[CONTEXT] Stat context built:\n{ctx}")
        return ctx

    async def _run_tool_loop(self, messages: list, state: Any) -> list:
        """도구 호출 루프: 주사위·스탯 변경을 처리하고 업데이트된 messages를 반환"""
        for iteration in range(5):
            logger.info(f"[TOOL_LOOP] Iteration {iteration + 1}/5 — invoking LLM with tools...")
            try:
                ai_msg = await self.llm.ainvoke(messages)
            except Exception as e:
                logger.error(f"[TOOL_LOOP] LLM invocation failed: {e}\n{traceback.format_exc()}")
                break

            if not ai_msg.tool_calls:
                logger.info(f"[TOOL_LOOP] No tool calls returned, exiting loop (iteration {iteration + 1})")
                break

            logger.info(f"[TOOL_LOOP] {len(ai_msg.tool_calls)} tool call(s) received")
            messages.append(ai_msg)

            for tc in ai_msg.tool_calls:
                logger.info(f"[TOOL_CALL] name={tc['name']}, args={tc['args']}")
                if tc["name"] == "roll_dice_tool":
                    try:
                        result = await roll_dice_tool.ainvoke(tc)
                        logger.info(f"[TOOL_RESULT] roll_dice => {result}")
                        messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
                    except Exception as e:
                        logger.error(f"[TOOL_CALL] roll_dice_tool error: {e}")
                        messages.append(ToolMessage(content=f"ERROR: {e}", tool_call_id=tc["id"]))
                elif tc["name"] == "modify_stats_tool":
                    args = tc["args"]
                    p, s_type, amt = args.get("player_name"), args.get("stat_type"), args.get("amount", 0)
                    if p in state.player_stats:
                        cur = state.player_stats[p].get(s_type, 10)
                        new_val = max(0, min(10, cur + amt))
                        state.player_stats[p][s_type] = new_val
                        res_msg = f"SUCCESS: {p}의 {s_type}가 {amt:+d}만큼 변하여 {new_val}이 되었습니다."
                        logger.info(f"[STATS_MODIFIED] {p}.{s_type}: {cur} -> {new_val} (delta={amt:+d})")
                    else:
                        res_msg = "ERROR: 플레이어를 찾을 수 없습니다."
                        logger.warning(f"[STATS_ERROR] Player '{p}' not found in stats")
                    messages.append(ToolMessage(content=res_msg, tool_call_id=tc["id"]))
                else:
                    logger.warning(f"[TOOL_CALL] Unknown tool: {tc['name']}")
        return messages

    async def generate_response(self, state: Any, player_name: str, message: str) -> dict:
        """
        구조화된 응답을 반환합니다.
        반환 형식: {"narrative": str, "choices": [...], "price_declarations": [...]}
        Harness: 사후 정규식 수리 X → 스키마 검증 기반 재시도
        """
        session_id = state.session_id
        logger.info(f"[GENERATE] session={session_id}, player={player_name}, message_preview={message[:100]}...")
        
        memory = self.get_memory(session_id)
        history = memory.load_memory_variables({})["history"]
        logger.debug(f"[GENERATE] History length: {len(history)} messages")
        stat_ctx = self._build_stat_context(state)

        system_msg = SystemMessage(content=SYSTEM_PROMPT + stat_ctx)
        messages = [system_msg] + history + [HumanMessage(content=f"{player_name}: {message}")]

        # 생존 중인 플레이어 목록
        active_players = [
            p for p in state.players
            if state.player_statuses.get(p, "Alive") not in ("Dead",)
        ]
        logger.info(f"[GENERATE] Active players: {active_players}")

        last_response = {"narrative": "혼란스러운 상황이 계속됩니다...", "choices": []}

        for attempt in range(3):
            logger.info(f"[GENERATE] Attempt {attempt + 1}/3")
            
            # 1) 도구 루프 실행 (주사위·스탯)
            messages = await self._run_tool_loop(messages, state)

            # 2) Structured Output으로 최종 응답 생성
            try:
                logger.info("[GENERATE] Invoking structured LLM for final response...")
                response = await self.llm_structured.ainvoke(messages)
                logger.info(f"[GENERATE] Structured response type: {type(response).__name__}")
            except Exception as e:
                logger.error(f"[GENERATE] Structured LLM invocation failed (attempt {attempt + 1}): {e}\n{traceback.format_exc()}")
                response = None

            if not isinstance(response, dict):
                logger.warning(f"[GENERATE] Response is not dict (type={type(response).__name__}), requesting retry...")
                messages.append(HumanMessage(
                    content="JSON 스키마에 맞춰 응답을 다시 작성해주세요."
                ))
                continue

            last_response = response
            logger.info(f"[GENERATE] Response keys: {list(response.keys())}")
            logger.debug(f"[GENERATE] Narrative preview: {response.get('narrative', '')[:200]}...")
            logger.info(f"[GENERATE] Choices count: {len(response.get('choices', []))}")

            # 3) 필수 검증: narrative 존재
            if not response.get("narrative", "").strip():
                logger.warning("[VALIDATE] Empty narrative, requesting retry...")
                messages.append(HumanMessage(
                    content="narrative 필드가 비어 있습니다. 서사 지문을 반드시 포함해주세요."
                ))
                continue

            # 4) 필수 검증: 활성 플레이어 1인당 선택지 3개
            choices_by_player: Dict[str, list] = {}
            for c in response.get("choices", []):
                choices_by_player.setdefault(c.get("player", ""), []).append(c)

            missing = [p for p in active_players if len(choices_by_player.get(p, [])) < 3]
            if missing:
                logger.warning(f"[VALIDATE] Missing choices for: {missing}")
                messages.append(HumanMessage(
                    content=f"다음 플레이어의 선택지가 3개 미만입니다: {', '.join(missing)}. 각 3개씩 추가해주세요."
                ))
                continue

            # 5) 검증 통과 → 메모리 저장 후 반환
            narrative = response["narrative"]
            memory.save_context({"input": message}, {"output": narrative})
            logger.info(f"[GENERATE] ✅ Response validated and saved (attempt {attempt + 1})")
            return response

        # 모든 재시도 실패 → 최선의 결과 반환
        logger.warning(f"[GENERATE] ⚠️ All 3 attempts exhausted, returning best-effort response")
        memory.save_context({"input": message}, {"output": last_response.get("narrative", "")})
        return last_response
