import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
    
from maxapi import Bot, Dispatcher, F
from maxapi.context import MemoryContext, State, StatesGroup
from maxapi.filters.command import Command, CommandStart
from maxapi.types import CallbackButton, InputMedia, MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from menu import (
    HELP_TEXT,
    WELCOME_TEXT,
    card_data_problem,
    card_ready_text,
    grade_prompt,
    remember_subject,
    remembered_subject,
    resolve_topic,
    rows_grades,
    rows_multi_continue,
    rows_stars,
    rows_subjects,
    rows_task_count,
    rows_topic_mode,
    rows_topics,
    rows_back_tmode,
    rows_new_card,
    rows_welcome,
    resolve_back_navigation,
    rows_variants,
    stars_to_difficulty,
    topic_specs_from_data,
)
from config import MAX_BOT_TOKEN, MAX_TASKS_PER_VARIANT, MIN_TASKS_PER_VARIANT
from generator import format_answer_key, generate_card_set, gigachat_queue_size
from pdf_builder import save_pdf

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(MAX_BOT_TOKEN) if MAX_BOT_TOKEN else Bot()
dp = Dispatcher()


class QuizForm(StatesGroup):
    welcome = State()
    subject = State()
    grade = State()
    topic_mode = State()
    topic = State()
    custom_topic = State()
    stars = State()
    task_count = State()
    variants = State()


def _kb(rows: list[list[tuple[str, str]]]) -> list:
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*[CallbackButton(text=t, payload=p) for t, p in row])
    return [builder.as_markup()]


def _message_text(event: MessageCreated) -> str:
    body = event.message.body
    if body and body.text:
        return body.text
    return ""


def _callback_chat_id(event: MessageCallback) -> int:
    chat_id, _user_id = event.get_ids()
    if chat_id is None:
        raise ValueError("Не удалось определить chat_id для callback")
    return chat_id


async def _edit_or_answer(
    event: MessageCallback, text: str, attachments: list | None = None
):
    try:
        await event.answer(new_text=text, attachments=attachments or [])
    except Exception:
        if event.message:
            await event.message.answer(text, attachments=attachments or [])


async def _ack_callback(event: MessageCallback, notification: str | None = None) -> None:
    try:
        await event.answer(notification=notification)
    except Exception:
        pass


async def _send_new_card_menu(chat_id: int) -> None:
    subject = remembered_subject(chat_id)
    if subject:
        await bot.send_message(
            chat_id=chat_id,
            text=grade_prompt(subject),
            attachments=_kb(rows_grades()),
        )
        return
    await bot.send_message(
        chat_id=chat_id,
        text="Выберите предмет:",
        attachments=_kb(rows_subjects()),
    )


async def _show_welcome(chat_id: int, context: MemoryContext) -> None:
    await context.clear()
    await context.set_state(QuizForm.welcome)
    await bot.send_message(
        chat_id=chat_id,
        text=WELCOME_TEXT,
        attachments=_kb(rows_welcome()),
    )


@dp.bot_started()
async def on_bot_started(event, context: MemoryContext):
    await _show_welcome(event.chat_id, context)


@dp.message_created(CommandStart())
@dp.message_created(Command("start"))
async def cmd_start(event: MessageCreated, context: MemoryContext):
    await context.clear()
    await context.set_state(QuizForm.welcome)
    await event.message.answer(WELCOME_TEXT, attachments=_kb(rows_welcome()))


@dp.message_callback(F.callback.payload == "flow:begin")
async def on_begin(event: MessageCallback, context: MemoryContext):
    chat_id = _callback_chat_id(event)
    subject = remembered_subject(chat_id)
    await context.clear()
    if subject:
        await context.update_data(subject=subject, topic_blocks=[])
        await context.set_state(QuizForm.grade)
        await _edit_or_answer(event, grade_prompt(subject), _kb(rows_grades()))
        return
    await context.set_state(QuizForm.subject)
    await _edit_or_answer(event, "Выберите предмет:", _kb(rows_subjects()))


@dp.message_created(Command("help"))
async def cmd_help(event: MessageCreated):
    await event.message.answer(HELP_TEXT)


