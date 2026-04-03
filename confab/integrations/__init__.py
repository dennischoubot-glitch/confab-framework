"""Integration modules for third-party agent frameworks.

Available integrations:

- ``langchain``: LangChain callback handler (``pip install confab-framework[langchain]``)
- ``crewai``: CrewAI task callback (``pip install confab-framework[crewai]``)
- ``autogen``: AutoGen intervention handler (``pip install confab-framework[autogen]``)
- ``agent_sdk``: Claude Agent SDK hook + message verifier (``pip install confab-framework[agent-sdk]``)
- ``openai_agents``: OpenAI Agents SDK guardrail + run verifier (``pip install confab-framework[openai-agents]``)
- Claude Code: shell hook via ``confab hook`` CLI (no extra deps, reads events from stdin)
"""
