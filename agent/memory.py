"""What Nova knows beyond the task in hand: the company handbook, indexed by section for retrieval.

Retrieval is for *how* work is done (procedures). What is *allowed* is never retrieved; it is policy code.
Phase 4 adds lessons learned from people's answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

_STOP = set("a an and are as at be by for from has have if in is it its of on or our the this to was we with you "
            "your their them they will can do does not no should must any all".split())


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOP:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(w) > 4 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.append(w)
    return out


@dataclass
class Section:
    doc: str  # file stem, e.g. "01_AP_Invoice_Processing"
    title: str  # the document's title
    heading: str  # the section heading ("" for the text before the first one)
    text: str


class Handbook:
    def __init__(self, folder: Path):
        self.folder = folder
        self.docs: dict[str, str] = {}
        self.titles: dict[str, str] = {}
        self.sections: list[Section] = []
        for path in sorted(folder.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), path.stem)
            self.docs[path.stem], self.titles[path.stem] = text, title
            heading, buf = "", []
            for line in text.splitlines():
                if line.startswith("## "):
                    self._add(path.stem, title, heading, buf)
                    heading, buf = line[3:].strip(), []
                elif not line.startswith("# "):
                    buf.append(line)
            self._add(path.stem, title, heading, buf)
        self.bm25 = BM25Okapi([tokens(f"{s.title} {s.heading} {s.text}") for s in self.sections]) \
            if self.sections else None

    def _add(self, doc: str, title: str, heading: str, lines: list[str]) -> None:
        text = "\n".join(lines).strip()
        if text:
            self.sections.append(Section(doc, title, heading, text))

    def toc(self) -> str:
        """One line per document: its file name, title and section headings."""
        lines = []
        for doc, title in self.titles.items():
            heads = [s.heading for s in self.sections if s.doc == doc and s.heading]
            lines.append(f"- {doc}: {title}" + (f" (sections: {'; '.join(heads)})" if heads else ""))
        return "\n".join(lines)

    def search(self, query: str, k: int = 5) -> list[tuple[Section, float]]:
        if not self.bm25 or not tokens(query):
            return []
        scores = self.bm25.get_scores(tokens(query))
        ranked = sorted(zip(self.sections, scores), key=lambda p: -p[1])
        return [(s, float(sc)) for s, sc in ranked[:k] if sc > 0]

    def find(self, name: str) -> str | None:
        """Resolve a loose document reference ('01', '01_AP_Invoice_Processing.md', 'approval matrix')."""
        key = re.sub(r"\.md$", "", name.strip(), flags=re.I).replace("Handbook/", "").lower()
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
        for doc in self.docs:
            if key in (doc.lower(), doc.split("_")[0]) or norm(key) in (norm(doc), norm(self.titles[doc])):
                return doc
        hits = [doc for doc in self.docs if norm(key) and (norm(key) in norm(doc) or norm(key) in norm(self.titles[doc]))]
        return hits[0] if len(hits) == 1 else None

    def read(self, name: str, section: str | None = None) -> tuple[str, str] | None:
        """(document, text) for a document, or one of its sections when `section` is given and matches."""
        doc = self.find(name)
        if not doc:
            return None
        if section:
            want = re.sub(r"[^a-z0-9]+", " ", section.lower()).strip()
            for s in self.sections:
                head = re.sub(r"[^a-z0-9]+", " ", s.heading.lower()).strip()
                if s.doc == doc and want and (want == head or want in head):
                    return doc, f"## {s.heading}\n{s.text}"
        return doc, self.docs[doc]

    def retrieve(self, request: str) -> tuple[str | None, list[Section]]:
        """For a new request: the procedure document that best matches it, plus the best sections elsewhere."""
        hits = self.search(request, k=8)
        if not hits:
            return None, []
        by_doc: dict[str, float] = {}
        for s, score in hits:
            by_doc[s.doc] = by_doc.get(s.doc, 0) + score
        best = max(by_doc, key=by_doc.get)
        others = [s for s, _ in hits if s.doc != best]
        return best, others[:3]


# --------------------------------------------------------------------------- lessons
LESSONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, situation TEXT NOT NULL, guidance TEXT NOT NULL, by TEXT,
    run_id TEXT, task TEXT, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    uses INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class Lesson:
    id: str
    kind: str  # "answer" (to Nova's question) | "rejection" (of an approval request, with the reason)
    situation: str  # what Nova asked, or what it asked approval for
    guidance: str  # what the person said
    by: str
    run_id: str
    task: str  # the request it came up in
    created_at: str
    status: str = "active"
    uses: int = 0

    def line(self) -> str:
        return (f'{self.id} ({self.by}, {self.created_at[:10]}), on "{self.situation[:220]}": '
                f'"{self.guidance[:300]}"')


def _words(text: str) -> set[str]:
    """The words of a question that say what it is about (numbers vary from one vendor to the next)."""
    return {w for w in tokens(text) if not w.isdigit()}


def similar(a: str, b: str) -> float:
    """How much of the shorter text's vocabulary the other shares (a question is often asked again in other words)."""
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, min(len(wa), len(wb)))


