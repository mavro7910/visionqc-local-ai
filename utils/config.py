# utils/config.py
from dotenv import load_dotenv
import os
from pathlib import Path

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = os.getenv("DB_PATH", str(PROJECT_ROOT / "image_log.db"))

# DB 폴더 자동 생성
_db_dir = os.path.dirname(os.path.abspath(DB_PATH))
if _db_dir and not os.path.exists(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)

DEFAULT_DEFECT_LABELS = (
    "no_defect, dent, scratch, crack, glass shatter, lamp broken, tire flat"
)

# 라벨을 리스트로 사용 가능하도록 변환
DEFECT_LABELS = [lbl.strip() for lbl in DEFAULT_DEFECT_LABELS.split(",") if lbl.strip()]

SEVERITY_UI = ["High", "Medium", "Low"]
SEVERITY_MAP = {
    "High": "A",
    "Medium": "B",
    "Low": "C"
}
SEVERITY_MAP_REVERSE = {v: k for k, v in SEVERITY_MAP.items()}