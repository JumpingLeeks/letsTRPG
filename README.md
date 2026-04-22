# AI TRPG Harness Server

AI 기반 TRPG 게임 마스터(GM) 기능을 제공하는 백엔드 서버입니다.

## 주요 기능
- **AI GM 채팅**: 플레이어의 행동에 반응하고 스토리를 진행합니다.
- **주사위 엔진**: `2d6+4`와 같은 표준 주사위 굴림을 지원합니다.
- **세션 관리**: 세션별 대화 내역을 기억하여 일관된 스토리를 유지합니다.

## 설치 및 실행
1. 필요한 패키지 설치:
   ```bash
   pip install -r requirements.txt
   ```
2. `.env` 파일 설정:
   ```env
   GOOGLE_API_KEY=your_google_api_key_here
   ```
3. 서버 실행:
   ```bash
   python app/main.py
   ```

## API 엔드포인트
- `POST /chat`: AI GM과 대화.
- `POST /roll`: 주사위 굴리기.
