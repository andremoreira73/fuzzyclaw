# Example Briefings

These are real briefings you can paste into FuzzyClaw's briefing editor. They range from simple (one agent, one task) to complex (multi-agent orchestration with iterative exploration).

## Three examples

- **[market_landscape.md](market_landscape.md)** — Quick market overview with a single agent dispatch. Good starting point to see how FuzzyClaw works.
- **[business_development.md](business_development.md)** — Two-agent pipeline: research agent finds leads, summarizer agent structures them into a BD report. Shows agent chaining.
- **[scanning_for_jobs.md](scanning_for_jobs.md)** — Multi-agent job search across target company career pages. The coordinator manages a URL queue, dispatches scrapers in parallel, reads reports, decides which links to explore further, and produces a curated list of relevant openings. Fill in the `<NB for USER>` placeholders with your profile and target companies.

## How to use

1. Copy the content of a briefing file
2. Create a new briefing in the FuzzyClaw dashboard
3. Paste the content, pick a coordinator model, and hit **Save & Launch**
4. Optionally add a schedule ("every weekday at 9am") and click **Schedule**

Feel free to modify these to fit your needs — briefings are just markdown.
