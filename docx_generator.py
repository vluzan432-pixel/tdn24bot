"""
docx_generator.py — збирає .docx «Щоденник практики» та «Звіт про проходження практики»
з дотриманням вимог кафедри (шрифти, інтервали, поля, шапка таблиці, додатки).

Вхід — прості словники/списки (профіль, налаштування групи, записи, секції звіту),
вихід — bytes готового файлу. Нічого не знає про Telegram і базу даних.
"""

import io
import re
from datetime import date, timedelta

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from PIL import Image, ImageOps

FONT = "Times New Roman"

# Титулка за офіційним зразком кафедри (вимоги для 3 курсу): угорі по центру
# «Київський національний університет культури і мистецтв / Кафедра музичного мистецтва»,
# нижче назва документа, далі поля зліва, внизу по центру «Київ-Рік».
# Якщо шапку вгорі прибрати (як у деяких студентських робіт) — постав False.
TITLE_SHOW_UNIVERSITY = True
CITY_SPACE_BEFORE_PT = 375  # відступ перед «Київ – Рік», щоб рядок був унизу сторінки

PLACEHOLDER_RE = re.compile(r"(\[(?:ПЕРЕВІРИТИ|ДОДАТИ)[^\]]*\])")

# ------------------------------------------------------------------ утиліти

_UK_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюяabcdefghijklmnopqrstuvwxyz"


def _uk_key(s: str):
    s = re.sub(r"^[^\w]+", "", s.lower())
    return [(_UK_ALPHABET.find(ch) if ch in _UK_ALPHABET else 100 + ord(ch)) for ch in s]


def _clean(text: str) -> str:
    """Прибирає markdown-залишки, які ШІ інколи додає."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    text = text.replace("*", "")
    return text.strip()


def fmt_date_short(iso: str) -> str:
    """2025-11-24 -> '24.11.25 р.'"""
    d = date.fromisoformat(iso)
    return d.strftime("%d.%m.%y") + " р."


def fmt_date_caption(iso: str) -> str:
    """2025-11-24 -> '24.11.2025р.'"""
    d = date.fromisoformat(iso)
    return d.strftime("%d.%m.%Y") + "р."


def _force_font_on_rpr(rpr, size_pt=None):
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if rfonts.get(qn(attr)) is not None:
            del rfonts.attrib[qn(attr)]
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), FONT)


def _setup_styles(doc, size_pt: float):
    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(size_pt)
    _force_font_on_rpr(normal.element.get_or_add_rPr())
    for name in ("Heading 1", "Heading 2"):
        st = doc.styles[name]
        st.font.name = FONT
        st.font.size = Pt(size_pt)
        st.font.bold = True
        st.font.italic = False
        st.font.color.rgb = None
        from docx.shared import RGBColor
        st.font.color.rgb = RGBColor(0, 0, 0)
        _force_font_on_rpr(st.element.get_or_add_rPr())


def _page_setup(section, left, right, top, bottom):
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(left)
    section.right_margin = Cm(right)
    section.top_margin = Cm(top)
    section.bottom_margin = Cm(bottom)


def _format_par(p, *, align=None, spacing=1.15, first_indent=None, left_indent=None,
                before=0, after=0, keep_next=False):
    pf = p.paragraph_format
    if align is not None:
        p.alignment = align
    pf.line_spacing = spacing
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    if first_indent is not None:
        pf.first_line_indent = Cm(first_indent)
    if left_indent is not None:
        pf.left_indent = Cm(left_indent)
    if keep_next:
        pf.keep_with_next = True


def _add_text(p, text, size, bold=False, italic=False):
    """Додає текст, підсвічуючи жовтим позначки [ПЕРЕВІРИТИ: ...] / [ДОДАТИ: ...]."""
    for chunk in PLACEHOLDER_RE.split(text):
        if not chunk:
            continue
        run = p.add_run(chunk)
        run.font.name = FONT
        run.font.size = Pt(size)
        run.bold = bold
        run.italic = italic
        _force_font_on_rpr(run._r.get_or_add_rPr())
        if PLACEHOLDER_RE.fullmatch(chunk):
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW
    return p


def _par(container, text="", *, size=14, bold=False, italic=False, **fmt):
    p = container.add_paragraph()
    _format_par(p, **fmt)
    if text:
        _add_text(p, text, size, bold=bold, italic=italic)
    return p


def _page_break(doc):
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def _field(run, instr: str):
    for kind, text in (("begin", None), (None, instr), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)


def _prepare_image(raw: bytes, max_w_cm: float, max_h_cm: float):
    """Нормалізує фото (EXIF-поворот, RGB, розумний розмір) і повертає (stream, ширина_см)
    — пропорції зберігаються, спотворення немає."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > 1800:
        img.thumbnail((1800, 1800))
    w_px, h_px = img.size
    ratio = w_px / h_px
    width_cm = min(max_w_cm, max_h_cm * ratio)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    out.seek(0)
    return out, width_cm


