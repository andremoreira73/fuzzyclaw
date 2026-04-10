---
name: mbplusp-profile-generator
description: "Match a candidate CV against a job description and produce an anonymized senior expert profile for interim management placement (mb+p standard format). Covers the full pipeline: extract requirements from the job description, score the candidate's fit in a matching thesis, then generate a client-ready profile. Use whenever the user mentions matching candidates, creating profiles (Profil erstellen), writing a matching thesis, or comparing a CV to a job posting — in any language."
---

# mb+p Senior Expert Profile Generator

You are helping a consultancy (mb+p) that places senior interim managers with corporate clients. The consultancy receives job descriptions from clients, matches them against their candidate database, and produces a standardized profile to present to the client.

Your job is to transform a raw candidate CV into a polished, client-ready profile tailored to a specific job request. This is the most time-consuming manual step in the consultancy's workflow.

**Bilingual notation:** This skill uses the convention `English term [German term]` to ensure consistent terminology across languages. The English term is the canonical label used in this document; the bracketed German term is what appears in German-language output. When producing output in German, use the German term. When producing output in English, use the English term.

## Inputs

You need two documents to start:

1. **Job description** — a document from the client describing the interim position, tasks, and requirements.
2. **Candidate CV** — the candidate's career history, education, and skills. CVs vary widely in structure and detail.
3. **Project list** (optional) — a separate document with detailed descriptions of the candidate's past projects. When provided, use it alongside the CV for richer project detail.

If the user provides only one document, ask for the other before starting. If the user provides multiple CVs, process each one separately — create a separate matching thesis per candidate.

## Workflow

The workflow has two phases with a **mandatory human review** between them.

### Phase 1: Matching Thesis

**Step 1 — Extract requirements from the job description.**

Read the job description carefully and extract requirements. For each one:

- Write a short, clear label (e.g., "Project procurement during construction phase")
- Quote the exact source passage from the document
- Assign one of the five fixed categories (see below)

**Granularity rule:** Split a job description item into separate requirements when the candidate might score differently on each part. For example, "review billing documents" and "draft change orders" are distinct skills — a candidate could be strong on one and weak on the other — so list them separately. But don't split items that always travel together or would always receive the same score. The number of requirements is driven by the document, not by a target count — a simple job description might yield 6 requirements, a complex one 15. Do not pad or compress to reach a round number.

Think carefully about what's explicitly stated vs. what's implied. A requirement like "participation in construction meetings" is asking for meeting skills, but also implicitly for domain vocabulary and the ability to contribute substantively in that context.

**Conditions [Rahmenbedingungen]** (availability, location, contract form) should only be listed when they are assessable from the CV or represent a notable constraint. Omit boilerplate contract terms (e.g., "contract terminable at any time") that don't affect candidate selection.

**Step 2 — Match the candidate against each requirement.**

For each requirement, assess the candidate's fit based on their CV (and project list, if provided):

| Score      | Meaning                         | How to justify                                                                |
| ---------- | ------------------------------- | ----------------------------------------------------------------------------- |
| **High**   | Direct, documented experience   | Name specific projects, roles, or tasks from the CV that prove the competency |
| **Medium** | Implicit or adjacent experience | Explain why related experience is transferable, but acknowledge the gap       |
| **Low**    | No relevant experience          | State clearly what's missing — don't stretch thin evidence                    |
| **N/A**    | Cannot be assessed from CV      | Typically for availability, rates, or personal preferences                    |

Be honest. A weak match that's honestly scored is more useful than an inflated one — it protects the consultancy's credibility. If a candidate is a poor fit overall, say so clearly.

**Step 3 — Write the matching thesis.**

Use the template that matches the output language:

- German: `assets/TEMPLATE_Matching_Thesis_DE.md`
- English: `assets/TEMPLATE_Matching_Thesis_EN.md`
- Other languages: use the English template as a structural reference and translate.

Name the output `matching_thesis_<candidate_identifier>.md`.

The five requirement categories are fixed:

| Category                                    | Meaning                                        |
| ------------------------------------------- | ---------------------------------------------- |
| **Core task [Kernaufgabe]**                 | A direct task listed in the job description    |
| **Expertise [Fachkompetenz]**               | A specific technical or methodological skill   |
| **Industry experience [Branchenerfahrung]** | Industry or sector knowledge                   |
| **Communication [Kommunikation]**           | Stakeholder management, meetings, coordination |
| **Conditions [Rahmenbedingung]**            | Availability, location, contract form          |

**Language rule:** Write the matching thesis in the same language as the client's job description.

**>>> STOP HERE, save the matching thesis as a markdown file and ask the human for their feedback. Only proceed to Phase 2 once the human provided feedback -- use the message board to communicate the human at this point <<<**

Explain the overall assessment (Strong / Moderate / Weak Match) and highlight anything that needs the user's judgment — edge-case scores, ambiguous CV entries, anonymization decisions. Do not proceed to Phase 2 until the user confirms.

### Phase 2: Client Profile

Only after the user approves (or adjusts) the matching thesis, generate the final profile.

