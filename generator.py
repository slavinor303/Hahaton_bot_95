import ast
import json
import logging
import operator
import random
import re
import threading
from fractions import Fraction
from typing import Any

from gigachat import GigaChat
from gigachat.exceptions import LengthFinishReasonError, NotFoundError
from gigachat.models.chat import Chat, Messages, MessagesRole
from sympy import N, sympify
from config import (
    GIGACHAT_CREDENTIALS,
    GIGACHAT_MODEL,
    GIGACHAT_SCOPE,
    GIGACHAT_VERIFY_SSL,
    GENERATION_MAX_RETRIES,
    SUBJECT_LABELS,
    TASKS_DB_PATH,
    DIFFICULTY_LABELS,
)
from models import CardSet, TaskItem, TopicSpec, Variant
from prompt_builder import (
    build_system_prompt,
    difficulty_rules_block,
    llm_few_shot_block,
    topic_focus_block,
    user_append as prompt_user_append,
)

logger = logging.getLogger(__name__)

# У аккаунта GigaChat один одновременный запрос: остальные ждут этот замок.
_GIGACHAT_LOCK = threading.Lock()
_queue_state = threading.Lock()
_queued_jobs = 0

_resolved_model: str | None = None
_length_blocked: set[str] = set()

_MODEL_CANDIDATES = (
    "GigaChat-2-Pro",
    "GigaChat-2",
    "GigaChat-3-Pro",
    "GigaChat-3-Lightning",
    "GigaChat-2-Max",
)

_SAFE_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def load_tasks_db() -> dict:
    with TASKS_DB_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def get_topics(subject: str, grade: int) -> list[str]:
    db = load_tasks_db()
    return db.get("topics", {}).get(subject, {}).get(str(grade), [])


def _sanitize_expr(expr: str) -> str:
    return expr.strip().replace(",", ".").replace("^", "**")


def _expr_too_heavy(expr: str) -> bool:
    """Длинное или многостепенное выражение sympy может считать очень долго."""
    return len(expr) > 120 or expr.count("**") > 3


def _needs_sympy(expr: str) -> bool:
    """Обычную арифметику считаем через AST — sympy на Windows может зависнуть на GIL."""
    return bool(re.search(r"[A-Za-z]", expr))


def _as_fraction(value: float | int | Fraction) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(value).limit_denominator(10_000)


def _format_number_or_fraction(value: float | Fraction) -> str:
    if isinstance(value, Fraction):
        if value.denominator == 1:
            return str(value.numerator)
        if abs(value.denominator) <= 1000:
            return f"{value.numerator}/{value.denominator}"
        return _normalize_number(float(value))
    return _normalize_number(float(value))


def _safe_eval_expr(expr: str) -> float | None:
    expr = _sanitize_expr(expr)
    if not expr or _expr_too_heavy(expr):
        return None
    try:
        tree = ast.parse(expr, mode="eval")
        return float(_eval_ast(tree.body))
    except Exception:
        pass
    if not _needs_sympy(expr):
        return None
    try:
        return float(N(sympify(expr)))
    except Exception:
        return None


def _format_sympy_value(val) -> str:
    if val.is_Integer or (val.is_Rational and val.q == 1):
        return str(int(val))
    if val.is_Rational:
        p, q = int(val.p), int(val.q)
        if q != 0 and q <= 1000:
            return f"{p}/{q}"
    f = float(N(val))
    return _normalize_number(f)


def _answer_from_expr(expr: str) -> str | None:
    """Канонический ответ по eval_expr (дроби, целые, десятичные)."""
    expr = _sanitize_expr(expr)
    if not expr or _expr_too_heavy(expr):
        return None
    try:
        tree = ast.parse(expr, mode="eval")
        return _format_number_or_fraction(_eval_ast(tree.body))
    except Exception:
        pass
    if not _needs_sympy(expr):
        return None
    try:
        return _format_sympy_value(sympify(expr))
    except Exception:
        return None