def _add_photo_block(doc, raw: bytes, caption: str, caption_size=12):
    try:
        stream, width_cm = _prepare_image(raw, 15.5, 12.5)
    except Exception:
        _par(doc, "[ДОДАТИ: не вдалося обробити фото]", size=12, align=WD_ALIGN_PARAGRAPH.CENTER)
        stream = None
    if stream:
        p = doc.add_paragraph()
        _format_par(p, align=WD_ALIGN_PARAGRAPH.CENTER, spacing=1.0, before=8, after=4, keep_next=True)
        p.add_run().add_picture(stream, width=Cm(width_cm))
    _par(doc, caption, size=caption_size, align=WD_ALIGN_PARAGRAPH.CENTER, spacing=1.15, after=10)


# ------------------------------------------------------------------ титулка

def _eff(profile: dict, cfg: dict, key: str, blank="__________"):
    return (profile.get(key) or cfg.get(key) or "").strip() or blank


def _title_page(doc, profile: dict, cfg: dict, title_lines: list, terms: bool):
    S = dict(size=14, spacing=1.15)
    C = WD_ALIGN_PARAGRAPH.CENTER
    L = WD_ALIGN_PARAGRAPH.LEFT
    if TITLE_SHOW_UNIVERSITY:
        _par(doc, cfg["university"], align=C, **S)
        _par(doc, cfg["department"], align=C, **S)

    for i, line in enumerate(title_lines):
        _par(doc, line["text"], bold=line.get("bold", False), align=C,
             before=(60 if (i == 0 and TITLE_SHOW_UNIVERSITY) else 0), **S)
    _par(doc, "", align=C, **S)
    _par(doc, "", align=C, **S)

    def field(label, value):
        _par(doc, f"{label}{value}", align=L, before=0, after=0, **S)

    field("Здобувача ", profile.get("full_name_gen") or "__________")
    field("Курс ", _eff(profile, cfg, "course"))
    field("Група ", cfg.get("group", ""))
    field("", cfg["department"])
    field("Спеціальність ", cfg["specialty"])
    field("Назва ОПП ", cfg["program"])
    field("Вид практики ", _eff(profile, cfg, "practice_kind"))

    # база практики і колектив: назва бази, кома, перенесення рядка, колектив (як у зразку)
    base = profile.get("practice_base") or "__________"
    collective = (profile.get("collective") or "").strip()
    p = doc.add_paragraph()
    _format_par(p, align=L, **{"spacing": 1.15})
    if collective:
        _add_text(p, f"Назва бази практики {base},", 14)
        p.add_run().add_break()
        _add_text(p, collective, 14)
    else:
        _add_text(p, f"Назва бази практики {base}", 14)

    field("Керівник від бази практик ", profile.get("base_supervisor") or "__________")
    field("Керівник від кафедри ", cfg.get("dept_supervisor") or "__________")
    if terms:
        field("Терміни: ", cfg.get("terms") or "__________")

    space = CITY_SPACE_BEFORE_PT - (30 if terms else 0) - (110 if TITLE_SHOW_UNIVERSITY else 0)
    _par(doc, f"Київ-{cfg['year']}", align=C, before=space, **S)


# ------------------------------------------------------------------ щоденник

def _set_cell_width(cell, cm):
    cell.width = Cm(cm)


def _repeat_header(row):
    trpr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    trpr.append(el)


def _cant_split(row):
    trpr = row._tr.get_or_add_trPr()
    trpr.append(OxmlElement("w:cantSplit"))


# Ширини колонок за зразком, см (разом 16.5 = ширина тексту при полях 30/15 мм)
DIARY_WIDTHS = [1.05, 2.5, 3.0, 9.95]


