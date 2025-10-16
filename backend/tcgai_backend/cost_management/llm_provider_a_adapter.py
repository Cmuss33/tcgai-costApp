from .api_clients import LLMAdapter
import requests
import os
from dotenv import load_dotenv

load_dotenv()

class AnthropicAdapter(LLMAdapter):
    def get_cost(self):
        url = "https://api.anthropic.com/v1/organizations/cost_report"
        params = {
            "starting_at": "2025-10-01T00:00:00Z",
            "group_by[]": ["workspace_id", "description"],
            "limit": 1
        }
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": os.environ.get('ANTHROPIC_ADMIN_KEY')
        }

        response = requests.get(url, params=params, headers=headers)

        if response.status_code == 200:
            return response.json()
        else:
            return {"error": response.text}
