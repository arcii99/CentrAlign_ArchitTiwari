# Architecture

How Nova is put together, in plain words. Last updated: 10 October 2026 (after Phase 5).

For the reasons behind each choice and the measurements, see [DESIGN.md](DESIGN.md). For the results, the
limitations and what comes next, see [WRITEUP.md](WRITEUP.md).

## The idea in one paragraph

Nova is an AI employee in the accounts-payable team of a pretend company, Northwind Robotics. You give it a short
request, like *"Pay the Globex invoice"*. Nova reads the company handbook to find out how that is done, reads the
email and the invoice, does the work in the company's systems, asks a person when the rules require it, and at
the end proves the work is really done by checking the systems again. The AI model behind it is small and runs
for free on this laptop. To make a small model safe and reliable, **the model only suggests what to do next;
ordinary code decides what is allowed to happen.**

## The big picture: two halves

The project has two separate halves. They only talk through the same doors a human employee would use.

```
 ┌───────────────────────────── NOVA (agent/) ──────────────────────────────┐
 │                                                                          │
 │   The model (qwen3.5:4b on Ollama)      The runtime (Python code)        │
 │   "the brain": reads, decides the  ◀──▶ "the manager": runs the stages,  │
 │   next action                           keeps the plan, enforces rules   │
 │                                                                          │
 │                         22 tools: Nova's hands                           │
 └────────────────┬─────────────────────────┬─────────────────────┬─────────┘
                  │ a browser               │ the mail API        │ files
                  ▼                         ▼                     ▼
 ┌────────────────────── THE PRETEND COMPANY (world/) ──────────────────────┐
 │   Finance Portal (:8101)     Mail server (:8102)    Shared drive         │
 │   an old web app: vendors,   the AP mailbox, where  handbook, AP         │
 │   bills, payments            invoices arrive        tracker sheet        │
 │                                                                          │
 │   The grader: reads the databases, mailbox and files afterwards and      │
 │   marks the work right or wrong. It never trusts what Nova says.         │
 └──────────────────────────────────────────────────────────────────────────┘
```

Nova's code never reaches inside the pretend company. The company's code never helps Nova. That separation is what
makes the grade honest.

## Half 1: the pretend company (`world/`)

| Part | What it is | How Nova uses it |
|---|---|---|
| **Finance Portal** | An old-style web app for vendors, purchase orders, receipts, bills and payments. Strict about formats; it doesn't check approvals and happily accepts duplicates, like many real legacy systems | Only through a browser: there is no API |
| **Mail server** | The AP team's mailbox. Invoices arrive here as PDF attachments, some of them scanned images | Through its web API |
| **Shared drive** | A folder with the company handbook (the procedures and the approval rules), the AP tracker spreadsheet, and other files | Reading and writing files |
| **Scenarios** | Five tasks, each with a trap (hidden tax rules, a fraud attempt, a duplicate payment claim, ...). A reset puts the company into the starting state of a task | Not visible to Nova |
| **Chaos mode** | Makes the portal misbehave on purpose: slow pages, expired logins, a pop-up that blocks the screen, errors after saving | Nova has to cope (Phase 3) |
| **Grader** | Compares the company's systems after a run with how they were before, and checks safety rules (no double payments, no paid bill reopened, no leaked account numbers, ...) | Not visible to Nova |

## Half 2: Nova (`agent/`)

Nova has three parts:

1. **The model** is the brain. It reads what Nova sees and answers with the next action, such as "open this page"
   or "fill in this form". It is small (4 billion parameters), so it is fast and free but makes mistakes.
2. **The runtime** is the manager. It is ordinary Python code. It runs the stages of a task, keeps the plan and the
   facts so the model doesn't have to remember them, shows the model a short, tidy view of what is happening, and
   checks every important action before it happens.
3. **The tools** are the hands: 22 actions the model can ask for.

| Tool group | Tools |
|---|---|
| Handbook | `handbook_search`, `handbook_read` |
| Mail | `mail_list`, `mail_read`, `mail_attachment`, `mail_send`, `mail_forward` |
| Files | `files_list`, `file_read`, `sheet_append_row`, `sheet_write` |
| Browser | `browser_open`, `browser_fill`, `browser_click` |
| Maths | `calc`, `date_calc` (the model never does sums or dates in its head) |
| People | `ask_human`, `request_approval` |
| The plan | `split_work`, `submit_plan`, `step_done`, `finish` |

## How one task runs, stage by stage

Every task goes through the same stages. They all use the same runtime and the same tools; what changes from
stage to stage is what the model is allowed to do and what it is shown.

