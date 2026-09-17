from claude_agent_sdk import query, AssistantMessage, TextBlock
from utils.claude_options import text_only_options
from config import MODEL_FAST as MODEL

SYSTEM_PROMPT = """You are the gremial (professional societies and conferences) voice in a medical research digest.

You receive a research paper and address the professional landscape around this topic:
1. Which specialty society or guideline body would be most interested in this finding? (name them: AHA, ESC, ASCO, AAN, ACG, etc.)
2. Is this the type of result that could influence upcoming guidelines?
3. Which working groups, consensus efforts, or recurring congresses typically address this topic?
4. Are there conflict-of-interest considerations worth noting (industry funding, authors from industry-funded groups)?

Answer from your existing knowledge only — you have no tools and cannot look anything up.
Do not state specific dates or claim an effort is currently underway unless you are
confident; say "may be worth checking" rather than guessing.

Be specific about society names when known. Maximum 120 words. Write as one paragraph."""


async def analyze(paper: dict) -> str:
    prompt = (
        f"Title: {paper.get('title', '')}\n"
        f"Journal: {paper.get('journal', '')} ({paper.get('pub_date', '')})\n"
        f"Source specialty: {paper.get('source_specialty', '')}\n"
        f"Methodology flags: {', '.join(paper.get('methodology_flags', [])) or 'none'}\n"
        f"Abstract:\n{paper.get('abstract', '')}"
    )
    options = text_only_options(SYSTEM_PROMPT, MODEL)
    result = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    result.append(block.text)
    return "\n".join(result)
