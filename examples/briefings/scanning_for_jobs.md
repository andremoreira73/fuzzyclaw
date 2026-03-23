# Context

This workflow is to help finding interesting job openings in the market, with specific target companies.

Profile:

**< NB for USER: add here your profile explaining what you do, what you want, etc. Paste a short CV. >**

# Coordinator Workflow

Your starting point is to read a list of URLs, see it below in <Initial URL list>.

## Steps

Step 1:
Prepare an internal list where each element is a single URL that has not been scraped yet.

Step 2:
Dispatch parallel **career-scraper agents** to scrape each URL from your URL list. One agent per URL. Give each web-scraper agent the prompt provided under <Prompt for the scraping agents>.

Step 3:
Use your URL list to keep track of the progress for the different URLs.

- URLs that have not been scraped are "pending"
- URLs that you sent for scraping are "in-progress"
- URLs that that agent finished scraping are "completed"

If an agent failed to scrape an URL (timed-out), you can try this URL one more time. If it still fails to scrape, mark it as "completed" in your list.

Step 4:
As agents report back, read their reports and decide:

- Did the agent scrape a single job description? If yes, the check if this is an appropriate job. If so, include it in your final report, otherwise, discard it.
- Is this a list of jobs? If yes and it has several URLs, decide which ones you should explore. Include these in your internal URL list. Be judicious in this decision, focus on promising URLs only.

Step 5:
Go back to step 2 until every URL in your internal URL list is either completed or permanently failed. Do NOT submit your report while agents are still running — wait for them to finish and read their reports first.

Once all URLs from your internal URL list are resolved and all reports have been read, prepare your final report with your findings including **only jobs that are truly relevant for this search**. For each job include the title, company, and the reasons why you think this is relevant; if other details like location, time to start, salary, etc. are available, include them too. Include at the bottom of the report the full list of URLs that were scraped.

Prioritize. Be critical and judicious, if only very few or none of the jobs found fit what we are looking for, say so, and do not include some jobs just for the sake of having them there. This workflow is to save time, not to fill blanks.

---

<URL list>

** < NB: add here a list of sites of the companies you are looking forward to work for:
e.g. https://www.anthropic.com/careers/jobs or https://openai.com/careers/search/ >**

</URL list>

---

<Prompt for the scraping agents>

(copy it as it is, only exchange <URL> for the actual URL):

You are an expert job analyzer.

Use your tools to scrape the website: <URL>

## Task

For each scraped text, decide whether it is:

1. JOB LIST - a page listing several job postings.
2. SINGLE JOB POSTING - a page describing one specific role.
3. NOT RELEVANT - anything else.

## Job criteria

A role is relevant when most of these apply:

- Seniority: **< NB for USER: add your criteria >**
- Focus: **< NB for USER: add your criteria >**
- **Exclude:** **< NB for USER: add your criteria >**

## Data extraction

### For JOB LIST pages

List of extracted URL links that lead to job postings fitting the Job criteria.

### For SINGLE JOB POSTING:

Extract: title, company, location, duration, key_requirements, description_summary, apply_url.

</Prompt for the scraping agents>
