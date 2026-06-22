# -*- coding: utf-8 -*-
"""
jd_docs.py — follow links to job-description documents and return their text.

The monitor pipeline normally scores a role from the text it can read on the
careers/detail page. Many organisations, though, put the *actual* job
description / person specification in a linked PDF or Word document, leaving the
on-page text thin or generic (e.g. JRF shows ~35 characters; Ceasefire says
"there are no current vacancies" while the real JD sits in a PDF). This module
finds those documents, downloads them through the caller's transport ladder,
extracts their text, and hands it back so it can be appended to the text sent to
the LLM.

It is deliberately self-contained (no import of monitor.py) so there is no
circular dependency. The caller passes in a `fetch_bytes(url) -> bytes` function
so the proven request/curl_cffi/worker ladder is reused for downloads.

Design choices that matter:
  • Detection is by file extension OR by a document-ish URL path (ViewAttachment,
    view_blob, .ashx, /download, …) OR by JD-ish link text — then CONFIRMED by
    magic bytes at download time. This catches JDs served with no extension
    (Goldsmiths' `.view_blob`, Amnesty's octet-stream `.bin`) while discarding
    links that turn out to be ordinary HTML pages.
  • A noise filter drops the boilerplate the audit surfaced — privacy notices,
    equality/diversity monitoring forms, cookie policies, blank application
    forms, "how to apply" guides, pay-gap/annual reports, etc. — so only
    role-relevant content reaches the model.
  • Output is capped (per-document and in total) so a stray large file can't blow
    past the evaluation character budget.

Public API:
  gather_jd_text(scope, page_url, fetch_bytes, *, max_docs, max_total_chars,
                 max_chars_per_doc, log) -> (combined_text, sources)
"""

import io
import os
import re
from urllib.parse import urljoin, urlsplit

# Document file extensions we treat as job-description candidates.
DOC_EXTS = {"pdf", "docx", "doc", "rtf", "odt"}

# URL-path fragments that commonly serve a document behind a non-document URL.
DOCISH_PATH_HINTS = (
    "viewattachment", "view_blob", "/blob", "download", "attachment", "getfile",
    "fetchfile", "downloadfile", "servefile", "fileticket", "/document",
    ".ashx", ".axd", "getdocument", "viewdocument", "/jd",
)

# Link text / filename fragments that strongly indicate a job description and so
# RESCUE an otherwise unrecognised link (extension-less, .aspx, .ashx, …).
JD_INCLUDE = (
    "job description", "job-description", "jobdescription", "job_descrip",
    "jobdescrip", "person spec", "person-spec", "personspec", "person_spec",
    "recruitment pack", "recruitment-pack", "recruitmentpack",
    "job pack", "job-pack", "jobpack", "candidate pack", "candidate-pack",
    "candidatepack", "application pack", "application-pack", "applicationpack",
    "role profile", "role-profile", "roleprofile", "further particulars",
    "role description", "job_pack", "_jd_", "_jd.", "-jd-", "-jd.", "(jd)",
    " jd ", " jd.", "jd ", "duties", "responsibilit",
)

# Fragments that mark a document as boilerplate to DROP, even if it is a real
# PDF/DOCX. Kept deliberately specific so "application pack" survives while
# "application form" is removed.
JD_EXCLUDE = (
    "privacy", "cookie", "gdpr", "data protection", "data-protection",
    "equal opportun", "equality", "diversity monitoring", "diversity-monitoring",
    "monitoring form", "monitoring-form", "monitoring_form", "ex-offender",
    "ex offender", "rehabilitation of offenders", "gift aid", "gift-aid",
    "gift_aid", "pay gap", "pay-gap", "gender pay", "annual report",
    "annual-report", "modern slavery", "modern-slavery", "accessibilit",
    "dichiarazione", "terms and conditions", "terms-and-conditions", "complaints",
    "safeguarding policy", "strategic plan", "strategic-plan", "strategy 20",
    "organisation chart", "org chart", "org-chart", "structure chart",
    "staff structure", "staff-structure", "benefits", "faq", "how to apply",
    "how-to-apply", "guide for applicant", "guidance for applicant",
    "applicant guidance", "applicants guidance", "information for applicant",
    "info for applicant", "application guidance", "application-guidance",
    "applicationguidance", "guidance notes", "guidance-notes",
    "information on applying", "applying for roles", "applying-for-roles",
    "guide to applying", "checklist", "fragebogen", "personalbogen",
    "cookiepolicy", "cookie-policy", "supporter promise", "newsletter",
    "application form", "application-form", "applicationform", "app form",
    "app-form", "cover sheet", "coversheet", "covering letter", "cover letter",
    "ethnicity", "eeo", "equal employment", "race identification",
)

SKIP_HREF_PREFIXES = ("mailto:", "tel:", "javascript:", "data:", "#")

# Magic-byte signatures → kind. Used to confirm a download really is a document.
_MAGIC = (
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "ooxml"),          # docx / odt (zip container)
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),   # legacy .doc/.xls/.ppt
    (b"{\\rt", "rtf"),
)


