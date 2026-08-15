"""
Task router — classifies user input to guide model/tool selection.

Returns one of:
  'claude_code'  — clearly a coding/system task; hint qwen to delegate immediately
  'browser'      — browser automation needed; hint qwen to use playwright tools
  'local'        — standard path (qwen + tool loop, no extra hint)

The router is pattern-based (no LLM cost) and adds a system hint to the
context before the main inference — it never bypasses qwen, so SUNI's
personality is always preserved.
"""
from __future__ import annotations
import re
from pathlib import Path

# Complex coding or system tasks that qwen should immediately delegate
_CODING_TASK = re.compile(
    r'\b('
    r'write|create|build|implement|code|program|script|debug|fix|refactor|'
    r'generate\s+(a\s+)?(script|function|class|module|api|report)|'
    r'schedule|configure|install|deploy|set\s+up|setup|'
    r'create\s+(a\s+)?(task|job|service|system|endpoint)|'
    r'update\s+(the\s+)?(config|database|server)'
    r')\b',
    re.IGNORECASE,
)

_TECH_KEYWORD = re.compile(
    r'\b(python|javascript|typescript|html|css|sql|bash|powershell|'
    r'function|class|method|api|endpoint|database|schema|json|yaml|xml|'
    r'scheduled task|schtasks|task scheduler)\b',
    re.IGNORECASE,
)

# ── Browser/web automation signals ───────────────────────────────────────────
_PDF_TASK = re.compile(
    r'\b(pdf|put.{0,30}pdf|create.{0,20}pdf|save.{0,20}pdf|make.{0,20}(pdf|document)|'
    r'generate.{0,20}(pdf|document)|put.{0,30}(document|file))\b',
    re.IGNORECASE,
)

_EMAIL_TASK = re.compile(
    r'\b(send.{0,30}email|email.{0,30}(to|him|her|them|me|it)|'
    r'send.{0,20}(to|him|her|me)|mail.{0,20}(to|him|her|me))\b',
    re.IGNORECASE,
)

# Tasks that need research/download BEFORE emailing — must go through agent loop
_EMAIL_NEEDS_FETCH = re.compile(
    r'\b(image|images|photo|photos|picture|pictures|video|file|files|'
    r'search|find|look\s+up|get\s+me|fetch|download|send\s+me\s+\d+|'
    r'attach.{0,20}(from|web|online|internet))\b',
    re.IGNORECASE,
)

_BROWSER_TASK = re.compile(
    r'\b('
    r'navigate\s+to|'
    r'open\s+(the\s+)?(url|website|page|browser)|'
    r'take\s+a?\s*screenshot|screenshot\s+of|'
    r'click\s+(on\s+)?the|fill\s+(in\s+)?the|'
    r'automate\s+(the\s+)?browser|scrape\s+.{0,30}(page|site|website)|'
    r'go\s+to\s+(the\s+)?(page|website|site|url)|'
    r'go\s+to\s+https?|'
    r'visit\s+(the\s+)?(page|website|site)|'
    r'visit\s+https?|'
    r'browse\s+to|'
    r'check\s+(out\s+)?(the\s+)?(page|website|site)'
    r')\b',
    re.IGNORECASE,
)

_PDF_HINT_TEMPLATE = (
    "[Router: The user wants a PDF. Use the create_pdf tool directly. "
    "Do not use write_file or filesystem_write_file — those cannot create PDFs. "
    "Default path: {output_dir}\\<descriptive_name>.pdf]"
)

_EMAIL_HINT = (
    "[Router: The user wants to send an email. Call send_email tool immediately. "
    "Do not describe the email — call the tool and report the result.]"
)

_CLAUDE_CODE_HINT = (
    "[Router: This is a complex coding or system task. "
    "Use claude_task immediately — do not attempt it directly. "
    "Pass the full task description to claude_task as-is.]"
)

_BROWSER_HINT = (
    "[Router: This task involves browser automation. "
    "Use the playwright_ tools (playwright_navigate, playwright_screenshot, etc.) "
    "to interact with web pages. Ask for clarification if the target URL is unclear.]"
)

# Matches an explicit-scheme URL anywhere in the text
_URL_IN_TEXT = re.compile(r'https?://', re.IGNORECASE)

# Matches a bare domain with no scheme (e.g. "example.com", "acme.co.uk").
# Curated TLD list so we don't misfire on version numbers ("2.0"), file names
# ("index.js", "notes.txt"), or decimals — only real public suffixes count.
_BARE_URL = re.compile(
    r'\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*'
    r'\.(?:com|net|org|io|online|ai|app|dev|co|gov|edu|info|news|site|xyz|pt|uk|eu)\b',
    re.IGNORECASE,
)

