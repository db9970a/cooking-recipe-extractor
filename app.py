import os
import re
import json
import base64
import shutil
import tempfile
import datetime
import cv2
import urllib3
import requests
import cloudscraper
import streamlit as st
import anthropic
import yt_dlp
from bs4 import BeautifulSoup
from fpdf import FPDF
from streamlit_javascript import st_javascript

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Cooking Video Recipe Extractor",
    page_icon="🍳",
    layout="wide",
)

# ── Constants ─────────────────────────────────────────────────────────────────
_LS_KEY = "recipe_history_v1"

STARS = ["☆☆☆☆☆", "★☆☆☆☆", "★★☆☆☆", "★★★☆☆", "★★★★☆", "★★★★★"]

RECIPE_TAGS = [
    "Breakfast", "Lunch", "Dinner", "Snack", "Dessert", "Baking",
    "Quick (<30 min)", "Vegetarian", "Vegan", "Gluten-free", "Dairy-free",
    "Seafood", "Chicken", "Beef", "Pork", "Pasta", "Soup", "Salad",
]

# ── Unicode font detection (runs once at import time) ─────────────────────────
def _find_unicode_font() -> tuple[str, dict[str, str]] | None:
    """Return (family_name, {style: path}) for a Unicode TTF font, or None."""
    # Windows — Arial
    win = "C:/Windows/Fonts"
    win_paths: dict[str, str] = {
        "":   os.path.join(win, "arial.ttf"),
        "B":  os.path.join(win, "arialbd.ttf"),
        "I":  os.path.join(win, "ariali.ttf"),
        "BI": os.path.join(win, "arialbi.ttf"),
    }
    if all(os.path.exists(p) for p in win_paths.values()):
        return "Arial", win_paths

    # Linux — DejaVu Sans
    dv = "/usr/share/fonts/truetype/dejavu"
    dv_paths: dict[str, str] = {
        "":   os.path.join(dv, "DejaVuSans.ttf"),
        "B":  os.path.join(dv, "DejaVuSans-Bold.ttf"),
        "I":  os.path.join(dv, "DejaVuSans-Oblique.ttf"),
        "BI": os.path.join(dv, "DejaVuSans-BoldOblique.ttf"),
    }
    if all(os.path.exists(p) for p in dv_paths.values()):
        return "DejaVu", dv_paths

    # Linux — Liberation Sans
    lib = "/usr/share/fonts/truetype/liberation"
    lib_paths: dict[str, str] = {
        "":   os.path.join(lib, "LiberationSans-Regular.ttf"),
        "B":  os.path.join(lib, "LiberationSans-Bold.ttf"),
        "I":  os.path.join(lib, "LiberationSans-Italic.ttf"),
        "BI": os.path.join(lib, "LiberationSans-BoldItalic.ttf"),
    }
    if all(os.path.exists(p) for p in lib_paths.values()):
        return "Liberation", lib_paths

    return None

_UNICODE_FONT = _find_unicode_font()

# ── History (browser localStorage) ────────────────────────────────────────────
def _write_ls(history: list) -> None:
    """Write history list to localStorage with UTF-8-safe base64 encoding."""
    st.session_state["_history"] = history
    b64 = base64.b64encode(json.dumps(history, ensure_ascii=False).encode()).decode()
    # TextDecoder handles UTF-8 correctly; bare atob() produces Latin-1 only
    st_javascript(
        f"localStorage.setItem('{_LS_KEY}', "
        f"new TextDecoder().decode(Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0)))); 1"
    )

def load_history() -> list:
    raw = st_javascript(f"localStorage.getItem('{_LS_KEY}')")
    if raw and raw != 0:
        try:
            history = json.loads(raw)
            st.session_state["_history"] = history
            return history
        except Exception:
            pass
    return st.session_state.get("_history", [])

def save_to_history(
    title: str, url: str, recipe: str, thumbnail: str = "",
    tags: list[str] | None = None, rating: int = 0,
) -> None:
    history = list(st.session_state.get("_history", []))
    entry = {
        "title": title,
        "url": url,
        "recipe": recipe,
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "thumbnail": thumbnail,
        "tags": tags or [],
        "rating": rating,
        "notes": "",
    }
    history = [h for h in history if h.get("url") != url]
    history.insert(0, entry)
    _write_ls(history)

def update_history_entry(url: str, **kwargs) -> None:
    """Patch specific fields of a saved recipe entry by URL."""
    history = list(st.session_state.get("_history", []))
    for entry in history:
        if entry.get("url") == url:
            entry.update(kwargs)
            break
    _write_ls(history)

