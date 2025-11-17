from .api_clients import LLMAdapter
import requests
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

class AnthropicAdapter(LLMAdapter):

    def get_cost(self, year=None, month=None):
        # Determine year and month
        today = datetime.today()
        year = int(year) if year else today.year
        month = int(month) if month else today.month

        starting_at = f"{year}-{month:02d}-01T00:00:00Z"

        # First, get the cost report
        url = "https://api.anthropic.com/v1/organizations/cost_report"
        params = {
            "starting_at": starting_at,  # dynamic starting date
            "group_by[]": "workspace_id",
            "group_by[]": "description",
            "limit": 31
        }
        headers = {
            "anthropic-version": "2023-06-01",
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

            # Now, get the messages usage report
            url_usage = "https://api.anthropic.com/v1/organizations/usage_report/messages"
            params_usage = {
                "starting_at": starting_at,
                "group_by[]": "workspace_id",
                "limit": 31
            }
            response_usage = requests.get(url_usage, params=params_usage, headers=headers)

            if response_usage.status_code == 200:
                usage_data = response_usage.json()
                daily_tokens = []
                for day_data in usage_data['data']:
                    day = day_data['starting_at'][:10]
                    input_tokens = 0
                    output_tokens = 0
                    for result in day_data["results"]:
                        input_tokens += result.get('uncached_input_tokens', 0)
                        output_tokens += result.get('output_tokens', 0)
                    daily_tokens.append({'day': day, 'input_tokens': input_tokens, 'output_tokens': output_tokens})

                return {"costs": daily_costs, "tokens": daily_tokens, "test_tokens": usage_data}
            else:
                return {"costs": daily_costs, "error_tokens": response_usage.text}
        else:
            return {"error": response.text}
