import factlayer
from factlayer import llm
print(llm.OPENAI_MODEL, '|', llm.OPENAI_BASE_URL)
print(llm.complete_json('Return JSON.', 'Say {"ok": true}'))