def _week_start(iso: str) -> date:
    d = date.fromisoformat(iso)
    return d - timedelta(days=d.weekday())


def _fill_cell(cell, text: str, align, size: int = 12):
    """Текст у клітинку: кожен непорожній рядок — окремий абзац (як у зразку кафедри)."""
    lines = [re.sub(r"^[-•–]\s+", "– ", l.strip()) for l in _clean(text).split("\n") if l.strip()]
    for i, line in enumerate(lines or [""]):
        p = cell.paragraphs[0] if i == 0 else cell.add_paragraph()
        _format_par(p, align=align, spacing=1.15, after=(3 if i < len(lines) - 1 else 0))
        _add_text(p, line, size)


def _week_table(doc, week_entries: list):
    """Таблиця одного тижня. Кожен запис — окремий рядок зі своїм номером; усередині однієї
    дати записи нумеруються 1., 2., 3., а нова дата знову починає з 1."""
    headers = ["№ з/п", "Дата", "Перелік виконаних завдань та робот", "Анотація"]
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    table._tbl.tblPr.append(layout)
    for i, col in enumerate(table.columns):
        col.width = Cm(DIARY_WIDTHS[i])

    hdr = table.rows[0]
    _repeat_header(hdr)
    for i, text in enumerate(headers):
        cell = hdr.cells[i]
        _set_cell_width(cell, DIARY_WIDTHS[i])
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        p = cell.paragraphs[0]
        _format_par(p, align=WD_ALIGN_PARAGRAPH.CENTER, spacing=1.15)
        _add_text(p, text, 12)

    per_date = {}  # нумерація 1, 2, 3… починається заново для кожної дати
    for e in week_entries:
        number = per_date[e["entry_date"]] = per_date.get(e["entry_date"], 0) + 1
        row = table.add_row()
        for i in range(4):
            _set_cell_width(row.cells[i], DIARY_WIDTHS[i])
        _fill_cell(row.cells[0], f"{number}.", WD_ALIGN_PARAGRAPH.CENTER)
        _fill_cell(row.cells[1], fmt_date_short(e["entry_date"]), WD_ALIGN_PARAGRAPH.CENTER)
        _fill_cell(row.cells[2], e["tasks"], WD_ALIGN_PARAGRAPH.LEFT)
        _fill_cell(row.cells[3], e["annotation"], WD_ALIGN_PARAGRAPH.JUSTIFY)
    return table


def build_diary(profile: dict, cfg: dict, entries: list) -> bytes:
    """entries: [{'entry_date': 'YYYY-MM-DD', 'tasks': str, 'annotation': str,
                  'photos': [{'bytes': bytes, 'caption': str}, ...]}, ...]
    Записи подаються по тижнях (Пн–Нд): «Тиждень N» + окрема таблиця, як у зразку кафедри."""
    entries = sorted(entries, key=lambda e: e["entry_date"])
    doc = Document()
    _setup_styles(doc, 14)
    _page_setup(doc.sections[0], left=3.0, right=1.5, top=2.0, bottom=2.0)

    _title_page(doc, profile, cfg, [{"text": "ЩОДЕННИК ПРАКТИКИ", "bold": True}], terms=False)

    weeks = {}
    for e in entries:
        weeks.setdefault(_week_start(e["entry_date"]), []).append(e)
    first_week = min(weeks) if weeks else None

    for idx, ws in enumerate(sorted(weeks)):
        number = (ws - first_week).days // 7 + 1  # номер тижня практики за календарем
        p = _par(doc, f"Тиждень {number}", size=12, align=WD_ALIGN_PARAGRAPH.CENTER, spacing=1.0,
                 before=(0 if idx == 0 else 14), after=6, keep_next=True)
        if idx == 0:
            p.paragraph_format.page_break_before = True
        _week_table(doc, weeks[ws])

    if any(e.get("photos") for e in entries):
        _par(doc, "Додатки:", size=14, bold=True, align=WD_ALIGN_PARAGRAPH.LEFT, before=18, after=6, keep_next=True)
        for e in entries:
            for ph in e.get("photos", []):
                _add_photo_block(doc, ph["bytes"], ph["caption"])

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ звіт