def delete_history_entry(url: str) -> None:
    history = [h for h in load_history() if h.get("url") != url]
    _write_ls(history)


# ── PDF generation ────────────────────────────────────────────────────────────
@st.cache_data
def generate_pdf(title: str, recipe_markdown: str) -> bytes:
    """Render a recipe as a Cooks Illustrated-style PDF."""

    def _sanitize(text: str) -> str:
        """Fallback: replace common Unicode chars with Latin-1 equivalents."""
        replacements = {
            "\u2022": "-", "\u2013": "-", "\u2014": "-",
            "\u2018": "'", "\u2019": "'",
            "\u201c": '"', "\u201d": '"',
            "\u2026": "...", "\u2192": "->",
            "\u00bc": "1/4", "\u00bd": "1/2", "\u00be": "3/4",
        }
        for char, repl in replacements.items():
            text = text.replace(char, repl)
        return text.encode("latin-1", errors="replace").decode("latin-1")

    if _UNICODE_FONT:
        F, font_paths = _UNICODE_FONT
    else:
        F = "Helvetica"
        font_paths = None
        title = _sanitize(title)
        recipe_markdown = _sanitize(recipe_markdown)

    NAVY  = (26,  45,  75)
    RED   = (175, 30,  30)
    GRAY  = (100, 100, 100)
    LGRAY = (240, 240, 240)

    pdf = FPDF()
    pdf.set_margins(22, 22, 22)
    pdf.set_auto_page_break(auto=True, margin=22)

    if font_paths:
        for style, path in font_paths.items():
            pdf.add_font(F, style, path)

    pdf.add_page()
    PAGE_W = pdf.w - pdf.l_margin - pdf.r_margin

    def font(style: str = "", size: int = 11) -> None:
        pdf.set_font(F, style=style, size=size)

    def color(rgb: tuple[int, int, int]) -> None:
        pdf.set_text_color(*rgb)

    def mc(text: str, style: str = "", size: int = 11,
           indent: int = 0, lh: float = 5.8, align: str = "L") -> None:
        font(style, size)
        w = PAGE_W - indent
        if indent:
            pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(w, lh, text, align=align,
                       new_x="LMARGIN", new_y="NEXT", markdown=True)

    def hrule(r: int, g: int, b: int, thickness: float = 0.3) -> None:
        pdf.set_draw_color(r, g, b)
        pdf.set_line_width(thickness)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + PAGE_W, pdf.get_y())

    # Title bar
    pdf.set_fill_color(*NAVY)
    color((255, 255, 255))
    font("B", 7)
    pdf.cell(PAGE_W, 6, "RECIPE", align="C", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(0.5)
    pdf.set_fill_color(*NAVY)
    font("B", 18)
    pdf.multi_cell(PAGE_W, 10, title.upper(), align="C",
                   new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(6)
    color((0, 0, 0))

    lines = recipe_markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("# "):
            pdf.ln(2)
            color(RED)
            font("BI", 15)
            pdf.multi_cell(PAGE_W, 8, line[2:].strip(), align="L",
                           new_x="LMARGIN", new_y="NEXT")
            hrule(*RED, thickness=0.6)
            pdf.ln(4)
            color((0, 0, 0))

        elif line.startswith("## "):
            pdf.ln(4)
            pdf.set_fill_color(*LGRAY)
            color(NAVY)
            font("B", 10)
            pdf.cell(PAGE_W, 7, f"  {line[3:].strip().upper()}", align="L",
                     new_x="LMARGIN", new_y="NEXT", fill=True)
            pdf.ln(2)
            color((0, 0, 0))

        elif line.startswith("### "):
            pdf.ln(2)
            color(GRAY)
            mc(line[4:].strip().upper(), style="B", size=9)
            hrule(*GRAY, thickness=0.2)
            pdf.ln(2)
            color((0, 0, 0))

        elif line in ("---", "***", "___"):
            pdf.ln(2)
            hrule(180, 180, 180)
            pdf.ln(3)

        elif re.match(r'^[-*]\s+', line):
            text = re.sub(r'^[-*]\s+', '', line)
            color(RED)
            font("B", 11)
            pdf.set_x(pdf.l_margin + 2)
            pdf.cell(5, 5.8, "-")
            color((0, 0, 0))
            font("", 11)
            pdf.set_x(pdf.l_margin + 7)
            pdf.multi_cell(PAGE_W - 7, 5.8, text,
                           new_x="LMARGIN", new_y="NEXT", markdown=True)

        elif re.match(r'^\d+\.\s+', line):
            m = re.match(r'^(\d+)\.\s+(.*)', line)
            num, text = m.group(1), m.group(2)
            color(RED)
            font("B", 11)
            pdf.set_x(pdf.l_margin)
            pdf.cell(10, 5.8, f"{num}.", align="R")
            color((0, 0, 0))
            font("", 11)
            pdf.set_x(pdf.l_margin + 12)
            pdf.multi_cell(PAGE_W - 12, 5.8, text,
                           new_x="LMARGIN", new_y="NEXT", markdown=True)

        elif line == "":
            pdf.ln(2)

        else:
            color((0, 0, 0))
            mc(line, size=11, lh=5.8)

        i += 1

    return bytes(pdf.output())


# ── Anthropic client ──────────────────────────────────────────────────────────
@st.cache_resource
def get_anthropic_client() -> anthropic.Anthropic | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return anthropic.Anthropic(api_key=api_key)


# ── Claude Haiku helpers ──────────────────────────────────────────────────────
def generate_shopping_list(client: anthropic.Anthropic, recipe: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=(
            "You are a helpful kitchen assistant. Given a recipe, extract all ingredients "
            "and organize them as a shopping list grouped by supermarket section "
            "(Produce, Dairy & Eggs, Meat & Seafood, Pantry, Spices & Condiments, Frozen, Other). "
            "Format as a markdown checklist with ## section headers and '- [ ] item' lines. "
            "Output only the shopping list, nothing else."
        ),
        messages=[{"role": "user", "content": f"Recipe:\n\n{recipe}"}],
    )
    return resp.content[0].text


def scale_recipe(client: anthropic.Anthropic, recipe: str,
                 from_servings: int, to_servings: int) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=(
            "You are a recipe scaling assistant. Adjust all ingredient quantities "
            "proportionally when scaling from one serving size to another. "
            "Keep the same markdown structure and wording — only change the numbers. "
            "Update the Servings line to reflect the new count. "
            "Return only the rescaled recipe, no preamble or explanation."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Scale this recipe from {from_servings} to {to_servings} servings.\n\n"
                f"{recipe}"
            ),
        }],
    )
    return resp.content[0].text


