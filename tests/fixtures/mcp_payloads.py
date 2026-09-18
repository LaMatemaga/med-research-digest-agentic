"""MCP tool payloads matching the real upstream servers, used to test our parsers.

Markdown fixtures reproduce what each tool's `format()` renders, line for line:
- cyanheads/pubmed-mcp-server v2.10.13
  src/mcp-server/tools/definitions/search-articles.tool.ts  (format)
  src/mcp-server/tools/definitions/fetch-articles.tool.ts   (format, escapeMarkdownInline titles)
- cyanheads/clinicaltrialsgov-mcp-server
  src/mcp-server/tools/definitions/search-studies.tool.ts   (format, renderIndexEntry)

Structured fixtures follow each tool's zod `output` schema (the `structuredContent`).
"""

SEARCH_MARKDOWN = "\n".join(
    [
        "## PubMed Search Results",
        "**Query:** (Nervous System Diseases[MeSH]) AND (2026/08/18:2026/09/17[PDAT])",
        "**Returned:** 3 | **Offset:** 0",
        "**Search URL:** https://pubmed.ncbi.nlm.nih.gov/?term=Nervous+System+Diseases",
        "\n**PMIDs:** 40000001, 40000002, 40000003",
    ]
) + "\n\n**Effective Query:** Nervous System Diseases[MeSH]\n**3 total**"

SEARCH_MARKDOWN_NO_RESULTS = "\n".join(
    [
        "## PubMed Search Results",
        "**Query:** nonexistent term",
        "**Returned:** 0 | **Offset:** 0",
        "**Search URL:** https://pubmed.ncbi.nlm.nih.gov/?term=nonexistent",
    ]
)

SEARCH_STRUCTURED = {
    "query": "Nervous System Diseases[MeSH]",
    "offset": 0,
    "pmids": ["40000001", "40000002", "40000003"],
    "summaries": [],
    "searchUrl": "https://pubmed.ncbi.nlm.nih.gov/?term=Nervous+System+Diseases",
    "effectiveQuery": "Nervous System Diseases[MeSH]",
    "totalCount": 3,
}

FETCH_MARKDOWN = "\n".join(
    [
        "## PubMed Articles",
        "**Articles Returned:** 2",
        "\n### SGLT2 Inhibitors in Heart Failure: A \\[Randomized\\] Trial",
        "\n**Authors (2):**",
        "- John A Smith (JAS) [aff 0] · ORCID 0000-0001-2345-6789",
        "- Heart Failure Collaborative (collective)",
        "\n**Affiliations:**",
        "- [0] Department of Cardiology, Example University",
        "\n**Journal:** The New England journal of medicine, (N Engl J Med), 2026 Mar 5, **394**(10), 977-988, ISSN 0028-4793",
        "**Record Type:** journal-article",
        "**Type:** Journal Article, Randomized Controlled Trial",
        "**PMID:** 40000001",
        "**DOI:** 10.1056/NEJMoa2600001",
        "**PubMed:** https://pubmed.ncbi.nlm.nih.gov/40000001/",
        "\n#### Abstract\nBACKGROUND: SGLT2 inhibitors were tested. RESULTS: Mortality fell.",
        "\n**Keywords:** SGLT2, heart failure",
        "\n#### MeSH Terms",
        "- Heart Failure [D006333] (major) (drug therapy [Q000188] (major))",
        "- Humans [D006801]",
        "\n### Deep Brain Stimulation Outcomes",
        "**Record Type:** journal-article",
        "**PMID:** 40000002",
        "**PubMed:** https://pubmed.ncbi.nlm.nih.gov/40000002/",
        "\n#### Abstract\nA cohort of 400 patients.",
    ]
)

FETCH_MARKDOWN_EMPTY = (
    "## PubMed Articles\n**Articles Returned:** 0\n**Unavailable PMIDs:** 99999999"
    "\n\n> No articles were returned: PubMed matched no record to any of these PMIDs."
)

FETCH_STRUCTURED = {
    "articles": [
        {
            "recordType": "journal-article",
            "pmid": "40000001",
            "title": "SGLT2 Inhibitors in Heart Failure: A [Randomized] Trial",
            "abstractText": "BACKGROUND: SGLT2 inhibitors were tested. RESULTS: Mortality fell.",
            "authors": [
                {"lastName": "Smith", "firstName": "John A", "initials": "JA"},
                {"collectiveName": "Heart Failure Collaborative"},
            ],
            "journalInfo": {
                "title": "The New England journal of medicine",
                "publicationDate": {"year": "2026", "month": "Mar", "day": "5"},
            },
            "publicationTypes": ["Journal Article", "Randomized Controlled Trial"],
            "meshTerms": [
                {"descriptorName": "Heart Failure", "descriptorUi": "D006333", "isMajorTopic": True},
                {"descriptorName": "Humans", "isMajorTopic": False},
            ],
            "pubmedUrl": "https://pubmed.ncbi.nlm.nih.gov/40000001/",
        }
    ],
    "totalReturned": 1,
}

# Illustrative only: any non-JSON, non-format() text the CLI might substitute for an
# oversized result. The CLI's exact wording is not verified here.
CLI_TRUNCATION_NOTICE = (
    "Error: MCP tool \"pubmed_fetch_articles\" response (61234 tokens) exceeds maximum "
    "allowed tokens (25000). Please use pagination, filtering, or limit parameters to reduce the response size."
)

TRIALS_MARKDOWN = "\n".join(
    [
        "Found 2 studies (57 total matching)",
        "- **NCT04008394**: Dapagliflozin in Heart Failure With Preserved Ejection Fraction [COMPLETED]\n"
        "  PHASE3 | N=6263 | AstraZeneca | Heart Failure",
        "  Site: Research Site, Birmingham, Alabama, United States (1 of 350 sites)",
        "- **NCT01234567**: Heart Failure Registry [RECRUITING]\n  N=500 | Example University | Heart Failure",
    ]
)

TRIALS_STRUCTURED = {
    "studies": [
        {
            "nctId": "NCT04008394",
            "briefTitle": "Dapagliflozin in Heart Failure With Preserved Ejection Fraction",
            "overallStatus": "COMPLETED",
            "phases": ["PHASE3"],
            "enrollmentCount": 6263,
        }
    ],
    "totalCount": 57,
}
