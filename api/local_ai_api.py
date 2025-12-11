# api/local_ai_api.py

from pathlib import Path

import torch
from torch import nn
import torchvision.models as models
import torchvision.transforms as T
from PIL import Image

from utils.config import DEFAULT_DEFECT_LABELS  # 문자열, 예: "dent, scratch, ..."
# SEVERITY_MAP/REVERSE는 A/B/C <-> High/Medium/Low 매핑용 (이미 GUI에서 사용) :contentReference[oaicite:3]{index=3}

# -----------------------------
# 경로 / 디바이스 설정
# -----------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = PROJECT_ROOT / "models"
MODEL_PATH = MODELS_DIR / "visionqc_multitask_resnet34_best.pth"

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# .env / config 에서 관리하는 defect label (DB/GUI 용)
CONFIG_DEFECT_LABELS = [
    lbl.strip() for lbl in DEFAULT_DEFECT_LABELS.split(",") if lbl.strip()
]

# 학습한 CarDD 내부 defect 라벨 (모델 출력 순서와 반드시 같아야 함)
INTERNAL_DEFECT_LABELS = [
    "dent",
    "scratch",
    "crack",
    "glass shatter",
    "lamp broken",
    "tire flat",
]

SEVERITY_LABELS = ["minor", "moderate", "severe"]        # 모델 출력
LOCATION_LABELS = ["front", "rear", "side"]              # 모델 출력

# no_defect 임계값 (softmax confidence 기준)
NO_DEFECT_THRESHOLD = 0.25  # 필요하면 나중에 튜닝


# -----------------------------
# 내부 라벨 -> config 라벨 매핑 (이름 정리)
# -----------------------------

def _norm(s: str) -> str:
    return s.lower().replace(" ", "_").strip()

CONFIG_DEFECT_MAP = {
    _norm(lbl): lbl for lbl in CONFIG_DEFECT_LABELS
}


def map_internal_defect_to_config(name: str) -> str:
    """
    모델 내부 라벨(INTERNAL_DEFECT_LABELS)을
    config에서 정의한 DEFECT_LABELS 중 하나로 매핑.
    못 찾으면 원본 name을 그대로 사용.
    """
    key = _norm(name)
    return CONFIG_DEFECT_MAP.get(key, name)


# -----------------------------
# 모델 정의
# -----------------------------

class VisionQCMultiTaskResNet34(nn.Module):
    def __init__(self,
                 num_defect: int,
                 num_severity: int,
                 num_location: int,
                 pretrained: bool = False):
        super().__init__()
        # 학습할 때와 동일한 구조
        backbone = models.resnet34(
            weights=models.ResNet34_Weights.IMAGENET1K_V1 if pretrained else None
        )
        in_features = backbone.fc.in_features
        backbone.fc = nn.Identity()
        self.backbone = backbone

        self.fc_defect   = nn.Linear(in_features, num_defect)
        self.fc_severity = nn.Linear(in_features, num_severity)
        self.fc_location = nn.Linear(in_features, num_location)

    def forward(self, x):
        feat = self.backbone(x)
        logits_defect   = self.fc_defect(feat)
        logits_severity = self.fc_severity(feat)
        logits_location = self.fc_location(feat)

        return {
            "defect_type": logits_defect,
            "severity": logits_severity,
            "location": logits_location,
        }


# -----------------------------
# 전처리 / 모델 lazy 로딩
# -----------------------------

_transform = T.Compose([
    T.Resize((224, 224)),
    T.ToTensor(),
    T.Normalize(
        mean=[0.485, 0.456, 0.406],
        std=[0.229, 0.224, 0.225],
    ),
])

_model: VisionQCMultiTaskResNet34 | None = None


def _load_model_once() -> VisionQCMultiTaskResNet34:
    global _model
    if _model is not None:
        return _model

    model = VisionQCMultiTaskResNet34(
        num_defect=len(INTERNAL_DEFECT_LABELS),
        num_severity=len(SEVERITY_LABELS),
        num_location=len(LOCATION_LABELS),
        pretrained=False,   # pth에서 weight 로드할 거라 False
    ).to(DEVICE)

    state_dict = torch.load(MODEL_PATH, map_location=DEVICE)
    model.load_state_dict(state_dict)
    model.eval()

    _model = model
    return _model