# ── Video / web helpers ───────────────────────────────────────────────────────
def extract_frames(video_path: str, max_frames: int = 30) -> list[str]:
    """Extract evenly-spaced frames from a video as base64-encoded JPEGs."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    interval = max(1, total_frames // max_frames)
    frames_b64: list[str] = []
    frame_idx = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % interval == 0:
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            frames_b64.append(base64.standard_b64encode(buf).decode("utf-8"))
            if len(frames_b64) >= max_frames:
                break
        frame_idx += 1
    cap.release()
    return frames_b64


def _parse_vtt(vtt: str) -> str:
    """Strip WEBVTT formatting and return deduplicated plain text."""
    lines: list[str] = []
    for line in vtt.splitlines():
        if "-->" in line or line.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        clean = re.sub(r'<[^>]+>', '', line).strip()
        if clean and not clean.isdigit():
            lines.append(clean)
    deduped: list[str] = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)
    return " ".join(deduped)


def fetch_transcript(url: str) -> str:
    """Fetch auto-generated or manual captions. Returns empty string if unavailable."""
    tmpdir = tempfile.mkdtemp()
    try:
        ydl_opts = {
            "skip_download": True,
            "writeautomaticsub": True,
            "writesubtitles": True,
            "subtitleslangs": ["en", "en-US", "en-GB"],
            "subtitlesformat": "vtt",
            "outtmpl": os.path.join(tmpdir, "sub"),
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        for fname in os.listdir(tmpdir):
            if fname.endswith(".vtt"):
                with open(os.path.join(tmpdir, fname), encoding="utf-8") as f:
                    return _parse_vtt(f.read())[:8000]  # cap at ~2k tokens
    except Exception:
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return ""


def download_video(url: str) -> tuple[str | None, str]:
    """Download video. Returns (file_path, tmpdir); file_path is None on failure."""
    tmpdir = tempfile.mkdtemp()
    try:
        ydl_opts = {
            "format": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/worst",
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(tmpdir, "video.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        files = os.listdir(tmpdir)
        if files:
            return os.path.join(tmpdir, files[0]), tmpdir
    except Exception:
        pass
    return None, tmpdir


def get_video_metadata(url: str) -> dict:
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", "Unknown title"),
                "description": (info.get("description") or "")[:3000],
                "uploader": info.get("uploader", ""),
                "duration": info.get("duration_string", ""),
                "thumbnail": info.get("thumbnail", ""),
            }
    except Exception as e:
        return {"title": "", "description": "", "uploader": "", "duration": "", "error": str(e)}


def _extract_json_ld_recipe(soup) -> str:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            flat: list = []
            for item in items:
                flat.extend(item["@graph"]) if "@graph" in item else flat.append(item)
            for item in flat:
                if item.get("@type") in ("Recipe", "schema:Recipe"):
                    return _json_ld_recipe_to_text(item)
        except (json.JSONDecodeError, AttributeError):
            continue
    return ""


def _json_ld_recipe_to_text(recipe: dict) -> str:
    lines: list[str] = []
    if name := recipe.get("name"):
        lines.append(f"Recipe: {name}\n")
    if desc := recipe.get("description"):
        lines.append(f"Description: {desc}\n")
    if yield_ := recipe.get("recipeYield"):
        lines.append(f"Yield: {yield_ if isinstance(yield_, str) else ', '.join(str(y) for y in yield_)}")
    for time_key, label in [("prepTime", "Prep"), ("cookTime", "Cook"), ("totalTime", "Total")]:
        if t := recipe.get(time_key):
            lines.append(f"{label} time: {t}")
    if ingredients := recipe.get("recipeIngredient"):
        lines.append("\nIngredients:")
        for ing in ingredients:
            lines.append(f"- {ing}")
    if instructions := recipe.get("recipeInstructions"):
        lines.append("\nInstructions:")
        if isinstance(instructions, str):
            lines.append(instructions)
        else:
            for i, step in enumerate(instructions, 1):
                if isinstance(step, str):
                    lines.append(f"{i}. {step}")
                elif isinstance(step, dict):
                    text = step.get("text", step.get("name", ""))
                    if text:
                        lines.append(f"{i}. {text}")
    for key in ("notes", "note"):
        if notes := recipe.get(key):
            lines.append(f"\nNotes: {notes}")
            break
    return "\n".join(lines)


_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def _get_html(url: str) -> str:
    """
    Fetch raw HTML using progressively more permissive strategies:
      1. Standard requests (fast, works for most sites)
      2. cloudscraper (bypasses Cloudflare / 403 bot-detection)
      3. requests with SSL verification disabled (fixes handshake failures)
    Any non-SSL, non-403 HTTP error is raised immediately.
    """
    # Strategy 1: standard requests
    try:
        r = requests.Session().get(url, headers=_FETCH_HEADERS, timeout=20)
        r.raise_for_status()
        return r.text
    except requests.exceptions.SSLError:
        pass  # fall through to strategies 2 & 3
    except requests.exceptions.HTTPError as e:
        if e.response is None or e.response.status_code != 403:
            raise  # genuine error — surface it

    # Strategy 2: cloudscraper (handles Cloudflare JS challenges and 403s)
    try:
        r = cloudscraper.create_scraper().get(url, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception:
        pass  # fall through to strategy 3

    # Strategy 3: disable SSL verification (fixes servers with bad TLS configs)
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    r = requests.Session().get(url, headers=_FETCH_HEADERS, timeout=20, verify=False)
    r.raise_for_status()
    return r.text


def fetch_webpage_text(url: str) -> tuple[str, str]:
    html = _get_html(url)
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    page_title = title_tag.get_text(strip=True) if title_tag else ""
    json_ld_text = _extract_json_ld_recipe(soup)
    if json_ld_text:
        return page_title, json_ld_text
    for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                     "iframe", "noscript", "form", "button", "meta", "link",
                     "advertisement", "banner"]):
        tag.decompose()
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"class": re.compile(r"recipe|content|post|entry|article", re.I)})
        or soup.find("body")
    )
    raw_text = main.get_text(separator="\n", strip=True) if main else soup.get_text(separator="\n", strip=True)
    cleaned = "\n".join(l.strip() for l in raw_text.splitlines() if l.strip())
    if len(cleaned) > 40_000:
        cleaned = cleaned[:40_000] + "\n\n[Page content truncated]"
    return page_title, cleaned


# ── System prompts ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a professional recipe writer. You watch cooking videos and extract
complete, accurate recipes from them.

Watch the provided frames carefully. If a transcript is also provided, use it alongside
the visuals to capture spoken-only details (quantities, temperatures, timings, tips).

Produce a well-formatted recipe including:

1. **Dish name** — clear and descriptive
2. **Overview** — 1-2 sentences describing the dish
3. **Servings**, **Prep time**, **Cook time**, **Total time**
4. **Ingredients** — with precise quantities and any prep notes (e.g. "diced", "at room temperature")
5. **Instructions** — clear numbered steps matching exactly what is shown/heard in the video
6. **Chef's tips** — any useful tips, variations, or substitutions shown or mentioned
7. **Storage** — how to store leftovers (if mentioned or relevant)

Use markdown formatting. If a quantity is hard to determine precisely, give your best visual
estimate and note it with "(approx)". If the video does not appear to be a cooking video, say so.
"""

