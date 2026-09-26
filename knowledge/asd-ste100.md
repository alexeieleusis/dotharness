---
name: asd-ste100
description: "Use when an English document must be edited or rewritten for clarity: documentation, prompts, tool descriptions, error messages, emails, or prose that reads dense, jargon-heavy, or ambiguous. A quick, self-contained set of rules and a short editing workflow, applied while preserving the author's meaning, hedges, and voice. The text arrives as a file name or pasted directly. Triggers: tighten this, simplify, disambiguate, plain-language rewrite, plain-English edit, review the writing, rewrite so an agent cannot misread this. Not for copy where persuasion itself is the point, unless the user asks."
version: 2.1.0
---

# Plain-English Editing: A Quick Guide

This guide has one set of rules and one workflow for editing English text so it reads clearly. It works for any text a person reads. It also works for text a machine must parse alone: an error message, a tool description, or an inter-agent instruction. In that text, a wrong reading has a real cost. If a reader can misread "close the valve" as "the valve that is near" instead of a command, a language model can too.

The two main sources of misreading are words with more than one meaning and sentences with more than one possible structure. The rules below fix both. This guide is complete enough to edit most documents well. It adds nothing it does not need.

## How to Apply

**Strict** — use it where a wrong reading has a cost: procedures, error messages, tool and function descriptions, inter-agent instructions, and safety text. Apply every rule below.

**Everyday** — ordinary prose: READMEs, PR descriptions, changelogs, documentation, emails, posts, and marketing or persuasive copy. Apply the structure rules in full. Treat the word-choice rules as a preference. Prose needs some word range. Rewriting prose in Strict mode reads as a personality transplant, not as a clarification. For marketing or persuasive copy, let sentence length and parallelism vary where the persuasive effect depends on it — the structure rules exist to prevent misreading, not to flatten a deliberate rhetorical build-up.

Pick one before rewriting. If the user does not say which, infer from the text type.

## The Rules

### Sentence structure

| Rule | Do | Don't |
|---|---|---|
| One idea per sentence | "Open the file. Read line 3." | "Open the file and read line 3, then check if it matches." |
| Short sentences | ≤20 words for instructions, ≤25 words for descriptions | Long compound or subordinate-clause sentences |
| Active voice | "The agent deletes the file." | "The file is deleted." — unless the actor is unknown or irrelevant |
| Simple tenses | "We received the report." | "We have received the report." — except where the compound form carries information the simple form cannot: current relevance ("the job has completed" — the output is available now) or a hedge ("may have failed") |
| No semicolons | Split into separate sentences | Any semicolon at all |
| No phrasal verbs | "Remove the panel." / "Start the job." | "Take off the panel." / "Spin up the job." — a two-word verb has meanings the parts do not predict |
| Noun clusters ≤3 words | "fuel pump valve" | "high pressure fuel pump inlet valve assembly" |
| No elision | Keep the subject, verb, and article explicit even if the sentence reads longer. Resolve ambiguous pronouns | "Files not backed up will be lost" — it is ambiguous which files |
| One topic per paragraph | ≤6 sentences | Multi-topic paragraphs |
| Lists for sequences | Numbered or bulleted list for 3+ steps or conditions | A sequence hidden inside one prose sentence |
| Direct instructions | State the condition, action, and expected result | Procedures that hide the action |
| Code and names | Preserve identifiers, commands, product names, legal text, and quotations | Simplifying them silently |

### Word choice

- **One word, one meaning.** One term for the same thing throughout the document: not "user"/"customer"/"client", not "check"/"verify"/"confirm" for the same action.
- **Plain words.** Use the common, everyday word. No jargon, foreign phrases, idioms, slang, corporate buzzwords, or vague nouns ("thing", "aspect", "solution", "functionality") — unless a technical term is required for accuracy.
- **Verb, not noun.** "Analyze the log." not "Perform an analysis of the log."
- **Concrete over abstract.** Strong verbs over nominalizations. Specific claims over generalities.
- **Define technical terms at first use.** Keep established domain terms — do not replace a precise term merely because it is unfamiliar.
- **Preserve modality.** "May have failed" stays "may have failed". Never promote a hedge to a fact. Never invent a certainty the source did not state.

### Judgment calls

