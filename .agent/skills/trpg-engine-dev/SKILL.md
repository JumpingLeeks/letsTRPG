---
name: trpg-engine-dev
description: AI TRPG 엔진의 안정적인 유지보수 및 확장을 위한 스킬입니다. 턴 관리 로직 수정, 스탯 시스템 변경, 또는 langchain-classic 기반의 의존성 문제가 발생했을 때 사용합니다.
---

# TRPG Engine Developer Skill

## Goal
실시간 멀티플레이어 AI TRPG 시스템의 데이터 무결성을 유지하고, 환경 특이적(langchain-classic, venv) 오류 없이 엔진을 확장하는 것을 목표로 합니다.

## Instructions
1. **환경 검증 우선 수행**: 
   - 의존성 관련 작업 시 반드시 `scripts/verify_env.py`를 실행하여 현재 환경의 패키지 상태를 확인하십시오.
   - 명령어: `.\venv\Scripts\python.exe scripts/verify_env.py`
2. **비동기 턴 시스템 수정**:
   - `chat`, `timeout` 등의 로직 수정 시 반드시 `app/main.py`의 `advance_turn_if_needed` 함수를 중앙 집중식 통제점으로 사용하십시오.
   - 상태 변경 후에는 반드시 `manager.broadcast`를 호출하여 실시간 동기화를 보장하십시오.
3. **AI 서비스(AIGM) 관리**:
   - 도구(Tool)를 추가할 때는 반드시 클래스 정의보다 상단에 배치하십시오.
   - `gemini-2.5-flash` 모델의 특성에 맞게 도구 호출 결과가 올바르게 파싱되는지 확인하십시오.
4. **안전한 서버 재시작**:
   - 코드 반영 후 서버 재시작이 필요할 경우, 기존 프로세스를 강제 종료한 후 가상환경 파이썬을 명시적으로 사용하십시오.

## Examples
- **입력**: "새로운 스탯 '운(Luck)'을 추가해줘."
- **출력**: `SessionState` 수정 -> `advance_turn_if_needed`에 운 관련 로직 추가 -> `AIGMService`의 시스템 프롬프트 업데이트 -> `scripts/verify_env.py`로 구동 확인.

## Constraints
- 절대 시스템 전역의 `uvicorn`을 직접 실행하지 마십시오. (반드시 venv 사용)
- `langchain.memory`가 아닌 `langchain_classic.memory` 경로를 고수하십시오.
- AI GM 응답에 확률(%) 숫자가 직접적으로 노출되지 않도록 서사적 묘사를 강제하십시오.
