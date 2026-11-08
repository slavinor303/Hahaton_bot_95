import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
TASKS_DB_PATH = BASE_DIR / "tasks_db.json"
FONTS_DIR = BASE_DIR / "assets" / "fonts"
DEJAVU_FONT_PATH = FONTS_DIR / "DejaVuSans.ttf"

MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_VERIFY_SSL = os.getenv("GIGACHAT_VERIFY_SSL", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Имя из get_models() API; «GigaChat» без версии больше не поддерживается
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2")

GENERATION_TIMEOUT_SEC = int(os.getenv("GENERATION_TIMEOUT_SEC", "45"))
GENERATION_MAX_RETRIES = int(os.getenv("GENERATION_MAX_RETRIES", "2"))

DIFFICULTY_STARS: dict[str, int] = {
    "basic": 1,
    "medium": 2,
    "advanced": 3,
}


def stars_for_difficulty(difficulty: str) -> str:
    return "★" * DIFFICULTY_STARS.get(difficulty, 1)


def difficulty_mark(difficulty: str) -> str:
    labels = {
        "basic": "Сложн.: легкая",
        "medium": "Сложн.: средняя",
        "advanced": "Сложн.: высокая",
    }
    return labels.get(difficulty, "Сложн.: легкая")


def grade_scale(task_count: int) -> dict[int, str]:
    """5 — от 85% заданий, 4 — от 65%, 3 — от 45%, иначе 2."""
    n = max(1, task_count)

    def cut(num: int, den: int) -> int:
        return min(n, max(1, (n * num + den // 2) // den))

    five = cut(17, 20)
    four = min(five - 1, cut(13, 20)) if five > 1 else 0
    three = min(four - 1, cut(9, 20)) if four > 1 else 0
    scale: dict[int, str] = {}
    for points in range(n + 1):
        if points >= five:
            scale[points] = "5"
        elif four and points >= four:
            scale[points] = "4"
        elif three and points >= three:
            scale[points] = "3"
        else:
            scale[points] = "2"
    return scale

SUBJECT_LABELS = {
    "math": "Математика",
    "physics": "Физика",
    "russian": "Русский язык",
    "informatics": "Информатика",
}

DIFFICULTY_LABELS = {
    "basic": "Базовый",
    "medium": "Средний",
    "advanced": "Повышенный",
}

TOPICS_PER_PAGE = 6
MIN_TASKS_PER_VARIANT = 2
MAX_TASKS_PER_VARIANT = 8
