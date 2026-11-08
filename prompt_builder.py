"""Сборка system/user-дополнений для GigaChat по предмету и классу."""

from __future__ import annotations

import json

from config import TASKS_DB_PATH
from models import TopicSpec


def _load_db() -> dict:
    with TASKS_DB_PATH.open(encoding="utf-8") as f:
        return json.load(f)

# Классы → возрастная группа для подсказок (без отдельного JSON на каждый класс)
_GRADE_BUCKETS: tuple[tuple[range, str], ...] = (
    (range(5, 7), "5-6"),
    (range(7, 10), "7-9"),
    (range(10, 12), "10-11"),
)


def _grade_bucket(grade: int) -> str:
    for gr, label in _GRADE_BUCKETS:
        if grade in gr:
            return label
    return "7-9"


def _hint_layers(subject: str, grade: int) -> list[dict]:
    db = _load_db()
    hints = db.get("prompt_hints", {})
    layers: list[dict] = []
    bucket = _grade_bucket(grade)

    default = hints.get("_default")
    if isinstance(default, dict):
        layers.append({k: v for k, v in default.items() if k != "by_grade"})
        grade_hint = default.get("by_grade", {}).get(bucket)
        if isinstance(grade_hint, dict):
            layers.append(grade_hint)

    subj = hints.get(subject)
    if isinstance(subj, dict):
        layers.append({k: v for k, v in subj.items() if k != "by_grade"})
        grade_hint = subj.get("by_grade", {}).get(bucket)
        if isinstance(grade_hint, dict):
            layers.append(grade_hint)
    return layers


def _merge_lines(layers: list[dict], key: str) -> str:
    chunks: list[str] = []
    for layer in layers:
        val = layer.get(key)
        if isinstance(val, str) and val.strip():
            chunks.append(val.strip())
        elif isinstance(val, list):
            chunks.extend(str(x).strip() for x in val if str(x).strip())
    return "\n".join(chunks)


def content_rules_block(subject: str, grade: int) -> str:
    return _merge_lines(_hint_layers(subject, grade), "content_rules")


def system_append(subject: str, grade: int) -> str:
    return _merge_lines(_hint_layers(subject, grade), "system_append")


def topic_focus_block(topic_specs: list[TopicSpec], task_count: int) -> str:
    """Жёсткая привязка заданий к выбранным темам урока."""
    if not topic_specs:
        return ""
    if len(topic_specs) == 1:
        spec = topic_specs[0]
        return (
            f"ОБЯЗАТЕЛЬНО: все {task_count} заданий в варианте только по теме «{spec.topic}» "
            f"(сложность {spec.difficulty}). "
            "Не добавляй задачи из других разделов курса. "
            f"У каждого задания в JSON: topic = «{spec.topic}», difficulty = «{spec.difficulty}»."
        )
    names = " | ".join(f"«{s.topic}»" for s in topic_specs)
    return (
        f"ОБЯЗАТЕЛЬНО: каждое из {task_count} заданий относится только к одной из тем: {names}. "
        "Запрещены задачи вне этого списка. "
        "Поле topic — точная строка темы из списка выше; difficulty — как у этой темы. "
        "Распредели задания по выбранным темам (по возможности хотя бы одно задание на каждую)."
    )


def difficulty_rules_block(topic_specs: list[TopicSpec], check_focus: str = "") -> str:
    """Жёсткие рамки уровня: «сложный» не должен превращаться в определения."""
    rules = {
        "basic": (
            "Уровень ★ лёгкий: одно прямое действие, определение или подстановка в формулу. "
            "Без многошаговых задач."
        ),
        "medium": (
            "Уровень ★★ средний: запрещены вопросы только на определение "
            "(«что такое», «как называется», «запишите формулу»). "
            "Нужен короткий расчёт или сравнение в два шага."
        ),
        "advanced": (
            "Уровень ★★★ сложный — это требование, не пожелание. "
            "Запрещены определения, «что такое», «как называется», «запишите формулу», "
            "подстановка одного числа в одну формулу и качественный вопрос без расчёта. "
            "Каждое задание — 2–3 шага: обратная задача, два случая, перевод единиц, "
            "скрытое данное или несколько величин. Проверяй умение, а не память термина. "
            "Само условие короткое: text до 180 символов, без хода решения. "
            "В text_latex не больше одной короткой формулы. "
            "Поле difficulty у каждого задания строго advanced."
        ),
    }
    lines = ["Правила сложности (нельзя занижать):"]
    seen: set[str] = set()
    for spec in topic_specs:
        if spec.difficulty in seen:
            continue
        seen.add(spec.difficulty)
        lines.append(rules.get(spec.difficulty, rules["medium"]))
    focus = (check_focus or "").strip()
    if focus and focus != "auto":
        lines.append(
            "Учитель указал, какие задания нужны и какие знания проверить:\n"
            f"«{focus}»\n"
            "Все задания только про это. Не заменяй их более лёгкими или общими вопросами."
        )
    elif "advanced" in seen:
        lines.append(
            "Учитель не сузил тип заданий. Сам выбери одно конкретное умение по теме "
            "и проверь его сложными задачами, а не набором определений."
        )
    return "\n".join(lines)


