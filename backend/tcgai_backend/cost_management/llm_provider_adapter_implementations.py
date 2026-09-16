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
        workspace_id = os.environ.get('ANTHROPIC_WORKSPACE_ID') or None

        # First, get the cost report. cost_report has no server-side filter,
        # only group_by, so grouping by both workspace_id and description lets
        # us scope to one workspace ourselves below while still getting the
        # per-model/token_type breakdown description grouping provides.
        url = "https://api.anthropic.com/v1/organizations/cost_report"
        params = {
            "starting_at": starting_at,  # dynamic starting date
            "group_by[]": ["workspace_id", "description"],
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
            num_days = 0
            monthly_cost = 0
            for day_data in cost_data['data']:
                day = day_data['starting_at'][:10]  # Extract the date
                results = day_data['results']
                if workspace_id:
                    results = [r for r in results if r.get('workspace_id') == workspace_id]
                total_cost = round(sum(float(result['amount']) for result in results) / 100, 2) # TODO: Possibly convert to CAD (currently USD)
                daily_costs.append({'day': day, 'total_cost': total_cost})
                num_days += 1
                monthly_cost += total_cost

            if num_days > 0:
                monthly_average_cost = round(monthly_cost / num_days, 2)
            else:
                monthly_average_cost = 0

            return {"costs": daily_costs, "monthly_average_cost": monthly_average_cost, "workspace_id": workspace_id}
        else:
            return {"error": response.text}
        
    def get_tokens(self, year=None, month=None):
         # Determine year and month
        today = datetime.today()
        year = int(year) if year else today.year
        month = int(month) if month else today.month

        starting_at = f"{year}-{month:02d}-01T00:00:00Z"
        workspace_id = os.environ.get('ANTHROPIC_WORKSPACE_ID') or None

        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": os.environ.get('ANTHROPIC_ADMIN_KEY')
        }

        url = "https://api.anthropic.com/v1/organizations/usage_report/messages"

        params = {
            "starting_at": starting_at,
            "group_by[]": "workspace_id",
            "limit": 31
        }
        # usage_report/messages supports a real server-side workspace filter,
        # unlike cost_report which only supports group_by.
        if workspace_id:
            params["workspace_ids[]"] = workspace_id

        response = requests.get(url, params=params, headers=headers)

        if response.status_code == 200:
            usage_data = response.json()
            daily_tokens = []
            for day_data in usage_data['data']:
                day = day_data['starting_at'][:10]
                input_tokens = 0
                output_tokens = 0
                for result in day_data["results"]:
                    input_tokens += result.get('uncached_input_tokens', 0)
                    output_tokens += result.get('output_tokens', 0)
                daily_tokens.append({'day': day, 'input_tokens': input_tokens, 'output_tokens': output_tokens})

            return {"tokens": daily_tokens, "test_tokens": usage_data, "workspace_id": workspace_id}
        else:
            return {"error": response.text}

    def get_model_rates(self, year=None, month=None):
        """Effective $/token rate per model for the month, derived from Anthropic's
        own cost_report (billed amounts) and usage_report/messages (real token
        counts) - i.e. what Anthropic actually charged, not a hardcoded price list."""
        today = datetime.today()
        year = int(year) if year else today.year
        month = int(month) if month else today.month

        starting_at = f"{year}-{month:02d}-01T00:00:00Z"
        workspace_id = os.environ.get('ANTHROPIC_WORKSPACE_ID') or None
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": os.environ.get('ANTHROPIC_ADMIN_KEY')
        }

        cost_response = requests.get(
            "https://api.anthropic.com/v1/organizations/cost_report",
            params={"starting_at": starting_at, "group_by[]": ["workspace_id", "description"], "limit": 31},
            headers=headers,
        )
        if cost_response.status_code != 200:
            return {"error": cost_response.text}

        usage_params = {"starting_at": starting_at, "group_by[]": "model", "limit": 31}
        if workspace_id:
            usage_params["workspace_ids[]"] = workspace_id
        usage_response = requests.get(
            "https://api.anthropic.com/v1/organizations/usage_report/messages",
            params=usage_params,
            headers=headers,
        )
        if usage_response.status_code != 200:
            return {"error": usage_response.text}

        input_cents = {}
        output_cents = {}
        for day_data in cost_response.json().get('data', []):
            for result in day_data.get('results', []):
                model = result.get('model')
                if not model or result.get('cost_type') != 'tokens':
                    continue
                if workspace_id and result.get('workspace_id') != workspace_id:
                    continue
                amount = float(result.get('amount') or 0)
                token_type = result.get('token_type')
                if token_type == 'uncached_input_tokens':
                    input_cents[model] = input_cents.get(model, 0) + amount
                elif token_type == 'output_tokens':
                    output_cents[model] = output_cents.get(model, 0) + amount

        input_tokens = {}
        output_tokens = {}
        for day_data in usage_response.json().get('data', []):
            for result in day_data.get('results', []):
                model = result.get('model')
                if not model:
                    continue
                input_tokens[model] = input_tokens.get(model, 0) + result.get('uncached_input_tokens', 0)
                output_tokens[model] = output_tokens.get(model, 0) + result.get('output_tokens', 0)

        rates = {}
        for model in set(input_cents) | set(output_cents):
            entry = {}
            if input_tokens.get(model):
                entry['input'] = round((input_cents.get(model, 0) / 100) / input_tokens[model], 8)
            if output_tokens.get(model):
                entry['output'] = round((output_cents.get(model, 0) / 100) / output_tokens[model], 8)
            if entry:
                rates[model] = entry

        return {"rates": rates, "workspace_id": workspace_id}