@dp.message_callback(F.callback.payload.startswith("sub:"))
async def on_subject(event: MessageCallback, context: MemoryContext):
    subject = event.callback.payload.split(":", 1)[1]
    remember_subject(_callback_chat_id(event), subject)
    await context.update_data(subject=subject, topic_blocks=[])
    await context.set_state(QuizForm.grade)
    await _edit_or_answer(event, "Выберите класс:", _kb(rows_grades()))


@dp.message_callback(F.callback.payload.startswith("grade:"))
async def on_grade(event: MessageCallback, context: MemoryContext):
    grade = int(event.callback.payload.split(":", 1)[1])
    await context.update_data(
        grade=grade,
        topic_page=0,
        topic_blocks=[],
        pending_topic=None,
        topic=None,
        tmode=None,
    )
    await context.set_state(QuizForm.topic_mode)
    await _edit_or_answer(event, "Как задать тему?", _kb(rows_topic_mode()))


@dp.message_callback(F.callback.payload.startswith("tmode:"))
async def on_topic_mode(event: MessageCallback, context: MemoryContext):
    mode = event.callback.payload.split(":", 1)[1]
    # Смена режима сбрасывает набор тем, иначе «несколько» → «своя» тянет старые blocks.
    await context.update_data(
        tmode=mode,
        topic_blocks=[],
        pending_topic=None,
        topic=None,
        topic_page=0,
    )
    data = await context.get_data()
    if mode == "custom":
        await context.set_state(QuizForm.custom_topic)
        await _edit_or_answer(
            event, "Введите свою тему одним сообщением:", _kb(rows_back_tmode())
        )
        return
    await context.set_state(QuizForm.topic)
    multi = mode == "multi"
    text, rows = rows_topics(data["subject"], data["grade"], 0, multi=multi)
    await _edit_or_answer(event, text, _kb(rows))


@dp.message_callback(F.callback.payload.startswith("tpage:"))
async def on_topic_page(event: MessageCallback, context: MemoryContext):
    page = int(event.callback.payload.split(":", 1)[1])
    data = await context.get_data()
    await context.update_data(topic_page=page)
    multi = data.get("tmode") == "multi"
    text, rows = rows_topics(data["subject"], data["grade"], page, multi=multi)
    await _edit_or_answer(event, text, _kb(rows))


@dp.message_callback(F.callback.payload.startswith("topic:"))
async def on_topic_callback(event: MessageCallback, context: MemoryContext):
    payload = event.callback.payload
    if payload == "topic:custom":
        data = await context.get_data()
        # Из каталога — одиночная своя тема; из multi — добавить свою в набор.
        if data.get("tmode") != "multi":
            await context.update_data(
                tmode="custom",
                topic_blocks=[],
                pending_topic=None,
                topic=None,
            )
        await context.set_state(QuizForm.custom_topic)
        await _edit_or_answer(
            event, "Введите свою тему одним сообщением:", _kb(rows_back_tmode())
        )
        return
    suffix = payload.split(":", 1)[1]
    if not suffix.isdigit():
        return
    idx = int(suffix)
    data = await context.get_data()
    topic = resolve_topic(data["subject"], data["grade"], idx)
    await context.update_data(pending_topic=topic)
    await context.set_state(QuizForm.stars)
    await _edit_or_answer(
        event,
        f"Тема: {topic}\n\nСложность (звёзды на карточке):",
        _kb(rows_stars()),
    )


@dp.message_callback(F.callback.payload.startswith("tpcmulti:"))
async def on_topic_multi_pick(event: MessageCallback, context: MemoryContext):
    idx = int(event.callback.payload.split(":", 1)[1])
    data = await context.get_data()
    topic = resolve_topic(data["subject"], data["grade"], idx)
    await context.update_data(pending_topic=topic)
    await context.set_state(QuizForm.stars)
    await _edit_or_answer(
        event,
        f"Тема: {topic}\n\nСложность для этой темы:",
        _kb(rows_stars()),
    )


@dp.message_created(QuizForm.custom_topic)
async def on_custom_topic_text(event: MessageCreated, context: MemoryContext):
    topic = _message_text(event).strip()[:120]
    if len(topic) < 2:
        await event.message.answer("Тема слишком короткая. Введите ещё раз:")
        return
    await context.update_data(pending_topic=topic)
    await context.set_state(QuizForm.stars)
    await event.message.answer(
        f"Тема: {topic}\n\nВыберите сложность:",
        attachments=_kb(rows_stars()),
    )