def _answers_match(computed: str, expected: str) -> bool:
    c = computed.strip().lower().replace(",", ".")
    e = expected.strip().lower().replace(",", ".")
    if c == e:
        return True
    try:
        return abs(float(Fraction(c)) - float(Fraction(e))) < 1e-6
    except Exception:
        pass
    try:
        return abs(float(c) - float(e)) < 1e-2
    except ValueError:
        return False


def _eval_ast(node: ast.AST) -> float | Fraction:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, int):
            return Fraction(node.value)
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
        if isinstance(node.op, ast.Pow):
            exp = _eval_ast(node.right)
            if abs(float(exp)) > 12:
                raise ValueError("Слишком большая степень")
        left = _eval_ast(node.left)
        right = _eval_ast(node.right)
        if isinstance(node.op, ast.Div):
            return _as_fraction(left) / _as_fraction(right)
        if isinstance(left, Fraction) or isinstance(right, Fraction):
            left_f, right_f = _as_fraction(left), _as_fraction(right)
            if isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
                return _SAFE_OPS[type(node.op)](left_f, right_f)
        return _SAFE_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_eval_ast(node.operand))
    raise ValueError("Недопустимое выражение")


def _normalize_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    rounded = round(value, 4)
    text = f"{rounded:.4f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def repair_card_set(card_set: CardSet) -> CardSet:
    """Подставляет answer из eval_expr; нормализует формулы для PDF."""
    from math_render import enrich_task_math

    new_variants: list[Variant] = []
    for variant in card_set.variants:
        new_tasks: list[TaskItem] = []
        for task in variant.tasks:
            task = enrich_task_math(task)
            if not task.eval_expr:
                new_tasks.append(task)
                continue
            # У уравнения ответ вида x=… нельзя заменять числом из eval_expr.
            answer = task.answer.strip()
            if re.search(r"[A-Za-zА-Яа-я=]", answer) or "уравнен" in task.text.lower():
                new_tasks.append(task)
                continue
            canonical = _answer_from_expr(task.eval_expr)
            if canonical is not None:
                new_tasks.append(task.model_copy(update={"answer": canonical}))
            else:
                new_tasks.append(task.model_copy(update={"eval_expr": None}))
        new_variants.append(variant.model_copy(update={"tasks": new_tasks}))
    return card_set.model_copy(update={"variants": new_variants})


def normalize_variant_ids(card_set: CardSet) -> CardSet:
    """Варианты 1 и 2 вместо А, Б."""
    letter_map = {"А": "1", "Б": "2", "A": "1", "B": "2"}
    new_variants: list[Variant] = []
    for i, variant in enumerate(card_set.variants):
        vid = variant.variant_id.strip()
        if vid in letter_map:
            vid = letter_map[vid]
        elif not vid.isdigit():
            vid = str(i + 1)
        new_variants.append(variant.model_copy(update={"variant_id": vid}))
    return card_set.model_copy(update={"variants": new_variants})


def _allowed_topic_specs(card_set: CardSet) -> list[TopicSpec]:
    if card_set.topics:
        return card_set.topics
    return [TopicSpec(topic=card_set.topic, difficulty=card_set.difficulty)]


def _task_topic_ok(task_topic: str, spec: TopicSpec) -> bool:
    a = task_topic.strip().lower()
    b = spec.topic.strip().lower()
    if not a:
        return False
    return a == b or a in b or b in a


def align_task_topics(card_set: CardSet) -> CardSet:
    """Подставляет topic/difficulty, если модель не заполнила или ошиблась в названии."""
    specs = _allowed_topic_specs(card_set)
    if not specs:
        return card_set
    new_variants: list[Variant] = []
    for variant in card_set.variants:
        tasks = []
        for index, task in enumerate(variant.tasks):
            updates: dict = {}
            matched = next((s for s in specs if task.topic and _task_topic_ok(task.topic, s)), None)
            if matched is None:
                # Несколько тем: раскладываем по кругу, иначе валидация шлёт всю генерацию на повтор.
                matched = specs[index % len(specs)]
                updates["topic"] = matched.topic
            if not task.difficulty:
                updates["difficulty"] = matched.difficulty
            tasks.append(task.model_copy(update=updates) if updates else task)
        new_variants.append(variant.model_copy(update={"tasks": tasks}))
    return card_set.model_copy(update={"variants": new_variants})


