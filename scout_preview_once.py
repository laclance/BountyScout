import os

import opportunity_scout as scout
import scout_bounties as bounty

token = os.environ.get("GITHUB_TOKEN")
seen = bounty.load_seen_bounties()
repo_cache = {}
guide_cache = {}

tailscale_query = "is:issue is:open no:assignee repo:tailscale/tailscale sort:updated-desc"
tailscale_items = bounty.search_github(
    tailscale_query, token, per_page=scout.STRATEGIC_SEARCH_PER_PAGE
).get("items", [])
tailscale_clean = [item for item in tailscale_items if bounty.is_clean_candidate(item)]
print("=== TAILSCALE RAW POOL ===")
print(f"search results: {len(tailscale_items)}")
print(f"clean candidates before inspection cap: {len(tailscale_clean)}")
print(f"inspection cap: {scout.STRATEGIC_INSPECT_PER_REPO}")

paid, paid_rejects, paid_examples = scout.discover_paid(
    token, seen, repo_cache, guide_cache
)
strategic, strategic_rejects, strategic_examples, strategic_audit = (
    scout.discover_strategic(
        token,
        seen,
        {candidate["url"] for candidate in paid},
        repo_cache,
        guide_cache,
    )
)

tailscale_found = [
    candidate
    for candidate in strategic
    if candidate["repo"] == "tailscale/tailscale"
]
tailscale_found.sort(
    key=lambda item: (
        item["priority_score"],
        item["career_score"],
        -item["comments"],
    ),
    reverse=True,
)

print("=== TAILSCALE SURVIVORS ===")
if not tailscale_found:
    print("No Tailscale candidate survived full verification.")
for index, candidate in enumerate(tailscale_found, 1):
    print(scout.markdown_candidate(candidate, index))

by_url = {}
for candidate in paid + strategic:
    old = by_url.get(candidate["url"])
    if not old or candidate["priority_score"] > old["priority_score"]:
        by_url[candidate["url"]] = candidate

queue = sorted(
    by_url.values(),
    key=lambda item: (
        item["priority_score"],
        item["career_score"],
        item["cash_score"],
        -item["comments"],
    ),
    reverse=True,
)[: scout.REPORT_LIMIT]

print("=== VERIFIED QUEUE ===")
if not queue:
    print("No new verified OSS opportunities found.")
for index, candidate in enumerate(queue, 1):
    print(scout.markdown_candidate(candidate, index))

print("=== POTENTIAL SCANNER MISSES ===")
if not strategic_audit:
    print("No potential scanner misses were surfaced.")
for item in strategic_audit:
    print(f"- {item['url']}: {item['reason']}")

print("=== VERIFICATION REJECTS ===")
for item in (paid_examples + strategic_examples)[:30]:
    print(f"- {item['url']}: {item['reason']}")

print("=== REJECT COUNTS ===")
rejects = dict(paid_rejects)
for reason, count in strategic_rejects.items():
    rejects[reason] = rejects.get(reason, 0) + count
print(rejects)