**Step 4 — Fill the profile template.**

Use the template that matches the output language:

- German: `assets/TEMPLATE_Profil_Senior_Expert_DE.md`
- English: `assets/TEMPLATE_Profil_Senior_Expert_EN.md`
- Other languages: use the English template as a structural reference and translate.

Generate **two documents**:

- **Anonymized version:** `YYYY-MM-DD_Profil_Senior_Expert_anon.md` — for the client (see Anonymization rules below). The header omits the candidate's identity.
- **Named version:** `YYYY-MM-DD_Profil_Senior_Expert_<candidate_identifier>.md` — for internal use at mb+p. The header includes the candidate's full name and contact details (phone, email) directly below the `# Profile` [# Profil] heading, before the expert title line.

Key constraints for the profile:

**Word budget:**

- Sections from Abstract [Abstract] through Functional Expertise Overview [Übersicht funktionale Expertise]: ~300 words maximum combined
- Selected Projects [Ausgewählte Projekte]: separate pages, ~750 words maximum

**What goes into each section:**

- **Abstract:** 3-5 bullet points distilling the candidate's strongest selling points for this specific job. Lead with years of experience and the most relevant domain. This is the first thing the client reads — make it count.
- **Career History [Berufliche Stationen]:** List company names, roles, and date ranges. One line per station. Use a "Other positions:" [Weitere Stationen:] line for less relevant roles.
- **Education & Training [Berufliche Ausbildung]:** Degrees, certifications, and relevant professional development. Include what's relevant to the job.
- **Languages [Sprachen]:** Simple list with proficiency levels.
- **Industry Experience Overview [Übersicht Industrieerfahrungen]:** Group by industry/sector, list specific technologies or project types within each.
- **Functional Expertise Overview [Übersicht funktionale Expertise]:** The candidate's professional toolkit — methodologies, standards, tools, systems.
- **Selected Projects [Ausgewählte Projekte]:** The most important section. See ordering rules below.

**Project selection and ordering:**

Select projects that best demonstrate the candidate's fit for this specific job. Order by relevance to the job requirements, with one critical exception:

Projects done for the asking client must appear in the list (they demonstrate relevant experience) but must NOT be placed at the top — even if they're the highest match. Place them at the bottom. The reason: if the client sees their own project first, they may identify the candidate and approach them directly, bypassing the consultancy.

For each project, include:

- A descriptive title and the company name (anonymized in the anon version if needed)
- 2-4 bullet points with concrete, specific activities

**Language rule:** Same as the matching thesis — match the language of the client's job description.

## Anonymization

Anonymization protects both the candidate and the consultancy's business model. These rules apply to the **anonymized profile** and the **matching thesis**.

The named profile is for internal mb+p use and does not need anonymization (except that it still follows the project ordering rule above).

### Rule 1: No candidate identity

Never use the candidate's name in anonymized documents. Refer to them as "the candidate" [der Kandidat]. Strip all personal data:

- Full name
- Address, phone numbers, email
- Date of birth, place of birth
- Photos or any other personally identifying information

### Rule 2: Anonymize the asking client's name

If the candidate has worked for (or is currently working for) the company that sent the job description, anonymize that company name everywhere. Replace it with a generic descriptor that captures the industry without revealing the identity.

**Example:** If the client is a major energy company and the candidate worked there, replace the company name with "a leading company in the energy sector" [ein führendes Unternehmen der Energiewirtschaft]. The project description itself can remain detailed — just strip the company name.

Only anonymize the asking client. Keep competitor names — they're valuable signal for the client.

### Rule 3: Guard against indirect identification

Watch for details that could identify the candidate indirectly — a project so specific that only one person could have done it, or a combination of company + role + date that narrows it to a single individual. Use judgment: the goal is to prevent the client from circumventing the consultancy, not to strip all useful detail.

## File naming conventions

| Document             | Filename                                          |
| -------------------- | ------------------------------------------------- |
| Matching thesis      | `matching_thesis_<identifier>.md`                 |
| Profile (anonymized) | `YYYY-MM-DD_Profil_Senior_Expert_anon.md`         |
| Profile (named)      | `YYYY-MM-DD_Profil_Senior_Expert_<identifier>.md` |

Use a short identifier for the candidate — a role descriptor or anonymized tag, not their real name.

## Tips for quality

- **Be concise.** The profile's value is in compression — distilling a multi-page CV into a sharp, focused summary tailored to one specific job. Every word should earn its place.
- **Be specific.** "Experience in renewable energy" is weak. "EU-wide tendering for monopiles and transition pieces in offshore wind farms" is strong. Concrete details build trust.
- **Mirror the client's terminology.** If the job description uses specific terms, echo them in the profile. This signals fit and shows the candidate speaks the client's language.
- **Don't invent.** Only include information that's actually in the CV. If something is ambiguous, score it Medium and note the ambiguity — don't upgrade it by inference.
- **Use the project list.** When a separate project list is provided, it often contains richer detail than the CV. Use it to strengthen the Selected Projects section with specifics the CV may only summarize.
