from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
from langchain_classic.memory import ConversationBufferMemory
from langchain_core.tools import tool
from app.services.dice import DiceService
import os
import json
from typing import Dict, List, Optional, Any

# 1. 주사위 도구 정의
@tool
def roll_dice_tool(dice_str: str) -> str:
    """TRPG 주사위를 굴립니다. 예: '1d20', '2d6+4'"""
    dice = DiceService()
    result = dice.roll(dice_str)
    return json.dumps(result)

# 2. 스탯 수정 도구 정의
@tool
def modify_stats_tool(player_name: str, stat_type: str, amount: int) -> str:
    """플레이어의 스탯(hp 또는 ap)을 수정합니다. 
    stat_type: 'hp' 또는 'ap'
    amount: -2 ~ +2 사이의 정수 (예: -1, 1)
    """
    return json.dumps({"player": player_name, "stat": stat_type, "amount": amount})

class AIGMService:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("GOOGLE_API_KEY is not set")
            
        # 도구 바인딩
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=self.api_key,
            temperature=0.7
        )
        self.llm_with_tools = llm.bind_tools([roll_dice_tool, modify_stats_tool])
        
        self.system_prompt = """당신은 숙련된 TRPG 게임 마스터(GM)입니다.
플레이어들의 행동에 대해 다음 단계로 사고하고 답변하세요:

[스탯 시스템]
- 모든 플레이어는 HP(체력)와 AP(활동력)를 각 10씩 가지고 시작합니다. (최대 10)
- `modify_stats_tool`을 사용하여 스탯을 자율적으로 조절하세요. (수치 제한 없음)
  * 권장: 일반적인 피해/소모는 1~2, 치명적인 위기는 3~5, 기적적인 회복은 2~4 정도가 적당합니다.
- 고유 스킬의 '대가'를 책정할 때는 반드시 HP나 AP의 구체적인 소모량을 포함시키세요. (예: [PRICE: 이름: 스킬 사용 시 AP 2 소모])

[진행 로직]
1. 모든 플레이어의 행동을 종합하여 상황의 변화를 묘사하세요.
2. 판정 필요성 확인: 주사위 판정이 필요한 경우 `roll_dice_tool`을 사용하세요.
3. 스탯 변화: 필요하다면 `modify_stats_tool`을 사용하여 플레이어의 상태를 변경하세요.
4. 결과 묘사: 주사위 및 스탯 변화 결과를 종합하여 생생하게 묘사하세요.
   - 주의: 지문에 "성공 확률: 30%" 같은 숫자는 절대 노출하지 마세요.
5. 선택지 제시: 각 플레이어별로 3개씩의 선택지를 명확히 제시하세요.
   - 형식: `[플레이어이름 선택지 1(50): 내용]`

모든 지문은 몰입감 넘치는 소설체로 작성하세요.

[출력 형식 엄수 - 매우 중요]
- 당신의 출력은 반드시 '순수 텍스트'여야 합니다. 
- 어떠한 경우에도 JSON, List, Dictionary 같은 프로그래밍 데이터 구조를 그대로 출력하지 마세요.
- 오직 이야기 지문과 `[PRICE: ...]` 태그, 그리고 `[플레이어이름 선택지 N: ...]` 태그만 사용하세요."""
        self.memories = {}

    def get_memory(self, session_id: str):
        if session_id not in self.memories:
            self.memories[session_id] = ConversationBufferMemory(return_messages=True, memory_key="history")
        return self.memories[session_id]

    async def generate_response(self, state: Any, player_name: str, message: str) -> str:
        session_id = state.session_id
        memory = self.get_memory(session_id)
        history = memory.load_memory_variables({})["history"]
        
        stats_context = "\n[현재 파티 스탯 상태]\n"
        for p, s in state.player_stats.items():
            stats_context += f"- {p}: HP {s['hp']}/10, AP {s['ap']}/10\n"
            
        messages = [SystemMessage(content=self.system_prompt + stats_context)] + history + [HumanMessage(content=f"{player_name}: {message}")]
        
        # 최대 2회 시도 (Harness 검증)
        for attempt in range(2):
            ai_msg = await self.llm_with_tools.ainvoke(messages)
            
            # 1. 도구 호출 루프
            iteration = 0
            while ai_msg.tool_calls and iteration < 5:
                iteration += 1
                messages.append(ai_msg)
                for tool_call in ai_msg.tool_calls:
                    if tool_call["name"] == "roll_dice_tool":
                        tool_result = await roll_dice_tool.ainvoke(tool_call)
                        messages.append(ToolMessage(content=str(tool_result), tool_call_id=tool_call["id"]))
                    elif tool_call["name"] == "modify_stats_tool":
                        args = tool_call["args"]
                        p_target = args.get("player_name")
                        s_type = args.get("stat_type")
                        amt = args.get("amount", 0)
                        if p_target in state.player_stats:
                            current = state.player_stats[p_target].get(s_type, 10)
                            # AI가 보낸 수치를 그대로 반영 (단, 0~10 사이로 제한)
                            new_val = max(0, min(10, current + amt))
                            state.player_stats[p_target][s_type] = new_val
                            res_msg = f"SUCCESS: {p_target}의 {s_type}가 {amt}만큼 변하여 {new_val}이 되었습니다."
                        else:
                            res_msg = "ERROR: 플레이어를 찾을 수 없습니다."
                        messages.append(ToolMessage(content=res_msg, tool_call_id=tool_call["id"]))
                ai_msg = await self.llm_with_tools.ainvoke(messages)
            
            # 2. 응답 텍스트 추출 (순수 텍스트만 필터링)
            final_content = ""
            if isinstance(ai_msg.content, str):
                final_content = ai_msg.content
            elif isinstance(ai_msg.content, list):
                for part in ai_msg.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        final_content += part.get("text", "")
                    elif isinstance(part, str):
                        final_content += part
            
            # --- [Harness: 데이터 노이즈 강제 제거] ---
            import re
            # [{'type': 'text', ...}] 형태나 extras, signature 같은 잔재를 정규식으로 제거
            # 만약 텍스트 전체가 JSON 형태라면 내부 'text' 필드만 추출 시도
            if "[{'type': 'text'" in final_content or "'extras': {" in final_content:
                # 텍스트 내부에 포함된 실제 대사만 추출 시도 (간단한 클리너)
                text_match = re.search(r"'text':\s*'(.*?)'(?:,\s*'extras'|\]|\})", final_content, re.DOTALL)
                if text_match:
                    final_content = text_match.group(1).replace("\\n", "\n")
                else:
                    # 정제가 안 되면 재시도 요청
                    if attempt == 0:
                        messages.append(HumanMessage(content="방금 응답에 파이썬 데이터 구조(extras, signature 등)가 섞여 나왔습니다. 오직 자연어 지문과 선택지만 포함된 '순수 텍스트'로만 다시 답변해주세요."))
                        continue

            # 3. 필수 구조 체크 (선택지 존재 여부)
            if "[" in final_content and "]" in final_content:
                # 불필요한 메타데이터가 남아있을 수 있으므로 최종 트리밍
                final_content = re.sub(r"\[\{'type'.*?\}\]", "", final_content)
                memory.save_context({"input": message}, {"output": final_content})
                return final_content
            
            # 형식이 미흡하면 재시도
            if attempt == 0:
                messages.append(HumanMessage(content="모든 플레이어에 대한 선택지를 [플레이어이름 선택지 N: 내용] 형식으로 포함하여 다시 작성해주세요. 데이터 구조(JSON/List)는 절대 포함하지 마세요."))
            else:
                return final_content
                
        return final_content
