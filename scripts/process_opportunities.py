#!/usr/bin/env python3
"""
Script to process OSS opportunities from GitHub issues and update the BountyScout database.
"""

import json
import re
import sys
from datetime import datetime
from typing import Dict, List, Optional

import requests

GITHUB_API_BASE = "https://api.github.com"
REPO_OWNER = "laclance"
REPO_NAME = "BountyScout"

def fetch_issue(issue_number: int) -> Optional[Dict]:
    """Fetch issue data from GitHub API."""
    url = f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}"
    headers = {"Accept": "application/vnd.github.v3+json"}
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error fetching issue #{issue_number}: {e}")
        return None

def parse_opportunities(issue_body: str) -> List[Dict]:
    """Parse opportunities from issue body text."""
    opportunities = []
    
    # Pattern to match individual opportunity blocks
    opp_pattern = re.compile(
        r'#### \d+\. \[(?P<repo>[^\]]+)\]\((?P<url>[^)]+)\): (?P<title>.*?)\n'
        r'- \*\*Career score:\*\* (?P<career_score>\d+)/100\n'
        r'- \*\*Priority score:\*\* (?P<priority_score>\d+)/100\n'
        r'- \*\*Execution adjustment:\*\* (?P<adjustment>[+-]?\d+) \(career (?P<career2>\d+) → priority (?P<priority2>\d+)\)\n'
        r'- \*\*Effort:\*\* (?P<effort>.*?)\n'
        r'- \*\*Effort basis:\*\* (?P<effort_basis>.*?)\n'
        r'- \*\*Priority basis:\*\* (?P<priority_basis>.*?)\n'
        r'- \*\*Competition:\*\* (?P<competition>.*?)\n'
        r'- \*\*Repo stars:\*\* (?P<stars>\d+)\n'
        r'- \*\*Repo recent activity:\*\* (?P<activity>.*?)\n'
        r'- \*\*Language:\*\* (?P<language>.*?)\n'
        r'- \*\*Labels:\*\* (?P<labels>.*?)\n'
        r'- \*\*Contribution process:\*\* \[(?P<contrib_text>[^\]]+)\]\((?P<contrib_url>[^)]+)\)\n'
        r'- \*\*Career reasons:\*\* (?P<reasons>.*?)\n',
        re.MULTILINE
    )
    
    for match in opp_pattern.finditer(issue_body):
        opp = {
            "repo": match.group("repo"),
            "url": match.group("url"),
            "title": match.group("title").strip(),
            "career_score": int(match.group("career_score")),
            "priority_score": int(match.group("priority_score")),
            "execution_adjustment": int(match.group("adjustment")),
            "effort": match.group("effort").strip(),
            "effort_basis": match.group("effort_basis").strip(),
            "priority_basis": match.group("priority_basis").strip(),
            "competition": match.group("competition").strip(),
            "repo_stars": int(match.group("stars")),
            "recent_activity": match.group("activity").strip(),
            "language": match.group("language").strip(),
            "labels": [l.strip() for l in match.group("labels").split(",")],
            "contribution_guide": {
                "text": match.group("contrib_text").strip(),
                "url": match.group("contrib_url").strip()
            },
            "career_reasons": match.group("reasons").strip(),
            "discovered_at": datetime.utcnow().isoformat() + "Z"
        }
        opportunities.append(opp)
    
    return opportunities

def parse_rejection_reasons(issue_body: str) -> List[Dict]:
    """Parse verification rejects from issue body."""
    rejects = []
    
    reject_pattern = re.compile(
        r'- \[(?P<reason>[^\]]+)\]\((?P<url>[^)]+)\): (?P<description>.*?)\n',
        re.MULTILINE
    )
    
    # Find the verification rejects section
    rejects_section = re.search(
        r'### Verification rejects\n(.*?)(?=\n###|\Z)',
        issue_body,
        re.DOTALL
    )
    
    if rejects_section:
        for match in reject_pattern.finditer(rejects_section.group(1)):
            rejects.append({
                "reason": match.group("reason").strip(),
                "url": match.group("url").strip(),
                "description": match.group("description").strip()
            })
    
    return rejects

def parse_tuning_candidates(issue_body: str) -> List[Dict]:
    """Parse potential scanner misses from issue body."""
    candidates = []
    
    candidate_pattern = re.compile(
        r'- \[(?P<title>[^\]]+)\]\((?P<url>[^)]+)\): strong-looking near miss: career score (?P<score>\d+)/100 below strategic threshold 55/100\n',
        re.MULTILINE
    )
    
    tuning_section = re.search(
        r'### Potential scanner misses.*?\n(.*?)(?=\n###|\Z)',
        issue_body,
        re.DOTALL
    )
    
    if tuning_section:
        for match in candidate_pattern.finditer(tuning_section.group(1)):
            candidates.append({
                "title": match.group("title").strip(),
                "url": match.group("url").strip(),
                "career_score": int(match.group("score")),
                "below_threshold": 55 - int(match.group("score"))
            })
    
    return candidates

def update_database(opportunities: List[Dict], rejects: List[Dict], tuning: List[Dict]):
    """Update the local database with new opportunities."""
    try:
        with open("data/opportunities.json", "r") as f:
            db = json.load(f)
    except FileNotFoundError:
        db = {"opportunities": [], "rejects": [], "tuning_candidates": []}
    
    # Add new opportunities (avoid duplicates by URL)
    existing_urls = {opp["url"] for opp in db["opportunities"]}
    for opp in opportunities:
        if opp["url"] not in existing_urls:
            db["opportunities"].append(opp)
            existing_urls.add(opp["url"])
    
    # Update rejects
    db["rejects"] = rejects
    
    # Update tuning candidates
    db["tuning_candidates"] = tuning
    
    with open("data/opportunities.json", "w") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    
    print(f"Updated database: {len(opportunities)} opportunities, {len(rejects)} rejects, {len(tuning)} tuning candidates")

def main():
    if len(sys.argv) < 2:
        print("Usage: process_opportunities.py <issue_number>")
        sys.exit(1)
    
    issue_number = int(sys.argv[1])
    issue = fetch_issue(issue_number)
    
    if not issue:
        print(f"Failed to fetch issue #{issue_number}")
        sys.exit(1)
    
    body = issue.get("body", "")
    
    opportunities = parse_opportunities(body)
    rejects = parse_rejection_reasons(body)
    tuning = parse_tuning_candidates(body)
    
    update_database(opportunities, rejects, tuning)

if __name__ == "__main__":
    main()
