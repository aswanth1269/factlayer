import factlayer
from openai import OpenAI
import os
c = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['FACTLAYER_OPENAI_BASE_URL'])
for m in c.models.list():
    print(m.id)
