# Context

In this workflow, we will scans industry news and identifies companies that likely need our services — because they're building a new plant, launching a product in the EU, or expanding into regulated sectors.

# Step 1

Dispatch the market-researcher agent: "Identify recent announcements (last 30 days) of companies expanding industrial operations in Germany or the EU. Look for: new factory construction, product launches requiring CE marking, energy infrastructure projects, data center builds, or companies entering regulated markets (medical devices, automotive, renewable energy). For each, note the company name, what they're doing, and which certifications or inspections they would likely need."

# Step 2

Dispatch the summarizer agent with the findings. Produce a structured BD lead report with columns: Company | Activity | Likely Certification Need | Priority (high/medium/low based on deal size and urgency). Group by industry sector.