def _url_ext(url):
    base = urlsplit(url).path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _magic_kind(data):
    for sig, kind in _MAGIC:
        if data[: len(sig)] == sig:
            return kind
    return ""


def _is_noise(name):
    return any(k in name for k in JD_EXCLUDE)


def find_doc_links(scope, page_url):
    """Return candidate document links inside `scope` (a BeautifulSoup node).

    Each item: {"url": absolute_url, "text": anchor_text, "name": filename+text}.
    nav/footer/header are NOT stripped here — pass an already-scoped element (the
    detail content / listing block) so site chrome is excluded by scope, and the
    noise filter removes any boilerplate that slips through.
    """
    if scope is None:
        return []
    out, seen = [], set()
    for a in scope.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.lower().startswith(SKIP_HREF_PREFIXES):
            continue
        url = urljoin(page_url, href)
        if url in seen:
            continue
        text = a.get_text(" ", strip=True)
        # link title attribute often carries the descriptive label too
        title_attr = a.get("title") or ""
        fname = urlsplit(url).path.rsplit("/", 1)[-1]
        name = f"{fname} {text} {title_attr}".lower()

        ext = _url_ext(url)
        is_doc = ext in DOC_EXTS
        docish = any(h in url.lower() for h in DOCISH_PATH_HINTS)
        jd_text = any(k in name for k in JD_INCLUDE)

        if not (is_doc or docish or jd_text):
            continue
        if _is_noise(name):
            continue
        seen.add(url)
        out.append({"url": url, "text": text, "name": name})
    return out


def extract_text_from_bytes(data, content_type=""):
    """Extract plain text from PDF or DOCX bytes. Returns '' if unsupported/failed.

    Returns a tuple-free plain string (caller treats '' as "nothing usable").
    """
    if not data:
        return ""
    kind = _magic_kind(data)
    # PDF
    if kind == "pdf" or data[:4] == b"%PDF":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        except Exception:
            return ""
    # DOCX / ODT (zip container) — try docx2txt for Word; ODT unsupported → ''
    if kind == "ooxml" or data[:4] == b"PK\x03\x04":
        try:
            import docx2txt
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
                tf.write(data)
                tmp = tf.name
            try:
                txt = docx2txt.process(tmp) or ""
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return txt.strip()
        except Exception:
            return ""
    # legacy .doc (OLE) and RTF: no reliable pure-python extractor here. The
    # on-page text still carries the role; skip rather than emit garbage.
    return ""


def _clean(text):
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_links_text(links, fetch_bytes, *, max_docs=8, max_total_chars=20000,
                     max_chars_per_doc=15000, log=print):
    """Download + verify + extract a list of candidate links (from find_doc_links).

    Separated from discovery so a caller can discover cheaply on every cycle but
    only download when it actually needs the text (e.g. when a page-hash changed).
    Returns (combined_text, sources).
    """
    if not links:
        return "", []
    import hashlib
    parts, sources, total = [], [], 0
    seen_hashes = set()
    for link in links:
        if len(sources) >= max_docs or total >= max_total_chars:
            break
        url = link["url"]
        try:
            data = fetch_bytes(url)
        except Exception as e:
            log(f"      JD-doc fetch error ({url}): {e}")
            continue
        if not data:
            continue
        digest = hashlib.sha1(data).hexdigest()
        if digest in seen_hashes:
            continue  # same file linked twice (e.g. %20-encoded vs plain URL)
        seen_hashes.add(digest)
        if not _magic_kind(data):
            # resolved to something that isn't a document (an HTML page, etc.)
            continue
        text = extract_text_from_bytes(data)
        if not text or len(text) < 80:
            continue  # unparsable (e.g. legacy .doc/scanned) or near-empty
        text = _clean(text)[:max_chars_per_doc]
        remaining = max_total_chars - total
        if remaining <= 0:
            break
        text = text[:remaining]
        label = link["text"] or urlsplit(url).path.rsplit("/", 1)[-1] or "document"
        parts.append(f"\n\n===== Attached document: {label} =====\n{text}")
        sources.append({"url": url, "label": label, "chars": len(text)})
        total += len(text)
        log(f"      + JD doc text: {label[:55]} ({len(text)} chars)")
    return ("".join(parts), sources)


def gather_jd_text(scope, page_url, fetch_bytes, *, max_docs=8,
                   max_total_chars=20000, max_chars_per_doc=15000, log=print):
    """Discover JD-document links under `scope`, then download + extract them.

    Convenience wrapper = find_doc_links + fetch_links_text. Use this from
    detail-page paths (links are downloaded immediately, only for new roles).

    Returns (combined_text, sources); combined_text is '' if nothing usable.
    """
    links = find_doc_links(scope, page_url)
    return fetch_links_text(links, fetch_bytes, max_docs=max_docs,
                            max_total_chars=max_total_chars,
                            max_chars_per_doc=max_chars_per_doc, log=log)