REPORT_TOC = [
    (0, "ВСТУП"),
    (0, "РОЗДІЛ І. ЗАГАЛЬНІ ВИДИ ЗАВДАНЬ"),
    (1, "1.1. Робота у творчих колективах"),
    (1, "1.2. Майстер-класи і творчі зустрічі з провідними майстрами мистецтв"),
    (0, "РОЗДІЛ ІІ. ІНДИВІДУАЛЬНІ ЗАВДАННЯ ДО САМОСТІЙНОЇ РОБОТИ"),
    (1, "2.1. Репертуар різних форм і стилів"),
    (1, "2.2. Онлайн-документація професійних мистецьких колективів"),
    (1, "2.3. Методична та інформаційна література з питань менеджменту у мистецькій сфері"),
    (0, "ВИСНОВОК"),
    (0, "ЛІТЕРАТУРА"),
    (0, "ІНФОРМАЦІЙНІ РЕСУРСИ"),
    (0, "ДОДАТКИ"),
]

# схема довідки про колектив / установу (підрозділ 2.2)
INSTITUTION_POINTS = [
    ("year", "Рік створення"),
    ("direction", "Творче спрямування"),
    ("documents", "Опрацьовані законодавчі та нормативні документи, що стосуються функціонування колективу"),
]


def _body(doc, text: str):
    """Основний текст: по ширині, відступ 1.25 см, інтервал 1.5."""
    text = _clean(text)
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        for line in lines:
            line = re.sub(r"^[-•–]\s+", "– ", line)
            _par(doc, line, size=14, align=WD_ALIGN_PARAGRAPH.JUSTIFY, spacing=1.5, first_indent=1.25)


def _heading(doc, text: str, level: int, new_page: bool = False):
    p = doc.add_paragraph(style=f"Heading {level}")
    _format_par(
        p,
        align=WD_ALIGN_PARAGRAPH.CENTER if level == 1 else WD_ALIGN_PARAGRAPH.LEFT,
        spacing=1.5,
        first_indent=(0 if level == 1 else 1.25),
        before=0,
        after=6,
        keep_next=True,
    )
    if new_page:
        p.paragraph_format.page_break_before = True
    _add_text(p, text, 14, bold=True)
    return p


def _numbered(doc, items: list):
    for n, item in enumerate(items, start=1):
        _par(doc, f"{n}. {_clean(item)}", size=14, align=WD_ALIGN_PARAGRAPH.JUSTIFY, spacing=1.5, first_indent=1.25)


def _toc(doc):
    """Зміст: поле TOC зі статичним заповненням. Після оновлення полів у Word
    з'являються номери сторінок (бот просить Word оновити поля при відкритті)."""
    _heading(doc, "ЗМІСТ", 1, new_page=True)
    paras = []
    for level, title in REPORT_TOC:
        p = _par(doc, title, size=14, align=WD_ALIGN_PARAGRAPH.LEFT, spacing=1.5,
                 left_indent=(1.0 if level else 0))
        paras.append(p)

    first_run = paras[0].runs[0]
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    begin.set(qn("w:dirty"), "true")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = ' TOC \\o "1-2" \\h \\z \\u '
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    holder = OxmlElement("w:r")
    for el in (begin, instr, sep):
        holder.append(el)
    first_run._r.addprevious(holder)

    end_run = OxmlElement("w:r")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    end_run.append(end)
    paras[-1]._p.append(end_run)


def _enable_update_fields(doc):
    settings = doc.settings.element
    el = OxmlElement("w:updateFields")
    el.set(qn("w:val"), "true")
    settings.append(el)


def _institution_blocks(doc, institutions: list):
    for n, c in enumerate(institutions, start=1):
        _par(doc, f"{n}. {_clean(str(c.get('name') or '[ДОДАТИ: назва колективу / установи]'))}", size=14, bold=True,
             align=WD_ALIGN_PARAGRAPH.LEFT, spacing=1.5, first_indent=1.25, keep_next=True)
        for key, label in INSTITUTION_POINTS:
            value = _clean(str(c.get(key) or "")) or "[ДОДАТИ: немає даних]"
            _par(doc, f"– {label}: {value}", size=14, align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                 spacing=1.5, first_indent=1.25)