def validate_card_set(card_set: CardSet, expected_task_count: int | None = None) -> list[str]:
    errors: list[str] = []
    allowed = _allowed_topic_specs(card_set)
    allowed_names = [s.topic for s in allowed]
    for variant in card_set.variants:
        if expected_task_count is not None and len(variant.tasks) != expected_task_count:
            errors.append(
                f"Вариант {variant.variant_id}: {len(variant.tasks)} заданий "
                f"вместо {expected_task_count}"
            )
        for task in variant.tasks:
            if not task.text.strip():
                errors.append(f"Вариант {variant.variant_id}, задание {task.number}: пустой текст")
            if len(task.text) > 500:
                errors.append(
                    f"Вариант {variant.variant_id}, задание {task.number}: слишком длинный текст"
                )
            if not task.answer.strip():
                errors.append(
                    f"Вариант {variant.variant_id}, задание {task.number}: пустой answer"
                )
            if len(allowed) > 1 and not task.topic:
                errors.append(
                    f"Вариант {variant.variant_id}, задание {task.number}: не указана topic"
                )
            elif task.topic and not any(_task_topic_ok(task.topic, s) for s in allowed):
                errors.append(
                    f"Вариант {variant.variant_id}, задание {task.number}: "
                    f"тема «{task.topic}» не из списка ({', '.join(allowed_names)})"
                )
    return errors


def _few_shot_block(subject: str, task_count: int, variant_count: int) -> str:
    block = llm_few_shot_block(subject, task_count, variant_count)
    if block:
        return block
    if task_count * variant_count > 20:
        return ""
    db = load_tasks_db()
    examples = db.get("few_shot_examples", {}).get(subject, [])
    if not examples:
        return ""
    lines = ["Примеры формата заданий (не копируй дословно):"]
    for ex in examples[:2]:
        lines.append(json.dumps(ex, ensure_ascii=False))
    return "\n".join(lines)


def _should_split_generation(variant_count: int, task_count: int) -> bool:
    return task_count >= 8 or variant_count * task_count >= 16


def _chat_max_tokens(task_count: int, variants_in_response: int) -> int:
    estimate = 900 + variants_in_response * task_count * 550
    return min(max(estimate, 1536), 4096)


def _task_chunks(task_count: int, *, advanced: bool = False) -> list[int]:
    """Длинный JSON GigaChat обрезает. Части по 4 задания — меньше запросов, чем по 2."""
    if advanced:
        chunks: list[int] = []
        left = task_count
        while left > 0:
            if left == 1 and chunks:
                chunks[-1] += 1
                break
            take = 3 if left == 3 else min(4, left)
            chunks.append(take)
            left -= take
        return chunks
    if task_count <= 4:
        return [task_count]
    first = task_count // 2
    return [first, task_count - first]


def _hardest_difficulty(specs: list[TopicSpec]) -> str:
    order = {"basic": 1, "medium": 2, "advanced": 3}
    return max(specs, key=lambda s: order.get(s.difficulty, 1)).difficulty


def _topics_summary(specs: list[TopicSpec]) -> str:
    return "; ".join(s.topic for s in specs)


