# Prompt: Draft an Ideal Requirements Document

> Extracted from `phaseforge-requirements-draft.md` §5–6.2 — that draft is itself an example of the
> output this prompt produces. Paste this whole file into a coding session, followed by a rough
> brief (idea, existing code, conventions doc, reference links), to drive drafting of a project's
> `requirements.md`. This doc is one level up from phase files / user stories: it is the
> single source of truth those later, smaller work orders will quote from — not the work order
> itself.

## Your task

Turn a rough, possibly ambiguous brief into a requirements document precise enough that a later
process can carve it into small, independently implementable work items (phases, tickets, user
stories) without needing to re-derive intent, re-ask settled questions, or guess at anything the
brief left open. You are not writing prose for a human to admire — you are writing the document a
downstream agent (or engineer) will quote from verbatim. Every ambiguity you cannot resolve from
the brief becomes an explicit, answerable question — never a silent assumption.

## Required structure

Use these sections, in this order. Omit a section only if it is genuinely inapplicable (say so
explicitly rather than leaving a gap) — the closer this doc mirrors it, the more usable everything
downstream is.

1. **Purpose / origin.** Why this document exists and what it descends from: a prior pattern being
   generalized, a reference implementation, a competitor, a rough idea with no reference at all.
   State it plainly so a reader knows how much of what follows is derived vs. invented.

2. **Problem statement.** What's broken or missing today, in concrete terms, and why solving it
   needs a deliberate process rather than ad-hoc effort. Ground it in an observed failure mode or
   gap, not an abstract aspiration.

3. **Goals.** A numbered list (`G1`, `G2`, …) — each one a single, falsifiable outcome, not a
   direction. "Support X for any Y" beats "make things better." IDs let later documents (phase
   files, tickets) cite a goal instead of re-explaining it.

4. **Non-goals.** Explicit boundary-setting: what this effort deliberately will *not* do, even
   though it's adjacent or tempting. This is where scope creep dies before it starts. If something
   was considered and rejected, say so and say why — a rejected idea with no rationale invites
   someone to revisit it needlessly.

5. **Glossary** (if the domain has any term that could be misread). Define once, here, and never
   redefine downstream — anything that needs a definition twice is a sign the term is ambiguous.

6. **Feature/component breakdown.** If the effort has more than one moving part, a short
   at-a-glance table (question it answers / shape / input / output) before the detailed sections —
   orients a reader before they hit the detail.

7. **Detailed functional requirements, section by section.** This is the load-bearing part of the
   document. For each functional area:
   - State behavior specifically enough to quote **verbatim** into a smaller downstream work item
     — a later reader should never need to open this document again once they have their excerpt.
   - Name concrete things precisely: exact file/hook/command names, exact CLI subcommands, exact
     data shapes — never "follow conventions" or "handle appropriately."
   - Where a boundary cuts across this section (part of it belongs to a different downstream unit
     of work), say so explicitly, the way you'd carve a phase's scope.
   - Surface known ambiguities, discrepancies, or "don't fix this" traps inline, with an explicit
     instruction not to unilaterally resolve them.

8. **Non-functional requirements.** Performance, sizing bands, safety rails, operational
   constraints — anything downstream work must satisfy but that isn't a feature per se. Prefer
   empirically-derived bands (with their observed range) over invented targets, when you have data
   to derive them from.

9. **Out-of-scope / explicit exclusions** (if distinct from Non-goals — Non-goals is about the
   effort's purpose, this is about specific features/behaviors considered and excluded).

10. **System/tool shape** (if applicable). Where this lives, how it's built, what it's made of —
    concrete enough that scaffolding can start immediately, not "some kind of CLI."

11. **Open decisions log.** A numbered list of every question you could not resolve from the brief
    alone, each one phrased as a concrete, answerable question (not "TBD" or a vague concern) —
    with enough context that a human can answer it without re-reading the whole document. This is
    the single most important section for iterative refinement: an unresolved, flagged question is
    a valid, honest terminal state — never let one get silently dropped or silently guessed away.

12. **Next step.** What happens immediately after this document is approved — concretely, not "then
    we build it."

## Properties every version of this document must have

- **Verbatim-quotable.** Anything a downstream work item needs, it should be able to copy-paste
  from here rather than reference-and-hope. Write for that copy-paste, not for narrative flow.
- **ID'd and traceable.** Goals, phases, decisions — number them so later documents can cite an ID
  instead of re-describing intent, and so a reviewer can check every numbered item got addressed
  somewhere.
- **Explicit non-goals, always.** A document with goals but no non-goals hasn't actually bounded
  scope yet.
- **Ambiguity surfaced, never guessed.** If the brief doesn't say, don't infer silently — write the
  question down in the open-decisions log. A wrong guess costs far more than an explicit question.
- **Testable, not aspirational.** Prefer requirements phrased so a checklist item can be derived
  from them directly ("returns X given Y") over ones that only a human can judge subjectively
  ("works well").
- **Sized to reality, not invented.** When you have empirical data (an existing corpus, prior
  examples) to derive sizing bands or structural rules from, derive them and cite the source range;
  don't invent round numbers.
- **Self-contained per section.** A reader should be able to jump straight to the section they need
  without having read the ones before it — cross-reference by number rather than assuming context.

## Process guidance

- Treat this as iterative, not one-shot. Draft from the brief, flag every gap in the open-decisions
  log, and expect a human checkpoint before the document is "approved" — approval means the log has
  been reviewed and answered or explicitly deferred, not that drafting stopped.
- Never let a resolved answer disappear back into ambiguity — when a human answers an open
  decision, fold the answer into the relevant section and either remove the log entry or mark it
  resolved with a pointer to where it landed.
- Before declaring the document done, self-check it against the "Properties" list above and against
  the "Required structure" list — call out explicitly any section you omitted and why.