WEBSITE_SYSTEM_PROMPT = """You are a professional recipe writer. You are given the raw text
scraped from a recipe website, which may include lots of irrelevant content such as ads,
navigation menus, comments, related articles, and cookie notices.

Your job is to find the actual recipe buried in the text and rewrite it as a clean,
complete recipe. Ignore everything that is not part of the recipe.

Produce a well-formatted recipe including:

1. **Dish name** — clear and descriptive
2. **Overview** — 1-2 sentences describing the dish
3. **Servings**, **Prep time**, **Cook time**, **Total time**
4. **Ingredients** — with precise quantities and any prep notes (e.g. "diced", "at room temperature")
5. **Instructions** — clear numbered steps
6. **Chef's tips** — any useful tips, variations, or substitutions mentioned on the page
7. **Storage** — how to store leftovers (if mentioned or relevant)

Use markdown formatting. If no recipe can be found in the text, say so clearly.
"""


# ── Dialog (must be defined at module level, before any call sites) ───────────
@st.dialog("📖 Recipe", width="large")
def show_recipe_dialog() -> None:
    """Full-screen view of a saved recipe with notes, rating, downloads, and shopping list."""
    idx = st.session_state.get("_dialog_idx", 0)
    history = st.session_state.get("_history", [])
    if idx >= len(history):
        st.error("Recipe not found.")
        return
    entry = history[idx]

    if entry.get("thumbnail"):
        st.image(entry["thumbnail"], use_container_width=True)
    st.caption(f"Saved {entry['date']}")

    if entry.get("tags"):
        st.caption("  ".join(f"`{t}`" for t in entry["tags"]))

    st.markdown(entry["recipe"])
    st.divider()

    # ── Notes ────────────────────────────────────────────────
    st.markdown("**My Notes**")
    st.text_area(
        "Notes",
        key="dlg_notes",
        height=120,
        label_visibility="collapsed",
        placeholder="Add personal notes, substitutions you tried, how it turned out...",
    )

    # ── Rating ───────────────────────────────────────────────
    st.markdown("**Rating**")
    st.select_slider("Rating", options=STARS, key="dlg_rating", label_visibility="collapsed")

    if st.button("💾 Save Notes & Rating", type="primary", key="dlg_save"):
        update_history_entry(
            entry["url"],
            notes=st.session_state.get("dlg_notes", ""),
            rating=STARS.index(st.session_state.get("dlg_rating", STARS[0])),
        )
        st.rerun()

    st.divider()

    # ── Downloads ────────────────────────────────────────────
    st.markdown("**Export**")
    dl_col1, dl_col2 = st.columns(2)
    with dl_col1:
        pdf_bytes = generate_pdf(entry["title"], entry["recipe"])
        st.download_button(
            "🖨️ PDF",
            data=pdf_bytes,
            file_name=f"{entry['title'][:50].replace('/', '-')}.pdf",
            mime="application/pdf",
            use_container_width=True,
            key="dlg_pdf",
        )
    with dl_col2:
        st.download_button(
            "📄 TXT",
            data=entry["recipe"],
            file_name=f"{entry['title'][:50].replace('/', '-')}.txt",
            mime="text/plain",
            use_container_width=True,
            key="dlg_txt",
        )

    st.divider()

    # ── Shopping list ─────────────────────────────────────────
    st.markdown("**Shopping List**")

    # Generate if pending (set by button click on previous render)
    if st.session_state.get("_dlg_shop_pending"):
        client = get_anthropic_client()
        if client:
            with st.spinner("Generating shopping list..."):
                st.session_state["dlg_shopping_list"] = generate_shopping_list(client, entry["recipe"])
        st.session_state.pop("_dlg_shop_pending", None)

    if sl := st.session_state.get("dlg_shopping_list"):
        st.markdown(sl)
        shop_col1, shop_col2 = st.columns(2)
        with shop_col1:
            st.download_button(
                "📋 Download",
                data=sl,
                file_name=f"{entry['title'][:40]}_shopping.txt",
                mime="text/plain",
                use_container_width=True,
                key="dlg_shop_dl",
            )
        with shop_col2:
            if st.button("↺ Regenerate", use_container_width=True, key="dlg_shop_regen"):
                st.session_state["_dlg_shop_pending"] = True
    else:
        if st.button("🛒 Generate Shopping List", use_container_width=True, key="dlg_shop_btn"):
            st.session_state["_dlg_shop_pending"] = True


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("📖 Recipe History")
    history = load_history()

    if not history:
        st.caption("No recipes saved yet. Extract one to get started!")
    else:
        search = st.text_input(
            "Search", placeholder="🔍  Search recipes...", label_visibility="collapsed"
        )

        all_tags = sorted({tag for h in history for tag in h.get("tags", [])})
        tag_filter: list[str] = []
        if all_tags:
            tag_filter = st.multiselect(
                "Filter by tag", all_tags,
                label_visibility="collapsed",
                placeholder="Filter by tag...",
            )

        filtered = history
        if search:
            filtered = [h for h in filtered if search.lower() in h.get("title", "").lower()]
        if tag_filter:
            filtered = [h for h in filtered if any(t in h.get("tags", []) for t in tag_filter)]

        count_label = f"{len(filtered)} recipe{'s' if len(filtered) != 1 else ''}"
        if len(filtered) != len(history):
            count_label += f" of {len(history)}"
        st.caption(count_label)

        for entry in filtered:
            idx = history.index(entry)
            with st.container(border=True):
                st.markdown(
                    f"**{entry['title'][:38]}{'…' if len(entry['title']) > 38 else ''}**"
                )
                meta: list[str] = [entry["date"]]
                if entry.get("rating"):
                    meta.append(STARS[entry["rating"]])
                st.caption("  ·  ".join(meta))
                if entry.get("tags"):
                    st.caption("  ".join(f"`{t}`" for t in entry["tags"]))

                col_view, col_del = st.columns([3, 1])
                with col_view:
                    if st.button("View", key=f"view_{idx}", use_container_width=True):
                        st.session_state["_dialog_idx"] = idx
                        # Seed dialog widgets with current entry values
                        st.session_state["dlg_notes"] = entry.get("notes", "")
                        st.session_state["dlg_rating"] = STARS[entry.get("rating", 0)]
                        # Clear shopping list from any previous dialog
                        for k in ("dlg_shopping_list", "_dlg_shop_pending"):
                            st.session_state.pop(k, None)
                        show_recipe_dialog()
                with col_del:
                    if st.button("🗑️", key=f"del_{idx}", use_container_width=True):
                        delete_history_entry(entry["url"])
                        st.rerun()