class Lessons:
    """Standing instructions from people: answers and rejection reasons they asked Nova to remember.

    Only people write here, through the inbox or the terminal, and only when they say "remember this": one answer
    does not silently become a rule, and nothing read in an email or a document can become one. Lessons guide; they
    never override the handbook or the policy gate. `python -m agent lessons` lists them, `forget` retires one."""

    CANDIDATE = 0.4  # how alike a question must be to a remembered one to be put to the model as a possible match

    def __init__(self, db):
        self.db = db
        self.db.executescript(LESSONS_SCHEMA)

    def add(self, kind: str, situation: str, guidance: str, by: str, run_id: str, task: str) -> Lesson:
        from datetime import datetime

        n = self.db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] + 1
        lesson = Lesson(f"L-{n}", kind, situation.strip(), guidance.strip(), by, run_id, task,
                        datetime.now().isoformat(timespec="seconds"))
        self.db.execute("INSERT INTO lessons (id, kind, situation, guidance, by, run_id, task, created_at) VALUES "
                        "(?,?,?,?,?,?,?,?)", (lesson.id, kind, lesson.situation, lesson.guidance, by, run_id, task,
                                              lesson.created_at))
        return lesson

    def active(self) -> list[Lesson]:
        rows = self.db.execute("SELECT * FROM lessons WHERE status = 'active' ORDER BY created_at").fetchall()
        return [Lesson(**dict(r)) for r in rows]

    def all(self) -> list[Lesson]:
        return [Lesson(**dict(r)) for r in self.db.execute("SELECT * FROM lessons ORDER BY created_at").fetchall()]

    def candidates(self, question: str, k: int = 3) -> list[Lesson]:
        """Remembered answers that may answer this question (asked in other words, about another vendor). Word
        overlap only proposes; whether one really answers it is decided by the runtime (agent/runtime.py)."""
        scored = [(similar(question, x.situation), x) for x in self.active() if x.kind == "answer"]
        return [x for s, x in sorted(scored, key=lambda s: -s[0]) if s >= self.CANDIDATE][:k]

    def relevant(self, text: str, k: int = 3) -> list[Lesson]:
        """The lessons a task's request and documents touch on, most related first."""
        words = _words(text)
        scored = [(len(words & _words(x.situation + " " + x.guidance)) / max(1, len(_words(x.situation))), x)
                  for x in self.active()]
        return [x for s, x in sorted(scored, key=lambda s: -s[0]) if s >= 0.4][:k]

    def used(self, lesson_id: str) -> None:
        self.db.execute("UPDATE lessons SET uses = uses + 1 WHERE id = ?", (lesson_id,))

    def retire(self, lesson_id: str) -> bool:
        return self.db.execute("UPDATE lessons SET status = 'retired' WHERE id = ? AND status = 'active'",
                               (lesson_id,)).rowcount > 0
