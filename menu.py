"""Тексты и кнопки сценария."""

from config import DIFFICULTY_LABELS, SUBJECT_LABELS, TOPICS_PER_PAGE
from generator import get_topics
from models import TopicSpec

BTN_LABEL_MAX = 22


def btn_label(text: str, max_len: int = BTN_LABEL_MAX) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


HELP_TEXT = (
    "Портал в Будущее — генератор карточек-пятиминуток для учителей.\n\n"
    "Команды:\n"
    "/start — начать заново\n"
    "/help — эта справка\n\n"
    "Можно выбрать тему из каталога, ввести свою, собрать несколько тем "
    "с разной сложностью (★), задать число заданий и вариантов на лист."
)

WELCOME_TEXT = (
    "Портал в Будущее\n\n"
    "Проект хакатона «Идея Фикс» (Сферум, VK, MAX).\n\n"
    "Бот собирает школьную пятиминутку.\n\n"
    "Необходимо выбрать: предмет, класс и тема, "
    "затем лист заданий для печати и ответы для учителя.\n\n"
    "Нажмите «Создать карточку», чтобы начать."
)

def stars_to_difficulty(stars: int) -> str:
    return {1: "basic", 2: "medium", 3: "advanced"}.get(stars, "basic")


def rows_welcome() -> list[list[tuple[str, str]]]:
    return [[("Создать карточку", "flow:begin")]]


def rows_subjects() -> list[list[tuple[str, str]]]:
    return [
        [(btn_label(label), f"sub:{key}")] for key, label in SUBJECT_LABELS.items()
    ] + [[("« Назад", "back:welcome")]]


# Предмет учителя сохраняется между карточками (чат → предмет).
_teacher_subject: dict[int, str] = {}


def remember_subject(chat_id: int, subject: str) -> None:
    _teacher_subject[chat_id] = subject


def remembered_subject(chat_id: int) -> str | None:
    return _teacher_subject.get(chat_id)


def grade_prompt(subject: str) -> str:
    label = SUBJECT_LABELS.get(subject, subject)
    return f"Предмет: {label}\n\nВыберите класс:"


def rows_grades() -> list[list[tuple[str, str]]]:
    flat = [(f"{g} кл.", f"grade:{g}") for g in range(5, 12)]
    rows = [flat[i : i + 4] for i in range(0, len(flat), 4)]
    rows.append([("« К предметам", "back:subject")])
    return rows


def rows_topic_mode() -> list[list[tuple[str, str]]]:
    return [
        [("Из каталога", "tmode:list")],
        [("Своя тема", "tmode:custom")],
        [("Несколько тем", "tmode:multi")],
        [("« Предмет", "back:subject")],
    ]


def rows_topics(
    subject: str,
    grade: int,
    page: int,
    *,
    multi: bool = False,
) -> tuple[str, list[list[tuple[str, str]]]]:
    topics = get_topics(subject, grade) or ["Обобщение по курсу"]
    start = page * TOPICS_PER_PAGE
    chunk = topics[start : start + TOPICS_PER_PAGE]
    rows: list[list[tuple[str, str]]] = []
    prefix = "tpcmulti" if multi else "topic"
    for i, topic in enumerate(chunk):
        idx = start + i
        label = btn_label(topic, max_len=28)
        rows.append([(label, f"{prefix}:{idx}")])
    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("◀ Назад", f"tpage:{page - 1}"))
    if start + TOPICS_PER_PAGE < len(topics):
        nav.append(("Вперёд ▶", f"tpage:{page + 1}"))
    if nav:
        rows.append(nav)
    custom_label = "✏ Своя в набор" if multi else "✏ Своя тема"
    rows.append([(custom_label, "topic:custom")])
    rows.append([("« Назад", "back:tmode")])
    return f"Тема урока (стр. {page + 1}):", rows


def resolve_topic(subject: str, grade: int, idx: int) -> str:
    topics = get_topics(subject, grade) or ["Обобщение по курсу"]
    return topics[idx] if idx < len(topics) else topics[0]


def rows_stars() -> list[list[tuple[str, str]]]:
    return [
        [("★ Лёгкий", "stars:1")],
        [("★★ Средний", "stars:2")],
        [("★★★ Сложный", "stars:3")],
        [("« Назад", "back:topic")],
    ]


def rows_multi_continue() -> list[list[tuple[str, str]]]:
    return [
        [("➕ Ещё тема", "multi:add")],
        [("✅ Готово", "multi:done")],
        [("« Назад", "back:topic")],
    ]


