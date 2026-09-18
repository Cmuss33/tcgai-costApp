from .api_clients import LLMAdapter
import requests
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()


def app_api_key_ids():
    """This app's own Anthropic API key ids (ANTHROPIC_APP_API_KEY_IDS,
    comma-separated) -- past and present production keys, e.g. the old
    pre-rotation shared key plus the current per-surface keys. Used to scope
    get_cost/get_tokens' ABSOLUTE totals to just this app, since this
    Anthropic org also holds other, unrelated projects/workspaces
    (confirmed live 2026-09-16: Claude Code, BudgetETL, Photo Highlights,
    Roblox Studio) whose traffic must never leak into this app's numbers.
    Empty/unset is a safe default -- unscoped, whole-org totals. Deliberately
    NOT used by get_model_rates, which derives a $/token unit rate rather
    than an absolute total -- see WholeOrgRateDerivationTests for why that
    one must stay whole-org. Also used by stats_views.usage_by_key to filter
    the per-key panel down to keys this app actually issued -- the adapter's
    own get_usage_by_key stays unscoped (see its docstring); the filter is
    applied by the caller instead."""
    raw = os.environ.get('ANTHROPIC_APP_API_KEY_IDS') or ''
    return [k.strip() for k in raw.split(',') if k.strip()]


def chat_api_key_ids():
    """This app's chat-surface Anthropic API key ids specifically
    (ANTHROPIC_CHAT_API_KEY_IDS, comma-separated) -- a narrower subset of
    app_api_key_ids() covering only the chat widget, not the AI Search
    Curator/narrative/report surfaces the same app also owns keys for
    (ENG-147's three-way key split). Used by stats_views' cost_pc KPI
    (spend / chat-conversation count): that denominator only ever counts
    Chat rows, which only the chat surface's log_message calls create, so
    the numerator must be scoped the same way or growth on a different
    surface inflates "cost per conversation" with nothing behind it on the
    conversation side.

    Falls back to app_api_key_ids() when unset -- a safe default that
    reproduces the prior (app-wide) behavior until an operator explicitly
    narrows it to the real chat key id(s) (the pre-rotation shared key plus
    CLAUDE_API_KEY_CHATBOT's corresponding id)."""
    raw = os.environ.get('ANTHROPIC_CHAT_API_KEY_IDS') or ''
    scoped = [k.strip() for k in raw.split(',') if k.strip()]
    return scoped if scoped else app_api_key_ids()


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


