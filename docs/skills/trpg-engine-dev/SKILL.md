# TRPG Engine Development Guide (SKILL)

이 가이드는 실시간 멀티플레이어 AI TRPG 시스템의 안정적인 개발과 확장을 위한 표준 절차를 정의합니다.

## 1. 환경 분석 및 임포트 규칙 (Environment-Aware Import)
새로운 패키지나 모듈을 도입하기 전 반드시 다음 단계를 수행하십시오.

1. **Venv 검증**: `.\venv\Scripts\pip.exe list`를 실행하여 설치된 패키지 명칭과 버전을 확인합니다.
2. **패키지 매핑**:
   - `langchain-classic`이 설치된 경우: `from langchain_classic.memory import ...` 사용
   - `langchain` 0.3+ 인 경우: `from langchain_core` 및 `langchain_community` 위주로 구성
3. **임포트 테스트**: 실제 코드 적용 전 `.\venv\Scripts\python.exe -c "import [module]"`로 검증을 완료하십시오.

## 2. AI GM 서비스 설계 패턴
AI 서비스 리팩토링 시 다음 구조를 엄격히 준수하십시오.

- **정의 순서**: `@tool` 데코레이터가 붙은 모든 함수를 `AIGMService` 클래스보다 **반드시 상단**에 정의하여 참조 오류를 방지합니다.
- **State 연동**: `generate_response`는 `session_id` 대신 `SessionState` 객체를 직접 인자로 받아 AI가 도구 호출을 통해 상태를 즉시 수정할 수 있게 합니다.
- **클램핑(Clamping)**: AI가 조작하는 모든 수치(HP/AP 등)는 서버 레벨에서 허용 범위(예: ±2)로 강제 제한하십시오.

## 3. 비동기 턴 및 시스템 관리
실시간 통신의 안정성을 위해 다음 패턴을 사용하십시오.

- **턴 관리의 중앙화**: 턴 전환(자동 스킵, 사망 처리) 로직은 `advance_turn_if_needed`와 같은 단일 함수로 통합하여 `chat`, `timeout`, `resolve` 등 모든 경로에서 동일하게 작동하게 합니다.
- **Single Source of Truth**: `check_timeout`과 같은 검사 함수는 상태값만 변경하고, 실제 전파(Broadcast)는 호출부에서 `await`와 함께 수행하십시오.
- **서버 재시작 프로세스**:
  1. `taskkill /F /IM python.exe /T` (기존 프로세스 완전 종료)
  2. `.\venv\Scripts\python.exe -m uvicorn ...` (가상환경 인터프리터 명시적 사용)

## 4. UI/UX 정합성
- **확률 은폐**: AI 응답이나 UI에 숫자로 된 성공 확률(%)이 노출되지 않도록 서버와 클라이언트 양쪽에서 필터링을 유지하십시오.
- **상태 동기화**: 모든 중요한 상태 변화 후에는 `manager.broadcast`를 호출하여 플레이어 카드와 상태 바가 즉시 갱신되게 하십시오.