def _build_user_prompt(
    subject: str,
    grade: int,
    topic_specs: list[TopicSpec],
    variant_count: int,
    task_count: int,
    seed: str,
    fix_errors: list[str] | None = None,
    only_variant_id: str | None = None,
    check_focus: str = "",
    avoid_texts: list[str] | None = None,
) -> str:
    subject_label = SUBJECT_LABELS.get(subject, subject)
    variant_ids = [str(i + 1) for i in range(variant_count)]
    topic_lines = []
    for i, spec in enumerate(topic_specs, 1):
        diff_label = DIFFICULTY_LABELS.get(spec.difficulty, spec.difficulty)
        topic_lines.append(f"  {i}. «{spec.topic}» — сложность: {diff_label}")
    parts = [
        f"Предмет: {subject_label}",
        f"Класс: {grade}",
        "Темы на карточке:",
        *topic_lines,
        f"Заданий в каждом варианте: {task_count}",
        topic_focus_block(topic_specs, task_count),
        difficulty_rules_block(topic_specs, check_focus),
        f"Уникальный seed (используй для разных чисел): {seed}",
        prompt_user_append(subject, grade, topic_specs),
        _few_shot_block(subject, task_count, variant_count),
    ]
    if only_variant_id:
        parts.extend(
            [
                f"Сгенерируй только один вариант: variant_id = «{only_variant_id}».",
                f"В ответе — только этот вариант с {task_count} заданиями.",
            ]
        )
    else:
        parts.extend(
            [
                f"Количество вариантов: {variant_count}",
                f"Идентификаторы вариантов: {', '.join(variant_ids)}",
            ]
        )
    if fix_errors:
        parts.append("Исправь предыдущий ответ. Ошибки валидации:")
        parts.extend(f"- {e}" for e in fix_errors)
    if avoid_texts:
        parts.append(
            "Эти задания уже есть в варианте. Не повторяй их и не делай то же уравнение с другими числами:"
        )
        parts.extend(f"- {text[:180]}" for text in avoid_texts if text.strip())
    return "\n".join(p for p in parts if p)


def _gigachat_client(model: str | None = None) -> GigaChat:
    return GigaChat(
        credentials=GIGACHAT_CREDENTIALS,
        scope=GIGACHAT_SCOPE,
        model=model or GIGACHAT_MODEL,
        verify_ssl_certs=GIGACHAT_VERIFY_SSL,
    )


def _available_chat_models(client: GigaChat) -> set[str]:
    try:
        resp = client.get_models()
        return {
            m.id_
            for m in resp.data
            if m.id_ and not m.id_.lower().startswith("embedding")
        }
    except Exception as exc:
        logger.warning("Не удалось получить список моделей GigaChat: %s", exc)
        return set()


def _resolve_model(client: GigaChat) -> str:
    global _resolved_model
    if _resolved_model:
        return _resolved_model

    available = _available_chat_models(client)
    ordered: list[str] = []
    if GIGACHAT_MODEL.strip():
        ordered.append(GIGACHAT_MODEL.strip())
    for name in _MODEL_CANDIDATES:
        if name not in ordered:
            ordered.append(name)

    for name in ordered:
        if available and name not in available:
            continue
        _resolved_model = name
        logger.info("GigaChat: модель %s", name)
        return name

    if available:
        _resolved_model = sorted(available)[0]
        logger.info("GigaChat: модель %s (первая из API)", _resolved_model)
        return _resolved_model

    raise RuntimeError("Не найдена ни одна chat-модель GigaChat для вашего ключа")


def _call_gigachat(
    user_prompt: str,
    task_count: int,
    subject: str,
    grade: int,
    *,
    response_format: type = CardSet,
    variants_in_response: int = 1,
    compact: bool = False,
) -> CardSet | Variant:
    global _resolved_model
    with _gigachat_client() as client:
        model = _resolve_model(client)
        # Базовый GigaChat-2 часто обрезает строгий JSON. Тогда пробуем Pro/Max.
        models_try = [model]
        for alt in ("GigaChat-2-Pro", "GigaChat-2-Max"):
            if alt not in models_try:
                models_try.append(alt)
        messages = [
            Messages(
                role=MessagesRole.SYSTEM,
                content=build_system_prompt(
                    task_count, subject, grade, compact=compact
                ),
            ),
            Messages(role=MessagesRole.USER, content=user_prompt),
        ]
        last_length: LengthFinishReasonError | None = None
        for name in models_try:
            if name in _length_blocked:
                continue
            logger.info("GigaChat: запрос %s, заданий в ответе %s", name, task_count)
            chat = Chat(
                model=name,
                max_tokens=_chat_max_tokens(task_count, variants_in_response),
                messages=messages,
            )
            try:
                _completion, parsed = client.chat_parse(
                    chat, response_format=response_format, strict=True
                )
                return parsed
            except NotFoundError:
                logger.warning("Модель %s недоступна, пробуем следующую", name)
                _resolved_model = None
                continue
            except LengthFinishReasonError as exc:
                logger.warning("GigaChat %s: ответ обрезан по длине", name)
                _length_blocked.add(name)
                last_length = exc
                continue
        if last_length:
            raise last_length
        raise RuntimeError("Не удалось вызвать GigaChat")