```
  Request: "Pay the Globex invoice"
        │
        ▼
  1. UNDERSTAND   Pick the handbook procedures that apply; read the email, the invoice PDF, the files.
        │         Reading only: nothing in the company can be changed yet.
        ▼
  2. CONTRACT     One careful step that writes down, before any work:
        │           • the goal
        │           • the facts, copied from the documents (code checks each one is really there)
        │           • how we will know it is done (the success criteria)
        │           • the plan: the handbook's own steps
        ▼
  3. ACT          Work through the plan, one action at a time:
        │           the model picks an action → the guards check it → the tool runs → the result is shown
        │           After every action the model is reminded of the goal, the facts, the plan, and the
        │           handbook rule for the next step.
        │         When a person is needed (a question, or an approval the rules require), the request goes to
        │         an inbox. If nobody answers now, the run is saved and stops; it carries on later from the
        │         same point, even after a restart.
        ▼
  4. VERIFY       After Nova says "finished", a separate checker with its own read-only browser opens every
        │         record Nova created, finds every email it sent, re-reads every spreadsheet row, and checks
        │         each success criterion. If something is wrong, Nova gets one chance to look into it.
        ▼
  5. REPORT       A report a person can read (report.md): the verdict, the contract, every check and its
                  result, every approval, every rule that fired. Plus files for machines and for the grader.

  6. LEARN       If a person asked Nova to remember their answer, it becomes a standing instruction for
                  later tasks (see People in the loop).
```

**A request about several documents** ("Process the AP inbox", with four invoices in it) is split during
UNDERSTAND: the model lists one item per document, and each item then runs through all the stages above as its
own task, one after another, with its own contract, plan and verification. That is how a person works through an
inbox, and it keeps each task small enough for a small model. If one item waits for a person, the whole request
waits; when that item is done, the rest follow. The request's report lists every item with a link to its report.

## The guards: what every important action goes through

"Important" means anything that changes the company's systems: submitting a portal form, sending an email, adding
a spreadsheet row, asking for an approval. Each one passes these checks, in this order. If any check says no,
nothing is changed, and the model is told why and what to do instead.

```
  the model asks for an action
        │
        ▼
  ① Repeat check     Is this exactly what you already tried, with nothing changed since?  → "do something else"
        │
        ▼
  ② Provenance       Did every number, date and ID come from something you read or calculated?
        │            Made-up values are refused.                                       → "copy it or calc it"
        ▼
  ③ Maker-checker    A second, separate model session works out the dates and amounts again
        │            from the handbook rule, with the calculator. If it disagrees, stop.    → "here's why"
        ▼
  ④ Policy gate      The company's rules (agent/policy.yaml). For portal forms this check sits
        │            inside the browser itself, so it works however the model got there.
        │              allow · needs approval · deny
        ▼
  the action happens, and the result is shown to the model
```

The approval rules in plain words:

- A payment above ₹1,00,000 needs the CFO. A new vendor bank account needs the AP Manager. Cancelling a bill needs
  the CFO. Nothing may ever be deleted.
- An approval covers **one specific thing, once**: "₹1,74,000 for bill BILL-2026-0033" allows exactly that payment.
  It can't pay a different bill, a different amount, or the same bill twice.
- The rules decide who approves. The model can't pick a friendlier approver.
- Emails: a full bank account number is never allowed. An outside address must be a contact listed in the vendor
  records; otherwise the AP Manager must approve.
- Once Nova has said the work is finished, any further change counts as a correction, and the AP Manager approves it.

The model can't read or search the rules file, so no wording in a prompt or in an email can talk it out of them.

## When the company's systems misbehave

Real systems are slow, log you out, show pop-ups, and sometimes fail. The worst failure is an error page *after*
the change was saved: try again blindly and the invoice is paid twice. Nova handles these like this:

| What goes wrong | What happens |
|---|---|
| A slow page | The browser waits for it |
| Logged out | The browser signs in again by itself; a form that was lost is reported as not saved |
| A pop-up notice blocks the page | Nova is told, and clicks it away like a person would |
| A server error on a submission | **The runtime checks the system itself**, read-only, in a separate tab: if the record is there, the step is done (and its approval counts as used); if not, it is safe to try again |
| The same submission again | Refused: a payment for the same bill, or a bill for the same invoice, is never sent twice in one task |
| A tracker row with gaps, or a second row for the same invoice | Refused: a column every earlier row fills must be filled (or confirmed empty), and a second row for the same invoice must be one explained correction |

## People in the loop

There are two kinds of human input:

1. **Questions** Nova asks because something is unclear or contradictory (`ask_human`).
2. **Approvals** the rules require (`request_approval`). The runtime enforces these; the model can't skip them.

Both go into an **inbox** saved in a small database file. A person can answer at the terminal straight away, or
later with `agent inbox` and `agent approve ... --resume`. While Nova waits, its whole working state is saved, so a
restart loses nothing.

**Memory: the person decides what Nova remembers.** When answering, a person can say "remember this" (`--remember`,
or yes at the terminal). The answer then becomes a **standing instruction**, saved with who gave it and when. In
later tasks, before Nova asks someone a question, it checks these: if one already answers the same question (even
worded differently), Nova acts on it instead of interrupting again, and its report says whose instruction it used.
Instructions related to a task are also shown to the model as guidance.

Three limits keep memory safe:
- Only people can create a standing instruction; nothing in an email or a document can.
- An answer is remembered only when the person asks: one answer doesn't quietly become a rule.
- Instructions never override the company's rules. The policy check, approvals and the other guards work exactly as
  before.

`agent lessons` lists what Nova remembers; `agent forget L-1` retires an instruction.

