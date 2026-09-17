from .api_clients import LLMAdapter
import requests
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()


def cache_creation_tokens(result):
    """Sum a usage_report/messages result's cache-write tokens across both
    TTL variants. Confirmed against the live API 2026-09-16: unlike
    cache_read_input_tokens (a flat field), cache creation is a nested
    object -- {"cache_creation": {"ephemeral_5m_input_tokens": N,
    "ephemeral_1h_input_tokens": N}} -- since Anthropic bills 5-min and
    1-hour ephemeral caches at different rates. The chatbot only ever
    reports a single flat cache_creation_input_tokens total (no TTL
    breakdown) on the Messages API response it actually calls, so both
    variants are blended into one total here to match that granularity."""
    creation = result.get('cache_creation') or {}
    return creation.get('ephemeral_5m_input_tokens', 0) + creation.get('ephemeral_1h_input_tokens', 0)


def _tracked_api_key_ids():
    """Production Anthropic API key ids (ANTHROPIC_TRACKED_API_KEY_IDS,
    comma-separated) this app's own traffic is scoped to when deriving
    token totals and rates. Replaces the old ANTHROPIC_WORKSPACE_ID scoping
    (ENG-148 follow-up): confirmed live 2026-09-16 that cost_report reports
    workspace_id: null on cache-cost line items even for keys that ARE
    workspace-scoped at creation, so workspace_id can't be trusted as a
    filter dimension -- usage_report's api_key_id is a real, per-request
    field and (unlike workspace_id) never comes back null. Empty/unset is a
    safe default -- unscoped, whole-org totals, same as before this feature
    existed."""
    raw = os.environ.get('ANTHROPIC_TRACKED_API_KEY_IDS') or ''
    return [k.strip() for k in raw.split(',') if k.strip()]