def _call_gigachat_all_variants(
    subject: str,
    grade: int,
    topic_specs: list[TopicSpec],
    variant_count: int,
    task_count: int,
    seed: str,
    fix_errors: list[str] | None,
    compact: bool,
    check_focus: str = "",
    advanced: bool = False,
) -> list[Variant]:
    variants: list[Variant] = []
    for vid in (str(i + 1) for i in range(variant_count)):
        parts: list[Variant] = []
        already: list[str] = []
        for index, count in enumerate(_task_chunks(task_count, advanced=advanced), start=1):
            logger.info(
                "GigaChat: вариант %s, часть %s (%s заданий)",
                vid,
                index,
                count,
            )
            prompt = _build_user_prompt(
                subject,
                grade,
                topic_specs,
                variant_count,
                count,
                f"{seed}-{vid}-{index}",
                fix_errors=fix_errors,
                only_variant_id=vid,
                check_focus=check_focus,
                avoid_texts=already or None,
            )
            variant = _call_gigachat(
                prompt,
                count,
                subject,
                grade,
                response_format=Variant,
                variants_in_response=1,
                compact=compact or task_count >= 8,
            )
            if isinstance(variant, Variant):
                parts.append(variant)
                already.extend(task.text for task in variant.tasks)
        variants.append(_join_variant_parts(vid, parts))
    return variants


def _join_variant_parts(variant_id: str, parts: list[Variant]) -> Variant:
    tasks: list[TaskItem] = []
    number = 1
    for part in parts:
        for task in part.tasks:
            tasks.append(task.model_copy(update={"number": number}))
            number += 1
    return Variant(variant_id=variant_id, tasks=tasks)


def _single_fallback_task(subject: str, number: int, examples: list[dict]) -> TaskItem:
    if subject == "russian" and examples:
        ex = random.choice(examples)
        params = {k: random.randint(2, 15) if k != "word" else "урок" for k in ex.get("params", {})}
        text = ex["template"].format(**{**ex.get("params", {}), **params})
        return TaskItem(number=number, text=text[:400], answer=ex.get("answer", "—"), eval_expr=None)
    if examples:
        ex = random.choice(examples)
        params = {k: random.randint(2, 12) for k in ex.get("params", {})}
        expr_template = ex.get("eval_expr", "1+1")
        expr = expr_template.format(**params)
        if subject == "informatics" and ex.get("answer_from") == "bin":
            n = random.randint(5, 30)
            text = f"Переведите число {n} из десятичной в систему счисления с основанием 2."
            return TaskItem(
                number=number,
                text=text[:400],
                answer=bin(n)[2:],
                eval_expr=None,
            )
        text = ex["template"].format(**params)
        answer = _normalize_number(_safe_eval_expr(expr) or 0)
        return TaskItem(number=number, text=text[:400], answer=answer, eval_expr=expr)
    a, b = random.randint(2, 20), random.randint(2, 9)
    expr = f"{a} + {b}"
    return TaskItem(
        number=number,
        text=f"Вычислите: {a} + {b}.",
        answer=_normalize_number(float(a + b)),
        eval_expr=expr,
    )