def build_report(profile: dict, cfg: dict, sections: dict, entries: list) -> bytes:
    """sections: intro, s11, s12, s21, s22 {'intro','institutions'[], 'outro'}, s23,
    conclusion, literature [str], resources [str], media_links [str]"""
    entries = sorted(entries, key=lambda e: e["entry_date"])
    doc = Document()
    _setup_styles(doc, 14)
    first = doc.sections[0]
    _page_setup(first, left=3.0, right=1.5, top=2.0, bottom=2.0)

    _title_page(
        doc, profile, cfg,
        [{"text": "ЗВІТ", "bold": True}, {"text": "про проходження практики", "bold": True}],
        terms=True,
    )

    body_sec = doc.add_section(WD_SECTION.NEW_PAGE)
    _page_setup(body_sec, left=3.0, right=1.5, top=2.0, bottom=2.0)
    body_sec.footer.is_linked_to_previous = False
    fp = body_sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = fp.add_run()
    run.font.name = FONT
    run.font.size = Pt(14)
    _field(run, "PAGE")

    # перший абзац нового розділу порожній — прибираємо, щоб ЗМІСТ ішов з верху сторінки
    _toc(doc)
    _enable_update_fields(doc)

    _heading(doc, "ВСТУП", 1, new_page=True)
    _body(doc, sections.get("intro", ""))

    _heading(doc, "РОЗДІЛ І. ЗАГАЛЬНІ ВИДИ ЗАВДАНЬ", 1, new_page=True)
    _heading(doc, "1.1. Робота у творчих колективах", 2)
    _body(doc, sections.get("s11", ""))
    _heading(doc, "1.2. Майстер-класи і творчі зустрічі з провідними майстрами мистецтв", 2)
    _body(doc, sections.get("s12", ""))

    _heading(doc, "РОЗДІЛ ІІ. ІНДИВІДУАЛЬНІ ЗАВДАННЯ ДО САМОСТІЙНОЇ РОБОТИ", 1, new_page=True)
    _heading(doc, "2.1. Репертуар різних форм і стилів", 2)
    _body(doc, sections.get("s21", ""))
    _heading(doc, "2.2. Онлайн-документація професійних мистецьких колективів", 2)
    s22 = sections.get("s22") or {}
    _body(doc, s22.get("intro", ""))
    _institution_blocks(doc, s22.get("institutions") or [])
    if s22.get("outro"):
        _body(doc, s22["outro"])
    _heading(doc, "2.3. Методична та інформаційна література з питань менеджменту у мистецькій сфері", 2)
    _body(doc, sections.get("s23", ""))

    _heading(doc, "ВИСНОВОК", 1, new_page=True)
    _body(doc, sections.get("conclusion", ""))

    _heading(doc, "ЛІТЕРАТУРА", 1, new_page=True)
    lit = sorted([x for x in sections.get("literature", []) if x], key=_uk_key)
    if lit:
        _numbered(doc, lit)
    else:
        _par(doc, "[ДОДАТИ: перелік літератури та нот в алфавітному порядку]", size=14,
             align=WD_ALIGN_PARAGRAPH.LEFT, spacing=1.5, first_indent=1.25)

    _heading(doc, "ІНФОРМАЦІЙНІ РЕСУРСИ", 1, new_page=True)
    res = [x for x in sections.get("resources", []) if x]
    if res:
        _numbered(doc, res)
    else:
        _par(doc, "[ДОДАТИ: веб-сайти та YouTube-джерела: Назва ресурсу: веб-сайт. URL: https://... "
                  "(дата звернення: ДД.ММ.РР р.)]", size=14, align=WD_ALIGN_PARAGRAPH.LEFT,
             spacing=1.5, first_indent=1.25)

    _heading(doc, "ДОДАТКИ", 1, new_page=True)
    media = [x for x in sections.get("media_links", []) if x]
    if media:
        _par(doc, "Посилання на аудіо- та відеоматеріали:", size=14, spacing=1.5, first_indent=1.25)
        _numbered(doc, media)
    any_photo = False
    for e in entries:
        for ph in e.get("photos", []):
            any_photo = True
            _add_photo_block(doc, ph["bytes"], ph["caption"], caption_size=14)
    if not any_photo and not media:
        _par(doc, "[ДОДАТИ: фото-матеріали з підписами (назва події + дата)]", size=14,
             align=WD_ALIGN_PARAGRAPH.LEFT, spacing=1.5, first_indent=1.25)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