## What Nova writes down

| Record | What it is for |
|---|---|
| **Event log** (`var/runs/runs.db`) | Every step of every run, in order. It can only be added to, never edited, so it is a trustworthy audit trail |
| **Saved state** | Everything needed to carry on a paused run: the conversation, the plan, the facts, the pending action |
| **report.md** | The evidence report for a person |
| **evidence.json** | The same, for machines |
| **facts.json** | The approvals and questions, for the grader |

## How we help a small model

A 4-billion-parameter model is much weaker than the big cloud models, so the system does a lot of the work:

- **Short, tidy views.** A web page becomes a few lines: errors first, tables as rows, forms as labelled fields.
  Only the last three results are shown in full; older ones shrink to one line.
- **The runtime keeps the memory.** The goal, the facts and the plan are shown back after every action, so the
  model never has to remember them.
- **Rules where they are needed.** When a form is on screen, the handbook rule for that form appears right under it.
- **No mental maths.** Sums and dates go through `calc` and `date_calc`. If the model works a value out in its
  head, the refusal suggests the exact `calc` call that proves it.
- **Spotting loops.** If the model says a step is done but doesn't mark it, repeats itself word for word, or keeps
  opening the same page, the runtime notices and tells it what to do instead. After five identical tries the run stops.
- **Thinking only when stuck.** The model answers directly most of the time (fast). It "thinks" step by step only
  after repeated failures.

## Where things live

| File | What it does |
|---|---|
| `agent/runtime.py` | The manager: the stages, the guards, the tidy view, loop spotting, pause and resume |
| `agent/contract.py` | Writes the task contract and checks its facts against the documents |
| `agent/provenance.py` | Refuses values that came from nowhere |
| `agent/review.py` | The maker-checker: works dates and amounts out again with the calculator |
| `agent/policy.yaml`, `agent/policy.py` | The company's rules for Nova, and the code that applies them |
| `agent/browser.py` | Drives the portal and turns each page into a short summary; the policy check sits in here |
| `agent/tools.py` | The 22 tools |
| `agent/verify.py` | The final, independent check |
| `agent/report.py` | Writes the evidence report |
| `agent/human.py` | The inbox; asking people at the terminal; a scripted "person" for tests |
| `agent/memory.py` | Searches the handbook; keeps the standing instructions people asked Nova to remember |
| `agent/eventlog.py`, `agent/state.py` | The event log and the run's state |
| `agent/llm.py` | Talks to the model (Ollama, or any compatible server) |
| `agent/docs.py` | Reads PDFs (with OCR for scans) and spreadsheets |
| `agent/prompts.py`, `agent/config.py` | The model's instructions; settings and the portal's address book |
| `agent/__main__.py` | The command line: `run`, `resume`, `inbox`, `approve`, `lessons`, `show`, `runs` |
| `agent/console/` | The console in the browser: runs as they happen, the inbox for people, memory, benchmark results |
| `world/portal/`, `world/mail/` | The Finance Portal and the mail server |
| `world/scenarios.py`, `world/checkers.py` | The five tasks and the grader |
| `bench/` | The benchmark: every task many times, with and without chaos, graded by the grader |
| `tests/` | 67 automatic tests, including a full task 1 with a scripted stand-in for the model |

## Does each phase have a different architecture?

**No. There is one architecture, and each phase fills in or strengthens a part of it.** The plan was drawn at the
start (UNDERSTAND → CONTRACT → ACT → VERIFY → REPORT → LEARN, with guards and people in the loop). Each phase
builds part of that drawing; none of them replaces another phase's work.

| Phase | What it adds | Where in the picture |
|---|---|---|
| 0. The pretend company | The portal, mailbox, drive, tasks, chaos and grader | The bottom half |
| 1. Core loop (done) | The stages, the tools, the runtime, provenance, the maker-checker | The top half: UNDERSTAND, CONTRACT, ACT |
| 2. Trust layer (done) | The policy gate, approvals, the inbox, pause and resume, VERIFY, REPORT | Guard ④, people in the loop, the last two stages |
| 3. Robustness (done) | Coping with chaos: checking the system after a server error so nothing is paid twice, never sending the same thing twice, a tracker that stays one row per invoice. Splitting a many-invoice request into one task per invoice. Tasks 2–4 | Inside ACT, the tools and the browser; a split request runs the same stages once per item. No new stage |
| 4. Memory (done) | Standing instructions: what people asked Nova to remember, used before asking again and shown as guidance | The LEARN stage, feeding back into later tasks; no change to the guards |
| 5. Measuring and showing (done) | The benchmark (every task many times, with and without chaos, next to a scripted bot), the console in the browser, the write-up and the demo | A harness and a screen **around** Nova; Nova itself doesn't change |
| Stretch: desktop apps | Seeing and clicking a desktop app through screenshots and OCR | One more set of tools; the stages and guards stay the same |

The stages within one task don't have separate architectures either. They share the same runtime, model, tools
and records. What differs is what each stage allows: UNDERSTAND can only read, CONTRACT writes the plan but changes
nothing, ACT can change things but only through the guards, and VERIFY uses its own browser that can only read.
