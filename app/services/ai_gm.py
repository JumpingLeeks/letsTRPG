from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
from langchain_classic.memory import ConversationBufferMemory
from langchain_core.tools import tool
from app.services.dice import DiceService
import os
import json

# 주사위 도구 정의
@tool
def roll_dice_tool(dice_str: str) -> str:
    """TRPG 주사위를 굴립니다. 예: '1d20', '2d6+4'"""
    dice = DiceService()
    result = dice.roll(dice_str)
    return json.dumps(result)

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
        self.llm_with_tools = llm.bind_tools([roll_dice_tool])
        
        self.system_prompt = """
        당신은 숙련된 TRPG 게임 마스터(GM)입니다. 당신은 파티원 전체의 행동을 종합하여 한 편의 이야기처럼 장면을 전개해야 합니다.
        
        [라운드 진행 로직]
        1. 모든 플레이어의 행동 보고가 들어오면, 이를 바탕으로 성공/실패 여부를 판단하여 전체적인 상황의 변화를 묘사하세요.
        2. 장면 묘사 후, **각 플레이어마다 개별적으로 3개의 선택지**를 제시해야 합니다.
        
        [선택지 형식 - 매우 중요]
        각 플레이어의 이름이 포함된 태그를 사용하세요:
        - `[플레이어이름 선택지 1(20): ...]` (어려움)
        - `[플레이어이름 선택지 2(50): ...]` (보통)
        - `[플레이어이름 선택지 3(70): ...]` (쉬움)
        예를 들어 플레이어가 '전사', '마법사'라면 각각 3개씩 총 6개의 선택지가 나와야 합니다.
        
        [주의 사항]
        - 플레이어에게 확률(P) 숫자를 절대 노출하지 마세요.
        - 다른 플레이어가 행동하는 동안 당신은 상황을 지켜보다가, 모든 인원이 행동을 마쳤을 때만 최종 결과를 말합니다.
        """
        self.memories = {}

    def get_memory(self, session_id: str):
        if session_id not in self.memories:
            self.memories[session_id] = ConversationBufferMemory(return_messages=True, memory_key="history")
        return self.memories[session_id]

    async def generate_response(self, session_id: str, player_name: str, message: str) -> str:
        memory = self.get_memory(session_id)
        history = memory.load_memory_variables({})["history"]
        
        messages = [SystemMessage(content=self.system_prompt)] + history + [HumanMessage(content=f"{player_name}: {message}")]
        
        # 1. AI에게 메시지 전달 (도구 호출 여부 결정)
        ai_msg = await self.llm_with_tools.ainvoke(messages)
        
        # 2. 도구 호출 루프 (AI가 주사위를 굴리고 싶어한다면)
        iteration = 0
        while ai_msg.tool_calls and iteration < 5:
            iteration += 1
            print(f">>> AI GM is calling tools: {ai_msg.tool_calls}")
            messages.append(ai_msg)
            for tool_call in ai_msg.tool_calls:
                # 실제로 주사위 굴리기
                tool_result = await roll_dice_tool.ainvoke(tool_call)
                messages.append(ToolMessage(content=str(tool_result), tool_call_id=tool_call["id"]))
            
            # 주사위 결과를 포함하여 다시 AI에게 물어보기
            ai_msg = await self.llm_with_tools.ainvoke(messages)
        
        if iteration == 0:
            print(">>> AI GM decided NOT to roll dice this time.")
        
        # 최종 결과를 문자열로 변환 (Gemini 2.5 등에서 리스트로 오는 경우 대응)
        final_content = ""
        if isinstance(ai_msg.content, str):
            final_content = ai_msg.content
        elif isinstance(ai_msg.content, list):
            for part in ai_msg.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    final_content += part.get("text", "")
                elif isinstance(part, str):
                    final_content += part
        else:
            final_content = str(ai_msg.content)

        # 최종 결과를 메모리에 저장
        memory.save_context({"input": message}, {"output": final_content})
        
        return final_content