def pad_variants_to_task_count(card_set: CardSet, task_count: int) -> CardSet:
    """Подрезает или дополняет задания до нужного числа."""
    db = load_tasks_db()
    examples = db.get("few_shot_examples", {}).get(card_set.subject, [])
    new_variants: list[Variant] = []
    for variant in card_set.variants:
        tasks = list(variant.tasks)[:task_count]
        while len(tasks) < task_count:
            num = len(tasks) + 1
            tasks.append(_single_fallback_task(card_set.subject, num, examples))
        tasks = [
            task.model_copy(update={"number": index})
            for index, task in enumerate(tasks, start=1)
        ]
        new_variants.append(variant.model_copy(update={"tasks": tasks}))
    return card_set.model_copy(update={"variants": new_variants, "task_count": task_count})


def _fallback_card_set(
    subject: str,
    grade: int,
    topic_specs: list[TopicSpec],
    variant_count: int,
    task_count: int,
) -> CardSet:
    topic = _topics_summary(topic_specs)
    difficulty = _hardest_difficulty(topic_specs)
    db = load_tasks_db()
    examples = db.get("few_shot_examples", {}).get(subject, [])
    variant_ids = [str(i + 1) for i in range(variant_count)]
    variants: list[Variant] = []

    for vid in variant_ids:
        tasks: list[TaskItem] = []
        for num in range(1, task_count + 1):
            tasks.append(_single_fallback_task(subject, num, examples))
        variants.append(Variant(variant_id=vid, tasks=tasks))

    return CardSet(
        subject=subject,
        grade=grade,
        topic=topic,
        difficulty=difficulty,
        task_count=task_count,
        topics=topic_specs,
        variants=variants,
    )


def gigachat_queue_size() -> int:
    """Сколько генераций уже идут или ждут GigaChat."""
    with _queue_state:
        return _queued_jobs


def _enter_gigachat_queue() -> int:
    global _queued_jobs
    with _queue_state:
        ahead = _queued_jobs
        _queued_jobs += 1
        return ahead


def _leave_gigachat_queue() -> None:
    global _queued_jobs
    with _queue_state:
        _queued_jobs = max(0, _queued_jobs - 1)


def generate_card_set(
    subject: str,
    grade: int,
    topic_specs: list[TopicSpec],
    variant_count: int,
    task_count: int,
    seed: str,
    check_focus: str = "",
) -> tuple[CardSet, bool]:
    """Один запрос к GigaChat за раз. Остальные карточки ждут в очереди."""
    if variant_count != 2:
        raise ValueError("Поддерживаются только 2 варианта на лист")
    ahead = _enter_gigachat_queue()
    try:
        if ahead:
            logger.info("GigaChat: запрос в очереди, перед ним %s", ahead)
        with _GIGACHAT_LOCK:
            return _generate_card_set(
                subject,
                grade,
                topic_specs,
                variant_count,
                task_count,
                seed,
                check_focus,
            )
    finally:
        _leave_gigachat_queue()