# -----------------------------
# 후처리 유틸
# -----------------------------

def _severity_to_abc(sev: str) -> str:
    """
    모델의 minor/moderate/severe -> 기존 UI에서 쓰는 A/B/C로 변환
    (config의 SEVERITY_MAP/REVERSE와 궁합 맞추기 위해)
    A=치명적, B=중대, C=경미 :contentReference[oaicite:4]{index=4}
    """
    sev = sev.lower()
    if sev == "severe":
        return "A"
    if sev == "moderate":
        return "B"
    return "C"  # minor, 그 외


def _decide_action(label: str, severity_abc: str) -> str:
    """
    단순 룰 기반 action 결정 (원하면 나중에 세분화)
    - no_defect: Pass
    - A: Scrap/Reject 수준일 수 있지만 여기선 일단 'Reject'로 둠
    - B: Rework
    - C: Hold (경미)
    """
    if label == "no_defect":
        return "Pass"

    if severity_abc == "A":
        return "Reject"
    if severity_abc == "B":
        return "Rework"
    # C
    return "Hold"


def _build_description(label: str, severity_abc: str, location: str) -> str:
    if label == "no_defect":
        return "눈에 띄는 결함이 감지되지 않았습니다."
    # UI에서는 A/B/C를 High/Medium/Low로 바꿔서 보여줄 것 (SEVERITY_MAP_REVERSE 사용) :contentReference[oaicite:5]{index=5}
    korean_location = {"front" : "전면부", "rear" : "후면부", "side" : "측면부"}
    korean_severity = {"C" : "경미한", "B" : "보통 수준의", "A" : "심각한"}
    korean_label = {
    "dent" : "찌그러짐",
    "scratch" : "긁힘",
    "crack" : "균열",
    "glass shatter" : "유리 깨짐",
    "lamp broken" : "램프 고장",
    "tire flat" : "타이어 터짐",
    }
    
    return f"위치: {korean_location[location]}, 등급: {korean_severity[severity_abc]} 수준의 {korean_label[label]} 결함이 감지되었습니다."


# -----------------------------
# Public API (OpenAI 대체)
# -----------------------------

def classify_image(image_path: str) -> dict:
    """
    기존 api/openai_api.py의 classify_image와 같은 형식으로 반환:

    {
        "label": "dent" or "no_defect",
        "confidence": 0.92,
        "description": "...",
        "severity": "A|B|C",
        "location": "front|rear|side",
        "action": "Pass|Rework|Scrap|Hold|Reject"
    }
    """
    model = _load_model_once()

    img = Image.open(image_path).convert("RGB")
    x = _transform(img).unsqueeze(0).to(DEVICE)

    with torch.no_grad():
        out = model(x)

        # defect
        logits_def = out["defect_type"]  # [1, 6]
        probs_def = torch.softmax(logits_def, dim=1)
        conf, idx = probs_def.max(dim=1)
        conf = float(conf.item())
        idx = int(idx.item())
        internal_defect = INTERNAL_DEFECT_LABELS[idx]

        # severity
        logits_sev = out["severity"]
        probs_sev = torch.softmax(logits_sev, dim=1)
        idx_sev = int(probs_sev.argmax(dim=1).item())
        sev_label_internal = SEVERITY_LABELS[idx_sev]  # minor/moderate/severe
        sev_abc = _severity_to_abc(sev_label_internal)

        # location
        logits_loc = out["location"]
        probs_loc = torch.softmax(logits_loc, dim=1)
        idx_loc = int(probs_loc.argmax(dim=1).item())
        loc_label = LOCATION_LABELS[idx_loc]

    # no_defect 처리 (conf가 너무 낮으면 none으로 처리)
    if conf < NO_DEFECT_THRESHOLD:
        final_label = "no_defect"
    else:
        # CarDD 내부 라벨 -> config 기반 라벨로 매핑
        final_label = map_internal_defect_to_config(internal_defect)

    action = _decide_action(final_label, sev_abc)
    description = _build_description(final_label, sev_abc, loc_label)

    return {
        "label": final_label,
        "confidence": conf,
        "description": description,
        "severity": sev_abc,   # A/B/C
        "location": loc_label, # front/rear/side
        "action": action,
    }
