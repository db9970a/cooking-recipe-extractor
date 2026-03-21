import os
import re
import json
import uuid
import base64
import shutil
import tempfile
import datetime
import cv2
import requests
import streamlit as st
import anthropic
import yt_dlp
from bs4 import BeautifulSoup
from fpdf import FPDF
from streamlit_cookies_controller import CookieController

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Cooking Video Recipe Extractor",
    page_icon="🍳",
    layout="wide",
)

# ── User identity (cookie-based) ─────────────────────────────────────────────

_cookie_controller = CookieController()

def get_user_id() -> str:
    if "user_id" not in st.session_state:
        uid = _cookie_controller.get("recipe_user_id")
        if not uid:
            uid = str(uuid.uuid4())
            _cookie_controller.set("recipe_user_id", uid)
        st.session_state["user_id"] = uid
    return st.session_state["user_id"]

# ── History file ─────────────────────────────────────────────────────────────

def _history_file(user_id: str) -> str:
    history_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")
    os.makedirs(history_dir, exist_ok=True)
    return os.path.join(history_dir, f"{user_id}.json")

def load_history(user_id: str) -> list:
    path = _history_file(user_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_to_history(user_id: str, title: str, url: str, recipe: str, thumbnail: str = "") -> None:
    history = load_history(user_id)
    entry = {
        "title": title,
        "url": url,
        "recipe": recipe,
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "thumbnail": thumbnail,
    }
    history = [h for h in history if h.get("url") != url]
    history.insert(0, entry)
    with open(_history_file(user_id), "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def delete_history_entry(user_id: str, url: str) -> None:
    history = load_history(user_id)
    history = [h for h in history if h.get("url") != url]
    with open(_history_file(user_id), "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def generate_pdf(title: str, recipe_markdown: str) -> bytes:
    """Render a recipe as a Cooks Illustrated-style PDF."""

    def _sanitize(text: str) -> str:
        """Replace common Unicode punctuation with Latin-1 equivalents."""
        replacements = {
            "\u2022": "-",   # bullet •
            "\u2013": "-",   # en dash –
            "\u2014": "-",   # em dash —
            "\u2018": "'",   # left single quote '
            "\u2019": "'",   # right single quote '
            "\u201c": '"',   # left double quote "
            "\u201d": '"',   # right double quote "
            "\u2026": "...", # ellipsis …
            "\u2192": "->",  # right arrow →
            "\u00bc": "1/4", # ¼
            "\u00bd": "1/2", # ½
            "\u00be": "3/4", # ¾
        }
        for char, repl in replacements.items():
            text = text.replace(char, repl)
        return text.encode("latin-1", errors="replace").decode("latin-1")

    title = _sanitize(title)
    recipe_markdown = _sanitize(recipe_markdown)

    # ── Colours ──────────────────────────────────────────────────────────────
    NAVY   = (26,  45,  75)   # headings / title bar
    RED    = (175, 30,  30)   # accent (dish-name h1)
    GRAY   = (100, 100, 100)  # secondary text
    LGRAY  = (240, 240, 240)  # light fill

    # ── Setup ─────────────────────────────────────────────────────────────────
    pdf = FPDF()
    pdf.set_margins(22, 22, 22)
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.add_page()
    PAGE_W = pdf.w - pdf.l_margin - pdf.r_margin

    F = "Helvetica"

    def font(style="", size=11):
        pdf.set_font(F, style=style, size=size)

    def color(rgb):
        pdf.set_text_color(*rgb)

    def mc(text, style="", size=11, indent=0, lh=5.8, align="L", fill=False):
        """Multi-cell with optional left indent."""
        font(style, size)
        w = PAGE_W - indent
        if indent:
            pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(w, lh, text, align=align,
                       new_x="LMARGIN", new_y="NEXT",
                       markdown=True, fill=fill)

    def hrule(r, g, b, thickness=0.3):
        pdf.set_draw_color(r, g, b)
        pdf.set_line_width(thickness)
        pdf.line(pdf.l_margin, pdf.get_y(),
                 pdf.l_margin + PAGE_W, pdf.get_y())

    # ── Title bar ─────────────────────────────────────────────────────────────
    pdf.set_fill_color(*NAVY)
    color((255, 255, 255))
    font("B", 7)
    pdf.cell(PAGE_W, 6, "RECIPE", align="C",
             new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(0.5)
    pdf.set_fill_color(*NAVY)
    font("B", 18)
    pdf.multi_cell(PAGE_W, 10, title.upper(), align="C",
                   new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(6)
    color((0, 0, 0))

    # ── Parse markdown line by line ───────────────────────────────────────────
    lines = recipe_markdown.splitlines()
    numbered_idx = 0   # tracks current numbered-list counter

    i = 0
    while i < len(lines):
        raw  = lines[i]
        line = raw.strip()

        # ── H1  (#) ── dish name
        if line.startswith("# "):
            text = line[2:].strip()
            pdf.ln(2)
            color(RED)
            font("BI", 15)
            pdf.multi_cell(PAGE_W, 8, text, align="L",
                           new_x="LMARGIN", new_y="NEXT")
            hrule(*RED, thickness=0.6)
            pdf.ln(4)
            color((0, 0, 0))
            numbered_idx = 0

        # ── H2  (##) ── section headings
        elif line.startswith("## "):
            text = line[3:].strip().upper()
            pdf.ln(4)
            pdf.set_fill_color(*LGRAY)
            color(NAVY)
            font("B", 10)
            pdf.cell(PAGE_W, 7, f"  {text}", align="L",
                     new_x="LMARGIN", new_y="NEXT", fill=True)
            pdf.ln(2)
            color((0, 0, 0))
            numbered_idx = 0

        # ── H3  (###) ── sub-headings
        elif line.startswith("### "):
            text = line[4:].strip()
            pdf.ln(2)
            color(GRAY)
            mc(text.upper(), style="B", size=9)
            hrule(*GRAY, thickness=0.2)
            pdf.ln(2)
            color((0, 0, 0))

        # ── Horizontal rule
        elif line in ("---", "***", "___"):
            pdf.ln(2)
            hrule(180, 180, 180)
            pdf.ln(3)

        # ── Bullet list  (- or *)
        elif re.match(r'^[-*]\s+', line):
            text = re.sub(r'^[-*]\s+', '', line)
            numbered_idx = 0
            # Small square bullet
            color(RED)
            font("B", 11)
            pdf.set_x(pdf.l_margin + 2)
            pdf.cell(5, 5.8, "-")
            color((0, 0, 0))
            font("", 11)
            pdf.set_x(pdf.l_margin + 7)
            pdf.multi_cell(PAGE_W - 7, 5.8, text,
                           new_x="LMARGIN", new_y="NEXT", markdown=True)

        # ── Numbered list  (1.  2.  …)
        elif re.match(r'^\d+\.\s+', line):
            m    = re.match(r'^(\d+)\.\s+(.*)', line)
            num  = m.group(1)
            text = m.group(2)
            numbered_idx = int(num)
            # Bold red number + body text
            color(RED)
            font("B", 11)
            pdf.set_x(pdf.l_margin)
            pdf.cell(10, 5.8, f"{num}.", align="R")
            color((0, 0, 0))
            font("", 11)
            pdf.set_x(pdf.l_margin + 12)
            pdf.multi_cell(PAGE_W - 12, 5.8, text,
                           new_x="LMARGIN", new_y="NEXT", markdown=True)

        # ── Blank line
        elif line == "":
            pdf.ln(2)

        # ── Regular paragraph
        else:
            color((0, 0, 0))
            mc(line, size=11, lh=5.8)

        i += 1

    return bytes(pdf.output())

# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_frames(video_path: str, max_frames: int = 30) -> list[str]:
    """Extract evenly-spaced frames from a video as base64-encoded JPEGs."""
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    interval = max(1, total_frames // max_frames)

    frames_b64 = []
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


def download_video(url: str) -> str | None:
    """Download video to a temp directory and return the file path, or None on failure."""
    try:
        tmpdir = tempfile.mkdtemp()
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
            return os.path.join(tmpdir, files[0])
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None
    except Exception:
        return None


def get_video_metadata(url: str) -> dict:
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": False}
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


SYSTEM_PROMPT = """You are a professional recipe writer. You watch cooking videos and extract
complete, accurate recipes from them.

Watch the provided video carefully and extract every ingredient, quantity, and step you observe.
Produce a well-formatted recipe including:

1. **Dish name** — clear and descriptive
2. **Overview** — 1-2 sentences describing the dish
3. **Servings**, **Prep time**, **Cook time**, **Total time**
4. **Ingredients** — with precise quantities and any prep notes (e.g. "diced", "at room temperature")
5. **Instructions** — clear numbered steps matching exactly what is shown in the video
6. **Chef's tips** — any useful tips, variations, or substitutions shown or mentioned
7. **Storage** — how to store leftovers (if mentioned or relevant)

Use markdown formatting. Only include ingredients and steps you can actually see or hear in the
video. If a quantity is hard to determine precisely, give your best visual estimate and note it
with "(approx)". If the video does not appear to be a cooking video, say so clearly.
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


def fetch_webpage_text(url: str) -> tuple[str, str]:
    """Fetch a webpage and return (page_title, cleaned_text)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Page title
    title_tag = soup.find("title")
    page_title = title_tag.get_text(strip=True) if title_tag else ""

    # Strip boilerplate tags
    for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                     "iframe", "noscript", "form", "button", "meta", "link",
                     "advertisement", "banner"]):
        tag.decompose()

    # Prefer the main content block when available
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"class": re.compile(r"recipe|content|post|entry|article", re.I)})
        or soup.find("body")
    )

    raw_text = main.get_text(separator="\n", strip=True) if main else soup.get_text(separator="\n", strip=True)

    # Collapse blank lines
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    cleaned = "\n".join(lines)

    # Keep to a reasonable token budget (~40 000 chars ≈ ~10 000 tokens)
    if len(cleaned) > 40_000:
        cleaned = cleaned[:40_000] + "\n\n[Page content truncated]"

    return page_title, cleaned


# ── Layout: sidebar (history) + main ─────────────────────────────────────────

user_id = get_user_id()

with st.sidebar:
    st.header("📖 Recipe History")
    history = load_history(user_id)

    if not history:
        st.caption("No recipes saved yet. Extract one to get started!")
    else:
        st.caption(f"{len(history)} recipe{'s' if len(history) != 1 else ''} saved")
        for i, entry in enumerate(history):
            with st.expander(f"**{entry['title'][:45]}{'…' if len(entry['title']) > 45 else ''}**\n\n_{entry['date']}_"):
                if entry.get("thumbnail"):
                    st.image(entry["thumbnail"], use_container_width=True)
                st.markdown(entry["recipe"])
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.download_button(
                        "📄 TXT",
                        data=entry["recipe"],
                        file_name=f"{entry['title'][:50].replace('/', '-')}.txt",
                        mime="text/plain",
                        key=f"dl_{i}",
                    )
                with col2:
                    pdf_bytes = generate_pdf(entry["title"], entry["recipe"])
                    st.download_button(
                        "🖨️ PDF",
                        data=pdf_bytes,
                        file_name=f"{entry['title'][:50].replace('/', '-')}.pdf",
                        mime="application/pdf",
                        key=f"pdf_{i}",
                    )
                with col3:
                    if st.button("🗑️ Delete", key=f"del_{i}", type="secondary"):
                        delete_history_entry(user_id, entry["url"])
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
        video_path = None
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

                st.write("Downloading video...")
                video_path = download_video(url)

                if not video_path:
                    status.update(label="Could not download video", state="error")
                    st.error("Unable to download this video. Make sure the URL is valid and the video is public.")
                    st.stop()

                file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
                st.write(f"Video downloaded ({file_size_mb:.1f} MB). Extracting frames...")
                frames = extract_frames(video_path)
                st.write(f"Extracted {len(frames)} frames.")
                status.update(label="Video ready!", state="complete")

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                st.error("ANTHROPIC_API_KEY environment variable is not set.")
                st.stop()

            client = anthropic.Anthropic(api_key=api_key)
            st.markdown("---")
            if metadata.get("title"):
                st.subheader(f"Recipe from: {metadata['title']}")

            recipe_placeholder = st.empty()
            full_recipe = ""
            title_hint = f" titled \"{metadata['title']}\"" if metadata.get("title") else ""

            with st.spinner("Claude is watching the video and writing the recipe..."):
                try:
                    with client.messages.stream(
                        model="claude-opus-4-6",
                        max_tokens=4096,
                        system=SYSTEM_PROMPT,
                        messages=[{
                            "role": "user",
                            "content": [
                                *[{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame}} for frame in frames],
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
            if video_path:
                shutil.rmtree(os.path.dirname(video_path), ignore_errors=True)

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

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            st.error("ANTHROPIC_API_KEY environment variable is not set.")
            st.stop()

        with st.status("Fetching page...", expanded=True) as status:
            try:
                st.write("Loading the page...")
                page_title, page_text = fetch_webpage_text(web_url)
                st.write(f"Page loaded ({len(page_text):,} characters). Looking for the recipe...")
                status.update(label="Page ready!", state="complete")
            except requests.exceptions.RequestException as e:
                status.update(label="Could not load page", state="error")
                st.error(f"Unable to fetch the page: {e}\n\nMake sure the URL is correct and the site is publicly accessible.")
                st.stop()

        client = anthropic.Anthropic(api_key=api_key)
        st.markdown("---")
        if page_title:
            st.subheader(f"Recipe from: {page_title}")

        recipe_placeholder = st.empty()
        full_recipe = ""

        with st.spinner("Claude is reading the page and writing the recipe..."):
            try:
                with client.messages.stream(
                    model="claude-opus-4-6",
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
# PENDING RECIPE SAVE BOX  (shared by both tabs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if st.session_state.get("pending_recipe"):
    pending = st.session_state["pending_recipe"]

    st.markdown("---")
    st.subheader(f"Recipe from: {pending['title']}")
    st.markdown(pending["recipe"])
    st.markdown("---")

    with st.container(border=True):
        st.subheader("Save Recipe")
        filename = st.text_input("File name", key="recipe_filename")

        col1, col2, col3 = st.columns(3)
        with col1:
            pdf_bytes = generate_pdf(pending["title"], pending["recipe"])
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
                data=pending["recipe"],
                file_name=f"{filename}.txt",
                mime="text/plain",
                use_container_width=True,
            )
        with col3:
            if st.button("📚 Add to Collection", use_container_width=True, type="primary"):
                save_to_history(
                    user_id, pending["title"], pending["url"],
                    pending["recipe"], pending["thumbnail"],
                )
                del st.session_state["pending_recipe"]
                del st.session_state["recipe_filename"]
                st.rerun()