def _generate_card_set(
    subject: str,
    grade: int,
    topic_specs: list[TopicSpec],
    variant_count: int,
    task_count: int,
    seed: str,
    check_focus: str = "",
) -> tuple[CardSet, bool]:
    """
    Возвращает (CardSet, used_fallback).
    """
    if not GIGACHAT_CREDENTIALS:
        logger.warning("GIGACHAT_CREDENTIALS не задан — используется fallback")
        return (
            _fallback_card_set(subject, grade, topic_specs, variant_count, task_count),
            True,
        )

    global _length_blocked
    _length_blocked = set()
    summary = _topics_summary(topic_specs)
    difficulty = _hardest_difficulty(topic_specs)
    last_errors: list[str] = []
    advanced = difficulty == "advanced"
    # Сложные условия длиннее: один общий ответ обрезается, и бот крутит повтор.
    split_mode = _should_split_generation(variant_count, task_count) or advanced
    compact = task_count >= 8 or advanced
    for attempt in range(GENERATION_MAX_RETRIES + 1):
        try:
            fix_errors = last_errors if attempt else None
            if split_mode:
                logger.info(
                    "GigaChat: генерация по одному варианту (%s×%s заданий)",
                    variant_count,
                    task_count,
                )
                generated_variants = _call_gigachat_all_variants(
                    subject,
                    grade,
                    topic_specs,
                    variant_count,
                    task_count,
                    seed,
                    fix_errors,
                    compact,
                    check_focus,
                    advanced,
                )
            else:
                prompt = _build_user_prompt(
                    subject,
                    grade,
                    topic_specs,
                    variant_count,
                    task_count,
                    seed,
                    fix_errors=fix_errors,
                    check_focus=check_focus,
                )
                parsed = _call_gigachat(
                    prompt,
                    task_count,
                    subject,
                    grade,
                    response_format=CardSet,
                    variants_in_response=variant_count,
                    compact=compact,
                )
                if not isinstance(parsed, CardSet):
                    raise TypeError("Ожидался CardSet")
                generated_variants = parsed.variants
            logger.info(
                "GigaChat: сырой ответ собран, заданий %s",
                [len(v.tasks) for v in generated_variants],
            )
            card_set = CardSet(
                subject=subject,
                grade=grade,
                topic=summary,
                difficulty=difficulty,
                task_count=task_count,
                topics=topic_specs,
                variants=generated_variants,
            )
            card_set = align_task_topics(card_set)
            card_set = normalize_variant_ids(card_set)
            if any(len(v.tasks) != task_count for v in card_set.variants):
                logger.warning(
                    "GigaChat вернул %s заданий, нормализуем до %s",
                    [len(v.tasks) for v in card_set.variants],
                    task_count,
                )
                card_set = pad_variants_to_task_count(card_set, task_count)
            logger.info("GigaChat: repair ответов…")
            card_set = repair_card_set(card_set)
            last_errors = validate_card_set(card_set, task_count)
            if not last_errors:
                logger.info("GigaChat: карточка готова")
                return card_set, False
            logger.warning("Валидация не прошла (попытка %s): %s", attempt + 1, last_errors)
        except LengthFinishReasonError as exc:
            logger.warning(
                "GigaChat: ответ обрезан по длине (попытка %s), включаем пошаговую генерацию",
                attempt + 1,
            )
            split_mode = True
            compact = True
            last_errors = [str(exc)]
        except Exception as exc:
            logger.exception("Ошибка GigaChat (попытка %s): %s", attempt + 1, exc)
            last_errors = [str(exc)]

    logger.warning("GigaChat не дал валидный результат — fallback")
    return (
        _fallback_card_set(subject, grade, topic_specs, variant_count, task_count),
        True,
    )


def _points_word(count: int) -> str:
    n = abs(count) % 100
    if 11 <= n <= 14:
        return "баллов"
    last = n % 10
    if last == 1:
        return "балл"
    if 2 <= last <= 4:
        return "балла"
    return "баллов"


def _score_ranges(scale: dict[int, str]) -> list[tuple[int, int, str]]:
    groups: list[list] = []
    for points, mark in sorted(scale.items()):
        if groups and groups[-1][2] == mark and groups[-1][1] == points - 1:
            groups[-1][1] = points
        else:
            groups.append([points, points, mark])
    return [(lo, hi, mark) for lo, hi, mark in reversed(groups)]


def format_answer_key(card_set: CardSet) -> str:
    from config import grade_scale
    from math_render import format_math_for_messenger

    n = card_set.task_count or len(card_set.variants[0].tasks)
    scale = grade_scale(n)

    lines = [
        "Ответы к пятиминутке",
        f"{SUBJECT_LABELS.get(card_set.subject, card_set.subject)}, "
        f"{card_set.grade} класс — {card_set.topic}",
        "",
    ]
    for variant in card_set.variants:
        lines.append(f"Вариант {variant.variant_id}")
        for task in variant.tasks:
            lines.append(f"{task.number}) {format_math_for_messenger(task.answer)}")
        lines.append("")
    lines.append(f"Шкала оценок (из {n} заданий):")
    for lo, hi, mark in _score_ranges(scale):
        if lo == hi:
            label = f"{hi} {_points_word(hi)}"
        else:
            label = f"{lo}–{hi} {_points_word(hi)}"
        lines.append(f"  {label} — оценка {mark}")
    return "\n".join(lines)
