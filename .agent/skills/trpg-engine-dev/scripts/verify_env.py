import sys
import os

def verify():
    print("--- TRPG Engine Environment Verification ---")
    
    # 1. Virtual Environment Check
    if not hasattr(sys, 'real_prefix') and not (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
        print("[WARNING] Not running inside a virtual environment!")
    else:
        print("[OK] Running inside Venv.")

    # 2. Key Package Check
    try:
        from langchain_classic.memory import ConversationBufferMemory
        print("[OK] langchain_classic.memory found.")
    except ImportError:
        print("[ERROR] langchain_classic not found. Check your installation.")
        sys.exit(1)

    try:
        import fastapi
        import uvicorn
        print(f"[OK] FastAPI {fastapi.__version__}, Uvicorn {uvicorn.__version__} ready.")
    except ImportError as e:
        print(f"[ERROR] Missing dependency: {e}")
        sys.exit(1)

    print("--- Verification Successful ---")

if __name__ == "__main__":
    verify()