class AnthropicAdapter(LLMAdapter):

    def get_cost(self, year=None, month=None, key_ids=None, rates_resp=None):
        """Estimated $ spend for this app's own tracked keys, this month.
        Scoped to `key_ids` when passed (e.g. chat_api_key_ids(), for a
        surface-specific total); defaults to app_api_key_ids() -- ALL of
        this app's keys -- when not passed, unchanged from prior behavior.
        cost_report can't be scoped by api_key_id at all (only
        description/workspace_id), and workspace_id comes back null on real
        cache-cost line items even for workspace-scoped keys -- so there's
        no way to pull an accurate per-app dollar total directly from
        cost_report when the org also holds unrelated projects (confirmed
        live 2026-09-16). Instead this multiplies get_model_rates' whole-org
        $/token unit rates (valid regardless of which keys generated the
        traffic -- see that method's docstring) by this app's own
        reliably-scoped (via usage_report's real api_key_id field) token
        counts -- the same estimation technique stats_views.usage_by_key
        already uses per individual key.

        `rates_resp` lets a caller that has already fetched get_model_rates
        for this exact (year, month) pass the raw response straight in,
        instead of this method deriving its own via a second, identical
        cost_report+usage_report round trip -- stats_views._build_stats and
        cost_reconciliation both also need this month's whole-org rates for
        their own proration math, so without this they'd fetch the same
        rates twice per request. None (the default) preserves the prior
        behavior exactly for every other caller."""
        today = datetime.today()
        year = int(year) if year else today.year
        month = int(month) if month else today.month

        if rates_resp is None:
            rates_resp = self.get_model_rates(year=year, month=month)
        if not isinstance(rates_resp, dict) or rates_resp.get("error"):
            return {"error": rates_resp.get("error") if isinstance(rates_resp, dict) else "rate derivation failed"}
        rates = rates_resp.get("rates", {})

        starting_at = f"{year}-{month:02d}-01T00:00:00Z"
        app_key_ids = key_ids if key_ids is not None else app_api_key_ids()
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": os.environ.get('ANTHROPIC_ADMIN_KEY')
        }
        response = requests.get(
            "https://api.anthropic.com/v1/organizations/usage_report/messages",
            params={"starting_at": starting_at, "group_by[]": ["model", "api_key_id"], "limit": 31},
            headers=headers,
        )
        if response.status_code != 200:
            return {"error": response.text}

        daily_costs = []
        num_days = 0
        monthly_cost = 0
        for day_data in response.json().get('data', []):
            day = day_data['starting_at'][:10]
            day_cost = 0.0
            for result in day_data.get('results', []):
                if app_key_ids and result.get('api_key_id') not in app_key_ids:
                    continue
                model_rates = rates.get(result.get('model'), {})
                day_cost += result.get('uncached_input_tokens', 0) * model_rates.get('input', 0)
                day_cost += result.get('output_tokens', 0) * model_rates.get('output', 0)
                day_cost += cache_creation_tokens(result) * model_rates.get('cache_creation', 0)
                day_cost += result.get('cache_read_input_tokens', 0) * model_rates.get('cache_read', 0)
            day_cost = round(day_cost, 2)
            daily_costs.append({'day': day, 'total_cost': day_cost})
            num_days += 1
            monthly_cost += day_cost

        monthly_average_cost = round(monthly_cost / num_days, 2) if num_days else 0
        return {"costs": daily_costs, "monthly_average_cost": monthly_average_cost}
        
    def get_tokens(self, year=None, month=None, key_ids=None):
         # Determine year and month
        today = datetime.today()
        year = int(year) if year else today.year
        month = int(month) if month else today.month

        starting_at = f"{year}-{month:02d}-01T00:00:00Z"
        app_key_ids = key_ids if key_ids is not None else app_api_key_ids()

        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": os.environ.get('ANTHROPIC_ADMIN_KEY')
        }

        url = "https://api.anthropic.com/v1/organizations/usage_report/messages"

        # This is an absolute total (not a unit rate like get_model_rates),
        # so it must be scoped to this app's own keys when configured -- see
        # _app_api_key_ids for why (the org also holds unrelated projects).
        # `key_ids`, when passed by the caller, narrows this further (e.g.
        # to just the chat surface -- see chat_api_key_ids).
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
                    if app_key_ids and result.get('api_key_id') not in app_key_ids:
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
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": os.environ.get('ANTHROPIC_ADMIN_KEY')
        }

        # Both sides of this ratio must cover the same population, so both
        # are whole-org -- unfiltered, no group_by[] dimension narrower than
        # "description"/"model". cost_report can't group/filter by
        # api_key_id at all (only description/workspace_id); a prior attempt
        # to scope just the usage_report side to a handful of tracked key
        # ids was retracted 2026-09-16 after it inflated one chat's estimated
        # cost ~1500x right after a key rotation -- see get_tokens' comment
        # and WholeOrgScopingTests.test_get_model_rates_stays_accurate_across_a_key_rotation.
        cost_response = requests.get(
            "https://api.anthropic.com/v1/organizations/cost_report",
            params={"starting_at": starting_at, "group_by[]": "description", "limit": 31},
            headers=headers,
        )
        if cost_response.status_code != 200:
            return {"error": cost_response.text}

        usage_params = {"starting_at": starting_at, "group_by[]": "model", "limit": 31}
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

        return {"rates": rates}

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

        Each by_model entry carries uncached_input_tokens/output_tokens/
        cache_creation_tokens/cache_read_tokens separately (not blended)
        so a caller can price each token type at its own rate the way
        get_cost does. A key's top-level "input_tokens" is the TRUE input
        total (uncached + both cache directions, ENG-148) -- matching
        get_tokens' definition -- not just the uncached slice.
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

        totals = {}  # api_key_id -> {model: {uncached_input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens}}
        for day_data in usage_response.json().get('data', []):
            for result in day_data.get('results', []):
                kid = result.get('api_key_id')
                if not kid:
                    continue
                model = result.get('model') or 'unknown'
                by_model = totals.setdefault(kid, {})
                entry = by_model.setdefault(model, {
                    'uncached_input_tokens': 0,
                    'output_tokens': 0,
                    'cache_creation_tokens': 0,
                    'cache_read_tokens': 0,
                })
                entry['uncached_input_tokens'] += result.get('uncached_input_tokens', 0)
                entry['output_tokens'] += result.get('output_tokens', 0)
                entry['cache_creation_tokens'] += cache_creation_tokens(result)
                entry['cache_read_tokens'] += result.get('cache_read_input_tokens', 0)

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
            input_tokens = sum(
                m['uncached_input_tokens'] + m['cache_creation_tokens'] + m['cache_read_tokens']
                for m in by_model.values()
            )
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
