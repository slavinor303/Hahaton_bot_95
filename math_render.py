"""Рендер LaTeX-фрагментов для PDF (matplotlib mathtext)."""

import io
import logging
import re
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)
# Тот же dpi, что у PNG: в PDF пиксели переводятся в пункты, а не рисуются 1:1.
FORMULA_DPI = 160

_MATH_HINT = re.compile(
    r"\\frac|\\sqrt|\\cdot|\\times|\\leq|\\geq|\\neq|\\pi|\\log|\^|_|"
    r"\\left|\\right|[<>]=?"
)
_NUM_FRAC = re.compile(r"(?<![/\d^])(\d+)\s*/\s*(\d+)(?![/\d])")
_SYM_FRAC = re.compile(r"(?<![/\w\\^])([A-Za-z0-9]+)\s*/\s*([A-Za-z0-9]+)(?![/\w])")
_MATH_CHARS = re.compile(
    r"^[\d\s/+\-()*^.=xXa-zA-Z,:·—–²³⁰¹⁴⁵⁶⁷⁸⁹√πτραωΔλμν\\{}_|≤≥≠<>]+$"
)
_EQUATION_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"([A-Za-z][A-Za-z0-9_]{0,12}\s*=\s*"
    r"[A-Za-z0-9\\^_²³/\(\)\+\-\*·× πτραωΔλμν√.,]+(?:\([A-Za-z0-9/+\-^²³√]+\))*)"
)

_GREEK_UNICODE = {
    "π": r"\pi",
    "τ": r"\tau",
    "α": r"\alpha",
    "β": r"\beta",
    "ω": r"\omega",
    "Δ": r"\Delta",
    "λ": r"\lambda",
    "μ": r"\mu",
    "ν": r"\nu",
    "ρ": r"\rho",
    "φ": r"\phi",
    "γ": r"\gamma",
    "θ": r"\theta",
    "σ": r"\sigma",
    "ε": r"\varepsilon",
    "η": r"\eta",
    "ψ": r"\psi",
    "Ω": r"\Omega",
}
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_DOLLAR_MATH = re.compile(r"\$([^$]+)\$")


@dataclass
class TaskDisplay:
    """Текст задания в PDF: куски фразы и формулы по порядку."""

    prefix: str
    latex: str
    parts: list[tuple[str, str]] | None = None


def needs_math_render(text: str | None) -> bool:
    if not text:
        return False
    if "$" in text:
        return True
    if _NUM_FRAC.search(text):
        return True
    return bool(_MATH_HINT.search(text))


