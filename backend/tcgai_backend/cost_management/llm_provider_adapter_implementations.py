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
            "limit": 31 # TODO: think about how to be able to change this depending on user selection
        }
        headers = {
            "anthropic-version": "2023-06-01", # TODO: LOOK AT WHAT VERSION TO USE
            "content-type": "application/json",
            "x-api-key": os.environ.get('ANTHROPIC_ADMIN_KEY')
        }

        response = requests.get(url, params=params, headers=headers)

        if response.status_code == 200:
            cost_data = response.json()
            daily_costs = []
            for day_data in cost_data['data']:
                day = day_data['starting_at'][:10]  # Extract the date
                total_cost = round(sum(float(result['amount']) for result in day_data['results']) / 100, 2) # TODO: Possibly convert to CAD (currently USD)
                daily_costs.append({'day': day, 'total_cost': total_cost})
            return {"costs": daily_costs}
        else:
            return {"error": response.text}