- Prefer clarity to impressiveness.
- Make every important claim specific enough to examine, and proportional to the evidence.
- Remove intensifiers ("very", "highly", "truly"), redundant qualifiers, superlatives, and promotional language.
- Keep a metaphor only when it clarifies. Remove mixed, decorative, or clichéd metaphors.
- Preserve distinctions, qualifications, and uncertainty. Brevity must not produce false confidence.
- Use examples early, especially for abstract or technical ideas. A small, specific example often explains more than a page of generalities.
- Lead with the point. Do not open with filler. Do not repeat claims or background already made.
- Retain the author's voice unless it obstructs the reader.
- Context, audience, and meaning matter more than a rigid rule. Do not apply the rules mechanically.

## How to Edit

The text arrives one of two ways: a **file name** (read that file before doing anything else) or **the text itself** in the request. Work on exactly that text. If a file name is given and the file cannot be read, say so and stop.

1. Pick the mode (Strict or Everyday).
2. Read the input text once for meaning — do not start rewriting before you understand what it must still say afterward.
3. Check the argument before changing sentences. Identify what the text tries to make the reader understand, believe, decide, or do. Make sure the important idea appears early and each section advances it. Do not polish sentences when the structure needs rebuilding. Fix the structure first.
4. Examine the text sentence by sentence. Flag every rule violation and every judgment call that applies.
5. Rewrite each flagged sentence to fix the violation while preserving the original meaning exactly:
   - Cut words, clauses, and sentences that serve no purpose.
   - Replace jargon, buzzwords, long words, and outworn figures of speech with everyday English when accuracy permits.
   - Convert passive constructions to active ones when the actor matters and is known.
   - Check modality before writing. Hedges are what a word limit makes you want to cut. The confidence level is part of the claim. A shorter sentence that upgrades a hedge to a fact is a different claim.
   - Never add a fact the source did not state — a supplied cause, frequency, or mechanism has stopped being a rewrite.
   - If a rewrite would drop necessary precision (a safety condition, a scope qualifier, a number), keep the longer phrasing. Flag it instead of silently simplifying.
6. Run a final pass. Check that each technical term is consistent, each instruction states the required action, and each exception is intentional.
7. Output the rewritten text (see Output).
8. If the input already complies, say so — do not force changes onto compliant text.

## Output

**Default: the rewritten text, and nothing else.** Most callers want a result they can paste straight back into the document. No preamble, mode announcement, violation count, summary, or closing offer.

Two permitted additions, both appended after the text, never in place of it:
- If a step kept a longer phrasing on purpose, add one line starting with `Kept as-is:` that names the phrase and the precision that would have been lost. Omit the line when there is nothing to report.
- If the input already complied, output it unchanged followed by one line: `No changes needed.`

**On request: the rule table.** When the user asks to see the reasoning — "show the diff", "which rules did it break", "explain the changes", "before/after" — output this table instead:

```markdown
| Rule violated | Original | Simplified |
|---|---|---|
| Present perfect | "We have received your request." | "We received your request." |
| Noun cluster | "the agent task queue priority handler" | "the handler that sets task-queue priority" |
```

Follow the table with a one-line note on anything you deliberately did **not** simplify, and why (usually: simplifying would lose required precision).

## What Not to Do

- Fabricate facts, measurements, quotations, or consensus. Flag claims that require verification.
- Convert "may have failed" into "failed", or "could be caused by X" into "X is the cause" — losing a hedge changes the claim.
- Make weak content sound true. These rules fix the *form* of a text, not its substance. A hollow paragraph rewritten under them becomes a clean, short, well-punctuated hollow paragraph. If the text has nothing to say, say so instead of polishing it.
- Shorten past the point of clarity, or confuse terseness with clarity. Cutting words is not the goal. Removing ambiguity is. Stop when the sentence is unambiguous, not when it is shortest. Necessary explanation is not clutter. Cutting it out is a defect, not a win.
- Force one dialect on text that is already internally consistent — make usage consistent first, and use American English spelling only for new or mixed text.

## Done When

The finished text should meet every item:

- The reader grasps the point quickly.
- The reader follows the reasoning without avoidable effort.
- The reader can tell evidence from assertion.
- The reader remembers the ideas, not the ornament.