_WEB_PDF_HINT = (
    "[Router: The user wants a PDF created from a web page. "
    "Step 1: call web_fetch(url) to retrieve the page content. "
    "Step 2: summarise the fetched content. "
    "Step 3: call create_pdf with the summary. "
    "Do NOT use playwright_navigate or playwright_screenshot — use web_fetch only. "
    "Do NOT use any cached or KB document in place of a live web_fetch — "
    "always fetch the URL first.]"
)

_WEB_URL_HINT = (
    "[Router: The user message contains a URL (or a bare domain like "
    "'example.com'). Use web_fetch(url) to retrieve the page content — "
    "if the URL has no scheme, prefix it with 'https://'. "
    "Do NOT use playwright_navigate or playwright_screenshot. "
    "web_fetch returns the actual page text for summarising or analysing.]"
)

# Questions ABOUT SUNIverse's published articles/news (EN + PT). SUNI is the
# author, so mentions of her articles/notícias/publicações refer to the ingested
# article memory — answer with the get_recent_articles / search_articles tools,
# NOT by web_fetching example.com (it's a JS/canvas SPA with no readable
# article text in the HTML) and NOT from semantic recall (recency queries need
# the tool's id-ordered listing, which RAG cannot reproduce).
_ARTICLES_TASK = re.compile(
    r'\b(suniverse|'
    r'articles?|publications?|headlines?|'
    r'not[ií]cias?|art[ií]gos?|publica[çc][õo]es|manchetes?)\b',
    re.IGNORECASE,
)
# ...unless the user is asking to WRITE/CREATE one (that's a generation task).
_ARTICLE_CREATE = re.compile(
    r'\b(write|create|draft|compose|escreve[r]?|cria[r]?|redige[r]?)\b',
    re.IGNORECASE,
)

_ARTICLES_HINT = (
    "[Router: This is a question about SUNIverse's PUBLISHED articles/news. "
    "Call get_recent_articles(n) for the latest published articles (use this for "
    "'last/latest/recent article' and 'titles' questions), or search_articles(query) "
    "to find specific ones — these read your ingested article memory. Do NOT "
    "web_fetch example.com and do NOT invent titles: call the tool and report "
    "only the actual results it returns.]"
)


class TaskRouter:
    """
    Fast pattern classifier. Call route() to get the strategy, then
    get_hint() to retrieve the system message to inject (or None).
    """

    def route(self, text: str) -> str:
        # Email only takes direct path when it's a simple send (no fetch required)
        if _EMAIL_TASK.search(text) and not _EMAIL_NEEDS_FETCH.search(text):
            return "email"
        # PDF + live URL → must fetch first; skip direct-pdf path
        if _PDF_TASK.search(text) and _URL_IN_TEXT.search(text):
            return "web_pdf"
        if _PDF_TASK.search(text):
            return "pdf"
        if _BROWSER_TASK.search(text):
            return "browser"
        if _CODING_TASK.search(text) and _TECH_KEYWORD.search(text):
            return "claude_code"
        if len(text.split()) > 50 and _CODING_TASK.search(text):
            return "claude_code"
        has_url = bool(_URL_IN_TEXT.search(text) or _BARE_URL.search(text))
        # Questions about SUNIverse's published articles → the article DB tools.
        # Checked BEFORE the URL fallback so "titles on example.com" steers to
        # get_recent_articles, not a web_fetch of the (unreadable) live site.
        # But if the user points at an explicit *external* page (a URL that isn't
        # suniverse), they mean THAT article — let it fall through to web_fetch.
        if _ARTICLES_TASK.search(text) and not _ARTICLE_CREATE.search(text):
            if not (has_url and "suniverse" not in text.lower()):
                return "articles"
        # URL or bare domain present, no other route matched — prefer web_fetch
        if has_url:
            return "web_url"
        return "local"

    def get_hint(self, route: str, output_dir: str = "") -> str | None:
        if route == "pdf":
            _dir = output_dir or str(Path.home() / "Desktop")
            return _PDF_HINT_TEMPLATE.format(output_dir=_dir)
        if route == "web_pdf":
            return _WEB_PDF_HINT
        if route == "email":
            return _EMAIL_HINT
        if route == "claude_code":
            return _CLAUDE_CODE_HINT
        if route == "browser":
            return _BROWSER_HINT
        if route == "web_url":
            return _WEB_URL_HINT
        if route == "articles":
            return _ARTICLES_HINT
        return None
