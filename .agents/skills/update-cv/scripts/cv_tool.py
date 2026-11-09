#!/usr/bin/env python3
"""CLI utility for discovering, building, and syncing Kyle Grealis's UM CV."""

import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SKILL_DIR.parent.parent.parent
DEFAULT_DATA = SKILL_DIR / "references" / "cv_data.json"
DEFAULT_TEMPLATE = SKILL_DIR / "references" / "um_cv_template.docx"
DOCS_DIR = Path.home() / "Documents"
DOWNLOADS_DIR = Path.home() / "Downloads"
LOCK_FILE = Path(tempfile.gettempdir()) / "update_cv_ratelimit.lock"


class RateLimitError(Exception):
    """Raised when an API returns HTTP 429."""


class RateLimitedClient:
    """HTTP client with cross-process file-locked rate limiting and retries."""

    def __init__(self, rps: float = 1.0, max_retries: int = 3):
        self.min_interval = 1.0 / rps
        self.max_retries = max_retries
        self._last_call = 0.0

    def _wait_rate_limit(self):
        LOCK_FILE.touch(exist_ok=True)
        with open(LOCK_FILE, "r+", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                now = time.monotonic()
                elapsed = now - self._last_call
                if elapsed < self.min_interval:
                    time.sleep(self.min_interval - elapsed)
                self._last_call = time.monotonic()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def fetch_json(self, url: str, headers: dict | None = None) -> dict:
        hdrs = {"User-Agent": "UpdateCV/1.0 (mailto:kylegrealis@miami.edu)"}
        if headers:
            hdrs.update(headers)
        for attempt in range(1, self.max_retries + 1):
            self._wait_rate_limit()
            req = urllib.request.Request(url, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")[:400]
                if e.code == 429:
                    raise RateLimitError(
                        f"HTTP 429 rate limit exceeded for {url} (limit={1.0/self.min_interval:.1f} rps): {body}"
                    ) from e
                if 500 <= e.code < 600 and attempt < self.max_retries:
                    backoff = 2 ** attempt
                    print(f"[retry {attempt}/{self.max_retries}] HTTP {e.code} on {url}; sleeping {backoff}s", file=sys.stderr)
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"HTTP {e.code} error on {url}: {body}") from e
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    backoff = 2 ** attempt
                    print(f"[retry {attempt}/{self.max_retries}] URL error on {url}: {e}; sleeping {backoff}s", file=sys.stderr)
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"Network error on {url}: {e}") from e
        raise RuntimeError(f"Exhausted retries for {url}")

    def fetch_text(self, url: str) -> str:
        self._wait_rate_limit()
        req = urllib.request.Request(url, headers={"User-Agent": "UpdateCV/1.0 (mailto:kylegrealis@miami.edu)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")


def normalize_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def cmd_discover(args: argparse.Namespace) -> None:
    data_path = Path(args.data)
    cv_data = json.loads(data_path.read_text(encoding="utf-8"))
    limit = args.limit

    known_titles = set()
    known_urls = set()
    for section in ("articles", "abstracts", "software_packages", "web_applications"):
        for item in cv_data.get("publications", {}).get(section, []):
            if "title" in item:
                known_titles.add(normalize_title(item["title"]))
            if "name" in item:
                known_titles.add(normalize_title(item["name"]))
            if item.get("url"):
                known_urls.add(item["url"].lower().rstrip("/"))

    client = RateLimitedClient(rps=1.0)
    orcid_id = cv_data.get("personal", {}).get("orcid", "0000-0002-9223-8854")

    report = {
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "orcid": orcid_id,
        "limit": limit,
        "new_works": [],
        "rmed_matches": [],
        "warnings": [],
    }

    # 1. OpenAlex by ORCID and Author ID
    try:
        oa_url = f"https://api.openalex.org/works?filter=author.orcid:{orcid_id}&per-page={limit}"
        oa_data = client.fetch_json(oa_url)
        for w in oa_data.get("results", []):
            title = w.get("title") or ""
            doi = (w.get("doi") or "").lower().rstrip("/")
            norm_t = normalize_title(title)
            pkg_prefix = norm_t.split(":")[0] if ":" in title else norm_t
            if norm_t and norm_t not in known_titles and doi not in known_urls:
                # Check if CRAN package name is already known
                if any(pkg.get("name", "").lower() in title.lower() for pkg in cv_data["publications"]["software_packages"]):
                    continue
                loc = (w.get("primary_location") or {}).get("source") or {}
                report["new_works"].append({
                    "source": "OpenAlex",
                    "year": w.get("publication_year"),
                    "title": title,
                    "venue": loc.get("display_name", ""),
                    "doi": w.get("doi"),
                })
    except Exception as exc:
        report["warnings"].append(f"OpenAlex lookup warning: {exc}")

    # 2. PubMed E-utilities
    try:
        esearch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=Grealis+K%5BAuthor%5D&retmode=json&retmax={limit}"
        esearch = client.fetch_json(esearch_url)
        pmids = esearch.get("esearchresult", {}).get("idlist", [])
        if pmids:
            esum_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id={','.join(pmids)}&retmode=json"
            esum = client.fetch_json(esum_url)
            for pid in pmids:
                doc = esum.get("result", {}).get(pid, {})
                title = (doc.get("title") or "").rstrip(".")
                pubdate = doc.get("pubdate") or ""
                # Skip pre-2015 unrelated Grealis K nursing papers
                if any(pubdate.startswith(yr) for yr in ("198", "199", "200")):
                    continue
                norm_t = normalize_title(title)
                if norm_t and norm_t not in known_titles:
                    report["new_works"].append({
                        "source": f"PubMed (PMID {pid})",
                        "year": pubdate,
                        "title": title,
                        "venue": doc.get("source", ""),
                        "volume": doc.get("volume", ""),
                        "issue": doc.get("issue", ""),
                        "pages": doc.get("pages", ""),
                    })
    except Exception as exc:
        report["warnings"].append(f"PubMed lookup warning: {exc}")

    # 3. R/Medicine Conference Sites
    for rmed_url in [
        "https://rconsortium.github.io/RMedicine_website/Abstracts.html",
        "https://rconsortium.github.io/RMedicine_2025/Program.html",
    ]:
        try:
            html = client.fetch_text(rmed_url)
            for m in re.finditer(r"Grealis", html, re.IGNORECASE):
                snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[max(0, m.start() - 250):min(len(html), m.end() + 350)])).strip()
                report["rmed_matches"].append({"url": rmed_url, "snippet": snippet})
        except Exception as exc:
            report["warnings"].append(f"R/Medicine check warning ({rmed_url}): {exc}")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Success! Discovery report written to: {out_path} (new_works={len(report['new_works'])})")