# ── Main area ─────────────────────────────────────────────────────────────────
st.title("🍳 Cooking Recipe Extractor")
st.markdown("Extract a clean, readable recipe from a cooking video **or** a cluttered recipe website.")

tab_video, tab_web = st.tabs(["🎬  From a Video", "🌐  From a Website"])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 1 — VIDEO
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with tab_video:
    st.markdown("Paste a TikTok, YouTube, Instagram, or other cooking video link.")
    with st.form("url_form"):
        url = st.text_input("Video URL", placeholder="https://www.tiktok.com/@...")
        submitted = st.form_submit_button("Extract Recipe from Video", type="primary")

    if submitted and url.strip():
        url = url.strip()
        tmpdir: str | None = None
        try:
            with st.status("Fetching video...", expanded=True) as status:
                st.write("Retrieving video metadata...")
                metadata = get_video_metadata(url)

                if "error" in metadata and not metadata["title"]:
                    status.update(label="Could not fetch video", state="error")
                    st.error(f"Unable to access the video: {metadata['error']}\n\nMake sure the URL is valid and the video is public.")
                    st.stop()

                if metadata.get("title"):
                    st.write(f"Found: **{metadata['title']}**")

                st.write("Fetching transcript (captions)...")
                transcript = fetch_transcript(url)
                if transcript:
                    st.write(f"Transcript found ({len(transcript.split()):,} words).")
                else:
                    st.write("No transcript available — using frames only.")

                st.write("Downloading video...")
                video_path, tmpdir = download_video(url)

                if not video_path:
                    status.update(label="Could not download video", state="error")
                    st.error("Unable to download this video. Make sure the URL is valid and the video is public.")
                    st.stop()

                file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
                st.write(f"Video downloaded ({file_size_mb:.1f} MB). Extracting frames...")
                frames = extract_frames(video_path)
                if not frames:
                    status.update(label="Could not read video frames", state="error")
                    st.error("Downloaded the video but could not extract frames. The file may be corrupted.")
                    st.stop()
                st.write(f"Extracted {len(frames)} frames.")
                status.update(label="Video ready!", state="complete")

            client = get_anthropic_client()
            if not client:
                st.error("ANTHROPIC_API_KEY environment variable is not set.")
                st.stop()

            st.markdown("---")
            if metadata.get("title"):
                st.subheader(f"Recipe from: {metadata['title']}")

            recipe_placeholder = st.empty()
            full_recipe = ""
            title_hint = f" titled \"{metadata['title']}\"" if metadata.get("title") else ""
            transcript_block = (
                [{"type": "text",
                  "text": f"Video transcript/captions (auto-generated, may contain errors):\n\n{transcript}"}]
                if transcript else []
            )

            with st.spinner("Claude is watching the video and writing the recipe..."):
                try:
                    with client.messages.stream(
                        model="claude-opus-4-6",
                        max_tokens=4096,
                        system=SYSTEM_PROMPT,
                        messages=[{
                            "role": "user",
                            "content": [
                                *[{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": f}} for f in frames],
                                *transcript_block,
                                {"type": "text", "text": f"These are {len(frames)} frames extracted in order from a cooking video{title_hint}. Please extract the complete recipe."},
                            ],
                        }],
                    ) as stream:
                        for text_chunk in stream.text_stream:
                            full_recipe += text_chunk
                            recipe_placeholder.markdown(full_recipe + "▌")
                    recipe_placeholder.markdown(full_recipe)
                except anthropic.AuthenticationError:
                    st.error("Invalid API key. Please check your ANTHROPIC_API_KEY.")
                except anthropic.RateLimitError:
                    st.error("Rate limit reached. Please wait a moment and try again.")
                except anthropic.APIError as e:
                    st.error(f"API error: {e}")

            if full_recipe:
                st.session_state["pending_recipe"] = {
                    "recipe": full_recipe,
                    "title": metadata.get("title", "Untitled"),
                    "url": url,
                    "thumbnail": metadata.get("thumbnail", ""),
                }
                st.session_state["recipe_filename"] = re.sub(r'[\\/*?:"<>|]', "", metadata.get("title", "Untitled"))[:80]
                st.rerun()

        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    elif submitted:
        st.warning("Please enter a video URL.")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 2 — WEBSITE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with tab_web:
    st.markdown(
        "Paste the URL of any recipe webpage. The AI will ignore the ads, menus, and clutter "
        "and pull out just the recipe."
    )
    with st.form("web_form"):
        web_url = st.text_input("Website URL", placeholder="https://www.seriouseats.com/...")
        web_submitted = st.form_submit_button("Extract Recipe from Website", type="primary")

    if web_submitted and web_url.strip():
        web_url = web_url.strip()

        client = get_anthropic_client()
        if not client:
            st.error("ANTHROPIC_API_KEY environment variable is not set.")
            st.stop()

        with st.status("Fetching page...", expanded=True) as status:
            try:
                st.write("Loading the page...")
                page_title, page_text = fetch_webpage_text(web_url)
                st.write(f"Page loaded ({len(page_text):,} characters). Looking for the recipe...")
                status.update(label="Page ready!", state="complete")
            except requests.exceptions.HTTPError as e:
                status.update(label="Could not load page", state="error")
                st.error(f"Unable to fetch the page: {e}\n\nMake sure the URL is correct and the site is publicly accessible.")
                st.stop()
            except requests.exceptions.RequestException as e:
                status.update(label="Could not load page", state="error")
                st.error(f"Unable to fetch the page: {type(e).__name__}: {e}\n\nMake sure the URL is correct and the site is publicly accessible.")
                st.stop()
            except Exception as e:
                status.update(label="Could not load page", state="error")
                st.error(f"Unexpected error fetching page: {type(e).__name__}: {e}")
                st.stop()

        st.markdown("---")
        if page_title:
            st.subheader(f"Recipe from: {page_title}")

        recipe_placeholder = st.empty()
        full_recipe = ""

        with st.spinner("Claude is reading the page and writing the recipe..."):
            try:
                with client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=WEBSITE_SYSTEM_PROMPT,
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Here is the text content scraped from this recipe page:\n"
                            f"URL: {web_url}\n\n"
                            f"{page_text}"
                        ),
                    }],
                ) as stream:
                    for text_chunk in stream.text_stream:
                        full_recipe += text_chunk
                        recipe_placeholder.markdown(full_recipe + "▌")
                recipe_placeholder.markdown(full_recipe)
            except anthropic.AuthenticationError:
                st.error("Invalid API key. Please check your ANTHROPIC_API_KEY.")
            except anthropic.RateLimitError:
                st.error("Rate limit reached. Please wait a moment and try again.")
            except anthropic.APIError as e:
                st.error(f"API error: {e}")

        if full_recipe:
            display_title = page_title or web_url
            st.session_state["pending_recipe"] = {
                "recipe": full_recipe,
                "title": display_title,
                "url": web_url,
                "thumbnail": "",
            }
            st.session_state["recipe_filename"] = re.sub(r'[\\/*?:"<>|]', "", display_title)[:80]
            st.rerun()

    elif web_submitted:
        st.warning("Please enter a website URL.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PENDING RECIPE AREA  (shared by both tabs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if st.session_state.get("pending_recipe"):
    pending = st.session_state["pending_recipe"]

    # Reset editor when a new recipe arrives (different URL)
    if st.session_state.get("_editor_url") != pending["url"]:
        st.session_state["recipe_editor"] = pending["recipe"]
        st.session_state["_editor_url"] = pending["url"]
        st.session_state.pop("_shop_list_pending", None)

    st.markdown("---")
    st.subheader(f"Recipe from: {pending['title']}")

    # Editable recipe — this value flows into all downstream operations
    edited_recipe: str = st.text_area(
        "Edit recipe",
        key="recipe_editor",
        height=500,
        label_visibility="collapsed",
        help="Edit the extracted recipe here. All exports and saves use this version.",
    )

    # ── Scale recipe ──────────────────────────────────────────────────────────
    with st.expander("⚖️ Scale Recipe"):
        col_from, col_to = st.columns(2)
        with col_from:
            from_servings = st.number_input("Current servings", min_value=1, value=4, key="scale_from")
        with col_to:
            to_servings = st.number_input("Scale to", min_value=1, value=4, key="scale_to")
        if st.button("Scale Recipe", key="btn_scale"):
            client = get_anthropic_client()
            if not client:
                st.error("ANTHROPIC_API_KEY environment variable is not set.")
            elif int(from_servings) == int(to_servings):
                st.info("From and to servings are the same — nothing to scale.")
            else:
                with st.spinner(f"Scaling from {int(from_servings)} to {int(to_servings)} servings..."):
                    scaled = scale_recipe(client, edited_recipe, int(from_servings), int(to_servings))
                st.session_state["recipe_editor"] = scaled
                st.session_state["pending_recipe"]["recipe"] = scaled
                st.rerun()

    # ── Shopping list ─────────────────────────────────────────────────────────
    with st.expander("🛒 Shopping List"):
        if st.button("Generate Shopping List", key="btn_shop_pending"):
            client = get_anthropic_client()
            if not client:
                st.error("ANTHROPIC_API_KEY environment variable is not set.")
            else:
                with st.spinner("Generating shopping list..."):
                    st.session_state["_shop_list_pending"] = generate_shopping_list(client, edited_recipe)
        if sl := st.session_state.get("_shop_list_pending"):
            st.markdown(sl)
            st.download_button(
                "📋 Download Shopping List",
                data=sl,
                file_name="shopping_list.txt",
                mime="text/plain",
                key="dl_shop_pending",
            )

    st.markdown("---")

    # ── Save / download box ───────────────────────────────────────────────────
    with st.container(border=True):
        st.subheader("Save Recipe")
        filename = st.text_input("File name", key="recipe_filename")

        chosen_tags: list[str] = st.multiselect("Tags", RECIPE_TAGS, key="pending_tags")
        rating_str: str = st.select_slider(
            "Rating", options=STARS, value=STARS[0], key="pending_rating"
        )
        rating_idx = STARS.index(rating_str)

        col1, col2, col3 = st.columns(3)
        with col1:
            pdf_bytes = generate_pdf(pending["title"], edited_recipe)
            st.download_button(
                "🖨️ Save as PDF",
                data=pdf_bytes,
                file_name=f"{filename}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        with col2:
            st.download_button(
                "📄 Save as TXT",
                data=edited_recipe,
                file_name=f"{filename}.txt",
                mime="text/plain",
                use_container_width=True,
            )
        with col3:
            if st.button("📚 Add to Collection", use_container_width=True, type="primary"):
                try:
                    save_to_history(
                        pending["title"], pending["url"],
                        edited_recipe, pending["thumbnail"],
                        tags=chosen_tags, rating=rating_idx,
                    )
                    for key in ("pending_recipe", "recipe_filename", "recipe_editor",
                                "_editor_url", "_shop_list_pending",
                                "pending_tags", "pending_rating"):
                        st.session_state.pop(key, None)
                    st.toast("Recipe saved to your collection!", icon="✅")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to save recipe: {type(e).__name__}: {e}")
