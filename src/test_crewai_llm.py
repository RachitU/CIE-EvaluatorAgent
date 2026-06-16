from crewai import LLM

llm = LLM(
    model="openai/qwen/qwen3.5-9b",
    base_url="http://localhost:1234/v1",
    api_key="lm-studio",
    temperature=0.2,
)

print("LLM created successfully")

response = llm.call(
    [{"role": "user", "content": "Say hello"}]
)

print(response)