def user_append(subject: str, grade: int, topic_specs: list[TopicSpec]) -> str:
    base = _merge_lines(_hint_layers(subject, grade), "user_append")
    diff_notes: list[str] = []
    for spec in topic_specs:
        diff_notes.append(f"Тема «{spec.topic}»: сложность {spec.difficulty}.")
    if diff_notes:
        return "\n".join(filter(None, [base, "\n".join(diff_notes)]))
    return base


def llm_few_shot_block(
    subject: str, task_count: int, variant_count: int, max_examples: int = 2
) -> str:
    if task_count * variant_count > 20:
        return ""
    db = _load_db()
    examples = db.get("llm_task_examples", {}).get(subject, [])
    if not examples:
        return ""
    lines = ["Примеры полей задания в JSON (не копируй дословно):"]
    for ex in examples[:max_examples]:
        lines.append(json.dumps(ex, ensure_ascii=False))
    return "\n".join(lines)


def build_system_prompt(
    task_count: int,
    subject: str,
    grade: int,
    *,
    compact: bool = False,
) -> str:
    compact_note = (
        " Условие в text — законченная фраза для ученика, до 220 символов, без обрыва. "
        "text_latex — только формула, если она нужна; не вместо текста."
        if compact
        else ""
    )
    rules = content_rules_block(subject, grade)
    rules_section = f"\n\nПравила содержания:\n{rules}" if rules else ""

    subject_extra = system_append(subject, grade)
    subject_section = f"\n\nПо предмету:\n{subject_extra}" if subject_extra else ""

    return (
        "Ты генератор учебных карточек для школьных проверочных работ («пятиминуток»). "
        "Создавай короткие, точные вопросы с однозначными ответами по заданному предмету и теме. "
        "Отвечай только валидным JSON по схеме, без текста до или после JSON. "
        f"Сложность строго под класс {grade} (программа РФ, ФГОС) и под уровень из запроса. "
        "Нельзя понижать сложный уровень до определения или одного арифметического действия. "
        f"В каждом варианте ровно {task_count} заданий (массив tasks длины {task_count}), "
        "на размышление около 30–40 секунд для лёгкого уровня, около минуты для среднего "
        "и до двух минут для сложного. "
        "Задания уникальные, параметрические, не из ГДЗ."
        f"{rules_section}"
        f"{subject_section}"
        "\n\nТехнические поля JSON:"
        " text — полное условие для ученика на русском: что найти или сделать. "
        "Фраза закончена, её нельзя заменять одной формулой. "
        "text_latex — только формула без русского текста "
        "(дроби \\frac{a}{b}, степени x^{2}, \\sqrt{x}, \\log_{2}(x), \\leq; без обёртки $). "
        "У каждого логарифма обязательно основание: \\log_{2}(x), не log(x) и не фигурные скобки вместо круглых. "
        "Степень с несколькими символами пиши целиком: 3^{x+1}, e^{2x}, не 3^x+1 и не e^2x. "
        "Если формула есть, в text всё равно напиши, что с ней сделать. "
        "eval_expr — одно числовое выражение (+ - * / **, скобки) для математики, физики "
        "и информатики, если ответ числовой; иначе null. "
        "answer — лаконично (слово, число, дробь a/b или до 2 коротких предложений). "
        "Для русского языка eval_expr всегда null. "
        "У каждого задания обязательны topic и difficulty — только из списка тем в запросе пользователя. "
        "variant_id: «1» или «2»."
        f"{compact_note}"
    )