@dp.message_callback(F.callback.payload.startswith("stars:"))
async def on_stars(event: MessageCallback, context: MemoryContext):
    stars = int(event.callback.payload.split(":", 1)[1])
    difficulty = stars_to_difficulty(stars)
    data = await context.get_data()
    topic = data.get("pending_topic") or data.get("topic", "Тема")
    tmode = data.get("tmode", "list")

    if tmode == "multi":
        blocks = list(data.get("topic_blocks") or [])
        blocks.append({"topic": topic, "difficulty": difficulty})
        await context.update_data(
            topic=topic,
            difficulty=difficulty,
            topic_blocks=blocks,
            pending_topic=None,
        )
        summary = "\n".join(f"• {b['topic']}" for b in blocks)
        await context.set_state(QuizForm.topic)
        await _edit_or_answer(
            event,
            f"Темы на карточке:\n{summary}\n\nДобавить ещё или продолжить?",
            _kb(rows_multi_continue()),
        )
        return

    await context.update_data(
        topic=topic,
        difficulty=difficulty,
        pending_topic=None,
        topic_blocks=[],
        check_focus="auto",
    )
    data = await context.get_data()
    await context.set_state(QuizForm.task_count)
    await _edit_or_answer(
        event,
        "Сколько заданий в одном варианте?",
        _kb(rows_task_count(data)),
    )


@dp.message_callback(F.callback.payload == "multi:add")
async def on_multi_add(event: MessageCallback, context: MemoryContext):
    data = await context.get_data()
    page = data.get("topic_page", 0)
    text, rows = rows_topics(data["subject"], data["grade"], page, multi=True)
    await _edit_or_answer(event, text, _kb(rows))


@dp.message_callback(F.callback.payload == "multi:done")
async def on_multi_done(event: MessageCallback, context: MemoryContext):
    data = await context.get_data()
    if not data.get("topic_blocks"):
        await event.answer(notification="Добавьте хотя бы одну тему")
        return
    last = data["topic_blocks"][-1]
    await context.update_data(
        topic=last["topic"], difficulty=last["difficulty"], check_focus="auto"
    )
    data = await context.get_data()
    await context.set_state(QuizForm.task_count)
    await _edit_or_answer(
        event,
        "Сколько заданий в одном варианте?",
        _kb(rows_task_count(data)),
    )


@dp.message_callback(F.callback.payload.startswith("tc:"))
async def on_task_count(event: MessageCallback, context: MemoryContext):
    payload = event.callback.payload
    suffix = payload.split(":", 1)[1]
    if not suffix.isdigit():
        return
    task_count = int(suffix)
    if not MIN_TASKS_PER_VARIANT <= task_count <= MAX_TASKS_PER_VARIANT:
        return
    await context.update_data(task_count=task_count)
    data = await context.get_data()
    await context.set_state(QuizForm.variants)
    await _edit_or_answer(event, card_ready_text(data), _kb(rows_variants()))


@dp.message_callback(F.callback.payload.startswith("var:"))
async def on_variants(event: MessageCallback, context: MemoryContext):
    await context.update_data(variant_count=2)
    await on_generate(event, context)