def build_docx_from_json(cv_data: dict, template_path: Path, out_docx: Path) -> None:
    hyperlinks: list[tuple[str, str]] = []

    def add_hyperlink(url: str) -> str:
        rid = f"rId_custom_{len(hyperlinks) + 1}"
        hyperlinks.append((rid, url))
        return rid

    def run_xml(text: str, bold: bool = False, italic: bool = False, underline: bool = False, superscript: bool = False) -> str:
        if not text:
            return ""
        rpr = []
        if bold:
            rpr.append("<w:b/><w:bCs/>")
        if italic:
            rpr.append("<w:i/><w:iCs/>")
        if underline:
            rpr.append('<w:u w:val="single"/>')
        if superscript:
            rpr.append('<w:vertAlign w:val="superscript"/>')
        rpr.append('<w:color w:val="000000"/>')
        rpr_str = f"<w:rPr>{''.join(rpr)}</w:rPr>"
        space_attr = ' xml:space="preserve"' if (text.startswith(" ") or text.endswith(" ") or "  " in text) else ""
        return f"<w:r>{rpr_str}<w:t{space_attr}>{escape(text)}</w:t></w:r>"

    def link_xml(url: str, display_text: str | None = None) -> str:
        if display_text is None:
            display_text = url
        rid = add_hyperlink(url)
        return (
            f'<w:hyperlink r:id="{rid}" w:history="1">'
            f'<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
            f'<w:t>{escape(display_text)}</w:t></w:r></w:hyperlink>'
        )

    def p_xml(runs_str: str, jc: str | None = None, left: int | None = None, hanging: int | None = None,
              firstLine: int | None = None, before: int | None = None, after: int | None = None,
              numId: int | None = None, keepNext: bool = False) -> str:
        ppr = []
        if numId is not None:
            ppr.append(f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{numId}"/></w:numPr>')
        if jc:
            ppr.append(f'<w:jc w:val="{jc}"/>')
        if before is not None or after is not None:
            b_attr = f' w:before="{before}"' if before is not None else ""
            a_attr = f' w:after="{after}"' if after is not None else ""
            ppr.append(f"<w:spacing{b_attr}{a_attr}/>")
        if left is not None or hanging is not None or firstLine is not None:
            l_attr = f' w:left="{left}"' if left is not None else ""
            h_attr = f' w:hanging="{hanging}"' if hanging is not None else ""
            f_attr = f' w:firstLine="{firstLine}"' if firstLine is not None else ""
            ppr.append(f"<w:ind{l_attr}{h_attr}{f_attr}/>")
        if keepNext:
            ppr.append("<w:keepNext/>")
        ppr_str = f"<w:pPr>{''.join(ppr)}</w:pPr>" if ppr else ""
        return f"<w:p>{ppr_str}{runs_str}</w:p>"

    def major_header(title: str, bold_italic: bool = False) -> str:
        if bold_italic:
            return p_xml(run_xml(title, bold=True, italic=True), before=120, after=60, keepNext=True)
        return p_xml(run_xml(title, bold=True, underline=True), before=180, after=60, keepNext=True)

    def numbered_header(num: int, label: str, value: str | None = None, indent: bool = True) -> str:
        runs = run_xml(f"{num}. {label}:", italic=True)
        if value is not None:
            runs += run_xml(f" {value}")
        if indent:
            return p_xml(runs, left=360, hanging=360, before=75, after=75, keepNext=(value is None))
        return p_xml(runs, before=75, after=75, keepNext=(value is None))

    def sub_header(title: str, left: int | None = 360, hanging: int | None = None, firstLine: int | None = None) -> str:
        return p_xml(run_xml(title, bold=True), left=left, hanging=hanging, firstLine=firstLine, before=60, after=40, keepNext=True)

    def indent_entry(prefix: str, rest: str, left: int = 720, hanging: int = 360) -> str:
        return p_xml(run_xml(prefix) + run_xml(rest), left=left, hanging=hanging, before=20, after=20)

    def format_citation_runs(item: dict) -> str:
        runs = ""
        if item.get("authors_before"):
            runs += run_xml(item["authors_before"])
        if item.get("author_bold"):
            runs += run_xml(item["author_bold"], bold=True)
        if item.get("authors_after"):
            runs += run_xml(item["authors_after"])
        if item.get("year"):
            runs += run_xml(f" ({item['year']}). ")
        else:
            runs += run_xml(". ")
        runs += run_xml(f"{item['title']} ")
        if item.get("journal"):
            runs += run_xml(item["journal"], italic=True)
        if item.get("details"):
            runs += run_xml(item["details"])
        if item.get("url"):
            runs += run_xml(" ") + link_xml(item["url"])
        return runs

    paras: list[str] = ["<w:p/>"]
    paras.append(p_xml(run_xml("UNIVERSITY OF MIAMI", bold=True), jc="center"))
    paras.append(p_xml(run_xml("Curriculum Vitae", bold=True), jc="center"))
    paras.append("<w:p/>")

    # 1. Date
    date_str = cv_data.get("date", "Nov 9th, 2026")
    m_date = re.match(r"^([A-Za-z]+\s+\d+)(st|nd|rd|th)(,\s+\d{4})$", date_str)
    if m_date:
        date_runs = (
            run_xml("1")
            + run_xml(". Date:", italic=True)
            + run_xml(f" {m_date.group(1)}")
            + run_xml(m_date.group(2), superscript=True)
            + run_xml(m_date.group(3))
        )
    else:
        date_runs = run_xml("1") + run_xml(". Date:", italic=True) + run_xml(f" {date_str}")
    paras.append(p_xml(date_runs))
    paras.append("<w:p/>")

    # Personal
    pers = cv_data["personal"]
    paras.append(major_header("Personal", bold_italic=True))
    paras.append(numbered_header(2, "Name", pers["name"], indent=False))
    email_runs = run_xml("3. Email Address:", italic=True) + run_xml(" ") + link_xml(f"mailto:{pers['email']}", pers["email"])
    paras.append(p_xml(email_runs, before=75, after=75))
    paras.append(numbered_header(4, "Current Academic Rank", pers["rank"], indent=False))
    paras.append(numbered_header(5, "Primary Department", pers["department"], indent=False))
    paras.append(numbered_header(6, "Secondary or Joint Appointments", pers["secondary_appointments"], indent=False))
    paras.append("<w:p/>")

    # Higher Education
    edu = cv_data["education"]
    paras.append(major_header("Higher Education"))
    paras.append(numbered_header(7, "Institutional"))
    for item in edu["institutional"]:
        paras.append(indent_entry(f"{item['year']}: ", item["text"]))
    paras.append(numbered_header(8, "Non-Institutional"))
    for item in edu["non_institutional"]:
        paras.append(indent_entry(f"{item['year']}: ", item["text"]))
    paras.append(numbered_header(9, "Certification, licensure"))
    for item in edu["certifications"]:
        paras.append(indent_entry(f"{item['year']}: ", item["text"]))
    paras.append("<w:p/>")

    # Experience
    exp = cv_data["experience"]
    paras.append(major_header("Experience"))
    paras.append(numbered_header(10, "Academic"))
    for item in exp["academic"]:
        paras.append(indent_entry(f"{item['years']}: ", item["role"]))
        if item.get("responsibilities"):
            paras.append(p_xml(run_xml(f"Responsibilities: {item['responsibilities']}"), left=720, before=10, after=40))
    paras.append(numbered_header(11, "Non-Academic"))
    for item in exp["non_academic"]:
        paras.append(indent_entry(f"{item['years']}: ", item["role"]))
        if item.get("responsibilities"):
            paras.append(p_xml(run_xml(f"Responsibilities: {item['responsibilities']}"), left=720, before=10, after=40))
    paras.append(numbered_header(12, "Military"))
    for item in exp["military"]:
        paras.append(indent_entry(f"{item['years']}: ", item["role"]))
    paras.append("<w:p/>")

    # Publications
    pubs = cv_data["publications"]
    paras.append(major_header("Publications"))
    paras.append(numbered_header(13, "Books and Monographs Published", pubs.get("books", "N/A")))
    paras.append(numbered_header(14, "Juried or Refereed Journal Articles or Exhibitions"))
    for art in pubs["articles"]:
        paras.append(p_xml(format_citation_runs(art), numId=9, before=30, after=30))

    paras.append(numbered_header(15, "Other works, publications and abstracts"))
    paras.append(p_xml(run_xml("\tNOTE: All abstracts listed below are for refereed conference presentations.", bold=True), left=360, hanging=360, before=60, after=60))
    for ab in pubs["abstracts"]:
        paras.append(p_xml(format_citation_runs(ab), numId=18, left=720, before=30, after=30))

    paras.append(sub_header("Published Software Packages:", left=None, firstLine=360))
    for pkg in pubs["software_packages"]:
        runs = run_xml(pkg["years"]) + run_xml(pkg["name"], italic=True) + run_xml(pkg["description"])
        paras.append(p_xml(runs, numId=12, before=20, after=10))
        paras.append(p_xml(link_xml(pkg["url"]), left=720, before=0, after=20))

    paras.append(sub_header("Published Web Applications:", left=None, firstLine=360))
    for app in pubs["web_applications"]:
        runs = run_xml(app["years"]) + run_xml(app["name"], italic=True) + run_xml(app["description"])
        paras.append(p_xml(runs, numId=2, before=20, after=10))
        paras.append(p_xml(link_xml(app["url"]), left=720, before=0, after=20))

    paras.append(numbered_header(16, "Other works accepted for publication", pubs.get("accepted", "N/A")))
    paras.append("<w:p/>")

    # Professional
    prof = cv_data["professional"]
    paras.append(major_header("Professional"))
    paras.append(numbered_header(17, "Funded Research Performed"))
    for st in prof["research_studies"]:
        paras.append(p_xml(run_xml(st["title"], bold=True), left=720, hanging=360, before=60, after=10, keepNext=True))
        paras.append(p_xml(run_xml(f"Principal Investigator: {st['pi']}"), left=720, hanging=360))
        paras.append(p_xml(run_xml(f"Protocol / IRB: {st['irb']}"), left=720, hanging=360))
        paras.append(p_xml(run_xml(f"Period: {st['period']}"), left=720, hanging=360))
        paras.append(p_xml(run_xml(f"Role: {st['role']}"), left=720, hanging=360, after=30))

    paras.append(numbered_header(18, "Editorial responsibilities", prof.get("editorial", "N/A")))
    paras.append(numbered_header(19, "Professional and Honorary Organizations"))
    for org in prof["organizations"]:
        paras.append(p_xml(run_xml(org), numId=8, left=720, before=20, after=20))

    paras.append(numbered_header(20, "Honors and Awards"))
    for aw in prof["awards"]:
        paras.append(p_xml(run_xml(aw), numId=22, before=20, after=20))

    paras.append(numbered_header(21, "Post-Doctoral Fellowships", prof.get("fellowships", "N/A")))
    paras.append(numbered_header(22, "Other Professional Activities", indent=False))
    paras.append(sub_header("Refereed International Presentations", left=360, hanging=360))
    for pr in prof["presentations_international"]:
        runs = (
            run_xml(pr["title_and_authors_before"])
            + run_xml(pr["author_bold"], bold=True)
            + run_xml(pr["authors_after"])
            + run_xml(pr["venue_italic"], italic=True)
            + run_xml(pr["details"])
        )
        paras.append(p_xml(runs, numId=20, before=20, after=20))

    paras.append(sub_header("Refereed Local Presentations", left=360, hanging=360))
    for pr in prof["presentations_local"]:
        runs = (
            run_xml(pr["title_and_authors_before"])
            + run_xml(pr["author_bold"], bold=True)
            + run_xml(pr["authors_after"])
            + run_xml(pr["venue_italic"], italic=True)
            + run_xml(pr["details"])
        )
        paras.append(p_xml(runs, numId=21, before=20, after=20))
    paras.append("<w:p/>")

    # Teaching
    teach = cv_data["teaching"]
    paras.append(major_header("Teaching"))
    paras.append(numbered_header(23, "Teaching Awards", teach.get("awards", "N/A")))
    paras.append(numbered_header(24, "Teaching Specialization", teach.get("specialization", "N/A")))
    paras.append(numbered_header(25, "Thesis and Dissertation Advising/Post-doctoral Student Supervision", teach.get("advising", "N/A"), indent=False))
    paras.append("<w:p/>")

    # Service
    srv = cv_data["service"]
    paras.append(major_header("Service"))
    paras.append(numbered_header(26, "University Committee and Administrative Responsibilities", srv.get("university_committees", "N/A")))
    paras.append(numbered_header(27, "Community Activities"))
    paras.append(sub_header("Unfunded Software Related Projects:", left=360))
    paras.append(p_xml(run_xml(srv["community_summary"]), numId=13, before=20, after=10))
    paras.append(p_xml(link_xml(pers["github"]), left=1080, hanging=360, before=0, after=10))
    paras.append(p_xml(link_xml(f"https://orcid.org/{pers['orcid']}", f"ORCID: {pers['orcid']}"), left=1080, hanging=360, before=0, after=20))

    out_docx.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(template_path, "r") as zin:
        orig_doc_xml = zin.read("word/document.xml").decode("utf-8")
        orig_rels_xml = zin.read("word/_rels/document.xml.rels").decode("utf-8")
        body_start = orig_doc_xml.find("<w:body>") + len("<w:body>")
        sect_start = orig_doc_xml.rfind("<w:sectPr")
        new_doc_xml = orig_doc_xml[:body_start] + "".join(paras) + orig_doc_xml[sect_start:]

        rels_insert = "".join(
            f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{escape(url)}" TargetMode="External"/>'
            for rid, url in hyperlinks
        )
        new_rels_xml = orig_rels_xml.replace("</Relationships>", rels_insert + "</Relationships>")

        with zipfile.ZipFile(out_docx, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "word/document.xml":
                    zout.writestr(item, new_doc_xml.encode("utf-8"))
                elif item.filename == "word/_rels/document.xml.rels":
                    zout.writestr(item, new_rels_xml.encode("utf-8"))
                else:
                    zout.writestr(item, zin.read(item.filename))


def convert_docx_to_pdf(docx_path: Path, pdf_path: Path) -> None:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    stage_docx = DOWNLOADS_DIR / "_cv_stage.docx"
    stage_pdf = DOWNLOADS_DIR / "_cv_stage.pdf"
    shutil.copy2(docx_path, stage_docx)
    if stage_pdf.exists():
        subprocess.run(["trash", str(stage_pdf)], check=False)
    script = f"""
    tell application "Microsoft Word"
        open POSIX file "{stage_docx}"
        delay 1
        save as active document file name "{stage_pdf}" file format format PDF
        close active document saving no
    end tell
    """
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if res.returncode != 0 or not stage_pdf.exists():
        raise RuntimeError(f"Failed to convert DOCX to PDF via Microsoft Word: {res.stderr}")
    shutil.copy2(stage_pdf, pdf_path)
    subprocess.run(["trash", str(stage_docx), str(stage_pdf)], check=False)


def cmd_build(args: argparse.Namespace) -> None:
    data_path = Path(args.data)
    template_path = Path(args.template)
    cv_data = json.loads(data_path.read_text(encoding="utf-8"))

    out_docx = REPO_DIR / "static" / "files" / "KGrealis_cv.docx"
    out_pdf = REPO_DIR / "static" / "files" / "KGrealis_cv.pdf"

    build_docx_from_json(cv_data, template_path, out_docx)
    convert_docx_to_pdf(out_docx, out_pdf)

    report = {
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "docx_path": str(out_docx),
        "docx_bytes": out_docx.stat().st_size,
        "pdf_path": str(out_pdf),
        "pdf_bytes": out_pdf.stat().st_size,
    }
    out_report = Path(args.output)
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Success! Built DOCX and PDF. Report written to: {out_report}")


def cmd_sync(args: argparse.Namespace) -> None:
    src_docx = REPO_DIR / "static" / "files" / "KGrealis_cv.docx"
    src_pdf = REPO_DIR / "static" / "files" / "KGrealis_cv.pdf"
    if not src_docx.exists() or not src_pdf.exists():
        raise RuntimeError("Source CV files missing in static/files/; run `build` first.")

    today = datetime.date.today().isoformat()
    docs_static_pdf = REPO_DIR / "docs" / "static" / "files" / "KGrealis_cv.pdf"
    docs_static_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_pdf, docs_static_pdf)

    # Update cache-busting query string on index.qmd and docs/index.html
    for qmd_or_html in [REPO_DIR / "index.qmd", REPO_DIR / "docs" / "index.html"]:
        if qmd_or_html.exists():
            txt = qmd_or_html.read_text(encoding="utf-8")
            updated = re.sub(r"/static/files/KGrealis_cv\.pdf(?:\?v=[0-9-]+)?", f"/static/files/KGrealis_cv.pdf?v={today}", txt)
            if updated != txt:
                qmd_or_html.write_text(updated, encoding="utf-8")

    # Copy to ~/Downloads
    dl_docx = DOWNLOADS_DIR / f"KGrealis_UM_CV_{today}.docx"
    dl_pdf = DOWNLOADS_DIR / f"KGrealis_UM_CV_{today}.pdf"
    dl_main_pdf = DOWNLOADS_DIR / "KGrealis_cv.pdf"
    shutil.copy2(src_docx, dl_docx)
    shutil.copy2(src_pdf, dl_pdf)
    shutil.copy2(src_pdf, dl_main_pdf)

    # Copy to ~/Documents/outputs/ and sync explicitly per ~/Documents/GEMINI.md
    brain_outputs = DOCS_DIR / "outputs"
    brain_outputs.mkdir(parents=True, exist_ok=True)
    brain_docx = brain_outputs / "KGrealis_cv.docx"
    brain_pdf = brain_outputs / "KGrealis_cv.pdf"
    shutil.copy2(src_docx, brain_docx)
    shutil.copy2(src_pdf, brain_pdf)

    brain_synced = False
    if (DOCS_DIR / ".git").exists():
        subprocess.run(["git", "-C", str(DOCS_DIR), "pull", "--rebase", "--autostash", "--quiet"], check=False)
        rel_files = ["outputs/KGrealis_cv.docx", "outputs/KGrealis_cv.pdf"]
        subprocess.run(["git", "-C", str(DOCS_DIR), "add", "--", *rel_files], check=True)
        diff_res = subprocess.run(["git", "-C", str(DOCS_DIR), "diff", "--cached", "--quiet", "--", *rel_files], check=False)
        if diff_res.returncode != 0:
            subprocess.run(
                ["git", "-C", str(DOCS_DIR), "commit", "-m", f"docs(outputs): archive updated CV ({today})", "--only", "--", *rel_files],
                check=True,
            )
            subprocess.run(["git", "-C", str(DOCS_DIR), "push", "--quiet"], check=True)
            brain_synced = True

    report = {
        "synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "website_files": [str(src_docx), str(src_pdf), str(docs_static_pdf)],
        "downloads_files": [str(dl_docx), str(dl_pdf), str(dl_main_pdf)],
        "brain_wiki_files": [str(brain_docx), str(brain_pdf)],
        "brain_wiki_pushed": brain_synced,
    }
    out_report = Path(args.output)
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Success! Synced CV to website, Downloads, and brain-wiki. Report written to: {out_report}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover, build, and sync Kyle Grealis's UM CV.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_disc = sub.add_parser("discover", help="Search PubMed, ORCID, OpenAlex, and R/Medicine for new works.")
    p_disc.add_argument("--data", default=str(DEFAULT_DATA), help="Path to cv_data.json")
    p_disc.add_argument("--limit", type=int, required=True, help="Max results per API query (required)")
    p_disc.add_argument("--output", required=True, help="Output JSON file path for discovery report")

    p_build = sub.add_parser("build", help="Rebuild KGrealis_cv.docx and KGrealis_cv.pdf from cv_data.json.")
    p_build.add_argument("--data", default=str(DEFAULT_DATA), help="Path to cv_data.json")
    p_build.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="Path to um_cv_template.docx")
    p_build.add_argument("--output", required=True, help="Output JSON file path for build report")

    p_sync = sub.add_parser("sync", help="Sync built DOCX/PDF to website, Downloads, and ~/Documents/outputs/.")
    p_sync.add_argument("--output", required=True, help="Output JSON file path for sync report")

    args = parser.parse_args()
    try:
        if args.command == "discover":
            cmd_discover(args)
        elif args.command == "build":
            cmd_build(args)
        elif args.command == "sync":
            cmd_sync(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