class AnthropicAdapter(LLMAdapter):

    def get_cost(self, year=None, month=None):
        # Determine year and month
        today = datetime.today()
        year = int(year) if year else today.year
        month = int(month) if month else today.month

        starting_at = f"{year}-{month:02d}-01T00:00:00Z"

        # cost_report has no server-side filter and its group_by only ever
        # accepts "description"/"workspace_id" -- never api_key_id (confirmed
        # against the real API 2026-09-16) -- so per-key cost scoping isn't
        # possible here at all. workspace_id grouping was tried previously,
        # but cost_report reports workspace_id: null on real cache-cost line
        # items even for workspace-scoped keys, which silently zeroed out
        # results whenever ANTHROPIC_WORKSPACE_ID was set. This Anthropic org
        # only holds this app's own keys, so an unscoped, whole-org total is
        # both the only option cost_report supports and (for this org) the
        # accurate one.
        url = "https://api.anthropic.com/v1/organizations/cost_report"
        params = {
            "starting_at": starting_at,  # dynamic starting date
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
            num_days = 0
            monthly_cost = 0
            for day_data in cost_data['data']:
                day = day_data['starting_at'][:10]  # Extract the date
                results = day_data['results']
                total_cost = round(sum(float(result['amount']) for result in results) / 100, 2) # TODO: Possibly convert to CAD (currently USD)
                daily_costs.append({'day': day, 'total_cost': total_cost})
                num_days += 1
                monthly_cost += total_cost

            if num_days > 0:
                monthly_average_cost = round(monthly_cost / num_days, 2)
            else:
                monthly_average_cost = 0

            return {"costs": daily_costs, "monthly_average_cost": monthly_average_cost}
        else:
            return {"error": response.text}
        
    def get_tokens(self, year=None, month=None):
         # Determine year and month
        today = datetime.today()
        year = int(year) if year else today.year
        month = int(month) if month else today.month

        starting_at = f"{year}-{month:02d}-01T00:00:00Z"
        tracked_key_ids = _tracked_api_key_ids()

        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": os.environ.get('ANTHROPIC_ADMIN_KEY')
        }

        url = "https://api.anthropic.com/v1/organizations/usage_report/messages"

        params = {
            "starting_at": starting_at,
            "group_by[]": "api_key_id",
            "limit": 31
        }

        response = requests.get(url, params=params, headers=headers)

        if response.status_code == 200:
            usage_data = response.json()
            daily_tokens = []
            total_creation = 0
            total_read = 0
            total_input = 0
            for day_data in usage_data['data']:
                day = day_data['starting_at'][:10]
                uncached = 0
                creation = 0
                read = 0
                output_tokens = 0
                for result in day_data["results"]:
                    # usage_report has no server-side per-key filter, so scope
                    # client-side to our own production keys when configured
                    # (see _tracked_api_key_ids) -- unset means unscoped,
                    # whole-org totals, same as before this feature existed.
                    if tracked_key_ids and result.get('api_key_id') not in tracked_key_ids:
                        continue
                    uncached += result.get('uncached_input_tokens', 0)
                    creation += cache_creation_tokens(result)
                    read += result.get('cache_read_input_tokens', 0)
                    output_tokens += result.get('output_tokens', 0)
                # input_tokens is the TRUE total input this day (ENG-148) --
                # uncached + both cache directions -- not just uncached.
                # Anthropic bills all three; excluding cache tokens here
                # silently understated the dashboard's own "Tokens" KPI.
                day_input = uncached + creation + read
                daily_tokens.append({'day': day, 'input_tokens': day_input, 'output_tokens': output_tokens})
                total_creation += creation
                total_read += read
                total_input += day_input

            hit_rate = (total_read / total_input) if total_input else None
            return {
                "tokens": daily_tokens,
                "cache": {
                    "creation_tokens": total_creation,
                    "read_tokens": total_read,
                    "hit_rate": hit_rate,
                },
                "test_tokens": usage_data,
                "tracked_key_ids": tracked_key_ids or None,
            }
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
        tracked_key_ids = _tracked_api_key_ids()
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": os.environ.get('ANTHROPIC_ADMIN_KEY')
        }

        # cost_report can't group/filter by api_key_id (only description/
        # workspace_id), so the $ numerator stays whole-org -- see get_cost's
        # comment for why that's both the only option and, for this
        # single-tenant org, the accurate one.
        cost_response = requests.get(
            "https://api.anthropic.com/v1/organizations/cost_report",
            params={"starting_at": starting_at, "group_by[]": "description", "limit": 31},
            headers=headers,
        )
        if cost_response.status_code != 200:
            return {"error": cost_response.text}

        # The token denominator, in contrast, CAN be scoped to our own keys
        # via usage_report's real api_key_id field (see _tracked_api_key_ids)
        # -- unlike cost_report's workspace_id, which comes back null on real
        # cache-cost rows even for workspace-scoped keys.
        usage_params = {"starting_at": starting_at, "group_by[]": ["model", "api_key_id"], "limit": 31}
        usage_response = requests.get(
            "https://api.anthropic.com/v1/organizations/usage_report/messages",
            params=usage_params,
            headers=headers,
        )
        if usage_response.status_code != 200:
            return {"error": usage_response.text}

        # ENG-148: cache_creation/cache_read get their own rates too, not
        # just input/output -- Anthropic bills cache writes at a premium
        # over base input and cache reads at a discount, so lumping them
        # into the plain "input" rate (or ignoring them, as before) would
        # misprice every cache-hit turn. cost_report's token_type strings
        # for these are "cache_creation.ephemeral_5m_input_tokens" /
        # "cache_creation.ephemeral_1h_input_tokens" (both roll into one
        # blended "cache_creation" rate, matching cache_creation_tokens()'s
        # TTL-blind granularity) and "cache_read_input_tokens".
        input_cents = {}
        output_cents = {}
        cache_creation_cents = {}
        cache_read_cents = {}
        for day_data in cost_response.json().get('data', []):
            for result in day_data.get('results', []):
                model = result.get('model')
                if not model or result.get('cost_type') != 'tokens':
                    continue
                amount = float(result.get('amount') or 0)
                token_type = result.get('token_type')
                if token_type == 'uncached_input_tokens':
                    input_cents[model] = input_cents.get(model, 0) + amount
                elif token_type == 'output_tokens':
                    output_cents[model] = output_cents.get(model, 0) + amount
                elif token_type in (
                    'cache_creation.ephemeral_5m_input_tokens',
                    'cache_creation.ephemeral_1h_input_tokens',
                ):
                    cache_creation_cents[model] = cache_creation_cents.get(model, 0) + amount
                elif token_type == 'cache_read_input_tokens':
                    cache_read_cents[model] = cache_read_cents.get(model, 0) + amount

        input_tokens = {}
        output_tokens = {}
        cache_creation_tok = {}
        cache_read_tok = {}
        for day_data in usage_response.json().get('data', []):
            for result in day_data.get('results', []):
                model = result.get('model')
                if not model:
                    continue
                if tracked_key_ids and result.get('api_key_id') not in tracked_key_ids:
                    continue
                input_tokens[model] = input_tokens.get(model, 0) + result.get('uncached_input_tokens', 0)
                output_tokens[model] = output_tokens.get(model, 0) + result.get('output_tokens', 0)
                cache_creation_tok[model] = cache_creation_tok.get(model, 0) + cache_creation_tokens(result)
                cache_read_tok[model] = cache_read_tok.get(model, 0) + result.get('cache_read_input_tokens', 0)

        rates = {}
        all_models = set(input_cents) | set(output_cents) | set(cache_creation_cents) | set(cache_read_cents)
        for model in all_models:
            entry = {}
            if input_tokens.get(model):
                entry['input'] = round((input_cents.get(model, 0) / 100) / input_tokens[model], 8)
            if output_tokens.get(model):
                entry['output'] = round((output_cents.get(model, 0) / 100) / output_tokens[model], 8)
            if cache_creation_tok.get(model):
                entry['cache_creation'] = round((cache_creation_cents.get(model, 0) / 100) / cache_creation_tok[model], 8)
            if cache_read_tok.get(model):
                entry['cache_read'] = round((cache_read_cents.get(model, 0) / 100) / cache_read_tok[model], 8)
            if entry:
                rates[model] = entry

        return {"rates": rates, "tracked_key_ids": tracked_key_ids or None}

    def get_usage_by_key(self, year=None, month=None):
        """Per-API-key token usage this month, with names resolved from
        Anthropic's key registry.

        Provider-specific, not part of LLMAdapter -- "API key" isn't a
        concept every provider necessarily has, and this can't derive a real
        per-key dollar cost the way get_model_rates derives a per-model one:
        confirmed 2026-09-16 against the real API that cost_report's
        group_by only accepts "description" and "workspace_id", never
        api_key_id, so there is no per-key billed-dollar source to divide by
        real tokens the way get_model_rates does per model. Callers that
        want an estimated cost per key should combine these token counts
        with get_model_rates' per-model rates themselves (see
        stats_views.usage_by_key) -- it's necessarily an estimate (this
        month's blended rate x tokens), not Anthropic's own billed figure.
        """
        today = datetime.today()
        year = int(year) if year else today.year
        month = int(month) if month else today.month

        starting_at = f"{year}-{month:02d}-01T00:00:00Z"

        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": os.environ.get('ANTHROPIC_ADMIN_KEY')
        }

        # Deliberately unscoped -- this view's whole purpose is to show every
        # key with usage (including one nobody expected), so it must not
        # filter itself down to only the known production keys.
        usage_params = {
            "starting_at": starting_at,
            "group_by[]": ["api_key_id", "model"],
            "limit": 31,
        }

        usage_response = requests.get(
            "https://api.anthropic.com/v1/organizations/usage_report/messages",
            params=usage_params,
            headers=headers,
        )
        if usage_response.status_code != 200:
            return {"error": usage_response.text}

        totals = {}  # api_key_id -> {model: {input_tokens, output_tokens}}
        for day_data in usage_response.json().get('data', []):
            for result in day_data.get('results', []):
                kid = result.get('api_key_id')
                if not kid:
                    continue
                model = result.get('model') or 'unknown'
                by_model = totals.setdefault(kid, {})
                entry = by_model.setdefault(model, {'input_tokens': 0, 'output_tokens': 0})
                entry['input_tokens'] += result.get('uncached_input_tokens', 0)
                entry['output_tokens'] += result.get('output_tokens', 0)

        # Best-effort name resolution -- a broken/unreachable /api_keys call
        # shouldn't sink usage data that already succeeded, so degrade to
        # raw ids as names rather than erroring the whole response.
        names = {}
        try:
            names_response = requests.get(
                "https://api.anthropic.com/v1/organizations/api_keys",
                params={"limit": 100},
                headers=headers,
            )
            if names_response.status_code == 200:
                for k in names_response.json().get('data', []):
                    names[k['id']] = k.get('name') or k['id']
        except requests.RequestException:
            pass

        keys = []
        for kid, by_model in totals.items():
            input_tokens = sum(m['input_tokens'] for m in by_model.values())
            output_tokens = sum(m['output_tokens'] for m in by_model.values())
            keys.append({
                "api_key_id": kid,
                "name": names.get(kid, kid),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "by_model": by_model,
            })
        keys.sort(key=lambda k: k['input_tokens'] + k['output_tokens'], reverse=True)

        return {"keys": keys}