async def on_generate(event: MessageCallback, context: MemoryContext):
    data = await context.get_data()
    problem = card_data_problem(data)
    if problem:
        await _edit_or_answer(event, problem)
        return
    chat_id = _callback_chat_id(event)
    seed = f"{chat_id}-{int(time.time())}"
    specs = topic_specs_from_data(data)

    ahead = gigachat_queue_size()
    wait = f" Перед вами в очереди: {ahead}." if ahead else ""
    tasks_n = data.get("task_count", 3)
    hard = any(
        (b.get("difficulty") == "advanced")
        for b in (data.get("topic_blocks") or [])
    ) or data.get("difficulty") == "advanced"
    eta = "обычно 1–2 минуты" if tasks_n >= 8 or hard else "до 30–45 сек"
    await _edit_or_answer(event, f"⏳ Генерирую пятиминутку ({eta}).{wait}")

    try:
        card_set, used_fallback = await asyncio.to_thread(
            generate_card_set,
            data["subject"],
            data["grade"],
            specs,
            2,
            data.get("task_count", 3),
            seed,
            "auto",
        )
    except Exception as exc:
        logger.exception("Generation failed")
        await bot.send_message(
            chat_id=chat_id,
            text=f"Не удалось сгенерировать задания: {exc}\nПопробуйте /start ещё раз.",
        )
        return

    logger.info(
        "Generation done chat_id=%s fallback=%s tasks=%s",
        chat_id,
        used_fallback,
        card_set.task_count,
    )
    note = ""
    if used_fallback:
        note = "\n\n⚠️ Использован резервный шаблон (GigaChat недоступен или вернул ошибку)."

    await bot.send_message(chat_id=chat_id, text="📄 Собираю PDF…")

    pdf_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            pdf_path = Path(tmp.name)
        await asyncio.to_thread(save_pdf, card_set, pdf_path)
        logger.info("PDF saved chat_id=%s path=%s", chat_id, pdf_path)
        await bot.send_message(
            chat_id=chat_id,
            text=f"📄 Готово! Лист для печати (A4, 2 варианта).{note}",
            attachments=[InputMedia(path=str(pdf_path))],
        )
        logger.info("PDF sent chat_id=%s", chat_id)
    except Exception:
        logger.exception("PDF build/send failed")
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "Задания собраны, но PDF не удалось отправить. "
                "Ниже — ответы; попробуйте создать карточку ещё раз."
                f"{note}"
            ),
        )

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=format_answer_key(card_set),
            attachments=_kb(rows_new_card()),
        )
    except Exception:
        logger.exception("Answer key send failed")
        await bot.send_message(
            chat_id=chat_id,
            text="Не удалось отправить ключ ответов. Нажмите /start и создайте карточку снова.",
        )

    if pdf_path is not None:
        try:
            pdf_path.unlink(missing_ok=True)
        except OSError:
            pass

    await context.clear()


@dp.message_callback(F.callback.payload == "flow:new")
async def on_new_card(event: MessageCallback, context: MemoryContext):
    chat_id = _callback_chat_id(event)
    await _ack_callback(event, notification="Новая карточка")
    subject = remembered_subject(chat_id)
    await context.clear()
    if subject:
        await context.update_data(subject=subject, topic_blocks=[])
        await context.set_state(QuizForm.grade)
    else:
        await context.set_state(QuizForm.subject)
    await _send_new_card_menu(chat_id)


_BACK_STATE = {
    "welcome": QuizForm.welcome,
    "subject": QuizForm.subject,
    "grade": QuizForm.grade,
    "topic_mode": QuizForm.topic_mode,
    "topic": QuizForm.topic,
    "stars": QuizForm.stars,
    "task_count": QuizForm.task_count,
    "variants": QuizForm.variants,
}


@dp.message_callback(F.callback.payload.startswith("back:"))
async def on_back(event: MessageCallback, context: MemoryContext):
    target = event.callback.payload.split(":", 1)[1]
    data = await context.get_data()
    nav = resolve_back_navigation(target, data)
    if not nav:
        await _ack_callback(event, notification="Нельзя вернуться")
        return
    text, rows, state_key = nav
    state = _BACK_STATE.get(state_key)
    if state:
        await context.set_state(state)
    await _edit_or_answer(event, text, _kb(rows))


async def main():
    if not MAX_BOT_TOKEN:
        raise SystemExit(
            "Задайте MAX_BOT_TOKEN в .env (токен чат-бота в кабинете MAX)."
        )
    mode = os.getenv("BOT_MODE", "polling").lower()
    if mode == "webhook":
        host = os.getenv("WEBHOOK_HOST", "0.0.0.0")
        port = int(os.getenv("WEBHOOK_PORT", "8080"))
        webhook_url = os.getenv("WEBHOOK_URL")
        webhook_secret = os.getenv("WEBHOOK_SECRET")
        if webhook_url and webhook_secret:
            await bot.subscribe_webhook(url=webhook_url, secret=webhook_secret)
        await dp.handle_webhook(bot=bot, host=host, port=port, log_level="info")
    else:
        try:
            await bot.delete_webhook()
        except Exception:
            pass
        await dp.start_polling(bot)