def normalize_llm_math(text: str) -> str:
    """Убирает обёртки $...$ и типичный мусор из ответа LLM."""
    s = text.strip()
    s = re.sub(r"\$+", "", s)
    # GigaChat иногда экранирует команды дважды: \\log → \log
    s = re.sub(r"\\{2,}([A-Za-z]+)", r"\\\1", s)
    s = s.replace(r"\dfrac", r"\frac").replace(r"\tfrac", r"\frac")
    s = s.replace(r"\lg", r"\log_{10}")
    s = s.replace("≤", r"\leq").replace("≥", r"\geq").replace("≠", r"\neq")
    s = s.replace("<=", r"\leq").replace(">=", r"\geq").replace("!=", r"\neq")
    s = s.replace("÷", "/").replace("·", "\\cdot ").replace("×", "\\times ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _apply_frac_subs(chunk: str) -> str:
    # Не режем слово посередине: dy/dx целиком, а не y/d внутри dy.
    paren_frac = re.compile(
        r"(?<![A-Za-z0-9\\^])([A-Za-z0-9]+(?:\^\{[^}]+\}|\^[0-9]+)?)\s*/\s*\(([^)]+)\)"
    )
    pow_frac = re.compile(
        r"(?<![A-Za-z0-9\\^])([A-Za-z][A-Za-z0-9]*(?:\^\{[^}]+\}|\^[0-9]+)?)\s*/\s*"
        r"([A-Za-z0-9]+(?:\^\{[^}]+\}|\^[0-9]+)?)(?![A-Za-z0-9])"
    )
    chunk = paren_frac.sub(
        lambda m: f"\\frac{{{m.group(1)}}}{{{m.group(2)}}}", chunk
    )
    chunk = pow_frac.sub(
        lambda m: f"\\frac{{{m.group(1)}}}{{{m.group(2)}}}", chunk
    )
    chunk = _NUM_FRAC.sub(lambda m: f"\\frac{{{m.group(1)}}}{{{m.group(2)}}}", chunk)
    chunk = _SYM_FRAC.sub(lambda m: f"\\frac{{{m.group(1)}}}{{{m.group(2)}}}", chunk)
    return chunk


def _separate_commands(s: str) -> str:
    """\\rho l не должен склеиваться в несуществующую команду \\rhol."""
    names = (
        "varepsilon|vartheta|alpha|beta|gamma|delta|theta|lambda|sigma|"
        "omega|Delta|Omega|rho|phi|tau|eta|psi|chi|nu|mu|pi"
    )
    return re.sub(rf"\\({names})(?=[A-Za-z0-9])", r"\\\1 ", s)


def _paren_powers(s: str) -> str:
    """2^(2x) и 3^(2+log_3(5)) → 2^{2x}, 3^{2+log_3(5)}."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "^" and i + 1 < n and s[i + 1] == "(":
            depth = 0
            for j in range(i + 1, n):
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                    if depth == 0:
                        out.append("^{" + s[i + 2 : j] + "}")
                        i = j + 1
                        break
            else:
                out.append(s[i])
                i += 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _opaque_formula(png: bytes) -> bytes:
    """Белый фон: прозрачная картинка в PDF пропадает и остаётся сырой текст."""
    try:
        from PIL import Image
    except ImportError:
        return png
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    background = Image.new("RGB", image.size, (255, 255, 255))
    background.paste(image, mask=image.getchannel("A"))
    out = io.BytesIO()
    background.save(out, format="PNG")
    return out.getvalue()


def _brace_bare_powers(s: str) -> str:
    """e^2x и 2^x → e^{2x}, 2^{x}. Иначе в степени остаётся только первый символ."""
    return re.sub(
        r"\^(?!\{)([A-Za-z0-9]+)",
        lambda m: "^{" + m.group(1) + "}",
        s,
    )


def _indexed_fracs(s: str) -> str:
    """A_{0}/A_{n} и q_{2}/r^2 → дробь, степень остаётся в знаменателе."""
    if "\\frac" in s:
        return s
    atom = r"[A-Za-z][A-Za-z0-9]*(?:_\{[^{}]+\})?(?:\^\{[^}]+\}|\^[0-9]+)?"
    s = re.sub(
        rf"(?<![A-Za-z0-9\\])({atom})\s*/\s*({atom})(?![A-Za-z0-9])",
        lambda m: f"\\frac{{{m.group(1)}}}{{{m.group(2)}}}",
        s,
    )
    s = re.sub(
        r"(\\frac\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\})\s*/\s*([A-Za-z0-9]+)",
        lambda m: f"\\frac{{{m.group(1)}}}{{{m.group(2)}}}",
        s,
    )
    return s


def _with_script_placeholders(s: str, transform) -> str:
    """Не трогаем _{...} и ^{...}: иначе log_{1/2} превращается в \\log_{1}/2}."""
    vault: list[str] = []

    def stash(match: re.Match[str]) -> str:
        vault.append(match.group(0))
        return f"@@SCR{len(vault) - 1}@@"

    protected = re.sub(r"[_^]\{[^{}]*\}", stash, s)
    protected = transform(protected)
    for index, chunk in enumerate(vault):
        protected = protected.replace(f"@@SCR{index}@@", chunk)
    return protected


def slashes_to_frac(s: str) -> str:
    """9/16, F/m → \\frac{...}{...} (не трогает уже записанные \\frac)."""

    def _convert(chunk: str) -> str:
        if "\\frac" in chunk:
            parts = re.split(r"(\\frac\{[^}]+\}\{[^}]+\})", chunk)
            return "".join(
                _apply_frac_subs(p) if not p.startswith("\\frac") else p for p in parts
            )
        return _apply_frac_subs(chunk)

    return _with_script_placeholders(s, _convert)


def _normalize_logs(s: str) -> str:
    """log₂, log_5{125}, loga(x), \\log_a → \\log_{base}(arg)."""
    # LLM: log\_a → log_a
    s = s.replace(r"\_", "_")
    # Пустое основание: log_{ }(x) или log_(x) → ln(x). Не трогаем log_{2} / log_{e}.
    s = re.sub(
        r"(?<![\\A-Za-z])\\?log\s*_\{\s*\}",
        r"\\ln",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"(?<![\\A-Za-z])\\?log\s*_(?=\()",
        r"\\ln",
        s,
        flags=re.I,
    )
    def _log_base(base: str) -> str:
        base = base.strip().replace(",", ".")
        return rf"\log_{{{base}}}"

    # Уже с основанием в скобках: log_{10}, log_{1/2}, log_{0,5}, log_{a}
    s = re.sub(
        r"(?<![\\A-Za-z])\\?log\s*_\{([^}]+)\}",
        lambda m: _log_base(m.group(1)),
        s,
        flags=re.I,
    )
    # log_0.5 и log_0,5 — до целого, иначе останется log_{0}.5
    s = re.sub(
        r"(?<![\\A-Za-z])\\?log_(\d+[.,]\d+)",
        lambda m: _log_base(m.group(1)),
        s,
        flags=re.I,
    )
    # log_10, log_2
    s = re.sub(
        r"(?<![\\A-Za-z])\\?log_(\d+)",
        lambda m: _log_base(m.group(1)),
        s,
        flags=re.I,
    )
    # log_x16 → log_x(16). Не трогаем log_{10}: основание здесь одна буква.
    s = re.sub(
        r"\\log_\{([A-Za-z])\}(\d+)",
        r"\\log_{\1}(\2)",
        s,
    )
    # log0.5( / log0,5{ — цифра без подчёркивания
    s = re.sub(
        r"(?<![\\A-Za-z])\\?log(\d+[.,]\d+)(?=\s*[\(\{])",
        lambda m: _log_base(m.group(1)),
        s,
        flags=re.I,
    )
    s = re.sub(
        r"(?<![\\A-Za-z])\\?log(\d+)(?=\s*[\(\{])",
        lambda m: _log_base(m.group(1)),
        s,
        flags=re.I,
    )
    # log_x16 → log_x(16), пока цифра не прилипла к основанию.
    s = re.sub(
        r"(?<![\\A-Za-z])\\?log_([A-Za-z])(\d+)",
        lambda m: _log_base(m.group(1)) + f"({m.group(2)})",
        s,
        flags=re.I,
    )
    # log_a
    s = re.sub(
        r"(?<![\\A-Za-z])\\?log_([A-Za-z])(?![A-Za-z0-9])",
        r"\\log_{\1}",
        s,
        flags=re.I,
    )
    # loga( / loga{ — основание одной буквой вплотную
    s = re.sub(
        r"(?<![\\A-Za-z])\\?log([A-Za-z])(?=\s*[\(\{])",
        r"\\log_{\1}",
        s,
        flags=re.I,
    )
    # Аргумент в фигурных скобках: \log_{5}{125} → \log_{5}(125)
    def _log_arg(match: re.Match[str]) -> str:
        base = match.group(1).strip()
        arg = match.group(2).strip()
        if not base:
            cmd = "\\ln"
        else:
            cmd = f"\\log_{{{base}}}"
        if arg.startswith("(") and arg.endswith(")"):
            return f"{cmd}{arg}"
        return f"{cmd}({arg})"

    s = re.sub(r"\\log_\{([^}]*)\}\s*\{([^{}]+)\}", _log_arg, s)
    # Ещё раз подчистить пустые базы
    s = re.sub(r"\\log_\{\s*\}", r"\\ln", s)
    # log(x) без основания — всё равно команда, не сырой текст.
    s = re.sub(r"(?<![\\A-Za-z_])log\s*\(", r"\\log(", s, flags=re.I)
    return s


def _unicode_superscripts_to_caret(s: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in "²³⁴⁵⁶⁷⁸⁹⁰¹":
            digits = ch.translate(_SUPERSCRIPT_DIGITS)
            out.append(f"^{{{digits}}}" if len(digits) > 1 else f"^{digits}")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_SUBSCRIPT_DIGITS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
_UNICODE_SUB_LETTERS = {
    "ₐ": "a",
    "ₑ": "e",
    "ₕ": "h",
    "ᵢ": "i",
    "ⱼ": "j",
    "ₖ": "k",
    "ₗ": "l",
    "ₘ": "m",
    "ₙ": "n",
    "ₒ": "o",
    "ₚ": "p",
    "ᵣ": "r",
    "ₛ": "s",
    "ₜ": "t",
    "ᵤ": "u",
    "ᵥ": "v",
    "ₓ": "x",
}
_CYR_MATH_HOMOGLYPHS = str.maketrans(
    {
        "а": "a",
        "А": "A",
        "е": "e",
        "Е": "E",
        "ё": "e",
        "Ё": "E",
        "о": "o",
        "О": "O",
        "р": "p",
        "Р": "P",
        "с": "c",
        "С": "C",
        "у": "y",
        "х": "x",
        "Х": "X",
        "К": "K",
        "М": "M",
        "Т": "T",
        "В": "B",
        "Н": "H",
    }
)


def _plain_subscripts_to_latex(s: str) -> str:
    """F_c, v_12 и log₂ / logₑ → F_{c}, v_{12}, log_{2}, log_{e}."""
    s = re.sub(
        r"[₀₁₂₃₄₅₆₇₈₉]+",
        lambda m: "_{" + m.group(0).translate(_SUBSCRIPT_DIGITS) + "}",
        s,
    )
    # Буквенные юникод-индексы: иначе matplotlib рисует □ вместо ₑ
    letter_class = "".join(_UNICODE_SUB_LETTERS)

    def _letter_subs(match: re.Match[str]) -> str:
        body = "".join(_UNICODE_SUB_LETTERS.get(ch, ch) for ch in match.group(0))
        return "_{" + body + "}"

    if letter_class:
        s = re.sub(f"[{re.escape(letter_class)}]+", _letter_subs, s)
    # log_0.5 и log_0,5 целиком, иначе останется log_{0}.5
    s = re.sub(
        r"([A-Za-z])_(\d+[.,]\d+)",
        lambda m: m.group(1) + "_{" + m.group(2).replace(",", ".") + "}",
        s,
    )
    # log_x16 — основание x и аргумент 16, не индекс x16.
    s = re.sub(
        r"([A-Za-z])_([A-Za-z])(\d+)(?!\})",
        r"\1_{\2}\3",
        s,
    )
    # Многоцифровые/буквенные индексы без скобок: v_12, m_eff
    s = re.sub(r"([A-Za-z])_([A-Za-z0-9]{2,})(?!\})", r"\1_{\2}", s)
    return re.sub(r"([A-Za-z])_([A-Za-z0-9])(?!\})", r"\1_{\2}", s)


def _strip_cyrillic_math(s: str) -> str:
    """В формуле не должно остаться русского текста и знаков $."""
    # е/а как «e/a» в основании log, иначе после вырезания кириллицы база пустая → □
    s = s.translate(_CYR_MATH_HOMOGLYPHS)
    # Единицы вроде м/с, кг·м/с² убираем целиком, иначе остаётся голый «/».
    s = re.sub(
        r"(?:^|[\s=])(?:"
        r"м\s*/\s*с(?:\s*\^?\s*2)?|"
        r"кг(?:\s*·\s*м\s*/\s*с(?:\s*\^?\s*2)?)?|"
        r"см|мм|км|г|Н|Дж|Вт|Ом|Гц|Кл|Ф|Гн|Тл|Па"
        r")(?=$|[\s,.])",
        " ",
        s,
        flags=re.I,
    )
    s = re.sub(r"[а-яА-ЯёЁ]+", " ", s)
    s = s.replace("$", "")
    s = re.sub(r"\\(text|mathrm|operatorname|textbf)\{([^{}]*)\}", r"\2", s)
    s = re.sub(r"\\left(?![A-Za-z])", "", s)
    s = re.sub(r"\\right(?![A-Za-z])", "", s)
    s = s.replace(r"\lor", r"\vee").replace(r"\land", r"\wedge")
    s = s.replace(r"\rightarrow", r"\to")
    s = re.sub(
        r"\\([A-Za-z]+)\s*/\s*([A-Za-z0-9]+)",
        lambda m: f"\\frac{{\\{m.group(1)}}}{{{m.group(2)}}}",
        s,
    )
    s = re.sub(r"\s*/\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .,;:")
    return s.strip()


_UNI_FRAC = {
    "½": (1, 2),
    "⅓": (1, 3),
    "⅔": (2, 3),
    "¼": (1, 4),
    "¾": (3, 4),
    "⅕": (1, 5),
    "⅙": (1, 6),
    "⅛": (1, 8),
}


def _brace_script_fractions(s: str) -> str:
    """Только x^\\pi/2 остаётся степенью. a^2/b — это дробь, не степень 2/b."""
    return re.sub(
        r"\^(\\[A-Za-z]+)\s*/\s*(\\[A-Za-z]+|[A-Za-z0-9]+)",
        lambda m: f"^{{{m.group(1)}/{m.group(2)}}}",
        s,
    )


def _command_fractions(s: str) -> str:
    """\\pi/2, 3\\pi/2 и \\alpha/\\beta → настоящая дробь. Степень ^\\pi/2 не трогаем."""
    s = re.sub(
        r"\^(\\[A-Za-z]+)\s*/\s*(\\[A-Za-z]+|[A-Za-z0-9]+)",
        lambda m: f"^{{\\frac{{{m.group(1)}}}{{{m.group(2)}}}}}",
        s,
    )
    s = re.sub(
        r"(\d+)\s*(\\[A-Za-z]+)\s*/\s*(\\[A-Za-z]+|[A-Za-z0-9]+)",
        lambda m: f"\\frac{{{m.group(1)}{m.group(2)}}}{{{m.group(3)}}}",
        s,
    )
    s = re.sub(
        r"(\\[A-Za-z]+)\s*/\s*(\\[A-Za-z]+|[A-Za-z0-9]+)",
        lambda m: f"\\frac{{{m.group(1)}}}{{{m.group(2)}}}",
        s,
    )
    return s


def _radical_parens(s: str) -> str:
    """sqrt((x^2-1)/(x-1)) целиком, а не до первой скобки."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        match = re.match(r"(?:\\sqrt|√|sqrt)\s*\(", s[i:], flags=re.I)
        if not match:
            out.append(s[i])
            i += 1
            continue
        open_at = i + match.end() - 1
        close = _closing_paren(s, open_at)
        if close is None:
            out.append(s[i])
            i += 1
            continue
        out.append(r"\sqrt{" + s[open_at + 1 : close] + "}")
        i = close + 1
    return "".join(out)


def _closing_paren(s: str, open_at: int) -> int | None:
    depth = 0
    for j in range(open_at, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return None


def _group_ending_at(s: str, slash: int) -> tuple[int, int] | None:
    """Скобки непосредственно перед «/», вместе со вложенными и степенью (a+b)^2."""
    end = slash
    while end > 0 and s[end - 1] == " ":
        end -= 1
    if end == 0:
        return None
    i = end - 1
    # (a+b)^2 / ... и (a+b)^{2} / ... — степень входит в числитель.
    if s[i] == "}":
        depth = 0
        for k in range(i, -1, -1):
            if s[k] == "}":
                depth += 1
            elif s[k] == "{":
                depth -= 1
                if depth == 0:
                    if k > 0 and s[k - 1] == "^":
                        i = k - 2
                    break
        else:
            return None
        while i >= 0 and s[i] == " ":
            i -= 1
    elif s[i].isdigit() and i > 0 and s[i - 1] == "^":
        i -= 2
        while i >= 0 and s[i] == " ":
            i -= 1
    if i < 0 or s[i] != ")":
        return None
    depth = 0
    for k in range(i, -1, -1):
        if s[k] == ")":
            depth += 1
        elif s[k] == "(":
            depth -= 1
            if depth == 0:
                # \sin(x)/x — имя функции входит в числитель.
                name = k - 1
                while name >= 0 and s[name].isalpha():
                    name -= 1
                if name < k - 1:
                    func = s[name + 1 : k]
                    if s[name] == "\\" or func in {
                        "sin", "cos", "tan", "cot", "ln", "log", "lim", "exp", "sqrt",
                    }:
                        k = name + 1 if s[name] != "\\" else name
                return k, end
    return None


def _atom_after(s: str, slash: int) -> tuple[int, int] | None:
    j = slash + 1
    n = len(s)
    while j < n and s[j] == " ":
        j += 1
    if j >= n:
        return None
    if s[j] == "(":
        end = _closing_paren(s, j)
        if end is None:
            return None
        return j, end + 1
    k = j
    if s[k] == "\\":
        k += 1
        while k < n and s[k].isalpha():
            k += 1
        if k < n and s[k] == "(":
            end = _closing_paren(s, k)
            if end is not None:
                k = end + 1
    else:
        while k < n and (s[k].isalnum() or s[k] in "^_"):
            if s[k] in "^_" and k + 1 < n and s[k + 1] == "{":
                end = s.find("}", k + 2)
                if end < 0:
                    break
                k = end + 1
                continue
            if s[k] in "^_":
                break
            k += 1
    if k == j:
        return None
    return j, k


def _simple_num_before(s: str, slash: int) -> tuple[int, int] | None:
    """Число или буква сразу перед «/», но не показатель степени ^2/."""
    i = slash - 1
    while i >= 0 and s[i] == " ":
        i -= 1
    if i < 0 or (i > 0 and s[i - 1] == "^"):
        return None
    end = i + 1
    while i >= 0 and (s[i].isalnum() or s[i] in "._"):
        i -= 1
    if i + 1 >= end:
        return None
    return i + 1, end


def _unwrap_parens(body: str) -> str:
    body = body.strip()
    if len(body) >= 2 and body[0] == "(" and body[-1] == ")":
        if _closing_paren(body, 0) == len(body) - 1:
            return body[1:-1].strip()
    return body


def _balanced_fractions(s: str) -> str:
    """(x^2-4)/(x-2) и ((3+h)^2-9)/h, в том числе со скобками внутри."""
    for _ in range(8):
        updated = s
        for i, ch in enumerate(s):
            if ch != "/":
                continue
            left = _group_ending_at(s, i)
            right = _atom_after(s, i)
            if left is None:
                # 1/(x-2) и 3/((x-1)/(x+1)): числитель — число, знаменатель в скобках.
                if right is None or s[right[0]] != "(":
                    continue
                left = _simple_num_before(s, i)
            if left is None or right is None:
                continue
            if right is None:
                continue
            num = _unwrap_parens(s[left[0] : left[1]])
            den = _unwrap_parens(s[right[0] : right[1]])
            if not num or not den:
                continue
            updated = s[: left[0]] + f"\\frac{{{num}}}{{{den}}}" + s[right[1] :]
            break
        if updated == s:
            break
        s = updated
    # x/\sin(x) — слева нет скобок, справа функция.
    s = re.sub(
        r"([A-Za-z0-9]+(?:\^\{[^}]+\})?)\s*/\s*\\([A-Za-z]+)\(([^()]*)\)",
        lambda m: f"\\frac{{{m.group(1)}}}{{\\{m.group(2)}({m.group(3)})}}",
        s,
    )
    return s


def enhance_latex(s: str) -> str:
    s = normalize_llm_math(s)
    s = s.replace("→", r" \to ")
    s = re.sub(r"->", r" \\to ", s)
    s = re.sub(r"(?<![\\A-Za-z])lim(?![A-Za-z])", r"\\lim", s)
    s = s.replace("∫", r"\int ")
    for uni, pair in _UNI_FRAC.items():
        s = s.replace(uni, f"\\frac{{{pair[0]}}}{{{pair[1]}}}")
    s = _brace_script_fractions(s)
    s = _strip_cyrillic_math(s)
    for uni, latex in _GREEK_UNICODE.items():
        s = s.replace(uni, latex)
    s = _separate_commands(s)
    s = _unicode_superscripts_to_caret(s)
    # LLM/шрифты: \√{8}, √{8}, √(8)
    s = s.replace("\\√", "\\sqrt")
    s = re.sub(r"√\s*\{([^}]+)\}", r"\\sqrt{\1}", s)
    s = _radical_parens(s)
    s = re.sub(r"√([A-Za-z0-9/]+)", r"\\sqrt{\1}", s)
    s = _plain_subscripts_to_latex(s)
    s = _normalize_logs(s)
    s = _radical_parens(s)
    s = re.sub(
        r"(?<![\\A-Za-z])(arcsin|arccos|arctan|sqrt|sin|cos|tan|cot|sec|csc|ln|exp)"
        r"(?=\s*[\(\^])",
        lambda m: "\\" + m.group(1),
        s,
        flags=re.I,
    )
    s = s.replace("*", r" \cdot ")
    s = s.replace("·", r" \cdot ").replace("×", r" \times ").replace("∙", r" \cdot ")
    s = _with_script_placeholders(s, _balanced_fractions)
    s = _with_script_placeholders(s, _indexed_fracs)
    s = slashes_to_frac(s)
    s = _with_script_placeholders(s, _command_fractions)
    s = _paren_powers(s)
    s = re.sub(r"\\int\s+_", r"\\int_", s)
    s = re.sub(
        r"\\int\s*\[\s*([^,\]]+)\s*,\s*([^\]]+)\]",
        lambda m: f"\\int_{{{m.group(1).strip()}}}^{{{m.group(2).strip()}}}",
        s,
    )
    # Слово pi → \pi, но не внутри \pi и не в epsilon.
    s = re.sub(r"(?<![\\A-Za-z])pi(?![A-Za-z])", r"\\pi", s)
    s = s.replace(r"\sin^2", r"\sin^{2}").replace(r"\cos^2", r"\cos^{2}")
    s = s.replace(r"\tan^2", r"\tan^{2}")
    s = _brace_bare_powers(s)
    # 4^{1/2} — дробь в степени, а не строка «1/2» над строкой.
    s = re.sub(
        r"\^\{([^{}]+)/([^{}]+)\}",
        lambda m: "^{\\frac{" + m.group(1) + "}{" + m.group(2) + "}}",
        s,
    )
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _formula_worthy(s: str) -> bool:
    """Числа через запятую — не формула. Так на карточке не остаётся «4, 10» вместо условия."""
    t = (s or "").strip()
    if not t:
        return False
    # Голое число из индекса a_{12} не считаем формулой.
    if re.fullmatch(r"\d+(?:[.,]\d+)?", t):
        return False
    # Одна буква — это «найдите x», а не формула на всю колонку.
    if re.fullmatch(r"\\?[A-Za-z]", t):
        return False
    if re.search(r"\\[a-zA-Z]+", t):
        return True
    if re.search(r"[A-Za-z]", t):
        return True
    return bool(re.search(r"[\^_=<>√!]|[+\-*/]", t))


def _mostly_math(s: str) -> bool:
    t = s.strip()
    if not t:
        return False
    if re.search(r"[а-яА-ЯёЁ]", t):
        return False
    if re.search(r"\\[a-zA-Z]+", t):
        return True
    return bool(_MATH_CHARS.match(t))


def _find_equation_in_text(s: str) -> tuple[str, str] | None:
    """Русский текст + формула вида a = F/m в конце."""
    matches = list(_EQUATION_IN_TEXT.finditer(s))
    if not matches:
        return None
    m = matches[-1]
    eq = m.group(1).strip().rstrip(".")
    if not re.search(r"[=+/^\\]", eq):
        return None
    prefix = s[: m.start()].strip().rstrip(",:;—–-").strip()
    return prefix, eq


def _extract_math_expr(s: str) -> str | None:
    s = s.strip()
    if _mostly_math(s):
        return s

    mixed = _find_equation_in_text(s)
    if mixed:
        return mixed[1]

    # Индексы v_1 / a_{12} целиком, до того как число 12 выдернут отдельно.
    sub = re.search(
        r"([A-Za-z](?:_\{[A-Za-z0-9]+\}|_[A-Za-z0-9]+)"
        r"(?:\s*=\s*[0-9]+(?:[.,][0-9]+)?)?)",
        s,
    )
    if sub:
        return sub.group(1).strip()

    frac = _NUM_FRAC.search(s)
    if frac:
        start = frac.start()
        end = frac.end()
        while end < len(s):
            tail = s[end:]
            more = re.match(r"\s*[\+\-\·]\s*\d+\s*/\s*\d+", tail)
            if not more:
                more = re.match(r"\s*[\+\-\·]\s*[A-Za-z0-9]+\s*/\s*[A-Za-z0-9]+", tail)
            if not more:
                break
            end += more.end()
        return s[start:end].strip()

    m = re.search(
        r"([A-Za-z0-9](?:[A-Za-z0-9_^²³/\(\)\+\-\*·×=<>\\.]|\{[A-Za-z0-9]+\})*)",
        s,
    )
    if m:
        chunk = m.group(1).strip()
        if re.fullmatch(r"\d+(?:[.,]\d+)?", chunk):
            return None
        if len(chunk) >= 2 and re.search(r"[=+/^\\*_]", chunk):
            return chunk
    m = re.search(r"([0-9xX+\-*/()^=<>\\.]+)", s)
    if m:
        chunk = m.group(1).strip()
        # Не вырывать «12» из a_{12} или одиночные числа из русского текста.
        if re.fullmatch(r"\d+(?:[.,]\d+)?", chunk):
            return None
        if len(chunk) >= 2 and re.search(r"[=<>^*/]", chunk):
            return chunk
    return None


_MATH_START = re.compile(
    r"(?:lim|arcsin|arccos|arctan|sqrt|sin|cos|tan|cot|sec|csc|ln|log|exp)"
    r"(?=\s*[₀₁₂₃₄₅₆₇₈₉_(\^\d])"
    r"|[A-Za-z](?=\s*(?:_\{|[₀₁₂₃₄₅₆₇₈₉_⁰¹²³⁴⁵⁶⁷⁸⁹('^]))"
    r"|\d+(?=\s*[\^/])"
    r"|[A-Za-z]\s*="
    r"|[π√∫]"
    r"|\(",
    re.I,
)


def _chunk_is_formula(chunk: str) -> bool:
    """Обычное «x=1» в фразе не формула. sin^2, π/2, f(x)=…, v_1 — формула."""
    if re.search(r"[\^π√∫\\*/'⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉]", chunk):
        return True
    if re.search(r"[A-Za-z]_\{?[A-Za-z0-9]", chunk):
        return True
    if re.search(
        r"arcsin|arccos|arctan|sqrt|sin|cos|tan|cot|sec|csc|ln|log|exp",
        chunk,
        re.I,
    ):
        return True
    return bool(re.search(r"[A-Za-z]\s*\([^)]*\)\s*=", chunk))


def _consume_math(s: str, i: int) -> int:
    """Конец выражения: скобки сбалансированы, русские слова не захватываем."""
    depth = 0
    brace = 0
    j = i
    n = len(s)
    while j < n:
        ch = s[j]
        if "а" <= ch.lower() <= "я" or ch in "ёЁ":
            break
        if ch == "{":
            brace += 1
            j += 1
            continue
        if ch == "}":
            if brace == 0:
                break
            brace -= 1
            j += 1
            continue
        if ch == "(":
            depth += 1
            j += 1
            continue
        if ch == ")":
            if depth == 0:
                break
            depth -= 1
            j += 1
            continue
        if depth == 0 and brace == 0 and ch in ".,;:?!":
            prev = s[j - 1] if j else ""
            nxt = s[j + 1] if j + 1 < n else ""
            # 0.5 и 0,5 — часть основания, не конец формулы.
            if ch in ".," and prev.isdigit() and nxt.isdigit():
                j += 1
                continue
            break
        if ch == " ":
            nxt = s[j + 1] if j + 1 < n else ""
            # «·» и «×» между логарифмами — часть одного выражения, не новая строка.
            if depth > 0 or brace > 0 or nxt in "·×∙⋅" or re.match(
                r"[A-Za-z0-9π√∫₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹(\+\-\^*/=<>≤≥≠_]", nxt
            ):
                j += 1
                continue
            break
        j += 1
    return j


def _include_leading_terms(s: str, start: int) -> int:
    """12 - 3 + (1/2) целиком, а не только дробь в скобках."""
    i = start
    while i > 0:
        k = i - 1
        while k >= 0 and s[k] == " ":
            k -= 1
        if k < 0 or s[k] not in "+-−–":
            break
        p = k - 1
        while p >= 0 and s[p] == " ":
            p -= 1
        if p < 0 or not s[p].isdigit():
            break
        while p > 0 and (s[p - 1].isdigit() or s[p - 1] in ".,"):
            p -= 1
        if p > 0 and ("а" <= s[p - 1].lower() <= "я" or s[p - 1] in "ёЁ"):
            break
        i = p
    return i


def _math_spans(plain: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        match = _MATH_START.search(plain, cursor)
        if not match:
            break
        start = match.start()
        if start > 0 and plain[start - 1].isascii() and plain[start - 1].isalpha():
            cursor = start + 1
            continue
        if start > 0 and plain[start - 1] == "\\":
            start -= 1
        start = _include_leading_terms(plain, start)
        # Коэффициент перед буквой: 2x^2, 3x^2-7x-6=0. Иначе «2» остаётся текстом.
        while start > 0 and plain[start - 1].isdigit():
            start -= 1
        end = _consume_math(plain, start)
        chunk = plain[start:end].strip()
        if end > start and _chunk_is_formula(chunk):
            spans.append((start, end))
            cursor = end
        else:
            cursor = start + 1
    return spans


def _parts_from_sentence(plain: str) -> list[tuple[str, str]] | None:
    """Формулы внутри фразы — отдельные куски, не текст с крышками."""
    spans = _math_spans(plain)
    if not spans:
        return None
    parts: list[tuple[str, str]] = []
    last = 0
    for start, end in spans:
        words = plain[last:start]
        words = re.sub(r"\s+", " ", words).strip()
        words = re.sub(r"\s+([,.;:?!])", r"\1", words)
        if parts and parts[-1][0] == "math":
            words = words.lstrip(".,; ")
        if words:
            parts.append(("text", words))
        formula = plain[start:end].strip().rstrip(",.;:")
        enhanced = enhance_latex(formula)
        if _formula_worthy(enhanced):
            parts.append(("math", enhanced))
        elif formula:
            parts.append(("text", formula))
        last = end
    tail = re.sub(r"\s+", " ", plain[last:]).strip()
    tail = re.sub(r"\s+([,.;:?!])", r"\1", tail)
    if parts and parts[-1][0] == "math":
        tail = tail.lstrip(".,; ")
    if tail:
        parts.append(("text", tail))
    if not any(kind == "math" for kind, _ in parts):
        return None
    return parts


_MATH_TAIL = re.compile(
    r"((?:\([^()]*\)|[A-Za-z0-9]+)"
    r"(?:\s*[\^+\-*/=]\s*(?:\([^()]*\)|[A-Za-z0-9]+))+)\s*\.?\s*$"
)


def _words_and_formula(plain: str) -> tuple[str, str]:
    """Формулу из $...$ или из хвоста a^2-16 забираем в картинку, из фразы убираем."""
    matches = [m for m in _DOLLAR_MATH.finditer(plain) if m.group(1).strip()]
    if matches:
        # В картинку идёт первая формула. Остальные ($a=2$, $x<0$) остаются в тексте,
        # иначе они слипаются в одну чужую формулу и съедают слова между ними.
        parts: list[str] = []
        last = 0
        for i, match in enumerate(matches):
            parts.append(plain[last : match.start()])
            if i:
                parts.append(match.group(1).strip())
            last = match.end()
        parts.append(plain[last:])
        words = re.sub(r"\s+", " ", "".join(parts))
        words = re.sub(r"\s*:\s*,\s*", ", ", words)
        words = re.sub(r"\s+([,.;:?!])", r"\1", words)
        words = words.strip().rstrip(":;,. ").strip()
        return words, matches[0].group(1).strip()
    match = _MATH_TAIL.search(plain)
    if match and re.search(r"[\^\\/]", match.group(1)):
        formula = match.group(1).strip()
        words = plain[: match.start()].strip().rstrip(":;,. ").strip()
        return words, formula
    return plain, ""


def _split_dollar_math(plain: str) -> TaskDisplay | None:
    """GigaChat иногда кладёт LaTeX в text как $...$."""
    if "$" not in plain:
        return None
    chunks = [p.strip() for p in _DOLLAR_MATH.findall(plain) if p.strip()]
    if not chunks:
        prefix = plain.replace("$", "").strip()
        return TaskDisplay(prefix=prefix, latex="") if prefix else None
    latex = ", ".join(chunks)
    prefix = _DOLLAR_MATH.sub(" ", plain)
    prefix = re.sub(r"\s+", " ", prefix).strip().rstrip(":;,").strip()
    return TaskDisplay(prefix=prefix, latex=enhance_latex(latex))


def prepare_task_display(text: str | None, text_latex: str | None) -> TaskDisplay:
    plain = (text or "").strip()
    latex = (text_latex or "").strip()
    # Все $...$ остаются в фразе. Иначе в картинку попадает только первое «$x$»,
    # а уравнение log_x(16)=4 остаётся сырым текстом.
    if "$" in plain:
        plain = _DOLLAR_MATH.sub(lambda m: " " + m.group(1).strip() + " ", plain)
        plain = re.sub(r"\s+", " ", plain).strip()
    if plain:
        parts = _parts_from_sentence(plain)
        if parts:
            texts = [piece for kind, piece in parts if kind == "text"]
            maths = [piece for kind, piece in parts if kind == "math"]
            return TaskDisplay(
                prefix=" ".join(texts).strip(),
                latex=maths[0] if maths else "",
                parts=parts,
            )

    words, inline = _words_and_formula(plain) if plain else ("", "")

    # Условие на русском иногда приходит в text_latex. После вырезания кириллицы
    # от него остаются только числа, и ученик видит «4, 10» вместо вопроса.
    if latex and re.search(r"[а-яА-ЯёЁ]", latex) and not inline:
        question = latex_to_unicode_fallback(latex)
        question = re.sub(r"\\[a-zA-Z]+", " ", question)
        question = question.replace("{", "").replace("}", "").replace("$", "")
        question = re.sub(r"\s+", " ", question).strip()
        if len(question) > len(plain):
            plain = question
            words, inline = _words_and_formula(plain)
        latex = ""

    # Формула из условия важнее text_latex: там она совпадает с заданием.
    formula_src = inline or latex
    if formula_src:
        enhanced = enhance_latex(formula_src)
        if _formula_worthy(enhanced):
            prefix = ""
            if words and not _mostly_math(words):
                prefix = words
            elif not words and plain and not _mostly_math(plain) and not inline:
                prefix = plain.rstrip(":").strip()
            return TaskDisplay(prefix=prefix, latex=enhanced)

    if not plain:
        return TaskDisplay(prefix="", latex="")

    plain = words or plain

    dollar = _split_dollar_math(plain)
    if dollar and dollar.prefix:
        plain = dollar.prefix

    if _mostly_math(plain):
        enhanced = enhance_latex(plain)
        if _formula_worthy(enhanced):
            return TaskDisplay(prefix="", latex=enhanced)
        return TaskDisplay(prefix=plain, latex="")

    mixed = _find_equation_in_text(plain)
    if mixed:
        prefix, eq = mixed
        return TaskDisplay(prefix=prefix, latex=enhance_latex(eq))

    # Длинное условие на русском не разбираем на «формулу» из отдельных чисел.
    if re.search(r"[а-яА-ЯёЁ]", plain) and len(plain) > 25:
        return TaskDisplay(prefix=plain, latex="")

    expr = _extract_math_expr(plain)
    if expr:
        enhanced = enhance_latex(expr)
        prefix = plain.replace(expr, "", 1).strip().rstrip(":").strip()
        return TaskDisplay(prefix=prefix, latex=enhanced)

    return TaskDisplay(prefix=plain, latex="")


def _take_brace(s: str, i: int) -> tuple[str, int] | None:
    """s[i] == '{'. Возвращает содержимое и индекс сразу после закрывающей скобки."""
    if i >= len(s) or s[i] != "{":
        return None
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1 : j], j + 1
    return None


def split_definite_integral(latex: str) -> tuple[str, str, str] | None:
    """Если пределы интеграла — дроби, их нельзя оставлять в индексе: там они слишком мелкие."""
    s = latex.strip()
    if not s.startswith(r"\int"):
        return None
    i = 4
    lower = upper = ""
    if i < len(s) and s[i] == "_":
        got = _take_brace(s, i + 1)
        if not got:
            return None
        lower, i = got
    if i < len(s) and s[i] == "^":
        got = _take_brace(s, i + 1)
        if not got:
            return None
        upper, i = got
    if r"\frac" not in lower and r"\frac" not in upper:
        return None
    return lower, upper, s[i:].strip()


def split_task_text(text: str | None, text_latex: str | None) -> tuple[str, str]:
    """Совместимость: (plain, latex) для отрисовки."""
    d = prepare_task_display(text, text_latex)
    if d.latex:
        return d.prefix, d.latex
    return d.prefix or (text or "").strip(), ""


def enrich_task_math(task) -> object:
    """Нормализует text_latex; при необходимости вытаскивает формулу из text."""
    from models import TaskItem

    display = prepare_task_display(task.text, task.text_latex)
    updates: dict = {}
    if display.latex:
        updates["text_latex"] = display.latex
    if updates:
        return task.model_copy(update=updates)
    return task


def _render_latex_to_png_uncached(
    latex: str, fontsize: int = 11, dpi: int = 160
) -> bytes | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams["mathtext.fontset"] = "dejavusans"
    except ImportError:
        return None

    try:
        fig = plt.figure(figsize=(0.01, 0.01))
        fig.patch.set_alpha(0)
        text = fig.text(0, 0, f"${latex}$", fontsize=fontsize)
        fig.canvas.draw()
        bbox = text.get_window_extent(renderer=fig.canvas.get_renderer())
        width = bbox.width / dpi + 0.16
        height = bbox.height / dpi + 0.16
        plt.close(fig)

        fig = plt.figure(figsize=(max(width, 0.5), max(height, 0.28)), dpi=dpi)
        fig.patch.set_alpha(0)
        text = fig.text(0.02, 0.5, f"${latex}$", fontsize=fontsize, va="center")
        text.set_clip_on(False)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", transparent=True, bbox_inches="tight", pad_inches=0.03)
        plt.close(fig)
        return _opaque_formula(buf.getvalue())
    except Exception:
        logger.exception("Не удалось нарисовать формулу: %s", latex)
        plt.close("all")
        return None


@lru_cache(maxsize=256)
def render_latex_to_png(latex: str, fontsize: int = 11, dpi: int = 160) -> bytes | None:
    latex = enhance_latex(latex)
    if not latex:
        return None
    # PDF уже собирается в отдельном потоке. Вложенный таймаут обрывал картинку
    # и подставлял плоский текст без степеней.
    return _render_latex_to_png_uncached(latex, fontsize, dpi)


def latex_to_pdf_text(text: str) -> str:
    """Текст формулы для PDF обычным шрифтом: log_3, не log₃ (глиф пропадает)."""
    s = normalize_llm_math(text or "")
    s = re.sub(r"\\frac\{([^}]+)\}\{([^}]+)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\sqrt\{([^}]+)\}", r"sqrt(\1)", s)
    s = re.sub(r"\\log_\{([^}]+)\}", r"log_\1", s)
    s = re.sub(r"\\log_(\w+)", r"log_\1", s)
    s = s.replace("\\ln", "ln").replace("\\log", "log")
    s = s.replace("\\cdot", "·").replace("\\times", "×")
    s = s.replace("\\leq", "<=").replace("\\geq", ">=").replace("\\neq", "!=")

    def _pdf_exponent(body: str) -> str:
        body = body.strip()
        if re.fullmatch(r"\d", body):
            return _sup(body)
        if re.fullmatch(r"[A-Za-z0-9]", body):
            return "^" + body
        return "^(" + body + ")"

    s = re.sub(r"\^\{([^{}]+)\}", lambda m: _pdf_exponent(m.group(1)), s)
    s = re.sub(r"\^(?!\()([A-Za-z0-9])", lambda m: _pdf_exponent(m.group(1)), s)
    s = re.sub(r"_\{([^}]+)\}", r"_\1", s)
    s = re.sub(r"\\[a-zA-Z]+", "", s)
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def latex_to_unicode_fallback(text: str) -> str:
    """Если PNG не собрался — хотя бы читаемый текст (дроби остаются a/b)."""
    s = normalize_llm_math(text)
    s = re.sub(r"\\frac\{([^}]+)\}\{([^}]+)\}", r"\1/\2", s)
    s = re.sub(r"\\sqrt\{([^}]+)\}", r"√(\1)", s)
    s = s.replace("\\sqrt", "√").replace("\\cdot", "·").replace("\\times", "×")
    s = s.replace("\\int", "∫").replace("\\ln", "ln")
    s = re.sub(r"\\log_\{([^}]+)\}", lambda m: f"log{_sub(m.group(1))}", s)
    s = re.sub(r"\\log_(\w+)", lambda m: f"log{_sub(m.group(1))}", s)
    s = s.replace("\\log", "log")
    s = re.sub(r"\\[a-zA-Z]+", "", s)
    s = re.sub(r"_\{([^}]+)\}", lambda m: _sub(m.group(1)), s)
    s = re.sub(r"_(\w)", lambda m: _sub(m.group(1)), s)
    s = re.sub(r"\^\{([^}]+)\}", lambda m: _sup(m.group(1)), s)
    s = re.sub(r"\^(\d)", lambda m: _sup(m.group(1)), s)
    s = s.replace("{", "").replace("}", "")
    return s


_SUB_LETTERS = str.maketrans(
    "aehijklmnoprstuvx",
    "ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
)


def _sup(chars: str) -> str:
    mapping = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾")
    return chars.translate(mapping)


def _sub(chars: str) -> str:
    mapping = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
    return chars.translate(mapping).translate(_SUB_LETTERS)


def _frac_messenger(num: str, den: str) -> str:
    return f"{_sup(num.strip())}⁄{_sub(den.strip())}"


def format_math_for_messenger(text: str) -> str:
    """Дроби и степени обычным текстом, без LaTeX."""
    if not text:
        return text
    s = text.strip()
    s = re.sub(
        r"\\frac\{([^}]+)\}\{([^}]+)\}",
        lambda m: _frac_messenger(m.group(1), m.group(2)),
        s,
    )
    s = re.sub(
        r"(?<![⁄/])(\d+)\s*/\s*(\d+)(?![⁄/])",
        lambda m: _frac_messenger(m.group(1), m.group(2)),
        s,
    )
    s = s.replace("\\cdot", "·").replace("\\times", "×").replace("\\leq", "≤")
    s = s.replace("\\geq", "≥").replace("\\neq", "≠")
    s = re.sub(r"\\sqrt\{([^}]+)\}", r"√(\1)", s)
    s = re.sub(
        r"\\log_\{([^}]+)\}",
        lambda m: f"log{_sub(m.group(1))}",
        s,
    )
    s = re.sub(r"\\log_(\d+)", lambda m: f"log{_sub(m.group(1))}", s)
    s = re.sub(r"\^\{([^}]+)\}", lambda m: _sup(m.group(1)), s)
    s = re.sub(r"\^(\d+)", lambda m: _sup(m.group(1)), s)
    s = re.sub(r"_\{([^}]+)\}", lambda m: _sub(m.group(1)), s)
    s = re.sub(r"_(\d+)", lambda m: _sub(m.group(1)), s)
    s = re.sub(r"\$+", "", s)
    return s