def rows_task_count(data: dict | None = None) -> list[list[tuple[str, str]]]:
    back = "multi" if data and data.get("tmode") == "multi" else "stars"
    return [
        [("2", "tc:2"), ("3", "tc:3"), ("4", "tc:4"), ("5", "tc:5")],
        [("6", "tc:6"), ("7", "tc:7"), ("8", "tc:8")],
        [("« Назад", f"back:{back}")],
    ]


def rows_back_tmode() -> list[list[tuple[str, str]]]:
    return [[("« Назад", "back:tmode")]]


def rows_variants() -> list[list[tuple[str, str]]]:
    return [
        [("Создать", "var:2")],
        [("« Назад", "back:tc")],
    ]


def rows_new_card() -> list[list[tuple[str, str]]]:
    return [[("📝 Новая карточка", "flow:new")]]


def resolve_back_navigation(
    target: str, data: dict
) -> tuple[str, list[list[tuple[str, str]]], str] | None:
    """
    Возврат на предыдущий шаг: (текст, клавиатура, имя состояния FSM).
    state: welcome | subject | grade | topic_mode | topic | stars | task_count | variants
    """
    if target == "focus":
        target = "multi" if data.get("tmode") == "multi" else "stars"
    subject = data.get("subject")
    grade = data.get("grade")
    if target == "welcome":
        return WELCOME_TEXT, rows_welcome(), "welcome"
    if target == "subject":
        return "Выберите предмет:", rows_subjects(), "subject"
    if target == "grade":
        return "Выберите класс:", rows_grades(), "grade"
    if target == "tmode":
        return "Как задать тему?", rows_topic_mode(), "topic_mode"
    if target == "topic":
        if data.get("tmode") == "custom":
            return "Как задать тему?", rows_topic_mode(), "topic_mode"
        if subject is None or grade is None:
            return None
        page = data.get("topic_page", 0)
        multi = data.get("tmode") == "multi"
        text, rows = rows_topics(subject, grade, page, multi=multi)
        return text, rows, "topic"
    if target == "stars":
        topic = data.get("pending_topic") or data.get("topic", "Тема")
        return (
            f"Тема: {topic}\n\nВыберите сложность:",
            rows_stars(),
            "stars",
        )
    if target == "multi":
        blocks = data.get("topic_blocks") or []
        if not blocks:
            return None
        summary = "\n".join(f"• {b['topic']}" for b in blocks)
        return (
            f"Темы на карточке:\n{summary}\n\nДобавить ещё или продолжить?",
            rows_multi_continue(),
            "topic",
        )
    if target == "tc":
        return (
            "Сколько заданий в одном варианте?",
            rows_task_count(data),
            "task_count",
        )
    if target == "var":
        return (
            card_ready_text(data),
            rows_variants(),
            "variants",
        )
    return None


def topic_specs_from_data(data: dict) -> list[TopicSpec]:
    blocks = [b for b in (data.get("topic_blocks") or []) if b.get("topic")]
    if blocks:
        return [TopicSpec(topic=b["topic"], difficulty=b["difficulty"]) for b in blocks]
    topic = data.get("topic") or data.get("pending_topic")
    if not topic:
        raise KeyError("topic")
    return [
        TopicSpec(
            topic=topic,
            difficulty=data.get("difficulty", "basic"),
        )
    ]


def card_data_problem(data: dict) -> str | None:
    if not data.get("subject") or not data.get("grade"):
        return "Сессия сбросилась. Нажмите /start и выберите предмет и класс заново."
    blocks = [b for b in (data.get("topic_blocks") or []) if b.get("topic")]
    if not blocks and not data.get("topic") and not data.get("pending_topic"):
        return "Тема не выбрана. Нажмите /start и заново выберите тему, затем сложность."
    return None


def card_ready_text(data: dict) -> str:
    subj = SUBJECT_LABELS.get(data.get("subject"), data.get("subject") or "—")
    lines = [
        "Карточка:",
        f"Предмет: {subj}",
        f"Класс: {data.get('grade', '—')}",
        "",
        "Темы:",
    ]
    try:
        specs = topic_specs_from_data(data)
    except KeyError:
        specs = []
    stars = {"basic": "★", "medium": "★★", "advanced": "★★★"}
    if specs:
        for spec in specs:
            diff = DIFFICULTY_LABELS.get(spec.difficulty, spec.difficulty)
            lines.append(f"  • {spec.topic} — {diff} {stars.get(spec.difficulty, '')}")
    else:
        lines.append("  • не выбрана")
    lines.extend(
        [
            "",
            f"Заданий в варианте: {data.get('task_count', 3)}",
            "На листе: 2 варианта",
        ]
    )
    return "\n".join(lines)
