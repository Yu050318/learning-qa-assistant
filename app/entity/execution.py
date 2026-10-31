"""Configuration fields that form a frozen Run identity."""
FROZEN_WORKFLOW_SETTINGS = (
    "deepseek_base_url", "deepseek_model", "deepseek_context_window", "ollama_base_url", "ollama_model",
    "ollama_context_window", "default_model_provider", "enabled_model_providers", "allow_model_fallback",
    "model_timeout", "model_max_tokens", "agent_timeout", "agent_max_model_calls", "agent_max_tool_calls",
    "agent_max_knowledge_calls", "agent_context_token_budget", "supervisor_max_delegations", "supervisor_max_per_agent",
    "knowledge_quality_enabled", "document_versions_enabled", "hierarchy_enabled", "hierarchy_merge_threshold",
    "memory_recent_messages", "embedding_model", "embedding_dimension", "web_search_enabled", "fixed_crawl_urls",
    "retrieval_top_k", "retrieval_parent_context", "retrieval_parent_group_size",
    "rerank_enabled", "rerank_endpoint", "rerank_model", "rerank_timeout",
)
