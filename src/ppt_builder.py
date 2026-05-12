from __future__ import annotations

import copy
import re
from datetime import date
from io import BytesIO
from typing import Dict, List, Optional, Tuple

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

from .models import JiraTicket, ModuleSnapshot, TrackSnapshot

# ── Colour palette (adjust to match your template branding) ──────────────────
DARK_BLUE = RGBColor(0x1F, 0x39, 0x64)
ACCENT_RED = RGBColor(0xC0, 0x00, 0x00)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
STRIPE = RGBColor(0xE8, 0xEF, 0xF8)

_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


# ── Public filename helper ────────────────────────────────────────────────────

def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{({1:'st',2:'nd',3:'rd'}.get(n % 10,'th'))}"


def build_output_filename(start_date: date, end_date: date) -> str:
    """Return filename matching the existing naming convention, e.g.
    ISB-Salesforce - Status - 27th Apr  - 01st May 2026.pptx"""
    s = f"{_ordinal(start_date.day)} {start_date.strftime('%b')}"
    e = f"{_ordinal(end_date.day)} {end_date.strftime('%b')} {end_date.year}"
    return f"ISB-Salesforce - Status - {s}  - {e}.pptx"


# ── Builder ───────────────────────────────────────────────────────────────────

class PptStatusReportBuilder:
    """Writes generated content into the existing template slides.

    Output structure per module that has data:
    - COUNT slide  : module header (TextBox 8) + centred summary count table
    - DETAIL slide : module header (TextBox 8) + a/b/c/d/e ticket sections

    Rules:
    - Slide 0 is normalized and date-corrected if needed.
    - Last slide (often Thank You) is not filled with module data.
    - Content slides are cleared in a template-agnostic way while preserving layout visuals.
    - Unused content slides (no matching module data) are deleted from the output.
    - SLCM tickets are merged onto the Education Cloud Student Success slide.
    """

    def __init__(self, template_bytes: bytes) -> None:
        self.prs = Presentation(BytesIO(template_bytes))

    # ── Cover date pattern ─────────────────────────────────────────────────────
    # Matches patterns like: "27th Apr – 1st May 2026", "1 May - 7 May 2026",
    # "Week ending 7th May 2026", "May 1 – May 7, 2026" etc.
    _DATE_RANGE_RE = re.compile(
        r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}"
        r"(?:\s+\d{4})?\s*[-\u2013\u2014]\s*"
        r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+\d{4}",
        re.IGNORECASE,
    )
    _SINGLE_DATE_RE = re.compile(
        r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+\d{4}",
        re.IGNORECASE,
    )

    # ── Primary entry point ───────────────────────────────────────────────────

    def fill_all_slides(
        self,
        module_data: Dict[str, ModuleSnapshot],
        track_snapshots: Dict[str, TrackSnapshot],
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> None:
        slides = list(self.prs.slides)
        if len(slides) < 3:
            return

        # Update cover slide dates before touching content slides
        if start_date and end_date:
            self.update_cover_slide(start_date, end_date)

        content_slides = slides[1:-1]  # skip cover (idx 0) and last (Thank You)

        # ── Step 1: Flush all content slides to just the header (TextBox 8) ────
        for slide in content_slides:
            self._flush_slide(slide)

        # ── Step 2: Build module assignments ────────────────────────────────────
        assigned: Dict[int, Optional[ModuleSnapshot]] = {}
        skip_ids: set = set()

        for idx, slide in enumerate(content_slides):
            title_lower = self._get_slide_title(slide).lower()
            snap = self._match_module_to_title(title_lower, module_data, skip_ids)
            if snap is not None:
                skip_ids.add(id(snap))
            assigned[idx] = snap

        # Template-agnostic fallback: if no title matching worked, map modules by order.
        if module_data and not any(s is not None for s in assigned.values()):
            ordered = [module_data[name] for name in sorted(module_data.keys(), key=lambda n: n.lower())]
            for idx, snap in zip(range(len(content_slides)), ordered):
                assigned[idx] = snap

        # ── Merge all unmatched SLCM snapshots → Education Cloud Student Success ─
        slcm_snaps = [
            snap for name, snap in module_data.items()
            if id(snap) not in skip_ids and "slcm" in name.lower()
        ]
        if slcm_snaps:
            merged_slcm = ModuleSnapshot(module="SLCM")
            for s in slcm_snaps:
                merged_slcm.carried_over.extend(s.carried_over)
                merged_slcm.created_in_period.extend(s.created_in_period)
                merged_slcm.moved_to_uat.extend(s.moved_to_uat)
                merged_slcm.moved_to_prod.extend(s.moved_to_prod)
                merged_slcm.moved_to_ready_qa.extend(s.moved_to_ready_qa)
            slcm_target: Optional[int] = None
            for idx, slide in enumerate(content_slides):
                if assigned.get(idx) is not None:
                    continue
                if "education cloud" in self._get_slide_title(slide).lower():
                    slcm_target = idx
                    break
            if slcm_target is None:
                for idx in range(len(content_slides)):
                    if assigned.get(idx) is None:
                        slcm_target = idx
                        break
            if slcm_target is not None:
                assigned[slcm_target] = merged_slcm

        # ── Warn about modules with no matching slide ────────────────────────
        all_assigned_ids = {id(s) for s in assigned.values() if s is not None}
        for name, snap in module_data.items():
            if id(snap) not in all_assigned_ids and "slcm" not in name.lower():
                total = len(snap.carried_over) + len(snap.created_in_period)
                print(
                    f"  [WARN] Module '{name}' has no matching slide — "
                    f"{total} ticket(s) not shown. Add it to the label mapping."
                )

        # ── Step 3: For each assigned module create count slide + detail slide ─
        for content_idx in sorted(
            [i for i, s in assigned.items() if s is not None], reverse=True
        ):
            snap = assigned[content_idx]
            count_slide = content_slides[content_idx]
            count_prs_idx = self._slide_index(count_slide)
            has_dependency_items = self._snapshot_has_dependency_items(snap)

            # Always fill count slide (summary table)
            self._fill_count_slide(count_slide, snap)

            # Only add dependency detail slide when there are matched dependency tickets.
            if not has_dependency_items:
                continue

            # Clone the flushed slide → becomes the detail slide (appended at end)
            self._clone_slide(count_slide)  # adds at end

            # Move detail slide (currently last) to immediately after count slide
            detail_prs_idx = len(list(self.prs.slides)) - 1
            target_pos = count_prs_idx + 1
            if detail_prs_idx != target_pos:
                self._move_slide(detail_prs_idx, target_pos)

            # Fill dependency-focused detail slide
            self._fill_detail_slide(self.prs.slides[count_prs_idx + 1], snap)

        # ── Step 4: Delete unused content slides ────────────────────────────
        unused = [
            content_slides[idx]
            for idx in range(len(content_slides))
            if assigned.get(idx) is None
        ]
        while unused:
            positions = []
            for s in unused:
                try:
                    positions.append((self._slide_index(s), s))
                except ValueError:
                    pass
            if not positions:
                break
            positions.sort(reverse=True)
            del_idx, del_slide = positions[0]
            self._delete_slide(del_idx)
            unused.remove(del_slide)

    def update_cover_slide(self, start_date: date, end_date: date) -> None:
        """Scan slide 0 (cover) for date/duration text and replace with the
        actual reporting window, preserving all other formatting."""
        cover = self.prs.slides[0]
        new_range = (
            f"{_ordinal(start_date.day)} {start_date.strftime('%b')} "
            f"\u2013 "
            f"{_ordinal(end_date.day)} {end_date.strftime('%b')} {end_date.year}"
        )
        # Also build a single-date replacement for "Week ending X" style text
        new_single = f"{_ordinal(end_date.day)} {end_date.strftime('%b')} {end_date.year}"

        full_cover_text = " ".join(
            (sh.text_frame.text or "").strip()
            for sh in cover.shapes
            if sh.has_text_frame
        ).strip()
        meaningful_words = [w for w in re.findall(r"[A-Za-z]+", full_cover_text) if len(w) > 2]
        if len(meaningful_words) < 4:
            self._rewrite_cover_slide(cover, new_range)
            return

        changed_any = False
        for shape in cover.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                # Collapse all run text for detection
                full_text = "".join(r.text for r in para.runs)
                if not full_text.strip():
                    continue

                changed = False
                if self._DATE_RANGE_RE.search(full_text):
                    new_text = self._DATE_RANGE_RE.sub(new_range, full_text)
                    changed = True
                elif self._SINGLE_DATE_RE.search(full_text):
                    new_text = self._SINGLE_DATE_RE.sub(new_single, full_text)
                    changed = True
                else:
                    continue

                # Preserve formatting from the first run, rebuild paragraph
                runs = para.runs
                if not runs:
                    continue
                # Copy key font attrs from first run
                first_run = runs[0]
                bold = first_run.font.bold
                italic = first_run.font.italic
                size = first_run.font.size
                try:
                    color = first_run.font.color.rgb
                except Exception:
                    color = None

                # Clear all runs from the paragraph XML
                from lxml import etree
                _ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
                p_elem = para._p
                for r_elem in p_elem.findall(f"{{{_ns}}}r"):
                    p_elem.remove(r_elem)

                # Add a single new run with the updated text
                new_para = para.add_run()
                new_para.text = new_text
                new_para.font.bold = bold
                new_para.font.italic = italic
                if size:
                    new_para.font.size = size
                if color:
                    new_para.font.color.rgb = color
                changed_any = True

        if not changed_any:
            # If no date-like text exists, append a clean reporting-window line.
            tx = cover.shapes.add_textbox(Inches(0.8), Inches(5.6), Inches(11.7), Inches(0.6))
            tf = tx.text_frame
            tf.clear()
            p = tf.paragraphs[0]
            run = p.add_run()
            run.text = f"Reporting Window: {new_range}"
            run.font.size = Pt(20)
            run.font.bold = True
            run.font.color.rgb = DARK_BLUE

    @staticmethod
    def _rewrite_cover_slide(cover_slide, window_text: str) -> None:
        """If cover text is unusable, build a clean title/date overlay."""
        for sh in cover_slide.shapes:
            if sh.has_text_frame:
                sh.text_frame.clear()

        title_box = cover_slide.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(11.8), Inches(1.2))
        tf_t = title_box.text_frame
        tf_t.clear()
        p1 = tf_t.paragraphs[0]
        r1 = p1.add_run()
        r1.text = "Weekly Status Report"
        r1.font.size = Pt(44)
        r1.font.bold = True
        r1.font.color.rgb = DARK_BLUE

        sub_box = cover_slide.shapes.add_textbox(Inches(0.8), Inches(3.2), Inches(11.8), Inches(0.8))
        tf_s = sub_box.text_frame
        tf_s.clear()
        p2 = tf_s.paragraphs[0]
        r2 = p2.add_run()
        r2.text = f"Reporting Window: {window_text}"
        r2.font.size = Pt(24)
        r2.font.bold = True
        r2.font.color.rgb = DARK_BLUE

    def to_bytes(self) -> bytes:
        buf = BytesIO()
        self.prs.save(buf)
        return buf.getvalue()

    # ── Slide writers ─────────────────────────────────────────────────────────

    def _fill_count_slide(self, slide, snapshot: ModuleSnapshot) -> None:
        """Count slide: ensure header exists, then add centred count table."""
        self._ensure_slide_header(slide, snapshot.module)
        self._underline_slide_header(slide)
        self._insert_count_table_centred(slide, snapshot)

    def _fill_detail_slide(self, slide, snapshot: ModuleSnapshot) -> None:
        """Detail slide: ensure header exists, then add ticket sections."""
        self._flush_slide(slide)  # Remove count table and other content inherited from clone
        self._ensure_slide_header(slide, snapshot.module)
        self._set_detail_slide_header(slide, snapshot.module)
        self._underline_slide_header(slide)
        txBox = slide.shapes.add_textbox(Inches(0.4), Inches(1.4), Inches(12.5), Inches(5.5))
        tf = txBox.text_frame
        tf.word_wrap = True
        self._write_sections_to_tf(tf, snapshot)

    @staticmethod
    def _snapshot_has_dependency_items(snapshot: ModuleSnapshot) -> bool:
        """True when any ticket in the module has a matched linked dependency."""
        buckets = (
            snapshot.carried_over,
            snapshot.created_in_period,
            snapshot.moved_to_uat,
            snapshot.moved_to_prod,
            snapshot.moved_to_ready_qa,
        )
        for bucket in buckets:
            for ticket in bucket:
                if ticket.linked_dependency_keys:
                    return True
        return False

    def _write_sections_to_tf(self, tf, snapshot: ModuleSnapshot) -> None:
        self._clear_tf(tf)
        tf.word_wrap = True

        sections: List[Tuple[str, List[JiraTicket]]] = [
            ("a) Carried Over", snapshot.carried_over),
            ("b) Created in Reporting Period", snapshot.created_in_period),
            ("c) Moved to Deployed to UAT", snapshot.moved_to_uat),
            ("d) Moved to Deployed to Production", snapshot.moved_to_prod),
            ("e) Moved to Ready for QA", snapshot.moved_to_ready_qa),
        ]

        # Flat list: (text, bold, italic, size_pt, color|None)
        lines: List[Tuple] = []
        for section_title, tickets in sections:
            dep_tickets = [t for t in tickets if t.linked_dependency_keys]
            lines.append((section_title, True, False, True, 10, DARK_BLUE))
            if not dep_tickets:
                lines.append(("  – None", False, False, False, 9, None))
            else:
                for ticket in dep_tickets[:8]:
                    summary = ticket.ai_summary or ticket.summary
                    lines.append((f"  – {ticket.key}: {summary}", False, False, False, 9, None))
            lines.append(("", False, False, False, 7, None))  # section spacer

        self._write_lines(tf, lines)

    # ── Count table ───────────────────────────────────────────────────────────

    def _insert_count_table_centred(self, slide, snapshot: ModuleSnapshot) -> None:
        """Large centred count table — the primary content of a count slide."""
        def cnt(tickets: List[JiraTicket], itype: str) -> int:
            return sum(1 for t in tickets if t.issue_type.lower() == itype)

        # Deduplicate across buckets for the TOTAL row
        seen: set = set()
        unique_all: List[JiraTicket] = []
        for bucket in (
            snapshot.carried_over, snapshot.created_in_period,
            snapshot.moved_to_uat, snapshot.moved_to_prod, snapshot.moved_to_ready_qa,
        ):
            for t in bucket:
                if t.key not in seen:
                    seen.add(t.key)
                    unique_all.append(t)

        rows_data = [
            ["Category",                 "Stories", "Bugs", "Total"],
            ["Carried Over",
             str(cnt(snapshot.carried_over, "story")),
             str(cnt(snapshot.carried_over, "bug")),
             str(len(snapshot.carried_over))],
            ["Created in Period",
             str(cnt(snapshot.created_in_period, "story")),
             str(cnt(snapshot.created_in_period, "bug")),
             str(len(snapshot.created_in_period))],
            ["\u2192 Deployed to UAT",
             str(cnt(snapshot.moved_to_uat, "story")),
             str(cnt(snapshot.moved_to_uat, "bug")),
             str(len(snapshot.moved_to_uat))],
            ["\u2192 Deployed to Production",
             str(cnt(snapshot.moved_to_prod, "story")),
             str(cnt(snapshot.moved_to_prod, "bug")),
             str(len(snapshot.moved_to_prod))],
            ["\u2192 Ready for QA",
             str(cnt(snapshot.moved_to_ready_qa, "story")),
             str(cnt(snapshot.moved_to_ready_qa, "bug")),
             str(len(snapshot.moved_to_ready_qa))],
            ["TOTAL ACTIVE",
             str(cnt(unique_all, "story")),
             str(cnt(unique_all, "bug")),
             str(len(unique_all))],
        ]

        # Large table centred below the module header
        left   = Inches(1.5)
        top    = Inches(2.0)
        width  = Inches(10.3)
        height = Inches(3.2)

        tbl = slide.shapes.add_table(len(rows_data), 4, left, top, width, height).table

        for i, cw in enumerate([Inches(5.0), Inches(1.8), Inches(1.8), Inches(1.8)]):
            tbl.columns[i].width = cw

        for r, row_vals in enumerate(rows_data):
            is_total = r == len(rows_data) - 1
            for c, val in enumerate(row_vals):
                cell = tbl.cell(r, c)
                cell.text = val
                for para in cell.text_frame.paragraphs:
                    para.alignment = PP_ALIGN.CENTER if c > 0 else PP_ALIGN.LEFT
                    for run in para.runs:
                        run.font.size = Pt(12 if (r == 0 or is_total) else 11)
                        if r == 0 or is_total:
                            run.font.bold = True
                            run.font.color.rgb = WHITE
                if r == 0 or is_total:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = DARK_BLUE
                elif r % 2 == 0:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = STRIPE
                else:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = WHITE

    # ── Text-frame helpers ────────────────────────────────────────────────────

    @staticmethod
    def _write_lines(tf, lines: List[Tuple]) -> None:
        """Write (text, bold, italic, underline, size, color|None) tuples into a text frame.
        Uses the first existing paragraph then appends the rest."""
        first = True
        for text, bold, italic, underline, size, color in lines:
            if first:
                p = tf.paragraphs[0]
                # Clear any existing runs
                for r in list(p.runs):
                    r._r.getparent().remove(r._r)
                run = p.add_run()
                first = False
            else:
                p = tf.add_paragraph()
                run = p.add_run()
            run.text = text
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.italic = italic
            run.font.underline = underline
            if color:
                run.font.color.rgb = color

    @staticmethod
    def _underline_slide_header(slide) -> None:
        """Underline likely header text for detail slides across template variants."""
        header = PptStatusReportBuilder._get_header_shape(slide)
        if not header or not header.has_text_frame:
            return
        tf = header.text_frame
        for p in tf.paragraphs:
            for r in p.runs:
                r.font.underline = True

    @staticmethod
    def _clear_tf(tf) -> None:
        """Remove all text content from a text frame leaving one empty paragraph."""
        txBody = tf._txBody
        paras = txBody.findall(f"{{{_NS}}}p")
        for p in paras[1:]:
            txBody.remove(p)
        if paras:
            for tag in ("r", "br", "fld"):
                for el in paras[0].findall(f"{{{_NS}}}{tag}"):
                    paras[0].remove(el)

    # ── Slide management helpers ──────────────────────────────────────────────

    def _flush_slide(self, slide) -> None:
        """Clear content in a template-agnostic way while keeping visual scaffolding."""
        header_shape = self._get_header_shape(slide)
        sp_tree = slide.shapes._spTree
        to_remove = []
        for sh in slide.shapes:
            if header_shape is not None and sh is header_shape:
                continue
            if sh.has_text_frame or sh.has_table:
                to_remove.append(sh._element)
        for el in to_remove:
            sp_tree.remove(el)

    def _clone_slide(self, source_slide):
        """Clone source_slide and append the copy to the end of the presentation."""
        layout = source_slide.slide_layout
        new_slide = self.prs.slides.add_slide(layout)
        src = source_slide.shapes._spTree
        dst = new_slide.shapes._spTree
        for child in list(dst):
            dst.remove(child)
        for child in src:
            dst.append(copy.deepcopy(child))
        return new_slide

    def _move_slide(self, from_idx: int, to_idx: int) -> None:
        """Reorder slides: move slide at from_idx to to_idx."""
        sldIdLst = self.prs.slides._sldIdLst
        elem = sldIdLst[from_idx]
        sldIdLst.remove(elem)
        sldIdLst.insert(to_idx, elem)

    def _delete_slide(self, idx: int) -> None:
        """Delete slide at idx from the presentation."""
        sldIdLst = self.prs.slides._sldIdLst
        sldId = sldIdLst[idx]
        rId = sldId.get(f"{{{_REL_NS}}}id")
        if rId:
            try:
                self.prs.part.drop_rel(rId)
            except Exception:
                if hasattr(self.prs.part, "_rels") and rId in self.prs.part._rels:
                    del self.prs.part._rels[rId]
        sldIdLst.remove(sldId)

    def _slide_index(self, slide) -> int:
        """Return the current index of a slide in the presentation."""
        for i, s in enumerate(self.prs.slides):
            if s is slide:
                return i
        raise ValueError("Slide not found in presentation")

    # ── Layout helpers ────────────────────────────────────────────────────────

    def _get_slide_title(self, slide) -> str:
        if slide.shapes.title and slide.shapes.title.text:
            return slide.shapes.title.text
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == 0:
                return ph.text or ""
        # Fallback: read text from likely header shape
        sh = self._get_header_shape(slide)
        if sh is not None and sh.has_text_frame:
            t = sh.text_frame.text.strip()
            if t:
                if "\u2013" in t:
                    return t.split("\u2013", 1)[-1].strip()
                if "-" in t and t.lower().startswith("status"):
                    return t.split("-", 1)[-1].strip()
                return t
        return ""

    @staticmethod
    def _get_header_shape(slide):
        """Best-effort header finder that works across templates."""
        if slide.shapes.title and slide.shapes.title.has_text_frame:
            return slide.shapes.title

        candidates = [
            sh for sh in slide.shapes
            if sh.has_text_frame and (sh.text_frame.text or "").strip()
        ]
        if not candidates:
            return None

        # Prefer top-most text shape with meaningful short title-like text.
        candidates.sort(key=lambda s: (s.top, -(s.width * s.height)))
        return candidates[0]

    @staticmethod
    def _ensure_slide_header(slide, module_name: str):
        """Create a header when template does not provide one."""
        existing = PptStatusReportBuilder._get_header_shape(slide)
        if existing is not None:
            txt = (existing.text_frame.text or "").strip() if existing.has_text_frame else ""
            if txt:
                return existing

        header = slide.shapes.add_textbox(Inches(0.45), Inches(0.35), Inches(12.0), Inches(0.7))
        tf = header.text_frame
        tf.clear()
        p = tf.paragraphs[0]
        r = p.add_run()
        r.text = f"Status - {module_name}"
        r.font.size = Pt(22)
        r.font.bold = True
        r.font.color.rgb = DARK_BLUE
        return header

    @staticmethod
    def _set_detail_slide_header(slide, module_name: str) -> None:
        """Retitle detail slide to indicate dependency-focused content."""
        header = PptStatusReportBuilder._get_header_shape(slide)
        if header is None or not header.has_text_frame:
            return

        tf = header.text_frame
        tf.clear()
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = f"Status - {module_name} (Dependency Summary)"
        run.font.size = Pt(22)
        run.font.bold = True
        run.font.color.rgb = DARK_BLUE

    def _get_body_placeholder(self, slide):
        """Return the primary body placeholder (prefer idx=1, then largest)."""
        candidates = [ph for ph in slide.placeholders if ph.placeholder_format.idx != 0]
        for ph in candidates:
            if ph.placeholder_format.idx == 1:
                return ph
        return max(candidates, key=lambda ph: ph.width * ph.height, default=None)

    def _get_content_shape(self, slide):
        """Return the content text frame shape: body placeholder first, then TextBox 4."""
        ph = self._get_body_placeholder(slide)
        if ph:
            return ph
        # Template content slides use TextBox 4 as the writable content area
        for sh in slide.shapes:
            if sh.name == "TextBox 4" and sh.has_text_frame:
                return sh
        # Last resort: largest text box that isn't the module header (TextBox 8)
        text_boxes = [
            sh for sh in slide.shapes
            if sh.has_text_frame
            and sh.name not in ("TextBox 8",)
            and (not slide.shapes.title or sh != slide.shapes.title)
        ]
        if text_boxes:
            return max(text_boxes, key=lambda s: s.width * s.height)
        return None

    def _clear_all_body_placeholders(self, slide) -> None:
        for ph in slide.placeholders:
            if ph.placeholder_format.idx != 0:
                self._clear_tf(ph.text_frame)

    @staticmethod
    def _match_module_to_title(
        title_lower: str, module_data: Dict[str, ModuleSnapshot], skip_ids: set
    ) -> Optional[ModuleSnapshot]:
        """Find the best-matching module for a slide title.

        Matching priority:
        1. Exact substring (module name wholly contained in slide title).
        2. All non-trivial words (>3 chars) in the module name appear in the title.

        Returns None if no module qualifies or the snapshot is already assigned.
        """
        best: Optional[ModuleSnapshot] = None
        best_score = 0

        for name, snap in module_data.items():
            if id(snap) in skip_ids:
                continue
            name_lower = name.lower()
            # Exact substring — highest confidence
            if name_lower in title_lower:
                score = len(name_lower) + 1000  # bias toward longer exact matches
                if score > best_score:
                    best_score = score
                    best = snap
                continue
            # All non-trivial words must be present
            key_words = [w for w in name_lower.split() if len(w) > 3]
            if key_words and all(w in title_lower for w in key_words):
                score = sum(len(w) for w in key_words)
                if score > best_score:
                    best_score = score
                    best = snap

        return best
